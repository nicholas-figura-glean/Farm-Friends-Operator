/* MCP switchboard tests.
 *
 * The switchboard is the only animated view in the dashboard, which makes it the
 * easiest place to accidentally ship a lie: a loop that keeps flying packets
 * when the run made no calls, a completed landing for a call still in flight, a
 * synthetic "typical" latency, or a burst that quietly drops the interesting
 * calls when it gets thinned. Every check here exists to make one of those
 * impossible.
 *
 * Usage: osascript -l JavaScript dashboard/test_mcp_wire.js
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
eval(slurp("dashboard/mcp_wire.js"));
var W = globalThisRef.MCPWire || MCPWire;
ok(!!W, "engine exposes MCPWire");
ok(!!(globalThisRef.MCPWirePanel || MCPWirePanel), "panel is exposed for the page");

var TOPO = {
  fingerprint: "test",
  steps: [
    { name: "collect", order: 0, functions: 4, tools: ["collect_produce"], modules: ["cycle"] },
    { name: "feed", order: 1, functions: 3, tools: ["feed_animals"], modules: ["cycle"] },
    { name: "board", order: 2, functions: 2, tools: ["leaderboard"], modules: ["cycle"] }
  ],
  nodes: [
    { id: "step:collect", kind: "step", label: "collect" },
    { id: "step:feed", kind: "step", label: "feed" },
    { id: "step:board", kind: "step", label: "board" },
    { id: "tool:collect_produce", kind: "tool", label: "collect_produce", steps: ["collect"] },
    { id: "tool:feed_animals", kind: "tool", label: "feed_animals", steps: ["feed"] },
    { id: "tool:leaderboard", kind: "tool", label: "leaderboard", steps: ["board"] }
  ],
  edges: []
};

var PIPELINE = {
  run: 412, status: "running", active: "feed",
  started_ts: "2026-08-24T10:00:00Z", updated_ts: "2026-08-24T10:00:20Z",
  steps: [
    { name: "collect", label: "Collect produce", status: "done", seconds: 6,
      started_ts: "2026-08-24T10:00:00Z", ended_ts: "2026-08-24T10:00:06Z" },
    { name: "feed", label: "Feed herd", status: "active", seconds: null,
      started_ts: "2026-08-24T10:00:06Z", ended_ts: null },
    { name: "board", label: "Read leaderboard", status: "pending", seconds: null,
      started_ts: null, ended_ts: null }
  ]
};

var TRACE = {
  coverage: "full",
  calls: [
    { id: "c1", tool: "collect_produce", step: "collect", started_ts: "2026-08-24T10:00:01.000Z",
      ended_ts: "2026-08-24T10:00:05.000Z", duration_ms: 4000, status: "ok",
      arguments: { animal_id: "all" }, result: "5000 units", source: "boundary" },
    { id: "c2", tool: "collect_produce", step: "collect", started_ts: "2026-08-24T10:00:01.500Z",
      ended_ts: "2026-08-24T10:00:01.520Z", duration_ms: 20, status: "ok",
      arguments: { animal_id: "all" }, result: "0 units", source: "boundary" },
    { id: "c3", tool: "feed_animals", step: "feed", started_ts: "2026-08-24T10:00:07.000Z",
      ended_ts: "2026-08-24T10:00:09.000Z", duration_ms: 2000, status: "error",
      arguments: { animal_id: "all" }, result: null, error: "transport reset", source: "boundary" },
    { id: "c4", tool: "feed_animals", step: "feed", started_ts: "2026-08-24T10:00:18.000Z",
      ended_ts: null, duration_ms: null, status: "active",
      arguments: { animal_id: "all" }, result: null, source: "boundary" }
  ]
};
var NOW = new Date("2026-08-24T10:00:20Z").getTime();

/* ---- measured model ---------------------------------------------------- */
var model = W.derive(TOPO, PIPELINE, TRACE, NOW, { speed: 4 });
ok(model.calls.length === 4, "every recorded call survives normalisation");
ok(model.stats.inFlight === 1, "an unfinished call is counted as in flight");
ok(model.stats.errors === 1, "errors are counted, not smoothed away");
ok(near(model.calls[3].duration, 2), "in-flight duration grows from its measured start",
   String(model.calls[3].duration));
ok(model.lanes.length === 3, "one lane per reachable server tool");
ok(model.lanes[0].name === "collect_produce" || model.lanes[0].name === "feed_animals",
   "lanes are ordered by observed traffic", model.lanes[0].name);
var silent = model.lanes.filter(function (lane) { return lane.silent; });
ok(silent.length === 1 && silent[0].name === "leaderboard",
   "a reachable but uncalled tool stays visible as silent", silent.map(function (l) { return l.name; }).join(","));
ok(model.stats.tools === 2 && model.stats.silent === 1, "used/silent tool split is explicit");
ok(near(model.stats.median, 2, 0.001), "median round trip is a real percentile of measured calls",
   String(model.stats.median));
ok(near(model.stats.boundarySeconds, 8.02, 0.001), "boundary time sums measured durations including active work",
   String(model.stats.boundarySeconds));
ok(near(model.stats.wallSeconds, 20, 0.001), "wall clock spans run start to now", String(model.stats.wallSeconds));
ok(model.stats.peak === 2, "peak concurrency comes from overlapping timestamps", String(model.stats.peak));
ok(model.hall.slowest.id === "c1" && model.hall.fastest.id === "c2",
   "superlatives pick real calls, and never an unfinished one");
ok(model.hall.chattiest.name === "collect" || model.hall.chattiest.name === "feed",
   "chattiest step is measured", model.hall.chattiest && model.hall.chattiest.name);
ok(W.kind("adopt_animal") === "write" && W.kind("prestige") === "write" &&
   W.kind("resolve_crisis") === "write" && W.kind("plant") === "write" &&
   W.kind("visit_farm") === "read" && W.kind("list_farm") === "read",
   "current mutating and read-only tools are classified");
ok(model.steps[0].count === 2 && model.steps[2].count === 0,
   "calls are attributed to the step that issued them");

/* replay arithmetic: measured durations, explicit compression, no invented time */
ok(near(model.loop, 5, 0.001), "replay loop is the run span divided by the chosen speed", String(model.loop));
ok(near(model.effectiveSpeed, 4, 0.001), "effective speed matches the chip when it fits", String(model.effectiveSpeed));
var first = model.packets[0];
ok(near(first.launchAt, 0.25, 0.001), "a packet launches at its real offset, scaled", String(first.launchAt));
ok(near(first.flight, 1, 0.001), "flight time is the measured duration, scaled", String(first.flight));
var padded = model.packets.filter(function (p) { return p.padded; });
ok(padded.length === 1 && padded[0].id === "c2" && near(padded[0].flight, W.MIN_FLIGHT, 0.001),
   "a sub-frame call is padded to a visible minimum and flagged as padded");
var flying = model.packets.filter(function (p) { return p.active; });
ok(flying.length === 1 && flying[0].progress < 1,
   "an in-flight call never gets a completed landing", flying.length ? String(flying[0].progress) : "none");

var slow = W.derive(TOPO, PIPELINE, TRACE, NOW, { speed: 1 });
ok(slow.loop > model.loop && slow.loop <= 26, "a slower chip lengthens the loop within the cap", String(slow.loop));
ok(model.windowed === false && near(model.window.span, 20, 0.001),
   "a short run is replayed whole, not sampled", String(model.window.span));

/* A long run replays its densest window, and says which one */
var longPipeline = {
  run: 700, status: "ok", started_ts: "2026-08-24T10:00:00Z", finished_ts: "2026-08-24T10:05:00Z",
  updated_ts: "2026-08-24T10:05:00Z", steps: []
};
var longTrace = { coverage: "full", calls: [] };
for (var q = 0; q < 12; q++) {                       // sparse traffic early
  longTrace.calls.push({
    id: "early" + q, tool: "list_farm", step: null,
    started_ts: new Date(Date.parse("2026-08-24T10:00:05Z") + q * 8000).toISOString(),
    ended_ts: new Date(Date.parse("2026-08-24T10:00:05Z") + q * 8000 + 900).toISOString(),
    duration_ms: 900, status: "ok", arguments: {}, result: "ok", source: "boundary"
  });
}
for (var b2 = 0; b2 < 300; b2++) {                   // a burst at +200s
  longTrace.calls.push({
    id: "burst" + b2, tool: "adopt_animal", step: null,
    started_ts: new Date(Date.parse("2026-08-24T10:03:20Z") + b2 * 60).toISOString(),
    ended_ts: new Date(Date.parse("2026-08-24T10:03:20Z") + b2 * 60 + 3000).toISOString(),
    duration_ms: 3000, status: "ok", arguments: { kind: "chicken" }, result: "joined", source: "boundary"
  });
}
var longAt = new Date("2026-08-24T10:05:30Z").getTime();
var longRun = W.derive(TOPO, longPipeline, longTrace, longAt, { speed: 1 });
ok(longRun.windowed === true && near(longRun.window.span, W.BURST_WINDOW, 0.001),
   "a long run replays a bounded window", longRun.window.span + "s");
ok(longRun.window.start > 190 && longRun.window.start < 205,
   "the window lands on the measured burst, not on the run start", String(longRun.window.start));
ok(longRun.stats.calls === 312 && longRun.window.calls < 312,
   "whole-run stats and windowed traffic are kept separate",
   longRun.stats.calls + "/" + longRun.window.calls);
ok(longRun.loop <= 26 && longRun.effectiveSpeed >= 1,
   "the window is compressed into the loop and reports its real speed",
   longRun.loop + " / " + longRun.effectiveSpeed);
var longHtml = W.render(TOPO, longPipeline, longTrace, { speed: 1 }, longAt).html;
ok(longHtml.indexOf("busiest") >= 0 && longHtml.indexOf("in this window") >= 0,
   "the page states that it is replaying a slice and how big the run is");
ok(longHtml.indexOf("mw-flow-window") >= 0, "the replayed slice is marked on the run's flow chart");
var wholeHtml = W.render(TOPO, longPipeline, longTrace, { speed: 1, window: "run" }, longAt).html;
ok(wholeHtml.indexOf("mw-flow-window") < 0 && wholeHtml.indexOf("all ") >= 0,
   "the whole run can still be replayed on request");
var sparseLane = longRun.packets.filter(function (p) { return p.tool === "list_farm"; });
ok(longRun.packets.filter(function (p) { return p.tool === "adopt_animal"; }).length > 20,
   "the busy lane keeps its density");
ok(sparseLane.length === 0 || longRun.window.start > 100,
   "packets outside the replayed window are not drawn");

/* ---- concurrency series ------------------------------------------------- */
var flow = W.concurrency(model.calls, model.stats.wallSeconds);
ok(flow.peak === 2 && flow.samples.length === 96, "flow series is downsampled to a fixed width");
ok(flow.samples.every(function (sample) { return sample.open >= 0; }), "flow never goes negative");

/* ---- rendered HTML ------------------------------------------------------ */
var state = { speed: 4, paused: false, focus: null };
var html = W.render(TOPO, PIPELINE, TRACE, state, NOW).html;
ok(count(html, "mw-packet ") === 4, "one DOM packet per observed call", String(count(html, "mw-packet ")));
ok(count(html, "mw-lane-label") === 3, "one lane row per tool", String(count(html, "mw-lane-label")));
ok(html.indexOf("--t0:0.250s") >= 0 && html.indexOf("--dur:1.000s") >= 0,
   "packet geometry carries the measured start and duration");
ok(html.indexOf("--x:") >= 0, "packets also carry a static position for reduced motion");
ok(html.indexOf("mw-packet error") >= 0, "a failed call is drawn as a failure");
ok(html.indexOf("flying") >= 0, "an in-flight call is drawn as in flight");
ok(html.indexOf("not called this run") >= 0, "silent lanes say so on the wire");
ok(html.indexOf("Collect produce") >= 0 && html.indexOf("collect_produce") >= 0,
   "steps and tools are named, not abbreviated to colour");
ok(html.indexOf("mutates") >= 0 && html.indexOf("reads") >= 0, "lane rows disclose whether a tool mutates");
ok(html.indexOf("every MCP call") >= 0, "coverage is stated");
ok(html.indexOf("measured duration") >= 0, "legend explains that flight time is measured");
ok(html.indexOf("peak 2 concurrent") >= 0, "the concurrency chart labels its peak");
ok(html.indexOf("canvas") < 0, "the switchboard needs no canvas");
ok(html.indexOf("<main>") < 0 && html.indexOf('class="full"') < 0,
   "nothing inherits the host page's layout classes");
ok(html.indexOf("Slowest round trip") >= 0 && html.indexOf("4.00s") >= 0,
   "the hall of fame quotes a real measurement");

/* focus filters lanes without hiding that it is filtering */
state.focus = "tool:feed_animals";
var focused = W.render(TOPO, PIPELINE, TRACE, state, NOW).html;
ok(count(focused, "mw-lane-label") === 1 && focused.indexOf("feed_animals") >= 0,
   "focusing a tool shows only that lane");
ok(focused.indexOf("data-wire-focus=\"\"") >= 0, "a focused view offers a way back");
state.focus = "step:collect";
focused = W.render(TOPO, PIPELINE, TRACE, state, NOW).html;
ok(count(focused, "mw-lane-label") === 1 && focused.indexOf("collect_produce") >= 0,
   "focusing a step shows the lanes it actually used");
state.focus = null;

/* pause is a real freeze, not a re-render */
state.paused = true;
var frozen = W.render(TOPO, PIPELINE, TRACE, state, NOW).html;
ok(frozen.indexOf("mw-stage paused") >= 0 && frozen.indexOf("Resume") >= 0, "freeze is expressed on the stage");
state.paused = false;

/* ---- thinning keeps the interesting calls ------------------------------ */
var many = { coverage: "full", calls: [] };
for (var i = 0; i < 480; i++) {
  many.calls.push({
    id: "m" + i, tool: "collect_produce", step: "collect",
    started_ts: new Date(Date.parse("2026-08-24T10:00:01Z") + i * 30).toISOString(),
    ended_ts: new Date(Date.parse("2026-08-24T10:00:01Z") + i * 30 + 400).toISOString(),
    duration_ms: 400, status: i === 300 ? "error" : "ok", arguments: {}, result: "ok", source: "boundary"
  });
}
var heavy = W.derive(TOPO, PIPELINE, many, NOW, { speed: 4 });
ok(heavy.stats.calls === 480, "the stat strip counts every call, not just drawn ones");
ok(heavy.packets.length <= W.PACKET_CAP, "drawn packets are capped", String(heavy.packets.length));
ok(heavy.thinned === true, "thinning is recorded so it can be disclosed");
ok(heavy.packets.filter(function (p) { return p.status === "error"; }).length === 1,
   "thinning never drops an error");

/* per-lane quotas: a one-call lane must not be starved by a 400-call lane */
var lopsided = { coverage: "full", calls: many.calls.slice() };
lopsided.calls.push({
  id: "rare", tool: "leaderboard", step: "board", started_ts: "2026-08-24T10:00:05.000Z",
  ended_ts: "2026-08-24T10:00:05.400Z", duration_ms: 400, status: "ok",
  arguments: {}, result: "rank 1", source: "boundary"
});
var mixed = W.derive(TOPO, PIPELINE, lopsided, NOW, { speed: 4 });
ok(mixed.packets.filter(function (p) { return p.tool === "leaderboard"; }).length === 1,
   "a lane with a single call still gets a packet after thinning");
var heavyHtml = W.render(TOPO, PIPELINE, many, { speed: 4 }, NOW).html;
ok(heavyHtml.indexOf("of 480") >= 0, "the page states how many calls it is drawing");

/* ---- degraded inputs ---------------------------------------------------- */
var finished = {
  run: 413, status: "ok",
  started_ts: "2026-08-24T10:00:00Z", finished_ts: "2026-08-24T10:00:20Z", updated_ts: "2026-08-24T10:00:20Z",
  steps: PIPELINE.steps.slice(0, 2)
};
var later = new Date("2026-08-24T10:09:00Z").getTime();   // nine minutes after the run ended
var closed = W.derive(TOPO, finished, TRACE, later, { speed: 4 });
ok(closed.stats.inFlight === 0 && closed.stats.unterminated === 1,
   "an unpaired start row on a finished run is unterminated, not in flight",
   closed.stats.inFlight + "/" + closed.stats.unterminated);
ok(near(closed.stats.boundarySeconds, 6.02, 0.001),
   "an unterminated span contributes no invented duration", String(closed.stats.boundarySeconds));
ok(closed.stats.peak === 2, "an unterminated span is not held open across the run", String(closed.stats.peak));
var closedHtml = W.render(TOPO, finished, TRACE, { speed: 4 }, later).html;
ok(closedHtml.indexOf("stranded") >= 0 && closedHtml.indexOf("no end row recorded") >= 0,
   "the missing end row is disclosed on the wire");

var legacy = W.derive(TOPO, PIPELINE, {
  coverage: "mutations_only",
  activity: [{ key: "x", tool: "feed_animals", step: "feed", ts: "2026-08-24T10:00:07Z" }]
}, NOW, {});
ok(legacy.calls.length === 1 && legacy.calls[0].event === true,
   "legacy activity rows are drawn as events, not as timed round trips");
ok(legacy.calls[0].duration === null, "an event has no invented duration");
var legacyHtml = W.render(TOPO, PIPELINE, {
  coverage: "mutations_only", activity: [{ key: "x", tool: "feed_animals", step: "feed", ts: "2026-08-24T10:00:07Z" }]
}, {}, NOW).html;
ok(legacyHtml.indexOf("mutations only") >= 0, "partial coverage is disclosed");

var emptyHtml = W.render(TOPO, { run: 9, status: "ok", steps: [] }, { coverage: "unavailable", calls: [] }, {}, NOW).html;
ok(emptyHtml.indexOf("No MCP boundary telemetry") >= 0 && emptyHtml.indexOf("mw-packet") < 0,
   "with no telemetry the switchboard animates nothing at all");

var threw = null;
try {
  W.derive({}, {}, {}, NOW, {});
  W.render({}, {}, {}, {}, NOW);
  W.render(TOPO, PIPELINE, { coverage: "full", calls: [{ id: null, tool: null }] }, { speed: 99 }, NOW);
} catch (error) { threw = error.message || String(error); }
ok(threw === null, "malformed payloads degrade without throwing", threw || "");

out.push("mcp-switchboard: " + (fails ? fails + " FAIL" : "PASS") + " " + out.length + " checks");
out.push(fails ? "FAIL" : "PASS");
out.join("\n");
