"""CLI: Importiert einen von istio-graph.py erzeugten JSON-Abhängigkeitsgraphen
(Knoten + Kanten) nach Neo4j.

Jeder Knoten wird als eigener Node mit seiner `kind` als Label angelegt (z. B.
`kind: "service"` -> Label `:Service`), jede Kante als Relationship mit ihrer
`relation` als Typ (z. B. `relation: "in_namespace"` -> Typ `IN_NAMESPACE`).
Verschachtelte Attribut-Werte (Maps, Listen von Maps – z. B. `selector`,
`ports`) lassen sich nicht direkt als Neo4j-Property speichern und werden
daher als JSON-String abgelegt. Der Import ist über `id` (Knoten) bzw.
`source`/`target`/Relationship-Typ/Attribut-Hash (Kanten) idempotent (MERGE),
mehrfaches Einspielen derselben graph.json dupliziert also nichts. Der
Attribut-Hash im Merge-Key ist nötig, weil derselbe Knoten-/Relationship-Typ-
Tripel mit unterschiedlichen Attributen mehrfach auftreten kann (z. B. ein
VirtualService-Canary-Split mit zwei `routes_to`-Kanten zum selben Host, aber
unterschiedlichem `subset`/`weight`) – ohne den Hash im Merge-Key würde die
zweite Kante beim `SET` die Attribute der ersten überschreiben statt eine
eigene Relationship anzulegen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from typing import Any

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase, Session
from neo4j.exceptions import GqlError

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _sanitize_identifier(value: str, *, kind: str) -> str:
    """Validiert einen Neo4j-Label-/Relationship-Type-Namen.

    Labels und Relationship-Typen lassen sich in Cypher – anders als
    Property-Werte – nicht parametrisieren und müssen direkt in den
    Query-String eingesetzt werden; daher hier eine strikte Whitelist statt
    einer reinen Escaping-Strategie."""
    if not _IDENTIFIER_RE.match(value):
        raise ValueError(f"Ungültiger {kind}-Name: {value!r}")
    return value


def _to_label(kind: str) -> str:
    return kind[:1].upper() + kind[1:]


def _to_property_value(value: object) -> object:
    """Wandelt einen Attribut-Wert aus graph.json in einen Neo4j-kompatiblen
    Property-Wert um. Neo4j-Properties erlauben nur primitive Typen oder
    homogene Listen davon – verschachtelte Maps (z. B. `selector`, `labels`)
    oder Listen von Maps (z. B. `ports`) werden daher als JSON-String
    serialisiert."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list) and all(isinstance(v, (str, int, float, bool)) for v in value):
        return value
    return json.dumps(value, sort_keys=True)


def _flatten_attributes(attributes: dict[str, object]) -> dict[str, object]:
    return {k: _to_property_value(v) for k, v in attributes.items()}


def _attrs_key(props: dict[str, object]) -> str:
    """Stabiler Hash über die (bereits geflatteten) Kanten-Attribute, als Teil
    des Merge-Keys in `_import_edges` – siehe Modul-Docstring."""
    return hashlib.sha1(json.dumps(props, sort_keys=True).encode()).hexdigest()


def _load_graph(path: str) -> dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _clear_database(session: Session) -> None:
    logger.info("Lösche alle vorhandenen Knoten und Kanten in der Zieldatenbank")
    session.run("MATCH (n) DETACH DELETE n")


def _ensure_constraints(session: Session, nodes: list[dict[str, Any]]) -> None:
    """Legt pro Knotenart eine Eindeutigkeits-Constraint auf `id` an, damit
    MERGE beim (Re-)Import über einen Index statt über einen Full-Scan läuft."""
    for kind in {n["kind"] for n in nodes}:
        label = _sanitize_identifier(_to_label(kind), kind="Label")
        session.run(
            f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:`{label}`) REQUIRE n.id IS UNIQUE",  # pyright: ignore[reportArgumentType]  # label whitelisted by _sanitize_identifier
        )


def _import_nodes(session: Session, nodes: list[dict[str, Any]]) -> None:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        by_kind.setdefault(n["kind"], []).append(n)

    for kind, group in by_kind.items():
        label = _sanitize_identifier(_to_label(kind), kind="Label")
        rows = [
            {
                "id": n["id"],
                "kind": n["kind"],
                "name": n["name"],
                "namespace": n.get("namespace"),
                "props": _flatten_attributes(n.get("attributes") or {}),
            }
            for n in group
        ]
        session.run(
            f"UNWIND $rows AS row "
            f"MERGE (n:`{label}` {{id: row.id}}) "
            f"SET n.kind = row.kind, n.name = row.name, n.namespace = row.namespace, n += row.props",  # pyright: ignore[reportArgumentType]  # label whitelisted by _sanitize_identifier
            rows=rows,
        )


def _import_edges(session: Session, edges: list[dict[str, Any]]) -> None:
    by_relation: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        by_relation.setdefault(e["relation"], []).append(e)

    for relation, group in by_relation.items():
        rel_type = _sanitize_identifier(relation.upper(), kind="Relationship-Type")
        rows = []
        for e in group:
            props = _flatten_attributes(e.get("attributes") or {})
            rows.append({
                "source": e["source"],
                "target": e["target"],
                "relation": e["relation"],
                "props": props,
                "attrs_key": _attrs_key(props),
            })
        session.run(
            f"UNWIND $rows AS row "
            f"MATCH (a {{id: row.source}}), (b {{id: row.target}}) "
            f"MERGE (a)-[r:`{rel_type}` {{_attrs_key: row.attrs_key}}]->(b) "
            f"SET r.relation = row.relation, r += row.props",  # pyright: ignore[reportArgumentType]  # rel_type whitelisted by _sanitize_identifier
            rows=rows,
        )


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def import_graph(driver: Driver, database: str, graph: dict[str, Any], *, clear: bool) -> tuple[int, int]:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    with driver.session(database=database) as session:
        if clear:
            _clear_database(session)
        _ensure_constraints(session, nodes)
        _import_nodes(session, nodes)
        _import_edges(session, edges)
    return len(nodes), len(edges)


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Importiert einen von istio-graph.py erzeugten JSON-Abhängigkeitsgraphen "
                    "(Knoten + Kanten) nach Neo4j.",
    )
    parser.add_argument(
        "input", nargs="?", default="-",
        help="Pfad zur graph.json (Ausgabe von istio-graph.py); '-' liest von stdin (Standard).",
    )
    parser.add_argument(
        "--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        help="Neo4j-Bolt-URI (Standard: %(default)s, überschreibbar via NEO4J_URI).",
    )
    parser.add_argument(
        "--user", default=os.environ.get("NEO4J_USER", "neo4j"),
        help="Neo4j-Benutzername (Standard: %(default)s, überschreibbar via NEO4J_USER).",
    )
    parser.add_argument(
        "--password", default=None,
        help="Neo4j-Passwort (Standard: Umgebungsvariable NEO4J_PASSWORD, geladen aus "
             "der Umgebung oder aus einer .env-Datei im aktuellen Verzeichnis).",
    )
    parser.add_argument(
        "--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"),
        help="Ziel-Datenbank (Standard: %(default)s, überschreibbar via NEO4J_DATABASE).",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Löscht vor dem Import alle vorhandenen Knoten und Kanten in der Zieldatenbank "
             "(MATCH (n) DETACH DELETE n).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Aktiviert Debug-Logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )

    try:
        graph = _load_graph(args.input)
    except OSError as e:
        print(f"Fehler: Graph-Datei konnte nicht gelesen werden: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Fehler: Graph-Datei ist kein gültiges JSON: {e}", file=sys.stderr)
        return 1

    password = args.password or os.environ.get("NEO4J_PASSWORD")
    if not password:
        print(
            "Fehler: Kein Neo4j-Passwort angegeben (--password, Umgebungsvariable "
            "NEO4J_PASSWORD oder .env-Datei).",
            file=sys.stderr,
        )
        return 1

    try:
        with GraphDatabase.driver(args.uri, auth=(args.user, password)) as driver:
            driver.verify_connectivity()
            node_count, edge_count = import_graph(driver, args.database, graph, clear=args.clear)
    except GqlError as e:
        print(f"Fehler: Import nach Neo4j ({args.uri}) fehlgeschlagen: {e}", file=sys.stderr)
        return 1

    print(f"Import abgeschlossen: {node_count} Knoten, {edge_count} Kanten.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())