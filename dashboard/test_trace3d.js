/* Trace3D engine tests - the layout, projection, growth and routing arithmetic.
 *
 * There is no node and no headless browser on this machine, so the engine is
 * written DOM-free precisely so it can be driven here:
 *
 *     osascript -l JavaScript dashboard/test_trace3d.js      (from the project root)
 *
 * What is worth asserting about a visualisation is not how it looks, it is that
 * the picture cannot lie: the same graph must lay out identically twice, tools
 * must end up outside functions, a step that has not run must not be lit, a
 * skipped step must look different from an unreached one, and a packet must
 * travel the route the code actually takes.
 */
ObjC.import('Foundation');
function slurp(path) {
  return $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null).js;
}

var out = [], fails = 0;
function ok(cond, label, detail) {
  out.push((cond ? "  ok   " : "  FAIL ") + label + (detail && !cond ? "  [" + detail + "]" : ""));
  if (!cond) fails++;
}
function near(a, b, tol) { return Math.abs(a - b) <= (tol == null ? 1e-6 : tol); }

var globalThisRef = this;
eval(slurp("dashboard/trace3d.js"));
var T = globalThisRef.Trace3D || Trace3D;
ok(!!T, "engine exposes Trace3D");

/* ---- a topology payload shaped exactly like farm/topology.py emits ---- */
var TOPO = {
  fingerprint: "test",
  stats: { functions: 4, tools: 2, edges: 6, modules: 3 },
  modules: [{ name: "cycle", nodes: 2 }, { name: "rules", nodes: 1 }, { name: "parse", nodes: 1 }],
  steps: [
    { name: "collect", order: 0, functions: 2, tools: ["collect_produce"], modules: ["cycle"] },
    { name: "harvest", order: 1, functions: 1, tools: ["harvest"], modules: ["cycle"] },
    { name: "plan", order: 2, functions: 2, tools: [], modules: ["rules"] }
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
    { id: "rules:expansion_plan", kind: "func", label: "expansion_plan", module: "rules",
      qual: "rules:expansion_plan", line: 300, loc: 88, steps: ["plan"], depth: 1 },
    { id: "tool:collect_produce", kind: "tool", label: "collect_produce", module: "mcp",
      qual: "tool:collect_produce", steps: ["collect"], depth: 2 },
    { id: "tool:harvest", kind: "tool", label: "harvest", module: "mcp",
      qual: "tool:harvest", steps: ["harvest"], depth: 2 }
  ],
  edges: [
    { source: "step:collect", target: "cycle:Cycle.collect", kind: "step" },
    { source: "step:harvest", target: "cycle:Cycle.harvest_if_needed", kind: "step" },
    { source: "step:plan", target: "rules:expansion_plan", kind: "step" },
    { source: "cycle:Cycle.collect", target: "mcp:Client.call", kind: "call" },
    { source: "cycle:Cycle.collect", target: "tool:collect_produce", kind: "tool" },
    { source: "cycle:Cycle.harvest_if_needed", target: "tool:harvest", kind: "tool" }
  ]
};

var LIVE = {
  status: "running", active: "harvest", baseline: { collect: 25.9 },
  steps: [
    { name: "collect", status: "done", seconds: 24.6, started_ts: "2026-08-21T17:16:35Z", ended_ts: "2026-08-21T17:17:00Z" },
    { name: "harvest", status: "active", seconds: null, started_ts: "2026-08-21T17:17:00Z", ended_ts: null },
    { name: "plan", status: "pending", seconds: null, started_ts: null, ended_ts: null }
  ]
};

/* ---- 1. determinism: the same graph must draw the same picture ---- */
var a = T.buildScene(TOPO), b = T.buildScene(TOPO);
T.settle(a, 60); T.settle(b, 60);
var drift = 0;
a.nodes.forEach(function (node, i) {
  drift += Math.abs(node.x - b.nodes[i].x) + Math.abs(node.y - b.nodes[i].y) + Math.abs(node.z - b.nodes[i].z);
});
ok(drift === 0, "layout is deterministic across builds", "drift=" + drift);
ok(T.hash32("step:collect") === T.hash32("step:collect"), "node seed hash is stable");
ok(T.hash32("a") !== T.hash32("b"), "node seed hash discriminates");

/* ---- 2. structure: the axes have to mean what the legend claims ---- */
var scene = T.buildScene(TOPO);
var steps = scene.steps;
ok(steps.length === 3, "every step becomes a spine node", "got " + steps.length);
ok(steps[0].y > steps[1].y && steps[1].y > steps[2].y,
   "execution order runs down the vertical axis");
ok(steps.every(function (s) { return s.fixed; }), "spine nodes are anchored, not free");

T.settle(scene, 300);
function planar(node) { return Math.sqrt(node.x * node.x + node.z * node.z); }
var toolRadius = scene.nodes.filter(function (n) { return n.kind === "tool"; }).map(planar);
var funcRadius = scene.nodes.filter(function (n) { return n.kind === "func"; }).map(planar);
var stepRadius = steps.map(planar);
ok(Math.min.apply(null, toolRadius) > Math.max.apply(null, stepRadius),
   "server tools settle outside the step spine",
   "tools>=" + Math.min.apply(null, toolRadius).toFixed(0) + " steps<=" + Math.max.apply(null, stepRadius).toFixed(0));
ok(Math.min.apply(null, toolRadius) > Math.min.apply(null, funcRadius),
   "the transport boundary is the outer shell");
var settled = scene.nodes.every(function (n) {
  return isFinite(n.x) && isFinite(n.y) && isFinite(n.z) && Math.abs(n.x) < 5000 && Math.abs(n.y) < 5000;
});
ok(settled, "layout stays finite and bounded after 300 iterations");

/* Node size must encode lines of code, or the picture is decoration. */
var big = scene.byId["rules:expansion_plan"], small = scene.byId["cycle:Cycle.harvest_if_needed"];
ok(T.nodeRadius(big) > T.nodeRadius(small), "node radius grows with lines of code");
ok(T.nodeRadius(scene.byId["step:collect"]) > T.nodeRadius(small), "steps are the largest nodes");

/* ---- 3. projection: a camera that cannot produce garbage ---- */
var cam = T.makeCamera({ dist: 600, yaw: 0, pitch: 0 });
var origin = T.project({ x: 0, y: 0, z: 0 }, cam, 800, 400);
ok(near(origin.x, 400, 0.001) && near(origin.y, 200, 0.001), "the target projects to the viewport centre");
var right = T.project({ x: 100, y: 0, z: 0 }, cam, 800, 400);
ok(right.x > origin.x, "+x is to the right at zero yaw");
var up = T.project({ x: 0, y: 100, z: 0 }, cam, 800, 400);
ok(up.y < origin.y, "+y is up the screen, not down");
var far = T.project({ x: 100, y: 0, z: -400 }, cam, 800, 400);
var nearPoint = T.project({ x: 100, y: 0, z: 400 }, cam, 800, 400);
ok(nearPoint.scale > far.scale, "nearer geometry (+z, toward the eye) projects larger",
   "near=" + nearPoint.scale.toFixed(3) + " far=" + far.scale.toFixed(3));
ok(far.depth > 0 && nearPoint.depth > 0, "depth stays positive");
var behind = T.project({ x: 0, y: 0, z: 5000 }, cam, 800, 400);
ok(isFinite(behind.x) && isFinite(behind.y) && behind.visible === false,
   "geometry behind the camera is finite and culled, not NaN");
T.orbit(cam, 0, -99);
ok(cam.pitch > -Math.PI / 2, "pitch is clamped short of the pole", "pitch=" + cam.pitch);
T.zoom(cam, 0.0001);
ok(cam.dist >= 200, "zoom is clamped to a minimum distance");
T.zoom(cam, 100000);
ok(cam.dist <= 6000, "zoom is clamped to a maximum distance");

/* Auto-framing must actually fit the content, at any viewport, or the panel is
 * either cropped or mostly empty.
 *
 * "Fit" deliberately means the 94th percentile, not every node: fitting the
 * single furthest outlier shrank the whole graph to a clump in the middle of a
 * large panel, which is worse than clipping a helper at the edge. What is
 * enforced instead is that almost everything is inside and that the content
 * genuinely fills the frame. */
var fitScene = T.buildScene(TOPO);
T.settle(fitScene, 240);
T.applyRun(fitScene, {});
[[900, 520], [420, 300], [1600, 700]].forEach(function (size) {
  var fitCam = T.makeCamera();
  T.frame(fitScene, fitCam, size[0], size[1]);
  var inside = 0, off = 0, box = null;
  fitScene.nodes.forEach(function (node) {
    var p = T.project(node, fitCam, size[0], size[1]);
    if (p.x >= 0 && p.x <= size[0] && p.y >= 0 && p.y <= size[1]) inside++; else off++;
    if (!box) box = { x0: p.x, x1: p.x, y0: p.y, y1: p.y };
    box.x0 = Math.min(box.x0, p.x); box.x1 = Math.max(box.x1, p.x);
    box.y0 = Math.min(box.y0, p.y); box.y1 = Math.max(box.y1, p.y);
  });
  ok(inside / fitScene.nodes.length >= 0.94,
     "auto-frame keeps at least 94% on screen at " + size[0] + "x" + size[1],
     off + " off-screen, dist=" + Math.round(fitCam.dist));
  // ...and does not leave the graph as a speck in the middle.
  var spread = 0;
  fitScene.nodes.forEach(function (node) {
    var p = T.project(node, fitCam, size[0], size[1]);
    spread = Math.max(spread, Math.abs(p.x - size[0] / 2), Math.abs(p.y - size[1] / 2));
  });
  ok(spread > Math.min(size[0], size[1]) * 0.3,
     "auto-frame uses the viewport at " + size[0] + "x" + size[1],
     "spread=" + Math.round(spread));
  // The projected content is centred, biased right to clear the label gutter.
  var midX = (box.x0 + box.x1) / 2;
  ok(Math.abs(midX - size[0] / 2 - 34) < size[0] * 0.12,
     "auto-frame centres the content at " + size[0] + "x" + size[1],
     "midX=" + Math.round(midX) + " want~" + Math.round(size[0] / 2 + 34));
});

/* ---- 4. growth: the web may only show what the run has reached ---- */
scene = T.buildScene(TOPO);
T.applyRun(scene, LIVE);
ok(scene.byId["step:collect"].revealTarget === 1, "a completed step is fully lit");
ok(scene.byId["step:harvest"].revealTarget === 1, "the active step is lit");
ok(scene.byId["step:plan"].revealTarget < 0.2, "a step the run has not reached stays dark",
   "reveal=" + scene.byId["step:plan"].revealTarget);
ok(scene.byId["rules:expansion_plan"].revealTarget < 0.2,
   "functions under an unreached step stay dark");
ok(scene.byId["cycle:Cycle.collect"].revealTarget === 1, "functions under a done step are lit");
ok(scene.byId["step:harvest"].heatTarget === 1 && scene.byId["step:collect"].heatTarget === 0,
   "only the active step is hot");
ok(scene.byId["mcp:Client.call"].status === "active",
   "a shared function inherits the furthest-along state that reaches it",
   scene.byId["mcp:Client.call"].status);

var skipped = JSON.parse(JSON.stringify(LIVE));
skipped.steps[1].status = "skipped";
skipped.active = null;
T.applyRun(scene, skipped);
ok(scene.byId["step:harvest"].revealTarget > 0.15 && scene.byId["step:harvest"].revealTarget < 0.4,
   "a skipped step is a ghost: visible, clearly not run",
   "reveal=" + scene.byId["step:harvest"].revealTarget);
ok(scene.byId["step:harvest"].heatTarget === 0, "a skipped step is not hot");

/* With no pipeline data at all the topology is still true, so show all of it. */
var idle = T.buildScene(TOPO);
T.applyRun(idle, {});
ok(idle.nodes.every(function (n) { return n.revealTarget === 1; }),
   "with no run data the whole web is shown rather than a black panel");

/* A failed step must not read as merely pending. */
var failedRun = JSON.parse(JSON.stringify(LIVE));
failedRun.steps[1].status = "failed";
var failScene = T.buildScene(TOPO);
T.applyRun(failScene, failedRun);
ok(failScene.byId["step:harvest"].revealTarget === 1 && failScene.byId["step:harvest"].heatTarget > 0.5,
   "a failed step stays lit and hot");

/* Completing a step during a run queues exactly one completion ring. */
var ringScene = T.buildScene(TOPO);
T.applyRun(ringScene, LIVE);                       // harvest active
var doneRun = JSON.parse(JSON.stringify(LIVE));
doneRun.steps[1].status = "done";
doneRun.steps[1].seconds = 1.4;
T.applyRun(ringScene, doneRun);
ok(ringScene.rings.length === 1, "a step finishing queues one completion ring",
   "rings=" + ringScene.rings.length);
T.applyRun(ringScene, doneRun);
ok(ringScene.rings.length === 1, "an unchanged payload does not queue more rings");

/* advance() must converge toward the targets and never overshoot into NaN. */
var animated = T.buildScene(TOPO);
T.applyRun(animated, LIVE);
for (var i = 0; i < 200; i++) T.advance(animated, 1 / 60);
ok(near(animated.byId["step:collect"].reveal, 1, 0.01), "reveal eases to its target",
   String(animated.byId["step:collect"].reveal));
ok(animated.edges.every(function (e) { return isFinite(e.flow) && e.flow >= 0 && e.flow <= 1; }),
   "edge flow stays in range");

/* ---- 5. packets travel the real call path ---- */
scene = T.buildScene(TOPO);
var path = T.route(scene, "step:collect", "tool:collect_produce");
ok(!!path && path[0] === "step:collect" && path[path.length - 1] === "tool:collect_produce",
   "a route exists from the step to the tool it calls", JSON.stringify(path));
ok(path.length === 3, "the route passes through the function that makes the call",
   JSON.stringify(path));
ok(T.route(scene, "step:plan", "tool:harvest") === null,
   "no route is invented between a step and a tool it never calls");

T.spawnPackets(scene, [
  { ts: "2026-08-21T17:16:40Z", tool: "collect_produce", step: "collect", key: "k1" },
  { ts: "2026-08-21T17:16:41Z", tool: "harvest", step: "harvest", key: "k2" }
]);
ok(scene.packets.length === 2, "recorded calls become packets", "n=" + scene.packets.length);
T.spawnPackets(scene, [{ ts: "2026-08-21T17:16:40Z", tool: "collect_produce", step: "collect", key: "k1" }]);
ok(scene.packets.length === 2, "the same recorded call is never replayed twice");
T.spawnPackets(scene, [{ ts: "x", tool: "no_such_tool", step: "collect", key: "k3" }]);
ok(scene.packets.length === 2, "an unknown tool spawns nothing rather than a stray dot");

var travelled = false;
for (i = 0; i < 400 && scene.packets.length; i++) {
  T.movePackets(scene, 1 / 60);
  if (scene.packets.length && scene.packets[0].trail.length) travelled = true;
}
ok(travelled, "packets move and leave a trail");
ok(scene.packets.length === 0, "packets retire at the end of their route");

/* ---- 6. picking and highlighting ---- */
scene = T.buildScene(TOPO);
T.settle(scene, 200);
T.applyRun(scene, {});
for (i = 0; i < 60; i++) T.advance(scene, 1 / 60);
cam = T.makeCamera({ dist: T.extent(scene) * 2.4, yaw: 0, pitch: 0 });
var target = scene.byId["step:collect"];
var screen = T.project(target, cam, 900, 520);
var hit = T.pick(scene, cam, 900, 520, screen.x, screen.y);
ok(hit && hit.id === "step:collect", "clicking a node picks that node", hit ? hit.id : "null");
ok(T.pick(scene, cam, 900, 520, -400, -400) === null, "empty space picks nothing");
var hood = T.neighbourhood(scene, scene.byId["cycle:Cycle.collect"]);
ok(hood["step:collect"] && hood["tool:collect_produce"] && hood["mcp:Client.call"],
   "the neighbourhood of a function is its callers and callees");
ok(!hood["rules:expansion_plan"], "unrelated nodes are not in the neighbourhood");

/* ---- 7. the fallback carries the same facts ---- */
var html = T.fallbackHtml(TOPO, LIVE);
ok(html.indexOf("collect_produce") >= 0, "fallback names the server tools");
ok(html.indexOf("active") >= 0, "fallback carries live step state");
ok(html.indexOf("4 functions") >= 0, "fallback carries the graph size", html.slice(0, 120));
ok(T.fallbackHtml({}, {}).indexOf("No topology") >= 0, "fallback degrades on an empty payload");
ok(T.fallbackHtml({
  steps: [{ name: "<img src=x onerror=alert(1)>", functions: 1, modules: [], tools: [] }],
  stats: {}
}, {}).indexOf("<img") < 0, "fallback escapes hostile text");

/* ---- 8. an empty or malformed topology must not throw ---- */
var threw = null;
try {
  var blank = T.buildScene({});
  T.settle(blank, 10);
  T.applyRun(blank, LIVE);
  T.advance(blank, 0.016);
  T.movePackets(blank, 0.016);
  T.spawnPackets(blank, [{ ts: "x", tool: "y", key: "z" }]);
  T.pick(blank, T.makeCamera(), 100, 100, 10, 10);
} catch (error) { threw = error.message || String(error); }
ok(threw === null, "an empty topology drives the whole engine without throwing", threw || "");

/* ---- 9. scale: the engine must survive a graph far larger than farm/ ---- */
var bigTopo = { nodes: [], edges: [], steps: [], stats: {}, modules: [] };
for (i = 0; i < 14; i++) {
  bigTopo.nodes.push({ id: "step:s" + i, kind: "step", label: "s" + i, module: "cycle", order: i, steps: [], depth: 0 });
}
for (i = 0; i < 400; i++) {
  var owner = "s" + (i % 14);
  bigTopo.nodes.push({ id: "fn:f" + i, kind: "func", label: "f" + i, module: "rules",
                       qual: "rules:f" + i, line: i, loc: 10 + (i % 40), steps: [owner], depth: 1 + (i % 4) });
  bigTopo.edges.push({ source: "step:" + owner, target: "fn:f" + i, kind: "step" });
  if (i > 14) bigTopo.edges.push({ source: "fn:f" + (i - 14), target: "fn:f" + i, kind: "call" });
}
var t0 = Date.now();
var heavy = T.buildScene(bigTopo);
T.settle(heavy, 60);
var elapsed = Date.now() - t0;
ok(heavy.nodes.length === 414, "a 414-node graph builds", "n=" + heavy.nodes.length);
ok(elapsed < 4000, "60 layout iterations on 414 nodes stay well inside a frame budget over time",
   elapsed + "ms");
ok(heavy.nodes.every(function (n) { return isFinite(n.x) && isFinite(n.y) && isFinite(n.z); }),
   "a large graph stays numerically stable");

console.log(out.join("\n"));
console.log(fails ? "trace3d: " + fails + " FAIL of " + out.length : "trace3d: PASS " + out.length + " checks");
if (fails) { $.NSFileHandle.fileHandleWithStandardError.writeData($.NSString.alloc.initWithUTF8String("trace3d failures\n").dataUsingEncoding($.NSUTF8StringEncoding)); }
fails ? "FAIL" : "PASS";
