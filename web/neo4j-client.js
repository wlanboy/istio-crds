(function () {
  "use strict";

  const STORAGE_KEY = "neo4jGraphViewerConnection";
  const EXCLUDED_NAMESPACES = [
    "kube-system",
    "kube-node-lease",
    "kube-public",
    "local-path-storage",
    "cert-manager",
    "default",
  ];

  let driver = null;

  // Remember the last connection (incl. password, in localStorage) so a
  // reload (F5) can reconnect automatically instead of asking again.
  // Only fine for a local/trusted-network tool - never do this for anything
  // reachable by other people.
  function loadSavedConnection() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function saveConnection(data) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  }

  function clearSavedConnection() {
    localStorage.removeItem(STORAGE_KEY);
  }

  async function connect(uri, user, password) {
    driver = neo4j.driver(uri, neo4j.auth.basic(user, password));
    try {
      await driver.verifyConnectivity();
    } catch (err) {
      await driver.close();
      driver = null;
      throw err;
    }
  }

  async function disconnect() {
    if (driver) {
      await driver.close();
      driver = null;
    }
  }

  async function loadGraph(limit) {
    const session = driver.session();
    try {
      const nodeResult = await session.run(
        "MATCH (n) " +
          "WHERE (n.namespace IS NULL OR NOT n.namespace IN $excludedNamespaces) " +
          "AND NOT (n.kind = 'namespace' AND n.name IN $excludedNamespaces) " +
          "RETURN n LIMIT $limit",
        { limit: neo4j.int(limit), excludedNamespaces: EXCLUDED_NAMESPACES }
      );

      const nodes = [];
      const nodeIds = new Set();
      for (const record of nodeResult.records) {
        const n = record.get("n");
        nodes.push({
          id: n.elementId,
          labels: n.labels,
          properties: n.properties,
        });
        nodeIds.add(n.elementId);
      }

      const edges = [];
      if (nodes.length > 0) {
        // Filtering by elementId() in Cypher requires Neo4j 5+; instead fetch
        // relationships and keep only those between nodes we already loaded -
        // works against any server version the driver itself supports.
        const relResult = await session.run(
          "MATCH (n)-[r]->(m) RETURN n, r, m LIMIT $limit",
          { limit: neo4j.int(limit * 4) }
        );
        for (const record of relResult.records) {
          const sourceId = record.get("n").elementId;
          const targetId = record.get("m").elementId;
          if (!nodeIds.has(sourceId) || !nodeIds.has(targetId)) continue;
          const r = record.get("r");
          edges.push({
            id: r.elementId,
            type: r.type,
            properties: r.properties,
            source: sourceId,
            target: targetId,
          });
        }
      }

      return { nodes, edges };
    } finally {
      await session.close();
    }
  }

  window.Neo4jClient = {
    loadSavedConnection,
    saveConnection,
    clearSavedConnection,
    connect,
    disconnect,
    loadGraph,
  };
})();
