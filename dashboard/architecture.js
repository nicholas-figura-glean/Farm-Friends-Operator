/* Architecture tab: a derived map of the system plus its version history.
 *
 * The rendering functions here are deliberately pure -- they take data and return
 * HTML strings, and only `renderArchitecture` touches the DOM. That is what lets
 * dashboard/test_architecture.js exercise the layout and diff logic in
 * JavaScriptCore without a browser, which is how the rest of this dashboard's
 * non-trivial logic is tested.
 */

var ARCH = null;                 // last /api/architecture payload
var ARCH_LOADING = false;
var ARCH_SELECTED = null;        // id of the inspected component
var ARCH_FILTER = "all";         // event-stream filter
var AUTONOMY = null;             // last /api/autonomy payload, for the health overlay
var AUTONOMY_LOADING = false;

function archEscape(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* Health overlay. The architecture describes the system as designed; whether each
 * piece is actually running is a different question, answered by /api/autonomy. A
 * diagram that cannot show a dead agent is decoration. */
function archAgentHealth(autonomyView) {
  var health = {};
  var agents = ((autonomyView || {}).agents || {}).agents || [];
  for (var i = 0; i < agents.length; i++) {
    var agent = agents[i];
    // Map a LaunchAgent label back to the module that implements it, so the node in
    // the diagram can carry the agent's liveness.
    var entry = String(agent.role || "");
    health[String(agent.label)] = {loaded: !!agent.loaded, state: agent.state, role: entry};
  }
  return health;
}

function archNodeClass(node, health) {
  var classes = ["arch-node"];
  if (node.kind === "agent") classes.push("agent");
  if (node.protected) classes.push("protected");
  // An agent module whose LaunchAgent is not loaded is drawn as down. Module nodes
  // have no liveness of their own -- they are libraries, not processes.
  if (node.kind === "agent" && node.down) classes.push("down");
  return classes.join(" ");
}

function archNodeHtml(node, health) {
  var loc = node.loc ? (node.loc + " loc") : "";
  return '<button class="' + archNodeClass(node, health) + '" role="button"' +
         ' aria-pressed="' + (ARCH_SELECTED === node.id ? "true" : "false") + '"' +
         ' data-arch-node="' + archEscape(node.id) + '">' +
         '<span>' + archEscape(node.id) + '</span>' +
         '<span class="nloc">' + archEscape(loc) + '</span>' +
         '</button>';
}

/* The layered diagram. Layers with no members are skipped rather than drawn empty:
 * an empty box invites the reader to wonder what is missing from it. */
function archLayersHtml(current, health) {
  var layers = current.layers || [];
  var nodes = current.nodes || [];
  var out = [];
  for (var i = 0; i < layers.length; i++) {
    var layer = layers[i];
    var members = [];
    for (var j = 0; j < nodes.length; j++) {
      if (nodes[j].layer === layer.id) members.push(nodes[j]);
    }
    if (layer.id === "world") {
      // The game is not our code, so it is drawn from the tool list rather than
      // from modules. It belongs on the diagram because every contract change we
      // have had to repair originated here.
      var tools = current.tools || [];
      var toolHtml = tools.map(function (t) {
        return '<span class="arch-node tool">' + archEscape(t) + '</span>';
      }).join("");
      out.push('<div class="arch-layer" data-layer="world"><header><h3>' +
               archEscape(layer.name) + '</h3><span class="lnote">' +
               archEscape(layer.note) + '</span></header>' +
               '<div class="arch-nodes">' + (toolHtml ||
                 '<span class="arch-note">no captured contract</span>') + '</div></div>');
      continue;
    }
    if (!members.length) continue;
    var nodeHtml = members.map(function (n) { return archNodeHtml(n, health); }).join("");
    out.push('<div class="arch-layer" data-layer="' + archEscape(layer.id) + '">' +
             '<header><h3>' + archEscape(layer.name) + '</h3>' +
             '<span class="lnote">' + archEscape(layer.note) + '</span></header>' +
             '<div class="arch-nodes">' + nodeHtml + '</div></div>');
  }
  return out.join("");
}

/* Detail panel for one component, including who depends on it. Reverse dependencies
 * are the useful direction when judging a change: "what breaks if I touch this". */
function archDetailHtml(current, id) {
  var nodes = current.nodes || [];
  var node = null;
  for (var i = 0; i < nodes.length; i++) if (nodes[i].id === id) node = nodes[i];
  if (!node) {
    return '<h4>Select a component</h4><p class="arch-note">Every box is derived from ' +
           'what is on disk right now: module imports, LaunchAgent plists, and the ' +
           'captured MCP contract. Nothing here is hand-maintained, so it cannot drift ' +
           'out of date the way a drawn diagram does.</p>';
  }
  var edges = current.edges || [];
  var uses = [], usedBy = [];
  for (var j = 0; j < edges.length; j++) {
    if (edges[j].source === id) uses.push(edges[j].target);
    if (edges[j].target === id) usedBy.push(edges[j].source);
  }
  var chips = function (list) {
    if (!list.length) return '<span class="arch-note">none</span>';
    return list.map(function (d) {
      return '<span class="arch-dep">' + archEscape(d) + '</span>';
    }).join("");
  };
  var html = '<h4>' + archEscape(node.id) + '</h4>' +
             '<div class="dpath">' + archEscape(node.path) + '</div>';
  if (node.doc) html += '<p class="ddoc">' + archEscape(node.doc) + '</p>';
  html += '<dl>' +
          '<dt>layer</dt><dd>' + archEscape(node.layer) + '</dd>' +
          '<dt>kind</dt><dd>' + archEscape(node.kind) + '</dd>' +
          '<dt>size</dt><dd>' + archEscape(node.loc || "?") + ' lines</dd>' +
          '<dt>writable</dt><dd>' + (node.protected ?
            '<span style="color:var(--yellow)">no &mdash; agents may not edit this</span>' :
            'yes &mdash; the author agent may patch this') + '</dd>' +
          '</dl>' +
          '<dl><dt>uses</dt><dd>' + chips(uses) + '</dd>' +
          '<dt>used by</dt><dd>' + chips(usedBy) + '</dd></dl>';
  return html;
}

function archEventHtml(event) {
  var when = String(event.ts || "").slice(5, 16).replace("T", " ");
  var changes = "";
  if (event.kind === "version") {
    var bits = [];
    if ((event.added || []).length) bits.push("<b>+" + event.added.join(", +") + "</b>");
    if ((event.removed || []).length) bits.push("<i>-" + event.removed.join(", -") + "</i>");
    if ((event.agents_added || []).length) {
      bits.push("<b>+" + event.agents_added.length + " agent(s)</b>");
    }
    if (bits.length) changes = '<div class="chg">' + bits.join(" &nbsp; ") + '</div>';
  }
  var ok = event.ok;
  var mark = ok === false ? ' \u2717' : (ok === true ? ' \u2713' : '');
  return '<div class="arch-ev" data-kind="' + archEscape(event.kind) + '">' +
         '<span class="when">' + archEscape(when) + '</span>' +
         '<span class="what"><span class="title"><span class="kind">' +
         archEscape(event.kind) + '</span>' + archEscape(event.title) + archEscape(mark) +
         '</span>' +
         (event.detail ? '<div class="detail">' + archEscape(event.detail) + '</div>' : '') +
         changes + '</span></div>';
}

function archEventsHtml(events, filter) {
  var rows = (events || []).filter(function (e) {
    if (filter === "all") return true;
    if (filter === "structural") return !!e.structural;
    return e.kind === filter;
  });
  if (!rows.length) return '<p class="arch-note">No events recorded yet.</p>';
  return rows.map(archEventHtml).join("");
}

function archStatsHtml(current) {
  var stats = current.stats || {};
  var cells = [
    ["modules", stats.modules],
    ["agents", stats.launch_agents],
    ["locked", stats.protected],
    ["edges", stats.edges],
    ["tools", stats.tools],
    ["lines", stats.loc]
  ];
  return cells.map(function (cell) {
    var value = cell[1];
    var shown = typeof value === "number" ? value.toLocaleString() : "-";
    return '<div class="arch-stat"><b>' + shown + '</b><span>' +
           archEscape(cell[0]) + '</span></div>';
  }).join("");
}

function renderArchitecture(payload, autonomyView) {
  var host = document.getElementById("tab-architecture");
  if (!host) return;
  if (!payload || payload.error) {
    host.innerHTML = '<div class="card full"><h2>Architecture</h2>' +
      '<p class="arch-note">Could not build the architecture view: ' +
      archEscape((payload || {}).error || "no data") + '</p></div>';
    return;
  }
  var current = payload.current || {};
  var health = archAgentHealth(autonomyView);

  // Mark agent modules whose LaunchAgent is not loaded. The mapping is by entry
  // path rather than by name, because the label and the module name differ
  // (com.nickfigura.farmfriends.contract runs experiments/contract_watch.py).
  var agentSpecs = (current.agents || []);
  var nodes = (current.nodes || []).map(function (node) {
    var copy = {};
    for (var key in node) if (Object.prototype.hasOwnProperty.call(node, key)) copy[key] = node[key];
    if (copy.kind === "agent") {
      for (var i = 0; i < agentSpecs.length; i++) {
        var entry = String(agentSpecs[i].entry || "");
        if (entry.indexOf(copy.id) !== -1) {
          var info = health[String(agentSpecs[i].label)];
          if (info && !info.loaded) copy.down = true;
        }
      }
    }
    return copy;
  });
  var currentWithHealth = {};
  for (var k in current) if (Object.prototype.hasOwnProperty.call(current, k)) currentWithHealth[k] = current[k];
  currentWithHealth.nodes = nodes;

  var drift = payload.live_matches_recorded === false
    ? '<div class="arch-warn">The live tree\u2019s shape does not match the newest recorded ' +
      'version. Something changed the architecture since the last scan; the dashboard ' +
      'agent will record it on its next pass.</div>'
    : '';
  var unmapped = (current.unmapped || []).length
    ? '<div class="arch-warn">Unclassified module(s): ' +
      archEscape((current.unmapped || []).join(", ")) +
      '. These fell back to the observation layer; classify them in ' +
      'farm/architecture.py so the diagram keeps meaning what it says.</div>'
    : '';

  var filters = ["all", "structural", "release", "canary", "order", "finding"];
  var filterHtml = filters.map(function (name) {
    return '<button data-arch-filter="' + name + '" aria-pressed="' +
           (ARCH_FILTER === name ? "true" : "false") + '">' + name + '</button>';
  }).join("");

  host.innerHTML =
    '<div class="card full">' +
      '<div class="arch-head"><h2 style="margin:0">Architecture</h2>' +
      '<span class="arch-sig">' + archEscape(current.short || "") + '</span>' +
      '<span class="arch-note">' + archEscape(payload.versions || 0) +
      ' recorded version(s) &middot; commit ' + archEscape(current.commit || "?") +
      ' on ' + archEscape(current.branch || "?") + '</span></div>' +
      '<p class="arch-note">Derived from source, plists and the captured contract on ' +
      'every scan, then hashed. The hash covers which parts exist, what depends on ' +
      'what, and which agents run \u2014 not line counts or comments, so editing a ' +
      'docstring does not mint a version. Yellow-barred boxes are files the agents ' +
      'are forbidden to edit.</p>' +
      drift + unmapped +
      '<div class="arch-stats" style="margin:12px 0 14px">' + archStatsHtml(current) + '</div>' +
      '<div class="arch-layout">' +
        '<div>' + archLayersHtml(currentWithHealth, health) + '</div>' +
        '<div class="arch-side">' +
          '<div class="card arch-detail" id="arch-detail">' +
            archDetailHtml(currentWithHealth, ARCH_SELECTED) + '</div>' +
          '<div class="card"><h2>Version history</h2>' +
            '<div class="arch-filter">' + filterHtml + '</div>' +
            '<div class="arch-timeline" id="arch-timeline">' +
              archEventsHtml(payload.events, ARCH_FILTER) + '</div>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';

  // Delegated once per render. Rebinding per node would leak listeners on a tab the
  // operator flips back to repeatedly.
  host.onclick = function (evt) {
    var target = evt.target;
    while (target && target !== host && !target.getAttribute) target = target.parentNode;
    if (!target || target === host) return;
    var nodeId = target.getAttribute && target.getAttribute("data-arch-node");
    if (!nodeId) {
      var parent = target.parentNode;
      if (parent && parent.getAttribute) nodeId = parent.getAttribute("data-arch-node");
    }
    if (nodeId) {
      ARCH_SELECTED = (ARCH_SELECTED === nodeId) ? null : nodeId;
      renderArchitecture(ARCH, AUTONOMY);
      return;
    }
    var filter = target.getAttribute && target.getAttribute("data-arch-filter");
    if (filter) {
      ARCH_FILTER = filter;
      renderArchitecture(ARCH, AUTONOMY);
    }
  };
}

/* Autonomy is fetched separately from the architecture because the two answer
 * different questions and change at different rates: the architecture is the system
 * as designed and moves only when code moves, while liveness moves whenever an agent
 * starts or dies. Refreshing this on tab activation keeps a dead agent from being
 * drawn as healthy indefinitely. */
async function loadAutonomy(force) {
  if ((!force && AUTONOMY) || AUTONOMY_LOADING) return;
  AUTONOMY_LOADING = true;
  try {
    const response = await fetch(`/api/autonomy?t=${Date.now()}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    AUTONOMY = await response.json();
  } catch (error) {
    AUTONOMY = {error: error && error.message ? error.message : String(error)};
  } finally {
    AUTONOMY_LOADING = false;
  }
}

async function loadArchitecture(force) {
  if ((!force && ARCH) || ARCH_LOADING) return;
  ARCH_LOADING = true;
  try {
    // Liveness first, so the diagram's first paint already shows agent health rather
    // than flashing every agent as up and then correcting itself.
    await loadAutonomy(force);
    const response = await fetch(`/api/architecture?t=${Date.now()}`, {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    ARCH = await response.json();
    safe("architecture", () => renderArchitecture(ARCH, AUTONOMY));
  } catch (error) {
    safe("architecture", () => renderArchitecture(
      {error: error && error.message ? error.message : String(error)}, null));
  } finally {
    ARCH_LOADING = false;
  }
}
