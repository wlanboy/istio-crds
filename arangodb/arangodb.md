# ArangoDB-Integration

Dieses Dokument beschreibt den kompletten ArangoDB-Pfad des Projekts: von den
JSON-Graphen (`istio-graph.py` / `connections-graph.py`) über den Import nach
ArangoDB bis zur Visualisierung im Browser.

Zwei unabhängige CLI-Pipelines erzeugen JSON-Graphen (`{"nodes": [...],
"edges": [...]}`) direkt aus dem Cluster:

| Quelle | Erzeugt | Beschreibung |
|---|---|---|
| [istio-graph.py](../istio-graph.py) | roher Abhängigkeitsgraph | jede aufgelöste Label-Selektor-/Host-/Principal-Beziehung als eigene Kante (Namespace, Service, ServiceAccount, Pod, NetworkPolicy, alle Istio-CRDs, Host, ...) |
| [connections-graph.py](../connections-graph.py) | Deployment-Verbindungsgraph | kuratierte, Deployment-zentrierte Sicht: welche Deployment-zu-Deployment-Verbindungen sind über Gateway/Service/ServiceEntry/VirtualService möglich, plus explizit per AuthorizationPolicy(DENY) verbotene Verbindungen |

Beide werden über einen jeweils eigenen Importer nach ArangoDB geschrieben:

| Importer | Liest | Ziel-Datenbank (Standard) |
|---|---|---|
| [datenimport-arangodb.py](datenimport-arangodb.py) | Ausgabe von `istio-graph.py` | `istio` |
| [datenimport-connections-arangodb.py](datenimport-connections-arangodb.py) | Ausgabe von `connections-graph.py` | `istio-connections` |
| [datenimport.py](../datenimport.py) | Ausgabe von `istio-graph.py` | (Neo4j, zum Vergleich, nicht Teil dieses Dokuments) |

**Wichtig:** Die beiden ArangoDB-Importer schreiben absichtlich in **zwei
getrennte Datenbanken** (jeweils mit gleich benannten Collections
`nodes`/`edges`, siehe [Collections](#collections-nodes--edges) unten) —
nicht in dieselbe. Beide Graphen verwenden zwar dieselbe `id`-
Bildungsvorschrift für Knoten (z. B. `service:default/httpbin` kann in
beiden Graphen vorkommen), sollen sich dadurch aber nicht vermischen: der
rohe Graph bleibt eine vollständige, ungefilterte Sicht auf den Cluster, der
Verbindungsgraph eine bewusst kuratierte, auf Deployment-Erreichbarkeit
reduzierte Sicht (siehe Docstring von
[connections-graph.py](../connections-graph.py)) — beide in derselben
Datenbank zu vermengen würde z. B. dazu führen, dass ein `service`-Knoten aus
dem einen Graphen Kanten aus dem jeweils anderen "erbt".

## Docker-Compose-Setup

[docker-compose.yml](docker-compose.yml) startet drei Services:

| Service | Zweck |
|---|---|
| `arangodb` | die eigentliche ArangoDB-Instanz, HTTP-Port `8529` |
| `arangodb-init` | Einmal-Job (läuft nach dem Healthcheck von `arangodb`, dann fertig): legt beide Datenbanken + Benutzer + Collections an, siehe [Init-Skript](#init-skript-initcreate-istio-dbjs) |
| `web` | nginx, liefert den statischen Graph-Viewer aus [../arangoweb/](../arangoweb/) auf Port `8080` |

Start:

```bash
cd arangodb
docker compose up -d
```

### Umgebungsvariablen (Docker Compose)

| Variable | Standard | Bedeutung |
|---|---|---|
| `ARANGO_ROOT_PASSWORD` | `changeme123` | Passwort des ArangoDB-`root`-Users (verwaltet den ganzen Server) |
| `ISTIO_DB` | `istio` | Name der Datenbank für den rohen Graphen (`istio-graph.py`) |
| `ISTIO_CONNECTIONS_DB` | `istio-connections` | Name der Datenbank für den Deployment-Verbindungsgraphen (`connections-graph.py`) |
| `ISTIO_USER` | `istio` | Name des dedizierten Datenbank-Benutzers (nicht `root`) — erhält Zugriff auf **beide** Datenbanken |
| `ISTIO_PASSWORD` | `changeme123` | Passwort dieses dedizierten Benutzers — **muss** mit dem `--password`/`ARANGO_PASSWORD` übereinstimmen, das die Importer-Skripte verwenden |

Diese lassen sich z. B. über eine `.env`-Datei in `arangodb/` setzen (von
Docker Compose automatisch geladen) — unabhängig von der `.env` im
Repo-Root, die die Python-Importer über `python-dotenv` lesen (siehe unten).

### Init-Skript (`init/create-istio-db.js`)

Läuft einmalig über `arangosh` im `arangodb-init`-Container und ist
idempotent (überspringt bereits vorhandene Objekte). Erstes Argument ist eine
kommagetrennte Liste von Datenbanknamen (`ISTIO_DB,ISTIO_CONNECTIONS_DB`):

1. Legt den Benutzer `ISTIO_USER`/`ISTIO_PASSWORD` an (falls noch nicht
   vorhanden).
2. Legt für **jede** der beiden Datenbanken:
   - die Datenbank selbst an (falls noch nicht vorhanden),
   - Zugriff (`rw`) für `ISTIO_USER` darauf,
   - zwei Collections darin an:
     - `nodes` — Dokument-Collection, ein Dokument pro Graph-Knoten.
     - `edges` — Edge-Collection, ein Dokument pro Graph-Kante (`_from`/`_to`).

Der Kommentar im Skript hält bewusst fest, dass die beiden Collections in
jeder Datenbank so benannt sind, dass der jeweilige Importer direkt
hineinschreiben kann, ohne etwas umzubenennen.

## Collections (`nodes`/`edges`)

Beide Datenbanken (`istio` und `istio-connections`) haben identisch benannte
Collections mit identischer Dokumentstruktur — nur ihr Inhalt unterscheidet
sich, je nachdem, welcher der beiden Importer hineingeschrieben hat.

### Knoten-Dokument (`nodes`)

```json
{
  "_key": "3f2504e0...",                 // SHA1-Hash der Graph-id, siehe unten
  "id": "deployment:default/httpbin",    // Original-id aus dem JSON-Graphen
  "kind": "deployment",
  "name": "httpbin",
  "namespace": "default",
  "service_account": "httpbin",          // ...alle weiteren `attributes` des Knotens, flach übernommen
  "labels": {"app": "httpbin"}
}
```

Die von `istio-graph.py`/`connections-graph.py` vergebene `id` (z. B.
`service:default/httpbin`) enthält Zeichen (u. a. `/`), die in einem
ArangoDB-`_key` nicht erlaubt sind. Der `_key` ist deshalb ein SHA1-Hash
dieser `id` (`_node_key()` in beiden Importern); die `id` bleibt zusätzlich
als normales Property erhalten, u. a. für Debugging/AQL-Filter.

### Kanten-Dokument (`edges`)

```json
{
  "_key": "9e107d9d...",                          // SHA1-Hash über source/target/relation/Attribute
  "_from": "nodes/3f2504e0...",
  "_to": "nodes/7c9e6679...",
  "relation": "forbidden",                        // z. B. "may_call", "selects", "routes_to", "forbidden", ...
  "action": "DENY"                                // ...alle weiteren `attributes` der Kante, flach übernommen
}
```

`relation: "forbidden"` (nur in der `istio-connections`-Datenbank) markiert
die von `connections-graph.py` aus einer `AuthorizationPolicy(DENY)`
abgeleiteten, explizit verbotenen Verbindungen (siehe Docstring von
[connections-graph.py](../connections-graph.py)).

### Import-Mechanik (UPSERT, idempotent)

Beide Importer (`_import_nodes`/`_import_edges` in
[datenimport-arangodb.py](datenimport-arangodb.py) und
[datenimport-connections-arangodb.py](datenimport-connections-arangodb.py))
funktionieren identisch, schreiben aber jeweils standardmäßig in ihre eigene
Datenbank (siehe Tabelle oben):

- **Knoten** werden per AQL `UPSERT {_key: ...} INSERT row UPDATE row IN nodes`
  geschrieben — mehrfaches Einspielen desselben Graphen dupliziert nichts,
  sondern aktualisiert die Properties.
- **Kanten** werden genauso per `_key` upgesertet, aber der `_key` ist hier
  ein Hash über `source`/`target`/`relation`/**Attribute** (`_attrs_key()`).
  Das ist nötig, weil derselbe Knoten-/Relationstyp-Tripel mit
  unterschiedlichen Attributen mehrfach auftreten kann (z. B. ein
  VirtualService-Canary-Split mit zwei `routes_to`-Kanten zum selben Host,
  aber unterschiedlichem `weight`, oder zwei verschiedene
  AuthorizationPolicy(DENY)-Regeln, die dieselbe Quelle→Ziel-Beziehung
  verbieten) — ohne den Attribut-Hash im Merge-Key würde die zweite Kante die
  Attribute der ersten überschreiben statt ein eigenes Dokument anzulegen.
- `--clear` leert vorher `nodes` und `edges` **der jeweils per `--database`
  gewählten Datenbank** (`truncate`) — ein `--clear`-Lauf von
  `datenimport-arangodb.py` betrifft die `istio-connections`-Datenbank also
  nicht mehr (und umgekehrt), da beide Importer seit der Trennung in
  unterschiedlichen Datenbanken arbeiten.

## Benutzung

Voraussetzung: `uv sync` (bzw. `pip install -r requirements.txt`) im
Repo-Root, ArangoDB läuft (siehe [Docker-Compose-Setup](#docker-compose-setup)
oben), sowie eine gültige Kubeconfig für die Graph-Erzeugung. Alle folgenden
Befehle laufen im Repo-Root.

### Rohen Abhängigkeitsgraphen importieren (Datenbank `istio`)

```bash
python3 istio-graph.py | python3 arangodb/datenimport-arangodb.py --clear
```

### Deployment-Verbindungsgraphen importieren (Datenbank `istio-connections`)

```bash
python3 connections-graph.py | python3 arangodb/datenimport-connections-arangodb.py --clear
```

Beide sind vollständig unabhängig voneinander — Reihenfolge und `--clear`
auf dem einen wirken sich nicht auf den anderen aus:

```bash
python3 istio-graph.py | python3 arangodb/datenimport-arangodb.py --clear
python3 connections-graph.py | python3 arangodb/datenimport-connections-arangodb.py --clear
```

### CLI-Optionen (beide Importer bis auf den `--database`-Standard identisch)

| Option | Beschreibung |
|---|---|
| `input` (positional) | Pfad zur `graph.json`/`connections.json`; `-` liest von stdin (Standard). |
| `--url` | ArangoDB-HTTP-Endpunkt (Standard: `http://localhost:8529`, überschreibbar via `ARANGO_URL`). |
| `--user` | ArangoDB-Benutzername (Standard: `istio`, überschreibbar via `ARANGO_USER` — muss zu `ISTIO_USER` aus dem Docker-Compose-Setup passen). |
| `--password` | ArangoDB-Passwort (Standard: Umgebungsvariable `ARANGO_PASSWORD`, auch aus einer `.env`-Datei im Repo-Root geladen — muss zu `ISTIO_PASSWORD` passen). |
| `--database` | Ziel-Datenbank. Bei `datenimport-arangodb.py` Standard `istio` (überschreibbar via `ARANGO_DATABASE`); bei `datenimport-connections-arangodb.py` Standard `istio-connections` (überschreibbar via `ARANGO_CONNECTIONS_DATABASE`). |
| `--clear` | Löscht vor dem Import alle vorhandenen Knoten und Kanten dieser einen Datenbank (`nodes`/`edges` komplett, siehe oben). |
| `-v`, `--verbose` | Aktiviert Debug-Logging. |

Gegen eine entfernte Instanz mit eigenen Zugangsdaten:

```bash
python3 connections-graph.py | python3 arangodb/datenimport-connections-arangodb.py \
    --url http://arango.gmk.lan:8529 --user istio --password "$ARANGO_PASSWORD" \
    --database istio-connections --clear
```

Da beide Skripte `load_dotenv()` aufrufen, reicht es, `ARANGO_PASSWORD=...`
(und optional `ARANGO_URL`/`ARANGO_USER`/`ARANGO_DATABASE`/
`ARANGO_CONNECTIONS_DATABASE`) einmal in die `.env` im Repo-Root
einzutragen (dort liegen aktuell nur die `NEO4J_*`-Variablen — `ARANGO_*`
muss bei Bedarf ergänzt werden).

## Visualisierung (`../arangoweb/`)

[../arangoweb/](../arangoweb/) ist ein winziger, abhängigkeitsfreier
Browser-Graph-Viewer (`index.html` + `app.js` + `style.css`), den der `web`-
Service aus dem Docker-Compose-Setup über nginx auf Port `8080` ausliefert.

- Verbindet sich **direkt aus dem Browser** über die ArangoDB-HTTP-API
  (`/_db/<database>/_api/cursor`) — kein eigenes Backend nötig.
- Login-Formular fragt Endpoint, **beide** Datenbanknamen (`Datenbank` für
  den rohen Graphen, `Verbindungs-Datenbank` für den Deployment-
  Verbindungsgraphen), Benutzer, Passwort und Knoten-Limit ab (Vorbelegung:
  `http://localhost:8529` / `istio` / `istio-connections` / `istio`).
- Zwei Tabs oberhalb des Graphen ("Gesamtgraph" / "Verbindungsgraph")
  wechseln zwischen den beiden Datenbanken, ohne neu einzuloggen — dieselbe
  Lade-Logik (`loadGraphFromDatabase()`) wird dabei nur mit dem jeweils
  anderen Datenbanknamen aufgerufen, da beide Datenbanken identisch benannte
  `nodes`/`edges`-Collections haben.
- Lädt Knoten per AQL (`FOR n IN nodes ... LIMIT @limit RETURN n`), blendet
  dabei einige Infrastruktur-Namespaces aus (`kube-system`,
  `kube-node-lease`, `kube-public`, `local-path-storage`, `cert-manager`),
  danach passende Kanten aus `edges`, und zeichnet das Ergebnis interaktiv
  (ziehbare Knoten, Zoom/Pan, Tooltip mit allen Properties) auf einem
  `<canvas>`.
- Farbcodierung/Legende nach `kind` (z. B. `deployment`, `service`,
  `gateway`, `virtualservice`, `serviceentry`, `authorizationpolicy`,
  `namespace`, ...); `forbidden`-Kanten (nur im Verbindungsgraph-Tab) werden
  immer rot gestrichelt gezeichnet, unabhängig vom Hover-Zustand.
- Merkt sich die letzte Verbindung (**inkl. Passwort**) in
  `localStorage`, um nach einem Reload nicht erneut fragen zu müssen — laut
  Kommentar im Code bewusst nur für den lokalen/vertrauenswürdigen
  Netzwerkeinsatz gedacht, nicht für ein öffentlich erreichbares Deployment.

Aufruf: `http://localhost:8080` (bzw. den Host, auf dem der `web`-Service
läuft), nachdem mindestens einer der beiden Importer gelaufen ist. Der
jeweils andere Tab bleibt leer (bzw. zeigt einen Ladefehler), solange seine
Datenbank noch nicht befüllt wurde.

## Beispiel-AQL-Abfragen

Direkt in der ArangoDB-Weboberfläche (`http://localhost:8529`) oder über
`arangosh`.

Alle explizit verbotenen Verbindungen (Datenbank `istio-connections`):

```aql
FOR e IN edges
  FILTER e.relation == "forbidden"
  LET src = DOCUMENT(e._from)
  LET dst = DOCUMENT(e._to)
  RETURN {von: src.id, nach: dst.id}
```

Alle Deployments mit ihrem ServiceAccount (Datenbank `istio-connections`):

```aql
FOR n IN nodes
  FILTER n.kind == "deployment"
  RETURN {name: n.name, namespace: n.namespace, service_account: n.service_account}
```

Alle (möglichen) eingehenden Verbindungen zu einem bestimmten Deployment,
zwei Hops tief (Datenbank `istio-connections`):

```aql
FOR v, e, p IN 1..2 INBOUND "nodes/<_key-des-Deployments>" edges
  RETURN {pfad: p.vertices[*].id, relations: p.edges[*].relation}
```

## Bekannte Einschränkungen

- Es gibt keine automatische Bereinigung verwaister Knoten/Kanten zwischen
  zwei Importläufen ohne `--clear` (z. B. wenn ein Deployment im Cluster
  gelöscht wurde) — ein alter Knoten bleibt bestehen, bis er per `--clear`
  oder manuell entfernt wird.
- `../arangoweb/` ist ein reines Debug-/Explorations-Werkzeug ohne
  Zugriffsschutz über die statische Auslieferung hinaus — nicht für ein
  öffentlich erreichbares Deployment gedacht (siehe Hinweis zu
  `localStorage` oben).
