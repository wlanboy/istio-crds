const dbName = ARGUMENTS[0];
const userName = ARGUMENTS[1];
const userPassword = ARGUMENTS[2];

if (!db._databases().includes(dbName)) {
  db._createDatabase(dbName, {}, [
    { username: userName, passwd: userPassword, active: true },
  ]);
  print(`Database '${dbName}' and user '${userName}' created.`);
} else {
  print(`Database '${dbName}' already exists, skipping.`);
}

db._useDatabase(dbName);

// "nodes"/"edges" match the top-level keys istio-graph.py already emits, so
// a future importer can write straight into these without renaming anything.
if (!db._collection("nodes")) {
  db._createDocumentCollection("nodes");
  print("Collection 'nodes' created.");
}
if (!db._collection("edges")) {
  db._createEdgeCollection("edges");
  print("Collection 'edges' created.");
}
