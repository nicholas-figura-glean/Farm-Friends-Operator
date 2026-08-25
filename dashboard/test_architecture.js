/* Architecture tab tests.
 *
 * The architecture view is the one panel whose whole purpose is to be trustworthy
 * about the system's own shape, which makes a plausible-but-wrong render the worst
 * possible outcome: an operator who believes a locked file is editable, or that a dead
 * agent is running, is worse off than one with no diagram at all. Every check here
 * exists to make one of those specific lies impossible.
 *
 * Usage: osascript -l JavaScript dashboard/test_architecture.js
 */
ObjC.import("Foundation");
function slurp(path) {
  return $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null).js;
}
var out = [], fails = 0;
function ok(cond, label, detail) {
  out.push((cond ? "  ok   " : "  FAIL ") + label + (detail && !cond ? "  [" + detail + "]" : ""));
  if (!cond) fails++;
}
function count(text, needle) { return String(text).split(needle).length - 1; }

/* The asset expects a browser: it declares `async function` and touches `document`
 * and `fetch` at call time. Only the pure render helpers are under test, so the file
 * is loaded with stubs standing in for the DOM rather than being rewritten for it. */
var document = {
  getElementById: function () { return null; }
};
var window = {};
function safe(name, fn) { return fn(); }
function fetch() { throw new Error("fetch must not be called by render code"); }

var globalThisRef = this;
eval(slurp("dashboard/architecture.js"));

var CURRENT = {
  short: "abc123def456",
  commit: "deadbee",
  branch: "main",
  layers: [
    { id: "world", name: "The game", note: "outside our control" },
    { id: "play", name: "Play loop", note: "deterministic" },
    { id: "guard", name: "Safety & rollback", note: "agents may not modify" },
    { id: "decide", name: "Authoring & research", note: "may call a model" },
    { id: "operate", name: "Scheduling", note: "keeps it running" }
  ],
  nodes: [
    { id: "cycle", kind: "module", layer: "play", path: "farm/cycle.py", loc: 400, protected: false, doc: "Plays one turn." },
    { id: "rules", kind: "module", layer: "play", path: "farm/rules.py", loc: 600, protected: true },
    { id: "canary", kind: "module", layer: "guard", path: "farm/canary.py", loc: 500, protected: true },
    { id: "author_agent", kind: "agent", layer: "decide", path: "experiments/author_agent.py", loc: 900, protected: true },
    { id: "research_agent", kind: "agent", layer: "decide", path: "experiments/research_agent.py", loc: 300, protected: true },
    { id: "scheduler", kind: "module", layer: "operate", path: "farm/scheduler.py", loc: 200, protected: false }
  ],
  edges: [
    { source: "cycle", target: "rules" },
    { source: "author_agent", target: "canary" },
    { source: "author_agent", target: "rules" }
  ],
  agents: [
    { label: "com.nickfigura.farmfriends.author", entry: "experiments/author_agent.py", interval_seconds: 600 },
    { label: "com.nickfigura.farmfriends.research", entry: "experiments/research_agent.py", interval_seconds: 3600 }
  ],
  tools: ["list_farm", "collect_produce", "call_fbi"],
  stores: [],
  unmapped: [],
  stats: { modules: 4, agent_modules: 2, launch_agents: 2, protected: 4, edges: 3, tools: 3, loc: 2700 }
};

var PAYLOAD = {
  current: CURRENT,
  versions: 2,
  live_matches_recorded: true,
  events: [
    { ts: "2026-08-25T21:54:00Z", kind: "version", structural: true, title: "architecture v2",
      detail: "architecture tab added", added: ["autonomy", "architecture"], removed: [], agents_added: [] },
    { ts: "2026-08-25T21:32:00Z", kind: "release", structural: false, title: "release 20260825T213240Z",
      detail: "Show the figure the canary decides on" },
    { ts: "2026-08-25T21:00:00Z", kind: "canary", structural: false, title: "canary resolved rev1", detail: "healthy", ok: true },
    { ts: "2026-08-25T20:41:00Z", kind: "order", structural: false, title: "published fix-evidence", detail: "bad fit", ok: true },
    { ts: "2026-08-25T20:34:00Z", kind: "finding", structural: false, title: "contract_finding: call_fbi", detail: "aliens", errors: 1 }
  ]
};

/* ---- layered diagram --------------------------------------------------- */
var layers = archLayersHtml(CURRENT, {});
ok(count(layers, 'class="arch-layer"') === 5, "every populated layer is drawn",
   String(count(layers, 'class="arch-layer"')));
ok(layers.indexOf('data-layer="world"') !== -1, "the game is on the diagram");
ok(layers.indexOf("call_fbi") !== -1, "live MCP tools appear in the world layer");
ok(layers.indexOf("list_farm") < layers.indexOf("cycle"),
   "the game is drawn above our own code");

/* A layer with no members must be omitted, not drawn empty. */
var sparse = {
  layers: CURRENT.layers,
  nodes: [{ id: "cycle", kind: "module", layer: "play", path: "farm/cycle.py", loc: 10 }],
  edges: [], tools: []
};
ok(count(archLayersHtml(sparse, {}), 'class="arch-layer"') === 2,
   "empty layers are omitted rather than drawn hollow");

/* ---- the protected-file marking, which must never be wrong -------------- */
ok(archNodeClass({ id: "rules", kind: "module", protected: true }, {}).indexOf("protected") !== -1,
   "a locked file is marked locked");
ok(archNodeClass({ id: "cycle", kind: "module", protected: false }, {}).indexOf("protected") === -1,
   "an editable file is not marked locked");
ok(archNodeHtml({ id: "rules", kind: "module", protected: true, loc: 600 }, {}).indexOf("protected") !== -1,
   "the locked marking survives into the rendered node");

/* ---- liveness overlay -------------------------------------------------- */
var HEALTH_DOWN = {
  agents: { agents: [
    { label: "com.nickfigura.farmfriends.author", loaded: false, state: "unknown", role: "writes the repairs" },
    { label: "com.nickfigura.farmfriends.research", loaded: true, state: "waiting", role: "finds strategy" }
  ] }
};
var health = archAgentHealth(HEALTH_DOWN);
ok(health["com.nickfigura.farmfriends.author"].loaded === false, "a down agent is read as down");
ok(health["com.nickfigura.farmfriends.research"].loaded === true, "a live agent is read as live");
ok(archNodeClass({ id: "author_agent", kind: "agent", down: true }, health).indexOf("down") !== -1,
   "a down agent module is drawn as down");
ok(archNodeClass({ id: "research_agent", kind: "agent" }, health).indexOf("down") === -1,
   "a live agent module is not drawn as down");
/* A plain module has no process of its own, so it can never be "down". Marking one
 * down would invent an outage that does not exist. */
ok(archNodeClass({ id: "canary", kind: "module", down: true }, health).indexOf("down") === -1,
   "a library module is never drawn as down");
ok(archAgentHealth(null) && Object.keys(archAgentHealth(null)).length === 0,
   "a missing autonomy payload yields no health claims");

/* ---- component detail -------------------------------------------------- */
var detail = archDetailHtml(CURRENT, "canary");
ok(detail.indexOf("farm/canary.py") !== -1, "detail names the real path");
ok(detail.indexOf("author_agent") !== -1, "detail lists reverse dependencies");
ok(detail.indexOf("may not edit") !== -1, "detail states a locked file is unwritable");
var editable = archDetailHtml(CURRENT, "cycle");
ok(editable.indexOf("may patch this") !== -1, "detail states an editable file is patchable");
ok(editable.indexOf("rules") !== -1, "detail lists forward dependencies");
ok(archDetailHtml(CURRENT, null).indexOf("Select a component") !== -1,
   "no selection shows guidance rather than an empty box");
ok(archDetailHtml(CURRENT, "nonexistent").indexOf("Select a component") !== -1,
   "an unknown id falls back to guidance");

/* ---- version history --------------------------------------------------- */
var events = archEventsHtml(PAYLOAD.events, "all");
ok(count(events, 'class="arch-ev"') === 5, "every event is listed", String(count(events, 'class="arch-ev"')));
ok(events.indexOf("+autonomy, +architecture") !== -1, "a structural version shows what was added");
ok(archEventsHtml(PAYLOAD.events, "structural").indexOf('data-kind="release"') === -1,
   "the structural filter excludes non-structural events");
ok(count(archEventsHtml(PAYLOAD.events, "structural"), 'class="arch-ev"') === 1,
   "the structural filter keeps only the version");
ok(count(archEventsHtml(PAYLOAD.events, "canary"), 'class="arch-ev"') === 1,
   "a kind filter selects that kind");
ok(archEventsHtml([], "all").indexOf("No events") !== -1,
   "an empty history says so rather than rendering nothing");

/* Chronology must be preserved exactly as the server ordered it. Re-sorting in the
 * client would risk reintroducing the timezone bug that put releases hours before
 * the findings they followed. */
var first = events.indexOf("architecture v2");
var second = events.indexOf("release 20260825T213240Z");
ok(first !== -1 && second !== -1 && first < second,
   "server ordering is preserved, newest first");

/* A failed canary or order must read as failed. */
ok(archEventHtml({ ts: "2026-08-25T21:00:00Z", kind: "canary", title: "reverted", ok: false })
   .indexOf("\u2717") !== -1, "a failed event is marked failed");
ok(archEventHtml({ ts: "2026-08-25T21:00:00Z", kind: "canary", title: "kept", ok: true })
   .indexOf("\u2713") !== -1, "a successful event is marked successful");

/* ---- stats ------------------------------------------------------------- */
var stats = archStatsHtml(CURRENT);
ok(count(stats, 'class="arch-stat"') === 6, "all six headline stats render");
ok(stats.indexOf("2,700") !== -1, "large counts are thousands-separated");
ok(archStatsHtml({ stats: {} }).indexOf("-") !== -1, "a missing stat shows a dash, not NaN");

/* ---- escaping ---------------------------------------------------------- */
ok(archEscape('<script>x</script>') === "&lt;script&gt;x&lt;/script&gt;", "markup is escaped");
ok(archEscape(null) === "", "null escapes to empty");
var hostile = archDetailHtml({
  nodes: [{ id: "<img src=x onerror=1>", kind: "module", layer: "play", path: "a\"b", loc: 1 }],
  edges: []
}, "<img src=x onerror=1>");
ok(hostile.indexOf("<img") === -1, "a hostile component name cannot inject markup");
ok(archEventHtml({ ts: "x", kind: "finding", title: "<b>no</b>", detail: "<i>no</i>" }).indexOf("<b>") === -1,
   "a hostile event title cannot inject markup");

/* ---- degradation ------------------------------------------------------- */
var threw = null;
try {
  archLayersHtml({}, {});
  archLayersHtml({ layers: null, nodes: null, tools: null }, null);
  archDetailHtml({}, "x");
  archEventsHtml(null, "all");
  archStatsHtml({});
  archAgentHealth({});
  archEventHtml({});
  renderArchitecture(null, null);
  renderArchitecture({ error: "boom" }, null);
  renderArchitecture(PAYLOAD, HEALTH_DOWN);
} catch (error) { threw = error.message || String(error); }
ok(threw === null, "malformed payloads degrade without throwing", threw || "");

out.push("architecture: " + (fails ? fails + " FAIL" : "PASS") + " " + out.length + " checks");
out.push(fails ? "FAIL" : "PASS");
out.join("\n");
