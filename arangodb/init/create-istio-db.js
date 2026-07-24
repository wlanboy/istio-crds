// Erstes Argument ist eine kommagetrennte Liste von Datenbanknamen (z. B.
// "istio,istio-connections") - der rohe Abhängigkeitsgraph
// (istio-graph.py/datenimport-arangodb.py) und der Deployment-Verbindungsgraph
// (connections-graph.py/datenimport-connections-arangodb.py) landen bewusst
// in getrennten Datenbanken statt in denselben nodes/edges-Collections, damit
// sich beide Graphen nicht über gleich benannte Knoten (z. B.
// "service:default/httpbin") vermischen. Derselbe Benutzer erhält Zugriff auf
// alle aufgeführten Datenbanken.
const dbNames = ARGUMENTS[0].split(",").map((s) => s.trim()).filter(Boolean);
const userName = ARGUMENTS[1];
const userPassword = ARGUMENTS[2];

const users = require("@arangodb/users");

function userExists(name) {
  try {
    users.document(name);
    return true;
  } catch (e) {
    return false;
  }
}

if (!userExists(userName)) {
  users.save(userName, userPassword, true);
  print(`User '${userName}' created.`);
} else {
  print(`User '${userName}' already exists, skipping.`);
}

for (const dbName of dbNames) {
  if (!db._databases().includes(dbName)) {
    db._createDatabase(dbName);
    print(`Database '${dbName}' created.`);
  } else {
    print(`Database '${dbName}' already exists, skipping.`);
  }
  users.grantDatabase(userName, dbName, "rw");

  db._useDatabase(dbName);
  // "nodes"/"edges" match the top-level keys istio-graph.py/connections-graph.py
  // already emit, so an importer can write straight into these without
  // renaming anything.
  if (!db._collection("nodes")) {
    db._createDocumentCollection("nodes");
    print(`Collection 'nodes' created in '${dbName}'.`);
  }
  if (!db._collection("edges")) {
    db._createEdgeCollection("edges");
    print(`Collection 'edges' created in '${dbName}'.`);
  }
  db._useDatabase("_system");
}
