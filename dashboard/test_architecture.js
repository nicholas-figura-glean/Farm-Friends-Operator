/* Headless architecture explorer tests.
 *
 * The view must never invent execution from imports, hide a dead agent, or understate
 * the blast radius of changing a protected component. These checks exercise the same
 * pure graph model, layout, SVG and inspector HTML used by the browser.
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
function clone(value) { return JSON.parse(JSON.stringify(value)); }

var HOST = null;
var document = { getElementById: function () { return HOST; } };
var window = {};
function safe(name, fn) { return fn(); }
function fetch() { throw new Error("fetch must not be called by render code"); }
eval(slurp("dashboard/architecture.js"));

var CURRENT = {
  short: "abc123def456", commit: "deadbee", branch: "main",
  layers: [
    {id:"world", name:"The game", note:"outside our control"},
    {id:"play", name:"Play loop", note:"deterministic"},
    {id:"observe", name:"Observation & evidence", note:"measurements"},
    {id:"guard", name:"Safety & rollback", note:"agents may not modify"},
    {id:"decide", name:"Authoring & research", note:"may call a model"},
    {id:"operate", name:"Scheduling", note:"keeps it running"}
  ],
  nodes: [
    {id:"cycle",kind:"module",layer:"play",path:"farm/cycle.py",loc:400,protected:false,doc:"Plays one turn."},
    {id:"rules",kind:"module",layer:"play",path:"farm/rules.py",loc:600,protected:true,doc:"Pure decisions."},
    {id:"mcp",kind:"module",layer:"play",path:"farm/mcp.py",loc:260,protected:false,doc:"The external boundary."},
    {id:"ledger",kind:"module",layer:"observe",path:"farm/ledger.py",loc:180,protected:false},
    {id:"canary",kind:"module",layer:"guard",path:"farm/canary.py",loc:500,protected:true},
    {id:"author_agent",kind:"agent",layer:"decide",path:"experiments/author_agent.py",loc:900,protected:true},
    {id:"research_agent",kind:"agent",layer:"decide",path:"experiments/research_agent.py",loc:300,protected:true},
    {id:"scheduler",kind:"module",layer:"operate",path:"farm/scheduler.py",loc:200,protected:false}
  ],
  edges: [
    {source:"cycle",target:"rules"},
    {source:"cycle",target:"mcp"},
    {source:"author_agent",target:"canary"},
    {source:"author_agent",target:"rules"},
    {source:"scheduler",target:"author_agent"}
  ],
  runtime_steps: [
    {name:"tools",order:0,modules:["ledger","mcp"],tools:["tools/list"]},
    {name:"collect",order:1,modules:["cycle","ledger","mcp"],tools:["collect_produce"]},
    {name:"verify",order:2,modules:["cycle","mcp","rules"],tools:["list_farm"]},
    {name:"finish",order:3,modules:["cycle","rules"],tools:[]}
  ],
  runtime_edges: [
    {source:"cycle",target:"mcp",kind:"call",steps:["collect","verify"]},
    {source:"cycle",target:"rules",kind:"call",steps:["verify","finish"]},
    {source:"cycle",target:"ledger",kind:"call",steps:["collect"]},
    {source:"mcp",target:"tool:collect_produce",kind:"tool",steps:["collect"]},
    {source:"mcp",target:"tool:list_farm",kind:"tool",steps:["verify"]},
    {source:"mcp",target:"tool:tools/list",kind:"tool",steps:["tools"]}
  ],
  runtime_errors: [],
  agents: [
    {label:"com.nickfigura.farmfriends.author",entry:"experiments/author_agent.py",interval_seconds:600},
    {label:"com.nickfigura.farmfriends.research",entry:"experiments/research_agent.py",interval_seconds:3600}
  ],
  tools:["collect_produce","list_farm","tools/list","call_fbi"],
  stores:[{name:"history.ndjson",bytes:1200,kind:"append-only"}], unmapped:[],
  stats:{modules:6,agent_modules:2,launch_agents:2,protected:4,edges:5,tools:4,loc:3340}
};
var PAYLOAD = {
  current:CURRENT, versions:2, live_matches_recorded:true,
  events:[
    {ts:"2026-08-25T21:54:00Z",kind:"version",structural:true,title:"architecture v2",detail:"architecture tab added",added:["autonomy","architecture"],removed:[],agents_added:[]},
    {ts:"2026-08-25T21:32:00Z",kind:"release",structural:false,title:"release 20260825T213240Z",detail:"Show the figure the canary decides on"},
    {ts:"2026-08-25T21:00:00Z",kind:"canary",structural:false,title:"canary resolved rev1",detail:"healthy",ok:true},
    {ts:"2026-08-25T20:41:00Z",kind:"order",structural:false,title:"published fix-evidence",detail:"bad fit",ok:true},
    {ts:"2026-08-25T20:34:00Z",kind:"finding",structural:false,title:"contract_finding: call_fbi",detail:"aliens",errors:1}
  ]
};
var HEALTH_DOWN = {agents:{agents:[
  {label:"com.nickfigura.farmfriends.author",loaded:false,state:"unknown",role:"writes repairs"},
  {label:"com.nickfigura.farmfriends.research",loaded:true,state:"waiting",role:"finds strategy"}
]},vcs:{dirty_source_paths:[]},canary:{status:"resolved"},activity:{events:[]}};
var HEALTH_OK = {agents:{agents:[
  {label:"com.nickfigura.farmfriends.author",loaded:true,state:"waiting",role:"writes repairs"},
  {label:"com.nickfigura.farmfriends.research",loaded:true,state:"waiting",role:"finds strategy"}
]},vcs:{dirty_source_paths:[]},canary:{status:"resolved"},activity:{events:[]}};

/* ---- compact fallback and protected-file truth ------------------------- */
var layers = archLayersHtml(CURRENT);
ok(count(layers, 'class="arch-layer"') === 6, "every populated layer is drawn", String(count(layers, 'class="arch-layer"')));
ok(layers.indexOf('data-layer="world"') !== -1 && layers.indexOf("call_fbi") !== -1, "captured MCP contract remains visible in fallback");
ok(layers.indexOf("list_farm") < layers.indexOf("cycle"), "the external system is ordered before our code");
ok(archNodeClass({kind:"module",protected:true}).indexOf("protected") !== -1, "a locked file is marked locked");
ok(archNodeClass({kind:"module",protected:false}).indexOf("protected") === -1, "an editable file is not marked locked");
ok(archNodeHtml({id:"rules",kind:"module",protected:true,loc:600}).indexOf("protected") !== -1, "locked marking reaches rendered fallback node");

/* ---- health overlay ---------------------------------------------------- */
var health = archAgentHealth(HEALTH_DOWN);
ok(health["com.nickfigura.farmfriends.author"].loaded === false, "a down agent is read as down");
ok(health["com.nickfigura.farmfriends.research"].loaded === true, "a live agent is read as live");
var healthyCurrent = archApplyHealth(CURRENT, HEALTH_DOWN);
var healthyById = {}; healthyCurrent.nodes.forEach(function (node) { healthyById[node.id] = node; });
ok(healthyById.author_agent.down === true && !healthyById.research_agent.down, "only the unloaded agent is marked down");
ok(archNodeClass(healthyById.author_agent).indexOf("down") !== -1, "down state reaches node styling");
ok(archNodeClass({kind:"module",down:true}).indexOf("down") === -1, "a library module is never claimed down");
ok(Object.keys(archAgentHealth(null)).length === 0, "missing autonomy yields no health claims");

/* ---- operator posture and architecture change summary ------------------ */
var cleanCurrent = archApplyHealth(CURRENT, HEALTH_OK);
var cleanPosture = archPosture(PAYLOAD, cleanCurrent, HEALTH_OK);
ok(cleanPosture.tone === "good" && cleanPosture.loadedAgents === 2, "coherent topology with loaded services requires no action");
ok(cleanPosture.liveTitle.indexOf("matches recorded v2") !== -1, "posture states live-to-recorded alignment");
var downPosture = archPosture(PAYLOAD, healthyCurrent, HEALTH_DOWN);
ok(downPosture.tone === "recovering" && downPosture.intervention.indexOf("Supervisor is restoring 1 unloaded service") !== -1,
   "an unloaded service is explicitly owned by automatic recovery");
var WATCH_VIEW = clone(HEALTH_OK);
WATCH_VIEW.vcs.dirty_source_paths = ["dashboard/architecture.js", "dashboard/architecture.css"];
WATCH_VIEW.canary = {status:"watching"};
WATCH_VIEW.activity.events = [{ts:"2026-08-26T16:00:00Z",phase:"act",actor:"Author agent",status:"published",title:"Published bounded architecture repair"}];
var watchPosture = archPosture(PAYLOAD, archApplyHealth(CURRENT,WATCH_VIEW), WATCH_VIEW);
ok(watchPosture.tone === "watch" && watchPosture.intervention.indexOf("containing 2 changed source files") !== -1,
   "pending source changes are contained by release automation");
var situation = archSituationHtml(PAYLOAD, cleanCurrent, WATCH_VIEW, watchPosture);
ok(count(situation,'class="arch-situation-cell ') === 4, "summary answers now, changed, autonomous action, and recovery ownership");
ok(situation.indexOf("Happening now") !== -1 && situation.indexOf("What changed") !== -1 &&
   situation.indexOf("Autonomous action") !== -1 && situation.indexOf("Recovery ownership") !== -1,
   "headless ownership is named explicitly");
ok(situation.indexOf("Published bounded architecture repair") !== -1, "latest autonomous action is projected without inventing activity");
var UNCHANGED_VERSION = {events:[{ts:"2026-08-26T15:00:00Z",kind:"version",structural:true,title:"architecture v3",detail:"scheduled scan",added:[],removed:[]}]};
ok(archSituationHtml(UNCHANGED_VERSION,cleanCurrent,HEALTH_OK,cleanPosture).indexOf("no component additions or removals") !== -1,
   "a recorded version with unchanged component membership says so explicitly");
var DRIFT_PAYLOAD = clone(PAYLOAD); DRIFT_PAYLOAD.live_matches_recorded = false;
ok(archPosture(DRIFT_PAYLOAD,cleanCurrent,HEALTH_OK).tone === "recovering", "unrecorded topology drift is owned by recovery");

/* ---- separate runtime and structure lenses ----------------------------- */
var runtime = archGraphModel(CURRENT, "runtime", "all", null, "");
ok(!!runtime.byId["tool:collect_produce"], "runtime lens promotes reached MCP tools to nodes");
ok(!runtime.byId.author_agent, "runtime lens omits background agents not reached by a cycle");
ok(runtime.byId.cycle && runtime.byId.mcp && runtime.byId.rules, "runtime lens keeps reached modules");
ok(runtime.edges.length === 6, "runtime lens uses only runtime call edges", String(runtime.edges.length));
ok(runtime.layers[0].id === "world", "external tools form the first runtime layer");
ok(runtime.positions.cycle && runtime.positions["tool:list_farm"], "every runtime node receives a stable SVG position");

var structure = archGraphModel(CURRENT, "structure", "all", null, "");
ok(!!structure.byId.author_agent && !!structure.byId.scheduler, "system map includes background architecture");
ok(!structure.byId["tool:collect_produce"], "system map does not disguise tool calls as imports");
ok(structure.edges.every(function (edge) { return edge.kind === "dependency"; }), "system map labels every relation as a dependency");

var collect = archGraphModel(CURRENT, "runtime", "collect", null, "");
ok(!!collect.byId["tool:collect_produce"] && !collect.byId["tool:list_farm"], "choosing a run stage narrows external tools");
ok(collect.edges.every(function (edge) { return edge.steps.indexOf("collect") !== -1; }), "stage view keeps only paths reachable in that stage");
ok(collect.byId.ledger && !collect.byId.rules, "stage view narrows modules as well as tools");

/* ---- selection, search, and blast radius ------------------------------- */
var selected = archGraphModel(CURRENT, "structure", "all", "canary", "");
ok(selected.directIn.author_agent, "selection identifies direct reverse dependencies");
ok(selected.impact.scheduler, "selection computes transitive change impact");
ok(archNodeStateClass(selected, "canary").indexOf("selected") !== -1, "selected node receives focus styling");
ok(archNodeStateClass(selected, "scheduler").indexOf("impact") !== -1, "transitive dependent receives impact styling");
ok(archNodeStateClass(selected, "cycle").indexOf("dim") !== -1, "unrelated nodes are de-emphasized");

var searched = archGraphModel(CURRENT, "structure", "all", null, "canary");
ok(searched.queryMatches.canary, "search matches component metadata");
ok(searched.queryNeighbors.author_agent, "search preserves a matching node's direct context");
ok(archNodeStateClass(searched, "cycle").indexOf("dim") !== -1, "search quiets unrelated nodes");

/* ---- actual SVG graph -------------------------------------------------- */
var runtimeSelected = archGraphModel(healthyCurrent, "runtime", "verify", "mcp", "");
var svg = archGraphHtml(runtimeSelected);
ok(svg.indexOf('<svg class="arch-map-svg"') !== -1, "graph renders as an SVG exploration surface");
ok(count(svg, 'class="arch-edge ') === runtimeSelected.edges.length, "every derived relationship gets one directional path");
ok(svg.indexOf('marker-end="url(#arch-arrow') !== -1, "relationships carry arrowheads");
ok(svg.indexOf('data-arch-node="mcp"') !== -1 && svg.indexOf('data-arch-node="tool:list_farm"') !== -1, "modules and external tools are selectable");
ok(svg.indexOf("selected-out") !== -1, "selecting a node highlights its outgoing route");
ok(svg.indexOf("LOCKED") !== -1, "protected status remains visible inside the map");

/* ---- inspector --------------------------------------------------------- */
var detail = archDetailHtml(CURRENT, "canary", selected);
ok(detail.indexOf("farm/canary.py") !== -1, "inspector names the real source path");
ok(detail.indexOf("author_agent") !== -1, "inspector lists reverse dependencies");
ok(detail.indexOf("2</b><span>components") !== -1, "inspector reports the transitive change radius");
ok(detail.indexOf("agents may not edit this file") !== -1, "inspector states protected change policy");
var editable = archDetailHtml(CURRENT, "cycle", archGraphModel(CURRENT,"structure","all","cycle",""));
ok(editable.indexOf("author agent may patch this file") !== -1, "inspector states editable change policy");
ok(editable.indexOf("rules") !== -1 && editable.indexOf("mcp") !== -1, "inspector exposes forward dependencies");
ok(archDetailHtml(CURRENT, null, structure).indexOf("Select any component") !== -1, "empty inspector teaches the interaction");
ok(archDetailHtml(CURRENT, "nonexistent", structure).indexOf("Explore the live model") !== -1, "unknown ids degrade to guidance");
var toolDetail = archDetailHtml(CURRENT, "tool:list_farm", archGraphModel(CURRENT,"runtime","verify","tool:list_farm",""));
ok(toolDetail.indexOf("External game capability") !== -1 && toolDetail.indexOf("verify") !== -1, "tool inspector connects the boundary to run stages");

/* ---- controls and progressive disclosure ------------------------------ */
ARCH_STEP = "collect";
var rail = archStepRailHtml(CURRENT);
ok(count(rail, "data-arch-step=") === 5, "run rail exposes the whole run plus every stage");
ok(rail.indexOf('data-arch-step="collect" aria-pressed="true"') !== -1, "active run stage is explicit");
ARCH_STEP = "all";
ARCH_VIEW = "runtime";
var toolbar = archToolbarHtml(CURRENT, runtime);
ok(toolbar.indexOf("How it runs") !== -1 && toolbar.indexOf("System map") !== -1, "relationship lenses are switchable");
ok(toolbar.indexOf("data-arch-search") !== -1 && toolbar.indexOf("data-arch-zoom") !== -1, "map supports search and zoom");

/* ---- version history --------------------------------------------------- */
var events = archEventsHtml(PAYLOAD.events, "all");
ok(count(events, 'class="arch-ev"') === 5, "every event is listed", String(count(events, 'class="arch-ev"')));
ok(events.indexOf("+autonomy, +architecture") !== -1, "structural version names additions");
ok(count(archEventsHtml(PAYLOAD.events,"structural"),'class="arch-ev"') === 1, "structural filter excludes behavioral events");
ok(count(archEventsHtml(PAYLOAD.events,"canary"),'class="arch-ev"') === 1, "kind filter selects that kind");
ok(archEventsHtml([],"all").indexOf("No events") !== -1, "empty history explains itself");
ok(events.indexOf("architecture v2") < events.indexOf("release 20260825T213240Z"), "server chronology is preserved");
ok(archEventHtml({ts:"x",kind:"canary",title:"reverted",ok:false}).indexOf("✗") !== -1, "failed canary is marked failed");
ok(archEventHtml({ts:"x",kind:"canary",title:"kept",ok:true}).indexOf("✓") !== -1, "successful canary is marked successful");
var detailedEvent = archEventHtml({ts:"x",kind:"release",title:"release 1",detail:"short release note"});
ok(detailedEvent.indexOf('class="arch-ev-detail"') !== -1 && detailedEvent.indexOf('class="detail"') === -1,
   "audit descriptions cannot inherit the global run-detail card spacing");
function filterButton(name) {
  return {pressed:null,getAttribute:function (attribute) { return attribute === "data-arch-filter" ? name : null; },
    setAttribute:function (attribute, value) { if (attribute === "aria-pressed") this.pressed = value; }};
}
var fakeTimeline = {innerHTML:""};
var allFilter = filterButton("all"), canaryFilter = filterButton("canary");
var historyHost = {
  querySelector:function (selector) { return selector === "#arch-timeline" ? fakeTimeline : null; },
  querySelectorAll:function () { return [allFilter,canaryFilter]; }
};
ok(archApplyHistoryFilter(historyHost,PAYLOAD.events,"canary") && count(fakeTimeline.innerHTML,'class="arch-ev"') === 1,
   "history filter updates only the event rows when DOM access is available");
ok(allFilter.pressed === "false" && canaryFilter.pressed === "true", "in-place filtering keeps filter state accessible");

/* ---- full render and delegated interaction ----------------------------- */
HOST = {innerHTML:"",className:""};
ARCH_VIEW = "runtime"; ARCH_STEP = "all"; ARCH_SELECTED = null; ARCH_QUERY = ""; ARCH_FILTER = "all"; ARCH_HISTORY_OPEN = false; archResetCamera();
renderArchitecture(PAYLOAD, HEALTH_DOWN);
ok(HOST.innerHTML.indexOf("Architecture control plane") !== -1 && HOST.innerHTML.indexOf("Happening now") !== -1,
   "full render leads with an operator architecture summary");
ok(HOST.innerHTML.indexOf('id="arch-map-svg"') !== -1, "full render includes the interactive graph");
ok(HOST.innerHTML.indexOf("Architecture audit trail") !== -1, "history remains available as secondary detail");
ok(typeof HOST.onclick === "function" && typeof HOST.onwheel === "function", "render binds click, pan, zoom, and wheel interaction");
function targetWith(attribute, value) {
  return {parentNode:HOST,getAttribute:function (name) { return name === attribute ? value : null; }};
}
HOST.onclick({target:targetWith("data-arch-node","mcp")});
ok(ARCH_SELECTED === "mcp" && HOST.innerHTML.indexOf("selected-out") !== -1, "clicking a node focuses its relationships");
HOST.oninput({target:{value:"cycle",getAttribute:function (name) { return name === "data-arch-search" ? "" : null; }}});
ok(ARCH_QUERY === "cycle" && ARCH_SELECTED === null && HOST.innerHTML.indexOf("search-match") !== -1,
   "typing a search immediately replaces node focus with search focus");
ARCH_QUERY = "";
HOST.onclick({target:targetWith("data-arch-view","structure")});
ok(ARCH_VIEW === "structure" && HOST.innerHTML.indexOf("Whole system") !== -1, "lens switch rerenders without a fetch");
HOST.onclick({target:targetWith("data-arch-step","verify")});
ok(ARCH_VIEW === "runtime" && ARCH_STEP === "verify" && HOST.innerHTML.indexOf("Run stage") !== -1, "stage selection drills into execution");
HOST.onclick({target:targetWith("data-arch-zoom","in")});
ok(ARCH_CAMERA.scale === 1.25, "zoom controls change the camera");
HOST.onclick({target:targetWith("data-arch-filter","canary")});
ok(ARCH_FILTER === "canary" && count(HOST.innerHTML,'class="arch-ev"') === 1, "history filtering remains interactive");
ok(ARCH_HISTORY_OPEN && HOST.innerHTML.indexOf('arch-history audit-drawer" open') !== -1,
   "filtering keeps the architecture audit drawer open");

/* ---- stats and escaping ------------------------------------------------ */
var stats = archStatsHtml(CURRENT, cleanPosture);
ok(count(stats,'class="delta ') === 6, "all headline posture facts render");
ok(archStatsHtml({stats:{modules:3340}},cleanPosture).indexOf("3,340") !== -1, "large values are thousands-separated");
ok(archStatsHtml({stats:{}},cleanPosture).indexOf("-") !== -1, "missing stats show a dash, not NaN");
ok(archEscape('<script>x</script>') === "&lt;script&gt;x&lt;/script&gt;", "markup is escaped");
ok(archEscape(null) === "", "null escapes to empty");
var hostileCurrent = clone(CURRENT);
hostileCurrent.nodes.push({id:'<img src=x onerror=1>',kind:"module",layer:"play",path:'a"b',loc:1});
hostileCurrent.edges.push({source:'<img src=x onerror=1>',target:"cycle"});
var hostileModel = archGraphModel(hostileCurrent,"structure","all",null,"");
ok(archGraphHtml(hostileModel).indexOf("<img") === -1, "hostile component names cannot inject SVG markup");
ok(archEventHtml({ts:"x",kind:"version",title:"<b>no</b>",added:["<img>"]}).indexOf("<img>") === -1, "event titles and diffs cannot inject markup");

/* ---- malformed data must never blank the dashboard --------------------- */
var threw = null;
try {
  archLayersHtml({}); archLayersHtml({layers:null,nodes:null,tools:null});
  archGraphIndex({},"runtime","all"); archGraphModel({},"runtime","all",null,"");
  archGraphHtml(archGraphModel({},"structure","all",null,""));
  archDetailHtml({},"x"); archEventsHtml(null,"all"); archStatsHtml({}); archAgentHealth({}); archEventHtml({});
  renderArchitecture(null,null); renderArchitecture({error:"boom"},null); renderArchitecture(PAYLOAD,HEALTH_DOWN);
} catch (error) { threw = error.message || String(error); }
ok(threw === null, "malformed payloads degrade without throwing", threw || "");

out.push("architecture: " + (fails ? fails + " FAIL" : "PASS") + " " + out.length + " checks");
out.push(fails ? "FAIL" : "PASS");
out.join("\n");
