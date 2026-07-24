"""CLI: Importiert einen von connections-graph.py erzeugten JSON-
Verbindungsgraphen (Deployment-zentrierte Knoten + Kanten) nach ArangoDB.

Schreibt standardmäßig in eine **eigene** Datenbank (`istio-connections`,
siehe `--database`/`ARANGO_CONNECTIONS_DATABASE`), getrennt von der
Datenbank, in die datenimport-arangodb.py den rohen Graphen aus
istio-graph.py importiert. Beide Graphen verwenden dieselbe `id`-
Bildungsvorschrift für Knoten (z. B. `service:default/httpbin`); in getrennten
Datenbanken bleiben sie trotzdem unabhängig voneinander, statt sich über
gleich benannte Knoten zu vermischen — siehe arangodb/init/create-istio-db.js,
das beide Datenbanken (jeweils mit `nodes`/`edges`-Collections) anlegt. Jeder
Knoten wird als Dokument in `nodes` angelegt (mit seiner `kind` als
Property), jede Kante als Edge-Dokument in `edges` mit `_from`/`_to` auf die
jeweiligen Knoten-Dokumente — Kanten mit `relation: "forbidden"` markieren
dabei die von connections-graph.py explizit per AuthorizationPolicy(DENY)
verbotenen Verbindungen. Der von connections-graph.py vergebene Knoten-`id`-
String (z. B. `deployment:default/httpbin`) enthält Zeichen (u. a. `/`), die
in einem ArangoDB-`_key` nicht erlaubt sind; der `_key` ist daher ein
SHA1-Hash dieser `id`, der Original-String bleibt zusätzlich als Property
`id` auf dem Dokument erhalten. Der Import ist über UPSERT idempotent: Knoten
über `_key` (siehe oben), Kanten über `_from`/`_to`/`relation`/Attribut-Hash.
Der Attribut-Hash im Merge-Key ist nötig, weil derselbe Knoten-/Relationstyp-
Tripel mit unterschiedlichen Attributen mehrfach auftreten kann (z. B. zwei
verschiedene AuthorizationPolicy(DENY)-Regeln, die dieselbe Quelle -> Ziel-
Beziehung verbieten) – ohne den Hash im Merge-Key würde die zweite Kante beim
UPSERT die Attribute der ersten überschreiben statt ein eigenes Edge-Dokument
anzulegen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from typing import Any

from arango.client import ArangoClient
from arango.database import StandardDatabase
from arango.exceptions import ArangoServerError, ServerConnectionError
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _node_key(node_id: str) -> str:
    """Leitet aus der connections-graph.py-Knoten-`id` einen gültigen
    ArangoDB-`_key` ab (siehe Modul-Docstring: die `id` enthält u. a. `/`, was
    in einem `_key` nicht erlaubt ist)."""
    return hashlib.sha1(node_id.encode()).hexdigest()


def _attrs_key(props: dict[str, object]) -> str:
    """Stabiler Hash über die Kanten-Attribute, als Teil des Merge-Keys in
    `_import_edges` – siehe Modul-Docstring."""
    return hashlib.sha1(json.dumps(props, sort_keys=True).encode()).hexdigest()


def _load_graph(path: str) -> dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _clear_database(db: StandardDatabase) -> None:
    logger.info("Lösche alle vorhandenen Knoten und Kanten in der Zieldatenbank")
    db.collection("nodes").truncate()
    db.collection("edges").truncate()


def _import_nodes(db: StandardDatabase, nodes: list[dict[str, Any]]) -> None:
    rows = [
        {
            "_key": _node_key(n["id"]),
            "id": n["id"],
            "kind": n["kind"],
            "name": n["name"],
            "namespace": n.get("namespace"),
            **(n.get("attributes") or {}),
        }
        for n in nodes
    ]
    db.aql.execute(
        "FOR row IN @rows "
        "UPSERT {_key: row._key} "
        "INSERT row "
        "UPDATE row "
        "IN nodes",
        bind_vars={"rows": rows},
    )


def _import_edges(db: StandardDatabase, edges: list[dict[str, Any]]) -> None:
    rows = []
    for e in edges:
        props = e.get("attributes") or {}
        rows.append({
            "_key": _attrs_key({"source": e["source"], "target": e["target"], "relation": e["relation"], **props}),
            "_from": f"nodes/{_node_key(e['source'])}",
            "_to": f"nodes/{_node_key(e['target'])}",
            "relation": e["relation"],
            **props,
        })
    db.aql.execute(
        "FOR row IN @rows "
        "UPSERT {_key: row._key} "
        "INSERT row "
        "UPDATE row "
        "IN edges",
        bind_vars={"rows": rows},
    )


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def import_graph(db: StandardDatabase, graph: dict[str, Any], *, clear: bool) -> tuple[int, int]:
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if clear:
        _clear_database(db)
    _import_nodes(db, nodes)
    _import_edges(db, edges)
    return len(nodes), len(edges)


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Importiert einen von connections-graph.py erzeugten JSON-Verbindungsgraphen "
                    "(Deployment-zentrierte Knoten + Kanten, inkl. explizit verbotener "
                    "AuthorizationPolicy(DENY)-Verbindungen) nach ArangoDB.",
    )
    parser.add_argument(
        "input", nargs="?", default="-",
        help="Pfad zur connections.json (Ausgabe von connections-graph.py); '-' liest von stdin (Standard).",
    )
    parser.add_argument(
        "--url", default=os.environ.get("ARANGO_URL", "http://localhost:8529"),
        help="ArangoDB-HTTP-Endpunkt (Standard: %(default)s, überschreibbar via ARANGO_URL).",
    )
    parser.add_argument(
        "--user", default=os.environ.get("ARANGO_USER", "istio"),
        help="ArangoDB-Benutzername (Standard: %(default)s, überschreibbar via ARANGO_USER).",
    )
    parser.add_argument(
        "--password", default=None,
        help="ArangoDB-Passwort (Standard: Umgebungsvariable ARANGO_PASSWORD, geladen aus "
             "der Umgebung oder aus einer .env-Datei im aktuellen Verzeichnis).",
    )
    parser.add_argument(
        "--database", default=os.environ.get("ARANGO_CONNECTIONS_DATABASE", "istio-connections"),
        help="Ziel-Datenbank (Standard: %(default)s, überschreibbar via ARANGO_CONNECTIONS_DATABASE) — "
             "bewusst eine eigene Datenbank, getrennt von der des rohen Graphen aus datenimport-arangodb.py.",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Löscht vor dem Import alle vorhandenen Knoten und Kanten in der Zieldatenbank "
             "(truncate der Collections nodes/edges dieser Datenbank).",
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

    password = args.password or os.environ.get("ARANGO_PASSWORD")
    if not password:
        print(
            "Fehler: Kein ArangoDB-Passwort angegeben (--password, Umgebungsvariable "
            "ARANGO_PASSWORD oder .env-Datei).",
            file=sys.stderr,
        )
        return 1

    try:
        client = ArangoClient(hosts=args.url)
        db = client.db(args.database, username=args.user, password=password, verify=True)
        node_count, edge_count = import_graph(db, graph, clear=args.clear)
    except (ServerConnectionError, ArangoServerError) as e:
        print(f"Fehler: Import nach ArangoDB ({args.url}) fehlgeschlagen: {e}", file=sys.stderr)
        return 1

    print(f"Import abgeschlossen: {node_count} Knoten, {edge_count} Kanten.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
