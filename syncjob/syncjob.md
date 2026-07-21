# sync-job.py

Führt [`istio-graph.py`](../istio-graph.py) und [`datenimport.py`](../datenimport.py)
einmalig hintereinander aus (Graph erzeugen → nach Neo4j importieren) und
beendet sich danach. Gedacht für den Betrieb als Kubernetes CronJob — die
Wiederholung übernimmt dabei der CronJob, dieses Skript macht pro Aufruf
genau einen Durchlauf.

Konfiguration kommt aus Umgebungsvariablen bzw. einer `.env`-Datei im
aktuellen Verzeichnis:

| Variable | Beschreibung |
|---|---|
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | Wie bei `datenimport.py` — werden unverändert durchgereicht. |
| `GRAPH_OUTPUT` | Zieldatei für den erzeugten Graphen (Standard: `data/graph.json`). |
| `ISTIO_NAMESPACE` | Nur diesen Namespace erfassen (Standard: alle). |
| `ISTIO_INSECURE_SKIP_TLS_VERIFY` | `true`/`1`, um die TLS-Prüfung gegenüber dem API-Server zu deaktivieren. |
| `DATENIMPORT_CLEAR` | `true`/`1`, um vor jedem Import alle vorhandenen Knoten/Kanten in Neo4j zu löschen statt sie nur per MERGE zu aktualisieren (ohne diese Option bleiben Knoten für inzwischen gelöschte Cluster-Ressourcen bestehen). |
| `VERBOSE` | `true`/`1` für Debug-Logging. |

```bash
python3 sync-job.py
```

## Als Kubernetes CronJob

Im Ordner [syncjob/](.) liegen fertige Manifeste:

| Datei | Zweck |
|---|---|
| [namespace.yaml](namespace.yaml) | Namespace `istio-graph-sync` |
| [rbac.yaml](rbac.yaml) | ServiceAccount + ClusterRole (Leserechte auf CRDs, Istio-Objekte, Namespaces/Services/ServiceAccounts/Pods/NetworkPolicies) + ClusterRoleBinding |
| [secret.example.yaml](secret.example.yaml) | Vorlage für die Neo4j-Zugangsdaten |
| [cronjob.yaml](cronjob.yaml) | CronJob (Standard: 1x täglich um 03:00 Uhr), nutzt das ServiceAccount aus `rbac.yaml` und die Zugangsdaten aus dem Secret |

Image bauen und in eine erreichbare Registry pushen (das mitgelieferte
[Dockerfile](../Dockerfile) enthält alle für `sync-job.py` benötigten Skripte):

```bash
docker build -t <registry>/istio-crds-sync:latest .
docker push <registry>/istio-crds-sync:latest
```

`image:` in `syncjob/cronjob.yaml` entsprechend anpassen, dann anwenden:

```bash
kubectl apply -f syncjob/namespace.yaml
kubectl apply -f syncjob/rbac.yaml
cp syncjob/secret.example.yaml syncjob/secret.yaml   # NEO4J_PASSWORD anpassen
kubectl apply -f syncjob/secret.yaml
kubectl apply -f syncjob/cronjob.yaml
```

Da `kubectl.py` beim Laufen im Cluster automatisch die In-Cluster-Config
(ServiceAccount-Token) verwendet, ist kein Kubeconfig-Mount nötig.
