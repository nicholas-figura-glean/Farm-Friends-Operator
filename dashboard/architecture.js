/* Interactive architecture explorer.
 *
 * Structure and execution are intentionally separate lenses. The structure lens
 * renders module imports and answers "what breaks if I change this?". The runtime
 * lens renders module-level call paths aggregated from farm/topology.py and answers
 * "how does one cycle reach the game?". Both models are derived from source; neither
 * treats an import as observed control flow.
 *
 * Model, layout, relationship and HTML functions stay DOM-free so the same graph the
 * browser receives is exercised by dashboard/test_architecture.js in JavaScriptCore.
 */

var ARCH = null;
var ARCH_LOADING = false;
var ARCH_LAST_FETCH_MS = null;
var ARCH_SELECTED = null;
var ARCH_FILTER = "all";
var ARCH_HISTORY_OPEN = false;
var ARCH_VIEW = "runtime";
var ARCH_STEP = "all";
var ARCH_QUERY = "";
var ARCH_CAMERA = {scale: 1, x: 0, y: 0};
var ARCH_DRAG = null;
var ARCH_DID_PAN = false;
var AUTONOMY = null;
var AUTONOMY_LOADING = false;

function archEscape(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function archList(value) { return Array.isArray(value) ? value : []; }
function archOwn(object, key) { return Object.prototype.hasOwnProperty.call(object || {}, key); }
function archCopy(object) {
  var out = {};
  for (var key in (object || {})) if (archOwn(object, key)) out[key] = object[key];
  return out;
}
function archMap(list) {
  var out = {};
  archList(list).forEach(function (value) { out[String(value)] = true; });
  return out;
}
function archKeys(map) { return Object.keys(map || {}).filter(function (key) { return map[key]; }); }
function archTruncate(value, limit) {
  var text = String(value == null ? "" : value);
  return text.length > limit ? text.slice(0, Math.max(1, limit - 1)) + "…" : text;
}
function archUnique(list) {
  var seen = {}, out = [];
  archList(list).forEach(function (item) {
    var key = String(item);
    if (!seen[key]) { seen[key] = true; out.push(item); }
  });
  return out;
}
function archResetCamera() { ARCH_CAMERA = {scale: 1, x: 0, y: 0}; }

function archAge(timestamp) {
  var ms = Date.now() - Date.parse(String(timestamp || ""));
  if (!isFinite(ms) || ms < 0) return "";
  var minutes = Math.floor(ms / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return minutes + "m ago";
  var hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + "h ago";
  return Math.floor(hours / 24) + "d ago";
}

function archActivityEvents(autonomyView) {
  return archList((((autonomyView || {}).activity || {}).events));
}

function archLatest(items, predicate) {
  var list = archList(items);
  for (var i = 0; i < list.length; i++) if (!predicate || predicate(list[i] || {})) return list[i] || null;
  return null;
}

/* Architecture posture is deliberately narrower than overall farm health. A sleeping
 * scheduled service remains healthy architecture when it is loaded; topology drift,
 * unmapped code, path derivation failures, or an unloaded service are real defects. */
function archPosture(payload, current, autonomyView) {
  payload = payload || {};
  current = current || {};
  var stats = current.stats || {};
  var agents = archList(current.nodes).filter(function (node) { return node.kind === "agent"; });
  var knownAgents = agents.filter(function (node) { return !!node.agent_health; });
  var loadedAgents = knownAgents.filter(function (node) { return node.agent_health.loaded; }).length;
  var downAgents = agents.filter(function (node) { return !!node.down; }).length;
  var agentTotal = agents.length || Number(stats.launch_agents) || 0;
  var unmapped = archList(current.unmapped).length;
  var runtimeErrors = archList(current.runtime_errors).length;
  var drift = payload.live_matches_recorded === false;
  var vcs = (autonomyView || {}).vcs || {};
  var dirtySources = archList(vcs.dirty_source_paths).length;
  var canary = (autonomyView || {}).canary || {};
  var canaryStatus = String(canary.status || "").toLowerCase();
  var canaryBad = ["failed", "reverted", "unhealthy", "rollback"].indexOf(canaryStatus) !== -1;
  var canaryWatching = canaryStatus === "watching" || canaryStatus === "probation";
  var tone = drift || downAgents || unmapped || runtimeErrors || canaryBad ? "attention" :
    (dirtySources || canaryWatching ? "watch" : "good");
  var intervention = "No operator action required";
  if (drift) intervention = "Record the live topology before relying on change impact";
  else if (downAgents) intervention = "Restore " + downAgents + " unloaded service" + (downAgents === 1 ? "" : "s");
  else if (unmapped) intervention = "Classify " + unmapped + " unmapped module" + (unmapped === 1 ? "" : "s");
  else if (runtimeErrors) intervention = "Inspect " + runtimeErrors + " path derivation error" + (runtimeErrors === 1 ? "" : "s");
  else if (canaryBad) intervention = "Review the failed architecture canary outcome";
  else if (dirtySources) intervention = "Review " + dirtySources + " changed source file" + (dirtySources === 1 ? "" : "s") + " before release";
  else if (canaryWatching) intervention = "No action unless the active canary degrades";
  var version = Number(payload.versions) || 0;
  var liveTitle = drift ? "Live tree differs from recorded v" + version : "Live tree matches recorded v" + version;
  var generatedAge = archAge(current.generated_at);
  return {
    tone: tone,
    label: tone === "attention" ? "Architecture needs attention" :
      (tone === "watch" ? "Architecture coherent · review pending" : "Architecture coherent"),
    intervention: intervention,
    liveTitle: liveTitle,
    liveDetail: (generatedAge ? "Derived " + generatedAge : "Derived from the current source tree") +
      (current.short ? " · shape " + current.short : ""),
    version: version,
    drift: drift,
    unmapped: unmapped,
    runtimeErrors: runtimeErrors,
    dirtySources: dirtySources,
    canaryStatus: canaryStatus,
    agentTotal: agentTotal,
    loadedAgents: knownAgents.length ? loadedAgents : null,
    downAgents: downAgents,
    protectedCount: Number(stats.protected) || 0
  };
}

function archSituationHtml(payload, current, autonomyView, posture) {
  var structural = archLatest((payload || {}).events, function (event) {
    return !!event.structural || event.kind === "version";
  });
  var activity = archLatest(archActivityEvents(autonomyView), function (event) {
    return ["decide", "act", "verify"].indexOf(String(event.phase || "")) !== -1;
  });
  var changeBits = [];
  if (structural) {
    if (archList(structural.added).length) changeBits.push("+" + archList(structural.added).length + " components");
    if (archList(structural.removed).length) changeBits.push("−" + archList(structural.removed).length + " components");
    if (!archList(structural.added).length && !archList(structural.removed).length) {
      if (structural.detail) changeBits.push(structural.detail);
      changeBits.push("no component additions or removals");
    }
    if (archAge(structural.ts)) changeBits.push(archAge(structural.ts));
  }
  var activityBits = [];
  if (activity) {
    if (activity.actor) activityBits.push(activity.actor);
    if (activity.status) activityBits.push(activity.status);
    if (archAge(activity.ts)) activityBits.push(archAge(activity.ts));
  }
  function cell(kind, icon, label, title, detail) {
    return '<article class="arch-situation-cell ' + kind + '"><span class="arch-situation-icon" aria-hidden="true">' + icon +
      '</span><div><small>' + archEscape(label) + '</small><b>' + archEscape(archTruncate(title, 130)) +
      '</b><p>' + archEscape(archTruncate(detail, 180)) + '</p></div></article>';
  }
  return '<section class="arch-situation" aria-label="Architecture operating summary">' +
    cell(posture.drift ? "attention" : "good", "◎", "Happening now", posture.liveTitle, posture.liveDetail) +
    cell(structural ? "good" : "neutral", "↗", "What changed",
      structural ? (structural.title || "Structural version recorded") : "No structural version recorded",
      structural ? changeBits.join(" · ") : "The architecture ledger has no structural event yet") +
    cell(activity && activity.status === "failed" ? "attention" : "watch", "◇", "Autonomous action",
      activity ? (activity.title || "Autonomous decision recorded") : "No recent autonomous decision available",
      activity ? activityBits.join(" · ") : "Activity telemetry is unavailable") +
    cell(posture.tone, posture.tone === "attention" ? "!" : "✓", "Operator action", posture.intervention,
      posture.tone === "attention" ? "Intervention required" :
        (posture.tone === "watch" ? "Review before promotion; runtime may continue" : "No intervention required")) +
    '</section>';
}

/* Health is an overlay, not architecture. A missing liveness payload makes no claim. */
function archAgentHealth(autonomyView) {
  var health = {};
  var agents = (((autonomyView || {}).agents || {}).agents) || [];
  for (var i = 0; i < agents.length; i++) {
    var agent = agents[i] || {};
    health[String(agent.label)] = {
      loaded: !!agent.loaded,
      state: agent.state,
      role: String(agent.role || "")
    };
  }
  return health;
}

function archNodeClass(node) {
  var classes = ["arch-node"];
  if (node.kind === "agent") classes.push("agent");
  if (node.kind === "tool") classes.push("tool");
  if (node.protected) classes.push("protected");
  if (node.kind === "agent" && node.down) classes.push("down");
  return classes.join(" ");
}

/* Kept as a small pure primitive for degradation and truthfulness tests. */
function archNodeHtml(node) {
  var loc = node.loc ? (node.loc + " loc") : (node.kind === "tool" ? "MCP boundary" : "");
  return '<button class="' + archNodeClass(node) + '" role="button"' +
         ' aria-pressed="' + (ARCH_SELECTED === node.id ? "true" : "false") + '"' +
         ' data-arch-node="' + archEscape(node.id) + '">' +
         '<span>' + archEscape(node.id) + '</span>' +
         '<span class="nloc">' + archEscape(loc) + '</span></button>';
}

function archApplyHealth(current, autonomyView) {
  current = current || {};
  var out = archCopy(current);
  var health = archAgentHealth(autonomyView);
  var specs = archList(current.agents);
  out.nodes = archList(current.nodes).map(function (original) {
    var node = archCopy(original);
    if (node.kind !== "agent") return node;
    if (node.agent_label && health[String(node.agent_label)]) {
      node.agent_health = health[String(node.agent_label)];
      if (!node.agent_health.loaded) node.down = true;
      return node;
    }
    // Compatibility with historical snapshots, which represented only *_agent.py
    // source files and inferred their service from the entry string.
    for (var i = 0; i < specs.length; i++) {
      var spec = specs[i] || {};
      if (String(spec.entry || "").indexOf(String(node.id)) === -1) continue;
      var info = health[String(spec.label)];
      if (info) {
        node.agent_health = info;
        if (!info.loaded) node.down = true;
      }
    }
    return node;
  });
  return out;
}

function archRuntimeStep(current, name) {
  var steps = archList((current || {}).runtime_steps);
  for (var i = 0; i < steps.length; i++) if (String(steps[i].name) === String(name)) return steps[i];
  return null;
}

function archToolNode(name) {
  return {
    id: "tool:" + name,
    label: name,
    kind: "tool",
    layer: "world",
    path: "MCP / " + name,
    loc: null,
    protected: false,
    doc: "External game capability discovered from the captured MCP contract."
  };
}

/* Build one relationship lens. Runtime nodes are deliberately limited to modules
 * reachable from cycle steps; background agents return in the structure lens. */
function archGraphIndex(current, view, stepName) {
  current = current || {};
  view = view === "structure" ? "structure" : "runtime";
  var byId = {}, nodes = [], edges = [];
  archList(current.nodes).forEach(function (node) { byId[String(node.id)] = archCopy(node); });

  if (view === "runtime") {
    var wanted = {};
    var selectedStep = stepName !== "all" ? archRuntimeStep(current, stepName) : null;
    var steps = selectedStep ? [selectedStep] : archList(current.runtime_steps);
    steps.forEach(function (step) {
      archList(step.modules).forEach(function (name) { wanted[String(name)] = true; });
      archList(step.tools).forEach(function (name) { wanted["tool:" + String(name)] = true; });
    });
    wanted.cycle = true;
    if (steps.some(function (step) { return archList(step.tools).length > 0; })) wanted.mcp = true;

    var runtimeEdges = archList(current.runtime_edges).filter(function (edge) {
      if (selectedStep && archList(edge.steps).indexOf(String(selectedStep.name)) === -1) return false;
      return true;
    }).map(function (edge) {
      var copy = archCopy(edge);
      copy.kind = copy.kind || "call";
      wanted[String(copy.source)] = true;
      wanted[String(copy.target)] = true;
      return copy;
    });

    archKeys(wanted).forEach(function (id) {
      if (id.indexOf("tool:") === 0 && !byId[id]) byId[id] = archToolNode(id.slice(5));
      if (byId[id]) nodes.push(byId[id]);
    });
    edges = runtimeEdges.filter(function (edge) {
      return !!byId[String(edge.source)] && !!byId[String(edge.target)] &&
             !!wanted[String(edge.source)] && !!wanted[String(edge.target)];
    });
  } else {
    nodes = archList(current.nodes).map(archCopy);
    edges = archList(current.edges).map(function (edge) {
      var copy = archCopy(edge);
      copy.kind = "dependency";
      return copy;
    });
  }

  byId = {};
  var outgoing = {}, incoming = {};
  nodes.forEach(function (node) {
    byId[String(node.id)] = node;
    outgoing[String(node.id)] = [];
    incoming[String(node.id)] = [];
  });
  edges = edges.filter(function (edge) {
    return !!byId[String(edge.source)] && !!byId[String(edge.target)];
  });
  edges.forEach(function (edge) {
    outgoing[String(edge.source)].push(String(edge.target));
    incoming[String(edge.target)].push(String(edge.source));
  });
  Object.keys(outgoing).forEach(function (id) {
    outgoing[id] = archUnique(outgoing[id]).sort();
    incoming[id] = archUnique(incoming[id]).sort();
  });
  return {view: view, nodes: nodes, edges: edges, byId: byId, outgoing: outgoing, incoming: incoming};
}

function archTraverse(index, start, direction) {
  var links = direction === "incoming" ? index.incoming : index.outgoing;
  var seen = {}, queue = (links[start] || []).slice();
  while (queue.length) {
    var id = queue.shift();
    if (seen[id] || id === start) continue;
    seen[id] = true;
    (links[id] || []).forEach(function (next) { if (!seen[next]) queue.push(next); });
  }
  return seen;
}

function archNodeMatches(node, query) {
  query = String(query || "").toLowerCase().trim();
  if (!query) return false;
  var haystack = [node.id, node.label, node.path, node.doc, node.layer, node.kind].join(" ").toLowerCase();
  return haystack.indexOf(query) !== -1;
}

/* Stable, DOM-free layered layout. Rows preserve architectural intent; the graph is
 * not a force layout, so a refresh never makes the operator relearn the map. */
function archGraphModel(current, view, stepName, selected, query) {
  var index = archGraphIndex(current, view, stepName);
  var width = 1240, nodeW = 154, nodeH = 52, colGap = 15, rowGap = 14;
  var layerSpecs = archList((current || {}).layers).slice();
  var knownLayers = archMap(layerSpecs.map(function (layer) { return layer.id; }));
  index.nodes.forEach(function (node) {
    if (!knownLayers[String(node.layer)]) {
      layerSpecs.push({id: String(node.layer || "other"), name: String(node.layer || "Other"), note: ""});
      knownLayers[String(node.layer || "other")] = true;
    }
  });

  var positions = {}, layers = [], y = 14;
  layerSpecs.forEach(function (spec) {
    var members = index.nodes.filter(function (node) { return String(node.layer) === String(spec.id); });
    if (!members.length) return;
    members.sort(function (a, b) { return String(a.id).localeCompare(String(b.id)); });
    var columns = Math.min(7, Math.max(1, members.length));
    var rows = Math.ceil(members.length / columns);
    var layerHeight = 47 + rows * nodeH + Math.max(0, rows - 1) * rowGap + 15;
    layers.push({id: spec.id, name: spec.name, note: spec.note, x: 10, y: y,
                 width: width - 20, height: layerHeight, count: members.length});
    for (var row = 0; row < rows; row++) {
      var rowMembers = members.slice(row * columns, (row + 1) * columns);
      var rowWidth = rowMembers.length * nodeW + Math.max(0, rowMembers.length - 1) * colGap;
      var startX = (width - rowWidth) / 2;
      for (var col = 0; col < rowMembers.length; col++) {
        positions[String(rowMembers[col].id)] = {
          x: startX + col * (nodeW + colGap), y: y + 43 + row * (nodeH + rowGap),
          width: nodeW, height: nodeH, row: row
        };
      }
    }
    y += layerHeight + 12;
  });

  var queryMatches = {}, queryNeighbors = {};
  if (String(query || "").trim()) {
    index.nodes.forEach(function (node) {
      if (archNodeMatches(node, query)) queryMatches[String(node.id)] = true;
    });
    archKeys(queryMatches).forEach(function (id) {
      (index.outgoing[id] || []).concat(index.incoming[id] || []).forEach(function (neighbor) {
        queryNeighbors[neighbor] = true;
      });
    });
  }

  index.width = width;
  index.height = Math.max(250, y + 2);
  index.positions = positions;
  index.layers = layers;
  index.selected = index.byId[String(selected)] ? String(selected) : null;
  index.directOut = index.selected ? archMap(index.outgoing[index.selected]) : {};
  index.directIn = index.selected ? archMap(index.incoming[index.selected]) : {};
  index.impact = index.selected ? archTraverse(index, index.selected, "incoming") : {};
  index.dependencies = index.selected ? archTraverse(index, index.selected, "outgoing") : {};
  index.queryMatches = queryMatches;
  index.queryNeighbors = queryNeighbors;
  index.query = String(query || "").trim();
  return index;
}

function archNodeStateClass(model, id) {
  var classes = [];
  if (model.selected) {
    if (id === model.selected) classes.push("selected");
    else if (model.directOut[id]) classes.push("related-out");
    else if (model.directIn[id]) classes.push("related-in");
    else if (model.impact[id]) classes.push("impact");
    else classes.push("dim");
  } else if (model.query) {
    if (model.queryMatches[id]) classes.push("search-match");
    else if (model.queryNeighbors[id]) classes.push("search-neighbor");
    else classes.push("dim");
  }
  return classes.join(" ");
}

function archEdgeStateClass(model, edge) {
  var source = String(edge.source), target = String(edge.target);
  if (model.selected) {
    if (source === model.selected) return "selected-out";
    if (target === model.selected) return "selected-in";
    if ((model.impact[source] || source === model.selected) &&
        (model.impact[target] || target === model.selected)) return "impact";
    if ((model.dependencies[source] || source === model.selected) &&
        (model.dependencies[target] || target === model.selected)) return "dependency-path";
    return "dim";
  }
  if (model.query) {
    if (model.queryMatches[source] || model.queryMatches[target]) return "search";
    if (model.queryNeighbors[source] && model.queryNeighbors[target]) return "search";
    return "dim";
  }
  return "normal";
}

function archEdgePath(source, target, edgeIndex) {
  var sx, sy, tx, ty;
  var sourceCenter = source.x + source.width / 2;
  var targetCenter = target.x + target.width / 2;
  if (Math.abs((source.y + source.height / 2) - (target.y + target.height / 2)) < 8) {
    var rightward = targetCenter >= sourceCenter;
    sx = rightward ? source.x + source.width : source.x;
    tx = rightward ? target.x : target.x + target.width;
    sy = source.y + source.height / 2;
    ty = target.y + target.height / 2;
    var lift = 20 + (edgeIndex % 4) * 9;
    return "M" + sx + "," + sy + " C" + ((sx + tx) / 2) + "," + (sy - lift) +
           " " + ((sx + tx) / 2) + "," + (ty - lift) + " " + tx + "," + ty;
  }
  var downward = target.y > source.y;
  sx = sourceCenter;
  tx = targetCenter;
  sy = downward ? source.y + source.height : source.y;
  ty = downward ? target.y : target.y + target.height;
  var midY = (sy + ty) / 2;
  var bend = ((edgeIndex % 5) - 2) * 7;
  return "M" + sx + "," + sy + " C" + (sx + bend) + "," + midY +
         " " + (tx - bend) + "," + midY + " " + tx + "," + ty;
}

function archCameraViewBox(model) {
  var scale = Math.max(1, Math.min(2.5, Number(ARCH_CAMERA.scale) || 1));
  ARCH_CAMERA.scale = scale;
  var width = model.width / scale, height = model.height / scale;
  var maxX = Math.max(0, model.width - width), maxY = Math.max(0, model.height - height);
  ARCH_CAMERA.x = Math.max(0, Math.min(maxX, Number(ARCH_CAMERA.x) || 0));
  ARCH_CAMERA.y = Math.max(0, Math.min(maxY, Number(ARCH_CAMERA.y) || 0));
  return [ARCH_CAMERA.x, ARCH_CAMERA.y, width, height].join(" ");
}

function archGraphHtml(model) {
  var defs = '<defs>' +
    '<marker id="arch-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>' +
    '<marker id="arch-arrow-out" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>' +
    '<marker id="arch-arrow-in" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"></path></marker>' +
    '</defs>';
  var layerHtml = model.layers.map(function (layer) {
    return '<g class="arch-layer" data-layer="' + archEscape(layer.id) + '">' +
      '<rect x="' + layer.x + '" y="' + layer.y + '" width="' + layer.width +
      '" height="' + layer.height + '" rx="15"></rect>' +
      '<text class="arch-layer-title" x="26" y="' + (layer.y + 25) + '">' +
      archEscape(layer.name) + '</text><text class="arch-layer-note" x="' + (model.width - 26) +
      '" y="' + (layer.y + 25) + '" text-anchor="end">' + archEscape(layer.note || "") +
      '</text></g>';
  }).join("");

  var edgeHtml = model.edges.map(function (edge, index) {
    var source = model.positions[String(edge.source)], target = model.positions[String(edge.target)];
    if (!source || !target) return "";
    var state = archEdgeStateClass(model, edge);
    var marker = state === "selected-out" ? "arch-arrow-out" :
                 (state === "selected-in" ? "arch-arrow-in" : "arch-arrow");
    var verb = model.view === "runtime" ? (edge.kind === "tool" ? "crosses boundary to" : "calls") : "uses";
    return '<path class="arch-edge ' + state + '" d="' + archEdgePath(source, target, index) +
      '" marker-end="url(#' + marker + ')"><title>' + archEscape(edge.source + " " + verb + " " + edge.target) +
      '</title></path>';
  }).join("");

  var nodeHtml = model.nodes.map(function (node) {
    var pos = model.positions[String(node.id)];
    if (!pos) return "";
    var state = archNodeStateClass(model, String(node.id));
    var classes = archNodeClass(node) + (state ? " " + state : "");
    var label = node.label || node.id;
    var meta = node.kind === "tool" ? "external MCP tool" :
               (node.kind === "agent" ? (node.entry ? "service · " + node.entry : "background agent") : ((node.loc || "?") + " lines"));
    var status = node.down ? '<circle class="arch-node-health down" cx="' + (pos.width - 12) + '" cy="12" r="4"></circle>' :
                 (node.kind === "agent" && node.agent_health ? '<circle class="arch-node-health live" cx="' + (pos.width - 12) + '" cy="12" r="4"></circle>' : "");
    var lock = node.protected ? '<text class="arch-node-lock" x="' + (pos.width - 10) + '" y="' + (pos.height - 9) + '" text-anchor="end">LOCKED</text>' : "";
    return '<g class="' + classes + '" transform="translate(' + pos.x + ' ' + pos.y + ')"' +
      ' role="button" tabindex="0" aria-label="Inspect ' + archEscape(label) + '" data-arch-node="' +
      archEscape(node.id) + '"><rect width="' + pos.width + '" height="' + pos.height + '" rx="10"></rect>' +
      '<text class="arch-node-title" x="12" y="21">' + archEscape(archTruncate(label, 21)) + '</text>' +
      '<text class="arch-node-meta" x="12" y="39">' + archEscape(meta) + '</text>' + status + lock +
      '<title>' + archEscape(label + (node.path ? " · " + node.path : "")) + '</title></g>';
  }).join("");

  return '<div class="arch-map-stage" data-arch-map="true" style="--arch-ratio:' +
    (model.width / model.height).toFixed(4) + '">' +
    '<svg class="arch-map-svg" id="arch-map-svg" viewBox="' + archCameraViewBox(model) +
    '" data-arch-width="' + model.width + '" data-arch-height="' + model.height +
    '" role="img" aria-label="Interactive ' + archEscape(model.view) + ' architecture graph">' + defs +
    '<rect class="arch-map-bg" data-arch-pan="true" width="' + model.width + '" height="' + model.height + '"></rect>' +
    layerHtml + '<g class="arch-edges">' + edgeHtml + '</g><g class="arch-nodes-svg">' + nodeHtml +
    '</g></svg></div>';
}

/* Legacy layered HTML remains pure and useful for compact fallback/testing. */
function archLayersHtml(current) {
  current = current || {};
  var layers = archList(current.layers), nodes = archList(current.nodes), out = [];
  layers.forEach(function (layer) {
    var members = nodes.filter(function (node) { return node.layer === layer.id; });
    if (layer.id === "world") {
      var toolHtml = archList(current.tools).map(function (tool) {
        return '<span class="arch-node tool">' + archEscape(tool) + '</span>';
      }).join("");
      out.push('<div class="arch-layer" data-layer="world"><header><h3>' + archEscape(layer.name) +
        '</h3><span class="lnote">' + archEscape(layer.note) + '</span></header><div class="arch-nodes">' +
        (toolHtml || '<span class="arch-note">no captured contract</span>') + '</div></div>');
    } else if (members.length) {
      out.push('<div class="arch-layer" data-layer="' + archEscape(layer.id) + '"><header><h3>' +
        archEscape(layer.name) + '</h3><span class="lnote">' + archEscape(layer.note) +
        '</span></header><div class="arch-nodes">' + members.map(archNodeHtml).join("") + '</div></div>');
    }
  });
  return out.join("");
}

function archRelationButtons(ids, model) {
  if (!ids || !ids.length) return '<span class="arch-note">none</span>';
  return ids.map(function (id) {
    var node = model.byId[id] || {id: id, label: id};
    return '<button class="arch-dep" data-arch-node="' + archEscape(id) + '">' +
      archEscape(node.label || node.id) + '</button>';
  }).join("");
}

function archLayerName(current, id) {
  var layers = archList((current || {}).layers);
  for (var i = 0; i < layers.length; i++) if (String(layers[i].id) === String(id)) return layers[i].name;
  return id || "Unclassified";
}

function archStepsForNode(current, node) {
  var out = [];
  archList((current || {}).runtime_steps).forEach(function (step) {
    var inModules = archList(step.modules).indexOf(String(node.id)) !== -1;
    var toolName = String(node.id).indexOf("tool:") === 0 ? String(node.id).slice(5) : null;
    var inTools = toolName && archList(step.tools).indexOf(toolName) !== -1;
    if (inModules || inTools || (node.id === "cycle" && step.name)) out.push(String(step.name));
  });
  return out;
}

function archDetailHtml(current, id, model) {
  current = current || {};
  model = model || archGraphModel(current, "structure", "all", id, "");
  var node = model.byId[String(id)];
  if (!node) {
    var starts = model.view === "runtime" ? ["cycle", "mcp", "tool:list_farm"] : ["cycle", "author_agent", "canary"];
    var startButtons = starts.filter(function (start) { return !!model.byId[start]; }).map(function (start) {
      var item = model.byId[start];
      return '<button data-arch-node="' + archEscape(start) + '"><b>' + archEscape(item.label || item.id) +
        '</b><span>' + (model.view === "runtime" ? "follow its call path" : "inspect its dependencies") + '</span></button>';
    }).join("");
    return '<div class="arch-detail-empty"><span class="arch-kicker">Explore the live model</span>' +
      '<h3>Select any component or relationship</h3><p>' +
      (model.view === "runtime" ?
        'Choose a stage or component to follow the source-derived route to the game boundary.' :
        'Select a component to expose direct dependencies, reverse dependencies, and change radius.') +
      '</p><div class="arch-starts">' + startButtons + '</div></div>';
  }

  var outgoing = model.outgoing[String(node.id)] || [];
  var incoming = model.incoming[String(node.id)] || [];
  var impact = archKeys(archTraverse(model, String(node.id), "incoming"));
  var label = node.label || node.id;
  var health = "";
  if (node.kind === "agent") {
    if (node.down) health = '<span class="arch-badge bad">agent down</span>';
    else if (node.agent_health) health = '<span class="arch-badge good">agent ' +
      archEscape(node.agent_health.state || "loaded") + '</span>';
    else health = '<span class="arch-badge">health unknown</span>';
  }
  var locked = node.protected ? '<span class="arch-badge lock">protected</span>' : "";
  var type = '<span class="arch-badge">' + archEscape(node.kind || "module") + '</span>';
  var relationOut = model.view === "runtime" ? "Calls next" : "Uses / imports";
  var relationIn = model.view === "runtime" ? "Called by" : "Used by directly";
  var steps = model.view === "runtime" ? archStepsForNode(current, node) : [];
  var stepHtml = steps.length ? '<section><h5>Appears in run stages</h5><div class="arch-step-tags">' +
    steps.map(function (step) { return '<button data-arch-step="' + archEscape(step) + '">' + archEscape(step) + '</button>'; }).join("") +
    '</div></section>' : "";
  var impactHtml = model.view === "structure" ? '<div class="arch-impact"><b>' + impact.length +
    '</b><span>component' + (impact.length === 1 ? "" : "s") +
    ' in the transitive change radius</span></div>' : "";
  var editable = node.kind === "tool" ? "outside this repository" :
    (node.protected ? "agents may not edit this file" : "author agent may patch this file");
  var postureTitle = node.kind === "tool" ? "External boundary" :
    (node.down ? "Service unavailable" : (node.protected ? "Protected control surface" : "Autonomous edits permitted"));
  var postureTone = node.down ? "attention" : (node.protected || node.kind === "tool" ? "watch" : "good");
  function relation(title, ids) {
    return '<details class="arch-relation"><summary><span>' + archEscape(title) + '</span><b>' + ids.length +
      '</b></summary><div>' + archRelationButtons(ids, model) + '</div></details>';
  }

  return '<div class="arch-detail-head"><span class="arch-kicker">' + archEscape(archLayerName(current, node.layer)) +
    '</span><h3>' + archEscape(label) + '</h3><div class="arch-badges">' + type + locked + health + '</div></div>' +
    '<div class="arch-component-posture ' + postureTone + '"><small>Change posture</small><b>' + postureTitle +
    '</b><span>' + editable + '</span></div>' +
    '<div class="dpath">' + archEscape(node.path || "derived component") + '</div>' +
    (node.doc ? '<p class="ddoc">' + archEscape(node.doc) + '</p>' : "") + impactHtml +
    '<dl><dt>size</dt><dd>' + (node.loc ? archEscape(node.loc) + " lines" : "not local code") +
    '</dd><dt>relationship lens</dt><dd>' + (model.view === "runtime" ? "source-derived calls" : "parsed local dependencies") + '</dd></dl>' +
    '<div class="arch-relations">' + relation(relationOut, outgoing) + relation(relationIn, incoming) + '</div>' + stepHtml;
}

function archEventHtml(event) {
  event = event || {};
  var when = String(event.ts || "").slice(5, 16).replace("T", " ");
  var changes = "";
  if (event.kind === "version") {
    var bits = [];
    if (archList(event.added).length) bits.push("<b>+" + archList(event.added).map(archEscape).join(", +") + "</b>");
    if (archList(event.removed).length) bits.push("<i>-" + archList(event.removed).map(archEscape).join(", -") + "</i>");
    if (archList(event.agents_added).length) bits.push("<b>+" + archList(event.agents_added).length + " agent(s)</b>");
    if (bits.length) changes = '<div class="chg">' + bits.join(" &nbsp; ") + '</div>';
  }
  var mark = event.ok === false ? " ✗" : (event.ok === true ? " ✓" : "");
  return '<div class="arch-ev" data-kind="' + archEscape(event.kind) + '"><span class="when">' +
    archEscape(when) + '</span><span class="what"><span class="title"><span class="kind">' +
    archEscape(event.kind) + '</span>' + archEscape(event.title) + archEscape(mark) + '</span>' +
    (event.detail ? '<div class="arch-ev-detail">' + archEscape(event.detail) + '</div>' : "") + changes + '</span></div>';
}

function archEventsHtml(events, filter) {
  var rows = archList(events).filter(function (event) {
    if (filter === "all") return true;
    if (filter === "structural") return !!event.structural;
    return event.kind === filter;
  });
  return rows.length ? rows.map(archEventHtml).join("") : '<p class="arch-note">No events recorded yet.</p>';
}

function archApplyHistoryFilter(host, events, filter) {
  if (!host || !host.querySelector || !host.querySelectorAll) return false;
  var timeline = host.querySelector("#arch-timeline");
  var buttons = host.querySelectorAll("[data-arch-filter]");
  if (!timeline || !buttons) return false;
  timeline.innerHTML = archEventsHtml(events, filter);
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].setAttribute("aria-pressed", String(buttons[i].getAttribute("data-arch-filter") === filter));
  }
  return true;
}

function archStatsHtml(current, posture) {
  var stats = (current || {}).stats || {};
  posture = posture || archPosture({}, current || {}, null);
  var services = posture.loadedAgents == null ?
    (posture.agentTotal ? posture.agentTotal + " defined" : "-") :
    posture.loadedAgents + "/" + posture.agentTotal + " loaded";
  var cells = [
    ["recorded shape", posture.version ? "v" + posture.version : "-", posture.drift ? "bad" : "good"],
    ["modules", stats.modules, ""],
    ["services", services, posture.downAgents ? "bad" : "good"],
    ["dependencies", stats.edges, ""],
    ["boundary tools", stats.tools, ""],
    ["protected", stats.protected, "watch"]
  ];
  return cells.map(function (cell) {
    var shown = typeof cell[1] === "number" ? cell[1].toLocaleString() : String(cell[1] == null ? "-" : cell[1]);
    return '<span class="delta ' + cell[2] + '"><span>' + archEscape(cell[0]) + '</span><b>' +
      archEscape(shown) + '</b></span>';
  }).join("");
}

function archStepRailHtml(current) {
  var steps = archList((current || {}).runtime_steps);
  var all = '<button data-arch-step="all" aria-pressed="' + (ARCH_STEP === "all") + '"><b>Whole run</b><span>' +
    steps.length + ' stages</span></button>';
  return all + steps.map(function (step, index) {
    return '<button data-arch-step="' + archEscape(step.name) + '" aria-pressed="' +
      (ARCH_STEP === String(step.name)) + '"><small>' + ((index + 1) < 10 ? "0" : "") + (index + 1) +
      '</small><b>' + archEscape(step.name) + '</b><span>' + archList(step.modules).length + ' modules · ' +
      archList(step.tools).length + ' tools</span></button>';
  }).join("");
}

function archToolbarHtml(current, model) {
  var relation = model.view === "runtime" ?
    '<b>Calls</b> are source-derived paths, never invented timings.' :
    '<b>Uses</b> means a parsed local dependency, never observed execution.';
  return '<div class="arch-toolbar"><div class="arch-view-switch" role="group" aria-label="Architecture lens">' +
    '<button data-arch-view="runtime" aria-pressed="' + (model.view === "runtime") + '"><span>▶</span><b>Runtime flow</b><small>How it runs</small></button>' +
    '<button data-arch-view="structure" aria-pressed="' + (model.view === "structure") + '"><span>◇</span><b>Change impact</b><small>System map</small></button></div>' +
    '<label class="arch-search"><span>Find a component</span><input type="search" data-arch-search value="' +
      archEscape(ARCH_QUERY) + '" placeholder="module, path, layer…" autocomplete="off"></label>' +
    '<div class="arch-map-controls" role="group" aria-label="Map zoom"><button data-arch-zoom="out" aria-label="Zoom out">−</button>' +
      '<span id="arch-zoom-label">' + Math.round(ARCH_CAMERA.scale * 100) + '%</span>' +
      '<button data-arch-zoom="in" aria-label="Zoom in">+</button><button data-arch-zoom="fit">Fit</button></div>' +
    '<div class="arch-relation-note"><span>' + relation + '</span><strong>' + model.nodes.length +
      ' components · ' + model.edges.length + ' relationships</strong></div></div>' +
    (model.view === "runtime" ? '<div class="arch-stage-browser"><div class="arch-stage-label"><b>Follow one run stage</b><span>Whole run or one source-derived slice</span></div><div class="arch-step-rail" aria-label="Run stages">' +
      archStepRailHtml(current) + '</div></div>' : "");
}

function renderArchitecture(payload, autonomyView) {
  var host = document.getElementById("tab-architecture");
  if (!host) return;
  if (!payload || payload.error) {
    host.innerHTML = '<div class="arch-shell"><section class="page-hero arch-hero"><div><span class="page-kicker">Architecture telemetry unavailable</span>' +
      '<h2>Architecture control plane</h2><p>The source-derived model could not be built. The rest of the dashboard remains read-only and available.</p></div>' +
      '<div class="hero-verdict attention"><b>Model unavailable</b><span>' +
      archEscape((payload || {}).error || "No architecture data returned") + '</span></div></section></div>';
    return;
  }

  var current = archApplyHealth(payload.current || {}, autonomyView);
  var model = archGraphModel(current, ARCH_VIEW, ARCH_STEP, ARCH_SELECTED, ARCH_QUERY);
  var selected = model.selected;
  var posture = archPosture(payload, current, autonomyView);
  var drift = posture.drift ? '<div class="arch-warn attention">The live tree’s shape does not match the newest recorded version. Change-impact claims remain provisional until the next architecture scan records it.</div>' : "";
  var unmapped = posture.unmapped ? '<div class="arch-warn attention">Unclassified modules: ' +
    archEscape(archList(current.unmapped).join(", ")) + '. They remain visible in Observation &amp; evidence until classified.</div>' : "";
  var runtimeWarning = model.view === "runtime" && posture.runtimeErrors ?
    '<div class="arch-warn attention">Some runtime paths could not be derived: ' + archEscape(archList(current.runtime_errors).join("; ")) + '</div>' : "";
  var filters = ["all", "structural", "release", "canary", "order", "finding"];
  var filterHtml = filters.map(function (name) {
    return '<button data-arch-filter="' + name + '" aria-pressed="' + (ARCH_FILTER === name) + '">' + name + '</button>';
  }).join("");
  var step = model.view === "runtime" && ARCH_STEP !== "all" ? archRuntimeStep(current, ARCH_STEP) : null;
  var focusedNode = selected ? model.byId[selected] : null;
  var contextTitle = focusedNode ? (focusedNode.label || focusedNode.id) :
    (step ? step.name : (model.view === "runtime" ? "Whole run" : "Whole system"));
  var contextDetail = step ? archList(step.modules).length + ' reachable modules · ' + archList(step.tools).length + ' external tools' :
    model.nodes.length + ' visible components · ' + model.edges.length + ' directional relationships';
  var contextAction = selected ? '<button data-arch-node="' + archEscape(selected) + '">Clear focus</button>' :
    (step ? '<button data-arch-step="all">Show whole run</button>' : "");
  var context = '<div class="arch-map-context"><span>' +
    (step ? "Run stage" : (model.view === "runtime" ? "Runtime flow" : "Change impact")) + '</span><b>' +
    archEscape(contextTitle) + '</b><small>' + archEscape(contextDetail) + '</small>' + contextAction + '</div>';
  var outgoingLabel = model.view === "runtime" ? "selected calls" : "selected uses";
  var incomingLabel = model.view === "runtime" ? "calls selected" : "depends on selected";
  var latestEvent = archLatest(payload.events);
  var historyNote = archList(payload.events).length + ' recorded events';
  if (latestEvent && archAge(latestEvent.ts)) historyNote += ' · latest ' + archAge(latestEvent.ts);
  var revision = archEscape(current.branch || "?") + '@' + archEscape(current.commit || "?");

  host.innerHTML = '<div class="arch-shell">' +
    '<section class="page-hero arch-hero"><div><span class="page-kicker">Source-derived control plane</span>' +
      '<h2>Architecture control plane</h2><p>See the current shape, what changed, how autonomous changes are governed, and where operator review is required. Current source <code>' +
      revision + '</code>.</p><div class="delta-row">' + archStatsHtml(current, posture) + '</div></div>' +
      '<div class="hero-verdict ' + posture.tone + '"><b>' + archEscape(posture.label) + '</b><span>' +
      archEscape(posture.intervention) + '</span></div></section>' +
    archSituationHtml(payload, current, autonomyView, posture) + drift + unmapped + runtimeWarning +
    archToolbarHtml(current, model) +
    '<div class="arch-workspace"><section class="arch-map-card" aria-label="Interactive architecture map">' + context +
      '<div class="arch-map-legend"><span><i class="module"></i>module</span><span><i class="agent"></i>service</span>' +
      '<span><i class="tool"></i>external tool</span><span><i class="locked"></i>protected</span>' +
      '<span><i class="incoming"></i>' + incomingLabel + '</span><span><i class="outgoing"></i>' + outgoingLabel + '</span></div>' +
      archGraphHtml(model) + '<div class="arch-map-help">Scroll to zoom · drag to pan · select a component to inspect its route and change posture</div></section>' +
      '<aside class="card arch-detail" id="arch-detail" aria-label="Component inspector" aria-live="polite">' +
      archDetailHtml(current, selected, model) + '</aside></div>' +
    '<details class="card arch-history audit-drawer"' + (ARCH_HISTORY_OPEN ? ' open' : '') + '><summary><span><b>Architecture audit trail</b><small>' +
      archEscape(historyNote) + ' · structural versions, releases, canaries, findings and work orders</small></span><em>' +
      archEscape(payload.versions || 0) + ' structural versions</em></summary><div class="drawer-body arch-history-body"><div class="arch-filter">' +
      filterHtml + '</div><div class="arch-timeline" id="arch-timeline">' + archEventsHtml(payload.events, ARCH_FILTER) +
      '</div></div></details></div>';

  function rerender() { renderArchitecture(payload, autonomyView); }
  if (host.querySelector) {
    var historyDrawer = host.querySelector(".arch-history");
    if (historyDrawer) historyDrawer.ontoggle = function () { ARCH_HISTORY_OPEN = !!historyDrawer.open; };
  }
  host.onclick = function (event) {
    var target = archClosestAttr(event.target, host);
    if (!target) return;
    var nodeId = target.getAttribute("data-arch-node");
    if (nodeId) { ARCH_SELECTED = ARCH_SELECTED === nodeId ? null : nodeId; rerender(); return; }
    var view = target.getAttribute("data-arch-view");
    if (view) { ARCH_VIEW = view === "structure" ? "structure" : "runtime"; archResetCamera(); rerender(); return; }
    var stepName = target.getAttribute("data-arch-step");
    if (stepName) { ARCH_VIEW = "runtime"; ARCH_STEP = stepName; ARCH_SELECTED = null; archResetCamera(); rerender(); return; }
    var zoom = target.getAttribute("data-arch-zoom");
    if (zoom) {
      if (zoom === "fit") archResetCamera();
      else ARCH_CAMERA.scale = Math.max(1, Math.min(2.5, ARCH_CAMERA.scale + (zoom === "in" ? 0.25 : -0.25)));
      rerender(); return;
    }
    var filter = target.getAttribute("data-arch-filter");
    if (filter) {
      ARCH_FILTER = filter;
      ARCH_HISTORY_OPEN = true;
      if (!archApplyHistoryFilter(host, payload.events, filter)) rerender();
      return;
    }
    if (target.getAttribute("data-arch-pan") != null) {
      if (ARCH_DID_PAN) { ARCH_DID_PAN = false; return; }
      ARCH_SELECTED = null; rerender();
    }
  };
  host.oninput = function (event) {
    var target = event.target;
    if (!target || !target.getAttribute || target.getAttribute("data-arch-search") == null) return;
    ARCH_QUERY = String(target.value || "");
    if (ARCH_QUERY.trim()) ARCH_SELECTED = null;
    rerender();
    if (host.querySelector) {
      var input = host.querySelector("[data-arch-search]");
      if (input && input.focus) {
        input.focus();
        if (input.setSelectionRange) input.setSelectionRange(ARCH_QUERY.length, ARCH_QUERY.length);
      }
    }
  };
  host.onkeydown = function (event) {
    var target = event.target;
    if (!target || !target.getAttribute) return;
    if (target.getAttribute("data-arch-search") != null && event.key === "Escape") {
      ARCH_QUERY = ""; rerender(); return;
    }
    var nodeId = target.getAttribute("data-arch-node");
    if (nodeId && (event.key === "Enter" || event.key === " ")) {
      if (event.preventDefault) event.preventDefault();
      ARCH_SELECTED = ARCH_SELECTED === nodeId ? null : nodeId; rerender();
    }
  };
  host.onpointerdown = function (event) {
    var target = archClosestAttr(event.target, host);
    if (!target || target.getAttribute("data-arch-pan") == null || ARCH_CAMERA.scale <= 1) return;
    ARCH_DRAG = {clientX: event.clientX, clientY: event.clientY, x: ARCH_CAMERA.x, y: ARCH_CAMERA.y, moved: false};
    if (target.setPointerCapture && event.pointerId != null) target.setPointerCapture(event.pointerId);
    host.className += " arch-panning";
  };
  host.onpointermove = function (event) {
    if (!ARCH_DRAG) return;
    var svg = host.querySelector && host.querySelector("#arch-map-svg");
    if (!svg) return;
    var dx = Number(event.clientX) - ARCH_DRAG.clientX, dy = Number(event.clientY) - ARCH_DRAG.clientY;
    if (Math.abs(dx) + Math.abs(dy) > 3) { ARCH_DRAG.moved = true; ARCH_DID_PAN = true; }
    var clientW = Number(svg.clientWidth) || model.width, clientH = Number(svg.clientHeight) || model.height;
    ARCH_CAMERA.x = ARCH_DRAG.x - dx * (model.width / ARCH_CAMERA.scale) / clientW;
    ARCH_CAMERA.y = ARCH_DRAG.y - dy * (model.height / ARCH_CAMERA.scale) / clientH;
    svg.setAttribute("viewBox", archCameraViewBox(model));
  };
  host.onpointerup = host.onpointercancel = function () {
    ARCH_DRAG = null;
    host.className = String(host.className || "").replace(/\s*arch-panning/g, "");
  };
  host.onwheel = function (event) {
    var target = archFindAttr(event.target, host, "data-arch-map");
    if (!target) return;
    if (event.preventDefault) event.preventDefault();
    var oldScale = ARCH_CAMERA.scale;
    var next = Math.max(1, Math.min(2.5, oldScale + (event.deltaY < 0 ? 0.15 : -0.15)));
    if (next === oldScale) return;
    var oldW = model.width / oldScale, oldH = model.height / oldScale;
    ARCH_CAMERA.scale = next;
    ARCH_CAMERA.x += (oldW - model.width / next) / 2;
    ARCH_CAMERA.y += (oldH - model.height / next) / 2;
    var svg = host.querySelector && host.querySelector("#arch-map-svg");
    if (svg) svg.setAttribute("viewBox", archCameraViewBox(model));
    var label = host.querySelector && host.querySelector("#arch-zoom-label");
    if (label) label.textContent = Math.round(next * 100) + "%";
  };
}

function archClosestAttr(target, host) {
  var attributes = ["data-arch-node", "data-arch-view", "data-arch-step", "data-arch-zoom",
                    "data-arch-filter", "data-arch-pan", "data-arch-map"];
  while (target && target !== host) {
    if (target.getAttribute) {
      for (var i = 0; i < attributes.length; i++) {
        if (target.getAttribute(attributes[i]) != null) return target;
      }
    }
    target = target.parentNode;
  }
  return null;
}

function archFindAttr(target, host, attribute) {
  while (target && target !== host) {
    if (target.getAttribute && target.getAttribute(attribute) != null) return target;
    target = target.parentNode;
  }
  return null;
}

async function loadAutonomy(force) {
  if ((!force && AUTONOMY) || AUTONOMY_LOADING) return;
  AUTONOMY_LOADING = true;
  try {
    const response = await fetch(`/api/autonomy?t=${Date.now()}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    AUTONOMY = await response.json();
  } catch (error) {
    AUTONOMY = {error: error && error.message ? error.message : String(error)};
  } finally { AUTONOMY_LOADING = false; }
}

async function loadArchitecture(force) {
  if ((!force && ARCH) || ARCH_LOADING) return;
  ARCH_LOADING = true;
  try {
    await loadAutonomy(force);
    const response = await fetch(`/api/architecture?t=${Date.now()}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    ARCH = await response.json();
    ARCH_LAST_FETCH_MS = Date.now();
    safe("architecture", () => renderArchitecture(ARCH, AUTONOMY));
  } catch (error) {
    safe("architecture", () => renderArchitecture(
      {error: error && error.message ? error.message : String(error)}, null));
  } finally { ARCH_LOADING = false; }
}
