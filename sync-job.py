"""CLI: Führt istio-graph.py und datenimport.py einmalig hintereinander aus,
um den Neo4j-Graphen mit dem aktuellen Cluster-Zustand zu synchronisieren.

Für den Betrieb als Kubernetes CronJob gedacht (die Wiederholung übernimmt
dabei der CronJob selbst, dieses Skript macht pro Aufruf genau einen
Durchlauf). Konfiguration kommt aus Umgebungsvariablen bzw. einer .env-Datei
im aktuellen Verzeichnis; die Neo4j-Zugangsdaten
(NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD/NEO4J_DATABASE) werden unverändert an
datenimport.py durchgereicht, das sie selbst aus der Umgebung lädt.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def run(
    graph_output: Path,
    namespace: str | None,
    insecure_skip_tls_verify: bool,
    datenimport_clear: bool,
    verbose: bool,
) -> bool:
    graph_output.parent.mkdir(parents=True, exist_ok=True)

    graph_cmd = [sys.executable, str(BASE_DIR / "istio-graph.py")]
    if namespace:
        graph_cmd += ["-n", namespace]
    if insecure_skip_tls_verify:
        graph_cmd.append("--insecure-skip-tls-verify")
    if verbose:
        graph_cmd.append("-v")

    logger.info("Erzeuge Graph: %s > %s", " ".join(graph_cmd), graph_output)
    with open(graph_output, "w", encoding="utf-8") as f:
        result = subprocess.run(graph_cmd, stdout=f, cwd=BASE_DIR)
    if result.returncode != 0:
        logger.error("istio-graph.py fehlgeschlagen (Exit-Code %s)", result.returncode)
        return False

    import_cmd = [sys.executable, str(BASE_DIR / "datenimport.py"), str(graph_output)]
    if datenimport_clear:
        import_cmd.append("--clear")
    if verbose:
        import_cmd.append("-v")

    logger.info("Importiere Graph nach Neo4j: %s", " ".join(import_cmd))
    result = subprocess.run(import_cmd, cwd=BASE_DIR)
    if result.returncode != 0:
        logger.error("datenimport.py fehlgeschlagen (Exit-Code %s)", result.returncode)
        return False

    return True


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Synchronisiert den Istio-Abhängigkeitsgraphen einmalig nach Neo4j "
                    "(istio-graph.py + datenimport.py). Für wiederkehrende Ausführung als "
                    "Kubernetes CronJob gedacht.",
    )
    parser.add_argument(
        "--output", default=os.environ.get("GRAPH_OUTPUT", "data/graph.json"),
        help="Zieldatei für den von istio-graph.py erzeugten Graphen (Standard: %(default)s, "
             "überschreibbar via GRAPH_OUTPUT).",
    )
    parser.add_argument(
        "-n", "--namespace", default=os.environ.get("ISTIO_NAMESPACE"),
        help="Nur diesen Namespace erfassen (Standard: alle, überschreibbar via ISTIO_NAMESPACE).",
    )
    parser.add_argument(
        "--insecure-skip-tls-verify", action="store_true",
        default=_env_flag("ISTIO_INSECURE_SKIP_TLS_VERIFY"),
        help="TLS-Prüfung gegenüber dem API-Server deaktivieren (überschreibbar via "
             "ISTIO_INSECURE_SKIP_TLS_VERIFY).",
    )
    parser.add_argument(
        "--clear", action="store_true",
        default=_env_flag("DATENIMPORT_CLEAR"),
        help="Löscht vor dem Import alle vorhandenen Knoten/Kanten in Neo4j, statt sie nur "
             "per MERGE zu aktualisieren (überschreibbar via DATENIMPORT_CLEAR). Ohne diese "
             "Option bleiben Knoten für inzwischen gelöschte Cluster-Ressourcen bestehen.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        default=_env_flag("VERBOSE"),
        help="Aktiviert Debug-Logging (überschreibbar via VERBOSE).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    graph_output = Path(args.output)
    if not graph_output.is_absolute():
        graph_output = BASE_DIR / graph_output

    ok = run(
        graph_output=graph_output,
        namespace=args.namespace,
        insecure_skip_tls_verify=args.insecure_skip_tls_verify,
        datenimport_clear=args.clear,
        verbose=args.verbose,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
