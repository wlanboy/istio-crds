# istio-crds

Kommandozeilen-Tool, das alle in einem Kubernetes-Cluster installierten
**Istio-CRDs** (Gruppe `*.istio.io`) mit jeder API-Version auflistet.
Zusätzlich werden potenzielle Cluster-Probleme rund um diese CRDs erkannt:
veraltete API-Versionen, ausstehende Storage-Version-Migrationen sowie
nicht erreichbare/ungesunde CRDs.

## Funktionsumfang

- Listet jede Istio-CRD mit Scope (Namespaced/Cluster), Conversion-Strategie
  und allen API-Versionen (served/storage/deprecated).
- Warnt vor:
  - **veralteten API-Versionen** (`deprecated: true` im CRD-Schema)
  - **Storage-Version-Migrationen**, die noch nicht abgeschlossen sind
    (`status.storedVersions` enthält mehr als die aktuelle Storage-Version)
  - **ungesunden CRDs** (Status-Conditions `Established`/`NamesAccepted` sind
    `False`)

## Projektstruktur

| Datei | Zweck |
|---|---|
| [main.py](main.py) | CLI-Einstiegspunkt (Argument-Parsing, Tabellenausgabe) für die CRD-Übersicht |
| [istio-objekt-liste.py](istio-objekt-liste.py) | CLI-Einstiegspunkt, der alle gesammelten Kubernetes-/Istio-Objekte als ein flaches JSON-Dokument ausgibt |
| [istio-graph.py](istio-graph.py) | CLI-Einstiegspunkt, der aus denselben Objekten einen JSON-Abhängigkeitsgraphen (Knoten + Kanten) baut |
| [datenimport.py](datenimport.py) | CLI-Einstiegspunkt, der einen von istio-graph.py erzeugten Abhängigkeitsgraphen nach Neo4j importiert |
| [sync-job.py](sync-job.py) | Führt istio-graph.py und datenimport.py einmalig hintereinander aus; für den Betrieb als Kubernetes CronJob (siehe [syncjob/syncjob.md](syncjob/syncjob.md)) |
| [kubectl.py](kubectl.py) | Generische Kubernetes-Datenerfassung: Namespaces (inkl. Labels), Services, ServiceAccounts, Pods, Mesh-Root-Namespace, CRD-Auflistung mit Versionen |
| [istio.py](istio.py) | Parser für die Istio-CRDs selbst (VirtualService, DestinationRule, Gateway, ServiceEntry, Sidecar, WorkloadEntry, WorkloadGroup, PeerAuthentication, AuthorizationPolicy, RequestAuthentication) in strukturierte Dataclasses — für eine künftige Traffic-/Policy-Graph-Auswertung vorbereitet |

## Installation

Benötigt Python ≥ 3.12 (siehe [.python-version](.python-version)).

Mit [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

Oder mit `pip`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Benutzung

Voraussetzung ist eine gültige Kubeconfig (`~/.kube/config`) bzw. — beim
Lauf im Cluster — ein ServiceAccount mit Leserechten auf
`customresourcedefinitions` sowie auf die jeweiligen Istio-CRD-Ressourcen.

```bash
python3 main.py [-n NAMESPACE] [--insecure-skip-tls-verify] [-v]
```

| Option | Beschreibung |
|---|---|
| `-n`, `--namespace` | Nur namespaced CRDs anzeigen (cluster-scoped CRDs werden nur angezeigt, wenn diese Option weggelassen wird). |
| `--insecure-skip-tls-verify` | TLS-Zertifikatsprüfung gegenüber dem API-Server deaktivieren (entspricht `kubectl`/`oc --insecure-skip-tls-verify`) — z. B. bei selbstsignierten Zertifikaten in Testclustern. |
| `-v`, `--verbose` | Debug-Logging aktivieren, u. a. für übersprungene API-Aufrufe. |

### Beispiele

Alle Istio-CRDs im gesamten Cluster:

```bash
python3 main.py
```

Nur namespaced CRDs anzeigen (cluster-scoped CRDs ausblenden):

```bash
python3 main.py -n istio-system
```

Gegen einen Cluster mit selbstsigniertem Zertifikat, mit Debug-Ausgabe:

```bash
python3 main.py --insecure-skip-tls-verify -v
```

### istio-objekt-liste.py

Gibt alle gesammelten Kubernetes-/Istio-Objekte (Namespaces, Services,
ServiceAccounts, Pods sowie alle Istio-CRDs) als ein flaches JSON-Dokument
aus. Nimmt dieselben Optionen wie `main.py` entgegen.

```bash
python3 istio-objekt-liste.py [-n NAMESPACE] [--insecure-skip-tls-verify] [-v]
```

Alle Objekte im gesamten Cluster als JSON:

```bash
python3 istio-objekt-liste.py
```

Nur Objekte aus einem Namespace:

```bash
python3 istio-objekt-liste.py -n istio-system
```

Gegen einen Cluster mit selbstsigniertem Zertifikat, mit Debug-Ausgabe,
Ausgabe in eine Datei umgeleitet:

```bash
python3 istio-objekt-liste.py --insecure-skip-tls-verify -v > objekte.json
```

### istio-graph.py

Baut aus denselben Objekten einen JSON-Abhängigkeitsgraphen (`{"nodes": [...],
"edges": [...]}`). Label-Selektoren (Service→Pod, Gateway/Sidecar/
PeerAuthentication/AuthorizationPolicy/RequestAuthentication/NetworkPolicy→
Pod bzw. Namespace), Host-Strings (VirtualService/DestinationRule/Gateway/
ServiceEntry/Sidecar→Host, Host→Service) sowie SPIFFE-Principals
(AuthorizationPolicy→ServiceAccount) und `targetRef`s werden dabei zu
konkreten Kanten aufgelöst. Nimmt dieselben Optionen wie `main.py` entgegen.

```bash
python3 istio-graph.py [-n NAMESPACE] [--insecure-skip-tls-verify] [-v]
```

Abhängigkeitsgraph des gesamten Clusters als JSON:

```bash
python3 istio-graph.py > graph.json
```

Nur ein Namespace, mit Debug-Ausgabe für nicht auflösbare Kanten
(z. B. ein Host ohne passenden Service):

```bash
python3 istio-graph.py -n default -v > graph.json
```

### datenimport.py

Importiert einen von `istio-graph.py` erzeugten Abhängigkeitsgraphen nach
[Neo4j](https://neo4j.com/). Jeder Knoten wird als Node mit seiner `kind`
als Label angelegt (z. B. `service` → `:Service`), jede Kante als
Relationship mit ihrer `relation` als Typ (z. B. `in_namespace` →
`IN_NAMESPACE`). Der Import läuft über `MERGE` auf `id` (Knoten) bzw.
`source`/`target`/Relationship-Typ (Kanten) und ist damit idempotent —
mehrfaches Einspielen derselben `graph.json` dupliziert nichts.

Voraussetzung ist eine erreichbare Neo4j-Instanz. Verbindungsdaten kommen
entweder aus den Umgebungsvariablen `NEO4J_URI`, `NEO4J_USER`,
`NEO4J_PASSWORD`, `NEO4J_DATABASE` oder aus den entsprechenden
Kommandozeilenoptionen; ohne Angabe wird `neo4j`/`$NEO4J_PASSWORD` gegen
`bolt://localhost:7687` verwendet.

```bash
python3 datenimport.py [INPUT] [--uri URI] [--user USER] [--password PASSWORT]
                        [--database DATENBANK] [--clear] [-v]
```

| Option | Beschreibung |
|---|---|
| `INPUT` | Pfad zur `graph.json`; `-` liest von stdin (Standard). |
| `--uri` | Neo4j-Bolt-URI (Standard: `bolt://localhost:7687`, überschreibbar via `NEO4J_URI`). |
| `--user` | Neo4j-Benutzername (Standard: `neo4j`, überschreibbar via `NEO4J_USER`). |
| `--password` | Neo4j-Passwort (Standard: `NEO4J_PASSWORD`, sonst `changeme123`). |
| `--database` | Ziel-Datenbank (Standard: `neo4j`, überschreibbar via `NEO4J_DATABASE`). |
| `--clear` | Löscht vor dem Import alle vorhandenen Knoten und Kanten in der Zieldatenbank. |
| `-v`, `--verbose` | Debug-Logging aktivieren. |

Graph aus einer Datei importieren, Zieldatenbank vorher leeren:

```bash
python3 datenimport.py graph.json --clear
```

Direkt aus `istio-graph.py` importieren, ohne Zwischendatei:

```bash
python3 istio-graph.py | python3 datenimport.py --clear

#Alle Parameter
python3 istio-graph.py | python3 datenimport.py --uri bolt://gmk.lan:7687 --user neo4j --password changeme123 --database neo4j --clear
```

Gegen eine entfernte Instanz mit eigenen Zugangsdaten:

```bash
python3 datenimport.py graph.json --clear \
    --uri bolt://neo4j.gmk.lan:7687 --user neo4j --password "$NEO4J_PASSWORD"
```

## Lizenz

[Apache License 2.0](LICENSE)
