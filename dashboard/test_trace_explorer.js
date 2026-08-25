/* Execution trace explorer tests.
 *
 * Runs the real DOM-free model and HTML renderer in JavaScriptCore. The contract
 * is that the view cannot lie: only measured steps/tool calls get timeline spans,
 * static functions are explicitly labelled reachability, every call remains
 * inspectable, and the matrix exactly matches the source-derived step/tool graph.
 *
 * Usage: osascript -l JavaScript dashboard/test_trace_explorer.js
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
function near(a, b, tolerance) { return Math.abs(a - b) <= (tolerance == null ? 1e-6 : tolerance); }

var globalThisRef = this;
eval(slurp("dashboard/trace_explorer.js"));
var T = globalThisRef.TraceExplorer || TraceExplorer;
ok(!!T, "engine exposes TraceExplorer");

var TOPO = {
  fingerprint: "test",
  stats: { functions: 6, tools: 2, edges: 9, modules: 3 },
  modules: [{ name: "cycle", nodes: 2 }, { name: "rules", nodes: 1 }, { name: "mcp", nodes: 3 }],
  steps: [
    { name: "collect", order: 0, functions: 4, tools: ["collect_produce"], modules: ["cycle", "mcp"] },
    { name: "harvest", order: 1, functions: 4, tools: ["harvest"], modules: ["cycle", "mcp"] },
    { name: "plan", order: 2, functions: 1, tools: [], modules: ["rules"] }
  ],
  nodes: [
    { id: "step:collect", kind: "step", label: "collect", module: "cycle", order: 0, steps: [], depth: 0 },
    { id: "step:harvest", kind: "step", label: "harvest", module: "cycle", order: 1, steps: [], depth: 0 },
    { id: "step:plan", kind: "step", label: "plan", module: "cycle", order: 2, steps: [], depth: 0 },
    { id: "cycle:Cycle.collect", kind: "func", label: "Cycle.collect", module: "cycle",
      qual: "cycle:Cycle.collect", line: 180, loc: 34, steps: ["collect"], depth: 1, doc: "Collect once." },
    { id: "cycle:Cycle.harvest_if_needed", kind: "func", label: "Cycle.harvest_if_needed", module: "cycle",
      qual: "cycle:Cycle.harvest_if_needed", line: 240, loc: 12, steps: ["harvest"], depth: 1 },
    { id: "mcp:Client.call", kind: "func", label: "Client.call", module: "mcp",
      qual: "mcp:Client.call", line: 153, loc: 13, steps: ["collect", "harvest"], depth: 2 },
    { id: "mcp:Client.rpc", kind: "func", label: "Client.rpc", module: "mcp",
      qual: "mcp:Client.rpc", line: 143, loc: 9, steps: ["collect", "harvest"], depth: 3 },
    { id: "mcp:Client._post", kind: "func", label: "Client._post", module: "mcp",
      qual: "mcp:Client._post", line: 112, loc: 30, steps: ["collect", "harvest"], depth: 4 },
    { id: "rules:expansion_plan", kind: "func", label: "expansion_plan", module: "rules",
      qual: "rules:expansion_plan", line: 300, loc: 88, steps: ["plan"], depth: 1 },
    { id: "tool:collect_produce", kind: "tool", label: "collect_produce", module: "mcp",
      qual: "tool:collect_produce", steps: ["collect"], depth: 3 },
    { id: "tool:harvest", kind: "tool", label: "harvest", module: "mcp",
      qual: "tool:harvest", steps: ["harvest"], depth: 3 }
  ],
  edges: [
    { source: "step:collect", target: "cycle:Cycle.collect", kind: "step" },
    { source: "step:harvest", target: "cycle:Cycle.harvest_if_needed", kind: "step" },
    { source: "step:plan", target: "rules:expansion_plan", kind: "step" },
    { source: "cycle:Cycle.collect", target: "mcp:Client.call", kind: "call" },
    { source: "cycle:Cycle.harvest_if_needed", target: "mcp:Client.call", kind: "call" },
    { source: "mcp:Client.call", target: "mcp:Client.rpc", kind: "call" },
    { source: "mcp:Client.rpc", target: "mcp:Client._post", kind: "call" },
    { source: "cycle:Cycle.collect", target: "tool:collect_produce", kind: "tool" },
    { source: "cycle:Cycle.harvest_if_needed", target: "tool:harvest", kind: "tool" }
  ]
};

var PIPELINE = {
  run: 27, status: "running", active: "harvest",
  started_ts: "2026-08-21T17:16:35Z", updated_ts: "2026-08-21T17:17:04Z",
  baseline: { collect: 25, harvest: 4, plan: 2 },
  steps: [
    { name: "collect", label: "Collect produce", hint: "collect all", status: "done", seconds: 25,
      started_ts: "2026-08-21T17:16:35Z", ended_ts: "2026-08-21T17:17:00Z", detail: { units: 10 } },
    { name: "harvest", label: "Harvest crops", hint: "ready crops", status: "active", seconds: null,
      started_ts: "2026-08-21T17:17:00Z", ended_ts: null, detail: {} },
    { name: "plan", label: "Plan expansion", hint: "arithmetic", status: "pending", seconds: null,
      started_ts: null, ended_ts: null, detail: {} }
  ]
};

var TRACE = {
  coverage: "full",
  calls: [
    { id: "a", tool: "collect_produce", step: "collect", started_ts: "2026-08-21T17:16:40.000Z",
      ended_ts: "2026-08-21T17:16:40.800Z", duration_ms: 800, status: "ok",
      arguments: { pass: 1 }, result: "10 units", source: "boundary" },
    { id: "b", tool: "collect_produce", step: "collect", started_ts: "2026-08-21T17:16:45.000Z",
      ended_ts: "2026-08-21T17:16:45.500Z", duration_ms: 500, status: "ok",
      arguments: { pass: 2 }, result: "0 units", source: "boundary" },
    { id: "c", tool: "harvest", step: null, started_ts: "2026-08-21T17:17:02.000Z",
      ended_ts: null, duration_ms: null, status: "active", arguments: { crop: "corn" }, source: "boundary" }
  ]
};
var NOW = new Date("2026-08-21T17:17:05Z").getTime();

/* topology and routing */
var index = T.prepare(TOPO);
ok(index.tools.length === 2, "all MCP tools become matrix columns");
ok(index.functionsByStep.collect.length === 4, "transport chain belongs to collect");
ok(index.functionsByStep.harvest.length === 4, "transport chain belongs to harvest");
ok(index.functionsByStep.plan.length === 1, "local planning function belongs to plan");
var path = T.route(index, "collect", "collect_produce");
ok(path.length === 6, "static path retains step, function, full transport and tool", path.map(function (n) { return n.id; }).join(" -> "));
ok(path[2].id === "mcp:Client.call" && path[3].id === "mcp:Client.rpc" && path[4].id === "mcp:Client._post",
   "inspector route prefers Client.call → rpc → _post over the semantic shortcut");
ok(path[0].id === "step:collect" && path[path.length - 1].id === "tool:collect_produce",
   "call path has the correct endpoints");
ok(T.route(index, "plan", "harvest").length === 0, "no tool path is invented for local-only planning");

/* measured model */
var model = T.derive(TOPO, PIPELINE, TRACE, NOW);
ok(model.steps.length === 3, "every pipeline step becomes a span row");
ok(model.calls.length === 3, "every observed call survives normalization");
ok(model.groups.length === 2, "repeated calls compress into one tool lane per parent step");
ok(model.groupsByStep.collect[0].instances.length === 2, "compressed lane retains every call instance");
ok(model.groupsByStep.harvest[0].instances.length === 1, "timestamp assigns an unparented call to its measured step");
ok(model.groupsByStep.harvest[0].active === 1, "an in-flight MCP call remains active");
ok(near(model.groupsByStep.harvest[0].instances[0].duration, 3),
   "in-flight MCP span grows from its measured start", String(model.groupsByStep.harvest[0].instances[0].duration));
ok(near(model.boundarySeconds, 4.3), "boundary duration includes elapsed active work", String(model.boundarySeconds));
ok(model.done === 1 && model.active.name === "harvest", "run progress is derived from measured statuses");
ok(near(model.steps[0].duration, 25), "completed step keeps its recorded duration");
ok(near(model.steps[1].duration, 5), "active step duration advances from wall clock", String(model.steps[1].duration));
ok(model.steps[2].duration === null, "pending step is not given an invented duration");
ok(model.horizon >= 31, "running timeline includes the expected/observed horizon", String(model.horizon));
ok(model.coverage === "full", "coverage label comes from the monitor");

/* backend run boundaries and effective status */
var closedPipeline = JSON.parse(JSON.stringify(PIPELINE));
closedPipeline.status = "running"; // raw progress can remain stale after a killed process
closedPipeline.active = "harvest";
closedPipeline.finished_ts = null;
var closedTrace = {
  coverage: "full",
  run_started_ts: "2026-08-21T17:16:35Z",
  run_finished_ts: "2026-08-21T17:16:50Z",
  effective_status: "stalled",
  calls: [
    { id: "inside", tool: "collect_produce", step: "collect", started_ts: "2026-08-21T17:16:40Z",
      ended_ts: "2026-08-21T17:16:41Z", duration_ms: 1000, status: "ok" },
    { id: "stranded", tool: "collect_produce", step: "collect", started_ts: "2026-08-21T17:16:45Z",
      ended_ts: null, duration_ms: null, status: "active" },
    { id: "next-run", tool: "harvest", step: "harvest", started_ts: "2026-08-21T17:16:55Z",
      ended_ts: "2026-08-21T17:16:56Z", duration_ms: 1000, status: "ok" }
  ]
};
var closed = T.derive(TOPO, closedPipeline, closedTrace,
  new Date("2026-08-21T18:16:35Z").getTime());
ok(closed.status === "stalled" && !closed.running && closed.active === null,
   "backend effective status closes a stale raw running pipeline");
ok(near(closed.elapsed, 15) && near(closed.horizon, 15),
   "completed trace freezes at backend run boundaries", closed.elapsed + "/" + closed.horizon);
ok(closed.calls.length === 2 && closed.calls[0].id === "inside" && closed.calls[1].id === "stranded",
   "post-finish calls are not attached to the completed run");
ok(!closed.calls[1].active && closed.calls[1].unterminated && closed.calls[1].duration === null,
   "an unpaired call in a closed run does not grow after finish");
ok(near(closed.boundarySeconds, 1),
   "closed boundary totals contain no invented or next-run duration", String(closed.boundarySeconds));

/* trace HTML: truthful density */
var state = { view: "trace", selected: null };
var rendered = T.render(TOPO, PIPELINE, TRACE, state, NOW);
var html = rendered.html;
ok(count(html, "te-row te-step") === 3, "trace renders exactly one row per step");
ok(count(html, "te-row te-call") === 2, "trace renders grouped tool lanes, not one noisy row per call");
ok(count(html, "te-call-span ") === 3, "every observed call is still a visible tick/span");
ok(html.indexOf("Collect produce") >= 0 && html.indexOf("collect_produce ×2") >= 0,
   "step and repeated tool names are legible");
ok(html.indexOf("all MCP calls") >= 0, "full boundary coverage is explicit");
ok(html.indexOf('class="te-coverage coverage-full"') >= 0 && html.indexOf('class="te-coverage full"') < 0,
   "coverage state cannot collide with the host dashboard full-width class");
ok(html.indexOf("Static Python path") >= 0 && html.indexOf("reachability, not measured time") >= 0,
   "functions are never presented as measured spans");
ok(html.indexOf("three-dimensional") < 0 && html.indexOf("canvas") < 0,
   "the replacement has no 3D or canvas dependency");
ok(html.indexOf('class="te-main"') >= 0 && html.indexOf("<main>") < 0,
   "explorer content cannot inherit the host page main sizing rules");
ok(html.indexOf("--left:") >= 0 && html.indexOf("--width:") >= 0,
   "measured spans carry timeline geometry");

/* matrix: the whole-system map */
state.view = "matrix";
html = T.render(TOPO, PIPELINE, TRACE, state, NOW).html;
ok(html.indexOf("MCP boundary") >= 0 && html.indexOf("Farm Friends server") >= 0,
   "matrix names the process boundary");
ok(count(html, "te-tool-head") === 2, "matrix has one column per tool");
ok(count(html, 'class="te-col-tool"') === 2 && html.indexOf("<colgroup>") >= 0,
   "matrix declares one fixed-layout column per tool");
ok(T.toolLabel("tools/list") === "tools/<wbr>list" && T.toolLabel("collect_produce") === "collect produce",
   "tool labels expose safe wrap points instead of breaking inside words");
ok(count(html, "reachable") >= 4, "source-derived reachable cells are present");
ok(html.indexOf("Plan expansion → harvest: no path") < 0,
   "impossible paths are blank rather than labelled or connected");
ok(html.indexOf("<b>2</b>") >= 0, "observed call count overlays a reachable cell");
ok(html.indexOf("Blank means the step has no path") >= 0, "matrix legend explains absence");

/* inspector */
state.view = "trace";
state.selected = "call:collect|collect_produce";
html = T.render(TOPO, PIPELINE, TRACE, state, NOW).html;
ok(html.indexOf("MCP BOUNDARY") >= 0, "tool selection opens a boundary inspector");
ok(html.indexOf("#1 +5.00s") >= 0 && html.indexOf("#2 +10.0s") >= 0,
   "every compressed call instance remains inspectable");
ok(html.indexOf("&quot;pass&quot;:1") >= 0 && html.indexOf("10 units") >= 0,
   "tool arguments and result are visible");
ok(html.indexOf("Cycle.collect") >= 0 && html.indexOf("Client.call") >= 0,
   "inspector explains the source-to-server path");
state.selected = "node:" + encodeURIComponent("cycle:Cycle.collect");
html = T.render(TOPO, PIPELINE, TRACE, state, NOW).html;
ok(html.indexOf("STATIC CODE") >= 0 && html.indexOf("cycle.py:180") >= 0,
   "function inspector names source and line");
ok(html.indexOf("not presented as a measured runtime span") >= 0,
   "function inspector repeats the static/runtime distinction");

/* fallback telemetry and malformed input */
var fallback = T.derive(TOPO, PIPELINE, { coverage: "mutations_only", activity: [
  { key: "x", tool: "harvest", step: "harvest", ts: "2026-08-21T17:17:03Z" }
]}, NOW);
ok(fallback.calls.length === 1 && fallback.calls[0].source === "activity",
   "legacy activity remains visible before boundary instrumentation is deployed");
html = T.render(TOPO, PIPELINE, { coverage: "mutations_only", activity: [] }, { view: "trace" }, NOW).html;
ok(html.indexOf("mutation calls only") >= 0 && html.indexOf("deploy the current farm/ release") >= 0,
   "partial coverage is honest and actionable");
var truncatedTrace = JSON.parse(JSON.stringify(TRACE));
truncatedTrace.metadata = { calls_truncated: true, calls_returned: 3, calls_total: 19 };
var truncated = T.derive(TOPO, PIPELINE, truncatedTrace, NOW);
ok(truncated.coverage === "partial" && truncated.coverageMeta.truncated,
   "truncation metadata downgrades a declared full payload");
html = T.render(TOPO, PIPELINE, truncatedTrace, { view: "trace" }, NOW).html;
ok(html.indexOf("partial MCP call data") >= 0 && html.indexOf("3 of 19 calls returned") >= 0,
   "coverage label discloses truncated payload counts");
var partialTrace = JSON.parse(JSON.stringify(TRACE));
partialTrace.calls_partial = true;
html = T.render(TOPO, PIPELINE, partialTrace, { view: "trace" }, NOW).html;
ok(html.indexOf("partial MCP call data") >= 0 && html.indexOf("backend reports partial call telemetry") >= 0,
   "explicit partial metadata cannot be labelled as all calls");
var malformed = null;
try {
  var blank = T.derive({}, {}, {}, NOW);
  T.render({}, {}, {}, { view: "trace" }, NOW);
  T.route(blank.index, "x", "y");
} catch (error) { malformed = error.message || String(error); }
ok(malformed === null, "empty payload degrades without throwing", malformed || "");

out.push("trace-explorer: " + (fails ? fails + " FAIL" : "PASS") + " " + (out.length) + " checks");
out.push(fails ? "FAIL" : "PASS");
out.join("\n");
