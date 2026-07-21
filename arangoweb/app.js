(function () {
  "use strict";

  // Categorical palette (fixed order, never cycled beyond slot 8 -> "Other")
  const PALETTE = [
    "#2a78d6", // blue
    "#eb6834", // orange
    "#1baf7a", // aqua
    "#eda100", // yellow
    "#e87ba4", // magenta
    "#008300", // green
    "#4a3aa7", // violet
    "#e34948", // red
  ];
  const OTHER_COLOR = "#898781"; // muted gray for overflow labels
  const NODE_RADIUS = 14;
  const STORAGE_KEY = "arangoGraphViewerConnection";
  const EXCLUDED_NAMESPACES = [
    "kube-system",
    "kube-node-lease",
    "kube-public",
    "local-path-storage",
    "cert-manager",
  ];

  const loginPanel = document.getElementById("login-panel");
  const loginForm = document.getElementById("login-form");
  const loginError = document.getElementById("login-error");
  const connectBtn = document.getElementById("connect-btn");
  const graphPanel = document.getElementById("graph-panel");
  const disconnectBtn = document.getElementById("disconnect-btn");
  const statsEl = document.getElementById("stats");
  const legendEl = document.getElementById("legend");
  const graphScroll = document.getElementById("graph-scroll");
  const canvas = document.getElementById("graph-canvas");
  const tooltip = document.getElementById("tooltip");
  const ctx = canvas.getContext("2d");

  let conn = null; // { endpoint, database, user, password }
  let nodes = [];
  let edges = [];
  let nodeById = new Map();
  let labelColor = new Map();
  let width = 0, height = 0;

  let draggingNode = null;
  let hoveredNode = null;
  let panStart = null; // { mouseX, mouseY, scrollLeft, scrollTop } while panning the empty background

  loginForm.addEventListener("submit", onConnect);
  disconnectBtn.addEventListener("click", onDisconnect);
  window.addEventListener("resize", onWindowResize);
  canvas.addEventListener("mousedown", onMouseDown);
  canvas.addEventListener("mousemove", onMouseMove);
  window.addEventListener("mouseup", onMouseUp);

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

  const saved = loadSavedConnection();
  if (saved) {
    document.getElementById("endpoint").value = saved.endpoint;
    document.getElementById("database").value = saved.database;
    document.getElementById("user").value = saved.user;
    document.getElementById("password").value = saved.password;
    document.getElementById("limit").value = saved.limit;
    onConnect(new Event("submit", { cancelable: true }));
  }

  async function onConnect(e) {
    e.preventDefault();
    loginError.textContent = "";
    connectBtn.disabled = true;
    connectBtn.textContent = "Verbinde...";

    const endpoint = document.getElementById("endpoint").value.trim().replace(/\/$/, "");
    const database = document.getElementById("database").value.trim();
    const user = document.getElementById("user").value.trim();
    const password = document.getElementById("password").value;
    const limit = parseInt(document.getElementById("limit").value, 10) || 300;

    const candidate = { endpoint, database, user, password };
    try {
      await arangoQuery(candidate, "RETURN 1", {});
      conn = candidate;
      saveConnection({ endpoint, database, user, password, limit });
      await loadGraph(limit);
      loginPanel.classList.add("hidden");
      graphPanel.classList.remove("hidden");
      layoutNetwork();
      draw();
    } catch (err) {
      loginError.textContent = "Verbindung fehlgeschlagen: " + (err.message || err);
      conn = null;
    } finally {
      connectBtn.disabled = false;
      connectBtn.textContent = "Verbinden";
    }
  }

  function onDisconnect() {
    conn = null;
    nodes = []; edges = []; nodeById = new Map(); labelColor = new Map();
    graphPanel.classList.add("hidden");
    loginPanel.classList.remove("hidden");
    document.getElementById("password").value = "";
    localStorage.removeItem(STORAGE_KEY);
  }

  function nodeDisplayLabel(labels) {
    return labels && labels.length ? labels[0] : "Node";
  }

  function labelFromKind(kind) {
    if (!kind) return "Node";
    return kind.charAt(0).toUpperCase() + kind.slice(1);
  }

  // Strips ArangoDB's internal document/edge fields so the rest of the code
  // (tooltip, caption lookup) sees the same flat "user properties" shape
  // that the Neo4j driver used to hand back via node.properties.
  function stripInternalFields(doc) {
    const { _key, _id, _rev, _from, _to, ...rest } = doc;
    return rest;
  }

  // Talks to ArangoDB's cursor API directly (HTTP + Basic Auth) instead of a
  // driver library, and follows the "hasMore" pagination so result sets
  // larger than one batch (e.g. a high node/edge limit) still come back
  // complete.
  async function arangoRequest(connection, path, options) {
    const url = connection.endpoint + path;
    const headers = Object.assign(
      {
        "Content-Type": "application/json",
        "Authorization": "Basic " + btoa(`${connection.user}:${connection.password}`),
      },
      (options && options.headers) || {}
    );
    const res = await fetch(url, Object.assign({}, options, { headers }));
    const body = await res.json().catch(() => null);
    if (!res.ok || (body && body.error)) {
      throw new Error((body && body.errorMessage) || `HTTP ${res.status}`);
    }
    return body;
  }

  async function arangoQuery(connection, query, bindVars) {
    const dbPath = `/_db/${encodeURIComponent(connection.database)}/_api/cursor`;
    let body = await arangoRequest(connection, dbPath, {
      method: "POST",
      body: JSON.stringify({ query, bindVars, batchSize: 1000 }),
    });
    const results = body.result.slice();
    while (body.hasMore) {
      body = await arangoRequest(connection, `${dbPath}/${body.id}`, { method: "PUT" });
      results.push(...body.result);
    }
    return results;
  }

  function colorForLabel(label) {
    if (labelColor.has(label)) return labelColor.get(label);
    if (labelColor.size < PALETTE.length) {
      const color = PALETTE[labelColor.size];
      labelColor.set(label, color);
      return color;
    }
    labelColor.set(label, OTHER_COLOR);
    return OTHER_COLOR;
  }

  function shortText(str, max) {
    if (!str) return str;
    return str.length > max ? str.slice(0, max - 1) + "…" : str;
  }

  function nodeCaption(props, labels) {
    const candidates = ["name", "title", "id", "key", "displayName"];
    for (const key of candidates) {
      if (props[key] !== undefined && props[key] !== null) {
        return shortText(String(props[key]), 24);
      }
    }
    return nodeDisplayLabel(labels);
  }

  async function loadGraph(limit) {
    const nodeDocs = await arangoQuery(
      conn,
      "FOR n IN nodes " +
        "FILTER n.namespace == null OR n.namespace NOT IN @excludedNamespaces " +
        'FILTER NOT (n.kind == "namespace" AND n.name IN @excludedNamespaces) ' +
        "LIMIT @limit " +
        "RETURN n",
      { limit, excludedNamespaces: EXCLUDED_NAMESPACES }
    );

    nodes = [];
    nodeById = new Map();

    for (const doc of nodeDocs) {
      const id = doc._id;
      const labels = [labelFromKind(doc.kind)];
      const label = nodeDisplayLabel(labels);
      const properties = stripInternalFields(doc);
      const node = {
        id,
        labels,
        properties,
        color: colorForLabel(label),
        caption: nodeCaption(properties, labels),
        x: 0,
        y: 0,
      };
      nodes.push(node);
      nodeById.set(id, node);
    }

    edges = [];
    if (nodes.length > 0) {
      // No AQL-side join on the node set here (an edge collection has no
      // notion of "only edges between these _ids" without listing them all
      // as bind vars) - fetch a batch of edges and keep only those between
      // nodes we already loaded, same approach the Neo4j version used.
      const edgeDocs = await arangoQuery(
        conn,
        "FOR e IN edges LIMIT @limit RETURN e",
        { limit: limit * 4 }
      );
      for (const doc of edgeDocs) {
        const sourceId = doc._from;
        const targetId = doc._to;
        if (!nodeById.has(sourceId) || !nodeById.has(targetId)) continue;
        edges.push({
          id: doc._id,
          type: (doc.relation || "").toUpperCase(),
          properties: stripInternalFields(doc),
          source: sourceId,
          target: targetId,
        });
      }
    }

    renderLegend();
    statsEl.textContent = `${nodes.length} Knoten, ${edges.length} Kanten`;
  }

  function renderLegend() {
    const counts = new Map();
    for (const node of nodes) {
      const label = nodeDisplayLabel(node.labels);
      counts.set(label, (counts.get(label) || 0) + 1);
    }
    legendEl.innerHTML = "";
    for (const [label, color] of labelColor.entries()) {
      const item = document.createElement("div");
      item.className = "legend-item";
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      swatch.style.background = color;
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(`${label} (${counts.get(label) || 0})`));
      legendEl.appendChild(item);
    }
  }

  const MARGIN_X = 70, MARGIN_Y = 60;
  const GRAVITY = 0.02; // pulls every node gently toward the center

  // Static force-directed network layout (Fruchterman-Reingold): nodes repel
  // each other, edges pull connected nodes together. The simulation runs to
  // completion synchronously right here and is only drawn once afterwards -
  // there is no requestAnimationFrame loop, so nothing visibly moves.
  function layoutNetwork() {
    const n = nodes.length;
    if (n === 0) return;

    nodes.forEach((node, i) => {
      const angle = (2 * Math.PI * i) / n;
      const radius = 150 + (i % 3) * 40; // slight stagger so the simulation has room to unfold
      node.x = Math.cos(angle) * radius;
      node.y = Math.sin(angle) * radius;
    });

    const k = 100; // ideal distance between connected nodes
    const iterations = n > 200 ? 100 : 300;
    // An isolated/poorly-connected node feels repulsion from every other
    // node, so how far it could drift grows with n - cap it directly instead
    // of trying to tune gravity to balance out at every graph size.
    const maxRadius = 150 + k * Math.sqrt(n);

    for (let iter = 0; iter < iterations; iter++) {
      const temp = 60 * (1 - iter / iterations); // cools down -> settles instead of oscillating

      for (const node of nodes) { node.fx = 0; node.fy = 0; }

      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          const a = nodes[i], b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y;
          let distSq = dx * dx + dy * dy;
          if (distSq < 1) distSq = 1;
          const dist = Math.sqrt(distSq);
          const force = (k * k) / dist;
          const fx = (dx / dist) * force, fy = (dy / dist) * force;
          a.fx += fx; a.fy += fy;
          b.fx -= fx; b.fy -= fy;
        }
      }

      for (const e of edges) {
        const a = nodeById.get(e.source), b = nodeById.get(e.target);
        if (!a || !b) continue;
        let dx = a.x - b.x, dy = a.y - b.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = (dist * dist) / k;
        const fx = (dx / dist) * force, fy = (dy / dist) * force;
        a.fx -= fx; a.fy -= fy;
        b.fx += fx; b.fy += fy;
      }

      for (const node of nodes) {
        // mild pull toward the center - without it, poorly connected nodes
        // (few/no edges) only ever feel repulsion and drift out indefinitely
        node.fx -= node.x * GRAVITY;
        node.fy -= node.y * GRAVITY;

        const disp = Math.sqrt(node.fx * node.fx + node.fy * node.fy) || 1;
        const limited = Math.min(disp, temp);
        node.x += (node.fx / disp) * limited;
        node.y += (node.fy / disp) * limited;

        // hard clamp: never let a node end up further out than maxRadius
        const r = Math.sqrt(node.x * node.x + node.y * node.y);
        if (r > maxRadius) {
          const s = maxRadius / r;
          node.x *= s;
          node.y *= s;
        }
      }
    }

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const node of nodes) {
      minX = Math.min(minX, node.x);
      maxX = Math.max(maxX, node.x);
      minY = Math.min(minY, node.y);
      maxY = Math.max(maxY, node.y);
    }
    const naturalWidth = maxX - minX;
    const naturalHeight = maxY - minY;

    const viewport = measureViewport();
    const canvasWidth = Math.max(viewport.w, naturalWidth + 2 * MARGIN_X);
    const canvasHeight = Math.max(viewport.h, naturalHeight + 2 * MARGIN_Y);
    setCanvasSize(canvasWidth, canvasHeight);

    const offsetX = (canvasWidth - naturalWidth) / 2 - minX;
    const offsetY = (canvasHeight - naturalHeight) / 2 - minY;
    for (const node of nodes) {
      node.x += offsetX;
      node.y += offsetY;
    }
  }

  function measureViewport() {
    const rect = graphScroll.getBoundingClientRect();
    return { w: rect.width, h: rect.height };
  }

  function setCanvasSize(w, h) {
    width = w;
    height = h;
    const dpr = window.devicePixelRatio || 1;
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // Resizing only grows the canvas floor to the new viewport - it must NOT
  // re-run the physics simulation (that's O(n^2) * iterations and made every
  // resize event, e.g. while dragging the window edge, painfully slow).
  function onWindowResize() {
    if (graphPanel.classList.contains("hidden")) return;
    const viewport = measureViewport();
    setCanvasSize(Math.max(viewport.w, width), Math.max(viewport.h, height));
    draw();
  }

  function roundRect(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function draw() {
    const styles = getComputedStyle(document.documentElement);
    const textSecondary = styles.getPropertyValue("--text-secondary").trim() || "#52514e";
    const textPrimary = styles.getPropertyValue("--text-primary").trim() || "#0b0b0b";
    const gridline = styles.getPropertyValue("--gridline").trim() || "#e1e0d9";
    const surface = styles.getPropertyValue("--surface-2").trim() || "#f9f9f7";
    const accent = styles.getPropertyValue("--accent").trim() || "#2a78d6";

    ctx.clearRect(0, 0, width, height);

    // neighborhood of the hovered node, used to focus attention on hover
    const neighborIds = new Set();
    if (hoveredNode) {
      neighborIds.add(hoveredNode.id);
      for (const e of edges) {
        if (e.source === hoveredNode.id) neighborIds.add(e.target);
        if (e.target === hoveredNode.id) neighborIds.add(e.source);
      }
    }

    // edges
    for (const e of edges) {
      const a = nodeById.get(e.source);
      const b = nodeById.get(e.target);
      if (!a || !b) continue;
      const isFocused = hoveredNode && (e.source === hoveredNode.id || e.target === hoveredNode.id);
      const isDimmed = hoveredNode && !isFocused;

      ctx.globalAlpha = isDimmed ? 0.25 : 1;
      ctx.strokeStyle = isFocused ? accent : gridline;
      ctx.lineWidth = isFocused ? 2.5 : 1.5;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();

      // arrowhead near target
      const angle = Math.atan2(b.y - a.y, b.x - a.x);
      const tx = b.x - Math.cos(angle) * NODE_RADIUS;
      const ty = b.y - Math.sin(angle) * NODE_RADIUS;
      const ah = isFocused ? 7 : 6;
      ctx.fillStyle = isFocused ? accent : gridline;
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(tx - ah * Math.cos(angle - 0.4), ty - ah * Math.sin(angle - 0.4));
      ctx.lineTo(tx - ah * Math.cos(angle + 0.4), ty - ah * Math.sin(angle + 0.4));
      ctx.closePath();
      ctx.fill();

      // relationship type label at midpoint, on a background pill so it
      // stays legible where lines cross
      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      ctx.font = "10px system-ui, sans-serif";
      const textW = ctx.measureText(e.type).width;
      ctx.fillStyle = surface;
      roundRect(mx - textW / 2 - 4, my - 8, textW + 8, 16, 4);
      ctx.fill();
      ctx.fillStyle = isFocused ? accent : textSecondary;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(e.type, mx, my);
    }
    ctx.globalAlpha = 1;

    // nodes
    for (const node of nodes) {
      const isDimmed = hoveredNode && !neighborIds.has(node.id);
      const isHovered = node === hoveredNode;
      const r = isHovered ? NODE_RADIUS + 2 : NODE_RADIUS;

      ctx.globalAlpha = isDimmed ? 0.3 : 1;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
      ctx.fillStyle = node.color;
      ctx.fill();
      ctx.lineWidth = isHovered ? 2.5 : 1.5;
      ctx.strokeStyle = isHovered ? textPrimary : surface;
      ctx.stroke();

      ctx.globalAlpha = isDimmed ? 0.5 : 1;
      ctx.fillStyle = textPrimary;
      ctx.font = (isHovered ? "bold 11px" : "11px") + " system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillText(node.caption, node.x, node.y + r + 4);
    }
    ctx.globalAlpha = 1;
  }

  function nodeAt(px, py) {
    for (let i = nodes.length - 1; i >= 0; i--) {
      const node = nodes[i];
      const dx = px - node.x;
      const dy = py - node.y;
      if (dx * dx + dy * dy <= (NODE_RADIUS + 2) * (NODE_RADIUS + 2)) return node;
    }
    return null;
  }

  function canvasPoint(evt) {
    const rect = canvas.getBoundingClientRect();
    return { x: evt.clientX - rect.left, y: evt.clientY - rect.top };
  }

  function onMouseDown(evt) {
    const p = canvasPoint(evt);
    draggingNode = nodeAt(p.x, p.y);
    if (!draggingNode) {
      // clicked the empty background - pan the scroll container instead
      panStart = {
        mouseX: evt.clientX,
        mouseY: evt.clientY,
        scrollLeft: graphScroll.scrollLeft,
        scrollTop: graphScroll.scrollTop,
      };
    }
  }

  function onMouseMove(evt) {
    const p = canvasPoint(evt);
    if (draggingNode) {
      draggingNode.x = p.x;
      draggingNode.y = p.y;
      hideTooltip();
      draw();
      return;
    }
    if (panStart) {
      graphScroll.scrollLeft = panStart.scrollLeft - (evt.clientX - panStart.mouseX);
      graphScroll.scrollTop = panStart.scrollTop - (evt.clientY - panStart.mouseY);
      hideTooltip();
      return;
    }
    const node = nodeAt(p.x, p.y);
    if (node !== hoveredNode) {
      hoveredNode = node;
      draw();
    }
    if (node) {
      showTooltip(node, evt.clientX, evt.clientY);
      canvas.style.cursor = "pointer";
    } else {
      hideTooltip();
      canvas.style.cursor = "grab";
    }
  }

  function onMouseUp() {
    draggingNode = null;
    panStart = null;
  }

  function showTooltip(node, clientX, clientY) {
    const labelStr = node.labels.join(", ");
    const props = Object.entries(node.properties)
      .map(([k, v]) => `${k}: ${v}`)
      .join("\n");
    tooltip.textContent = `[${labelStr}]\n${props}`;
    tooltip.style.left = clientX + 14 + "px";
    tooltip.style.top = clientY + 14 + "px";
    tooltip.classList.remove("hidden");
  }

  function hideTooltip() {
    tooltip.classList.add("hidden");
  }
})();
