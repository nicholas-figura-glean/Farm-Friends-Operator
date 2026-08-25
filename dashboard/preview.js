/* Headless render capture for dashboard/trace3d.js.
 *
 * There is no node, no headless browser and no image library on this machine, so
 * "does the picture read clearly?" cannot be answered by looking at it in the
 * usual way. This runs the REAL renderer in JavaScriptCore against a recording
 * 2D context and prints every draw call as JSON. deploy/preview_trace.py then
 * rasterises those calls to a PNG and measures them.
 *
 * The point is fidelity: the geometry, sizes, alphas and colours come from the
 * same code the browser runs, so a layout change can be judged (and regressions
 * caught) without a browser in the loop.
 *
 * Usage (driven by deploy/preview_trace.py):
 *   osascript -l JavaScript dashboard/preview.js <input.json>
 * where input.json is {topology, pipeline, activity, width, height, seconds, cam, state}
 */
ObjC.import("Foundation");

function readFile(path) {
  return $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null).js;
}

var here = "dashboard/";
var input = JSON.parse(readFile($.NSProcessInfo.processInfo.arguments.js[4].js));

// ---------------------------------------------------------------- recording ctx

var OPS = [];

function RecordingContext(sink, meta) {
  this.sink = sink;                 // array to push ops into
  this.meta = meta || {};
  this.path = [];
  this.stack = [];
  this.fillStyle = "#000";
  this.strokeStyle = "#000";
  this.lineWidth = 1;
  this.globalAlpha = 1;
  this.globalCompositeOperation = "source-over";
  this.font = "11px sans-serif";
  this.textBaseline = "alphabetic";
  this.lineCap = "butt";
  this.dash = [];
}

RecordingContext.prototype.state = function () {
  return {
    fill: this.fillStyle, stroke: this.strokeStyle, lineWidth: this.lineWidth,
    alpha: this.globalAlpha, composite: this.globalCompositeOperation,
    dash: this.dash.length > 0
  };
};
RecordingContext.prototype.save = function () {
  this.stack.push([this.fillStyle, this.strokeStyle, this.lineWidth, this.globalAlpha,
    this.globalCompositeOperation, this.font, this.dash]);
};
RecordingContext.prototype.restore = function () {
  var s = this.stack.pop();
  if (!s) return;
  this.fillStyle = s[0]; this.strokeStyle = s[1]; this.lineWidth = s[2];
  this.globalAlpha = s[3]; this.globalCompositeOperation = s[4]; this.font = s[5]; this.dash = s[6];
};
RecordingContext.prototype.setTransform = function () {};
RecordingContext.prototype.scale = function () {};
RecordingContext.prototype.translate = function () {};
RecordingContext.prototype.clearRect = function () { this.sink.push({ op: "clear" }); };
RecordingContext.prototype.setLineDash = function (d) { this.dash = d || []; };

RecordingContext.prototype.createRadialGradient = function (x0, y0, r0, x1, y1, r1) {
  var g = { kind: "radial", x0: x0, y0: y0, r0: r0, x1: x1, y1: y1, r1: r1, stops: [] };
  g.addColorStop = function (at, color) { g.stops.push([at, color]); };
  return g;
};
RecordingContext.prototype.createLinearGradient = function (x0, y0, x1, y1) {
  var g = { kind: "linear", x0: x0, y0: y0, x1: x1, y1: y1, stops: [] };
  g.addColorStop = function (at, color) { g.stops.push([at, color]); };
  return g;
};

RecordingContext.prototype.fillRect = function (x, y, w, h) {
  var style = this.fillStyle;
  this.sink.push({
    op: "rect", x: x, y: y, w: w, h: h,
    gradient: (style && style.kind) ? style : null,
    color: (style && style.kind) ? null : style,
    st: this.state()
  });
};

RecordingContext.prototype.beginPath = function () { this.path = []; };
RecordingContext.prototype.moveTo = function (x, y) { this.path.push(["M", x, y]); };
RecordingContext.prototype.lineTo = function (x, y) { this.path.push(["L", x, y]); };
RecordingContext.prototype.quadraticCurveTo = function (cx, cy, x, y) { this.path.push(["Q", cx, cy, x, y]); };
RecordingContext.prototype.bezierCurveTo = function (a, b, c, d, x, y) { this.path.push(["Q", c, d, x, y]); };
RecordingContext.prototype.arc = function (x, y, r, s, e) { this.path.push(["A", x, y, r, s, e]); };
RecordingContext.prototype.closePath = function () { this.path.push(["Z"]); };
RecordingContext.prototype.stroke = function () {
  if (!this.path.length) return;
  this.sink.push({ op: "stroke", path: this.path.slice(), st: this.state() });
};
RecordingContext.prototype.fill = function () {
  if (!this.path.length) return;
  this.sink.push({ op: "fill", path: this.path.slice(), st: this.state() });
};
RecordingContext.prototype.drawImage = function (img, x, y, w, h) {
  this.sink.push({
    op: "sprite", x: x, y: y, w: w, h: h,
    stops: (img && img.__stops) || null, st: this.state()
  });
};
RecordingContext.prototype.measureText = function (text) {
  // JavaScriptCore has no text metrics. 6.05px per character at 11px 600-weight
  // -apple-system measures within ~4% of Chrome for the labels this graph uses,
  // which is close enough to reproduce label collisions faithfully.
  return { width: String(text).length * 6.05 };
};
RecordingContext.prototype.fillText = function (text, x, y) {
  this.sink.push({ op: "text", text: String(text), x: x, y: y, st: this.state() });
};

// ---------------------------------------------------------------- DOM stubs

function makeCanvas(width, height, sink) {
  var canvas = {
    width: width || 300, height: height || 150,
    style: {},
    getContext: function () {
      if (!canvas.__ctx) canvas.__ctx = new RecordingContext(sink || [], {});
      return canvas.__ctx;
    }
  };
  return canvas;
}

var documentStub = {
  createElement: function (tag) {
    if (tag !== "canvas") return { style: {} };
    var spriteSink = [];
    var canvas = makeCanvas(0, 0, spriteSink);
    // A sprite is one gradient-filled rect; keep its stops so the rasteriser
    // knows what colour the blob is.
    var realGet = canvas.getContext;
    canvas.getContext = function () {
      var ctx = realGet();
      var fill = ctx.fillRect.bind(ctx);
      ctx.fillRect = function (x, y, w, h) {
        if (ctx.fillStyle && ctx.fillStyle.stops) canvas.__stops = ctx.fillStyle.stops;
        fill(x, y, w, h);
      };
      return ctx;
    };
    return canvas;
  }
};

// ---------------------------------------------------------------- run

var source = readFile(here + "trace3d.js");
var globalThisRef = this;
eval(source);                            // the IIFE attaches itself to the global
var T = globalThisRef.Trace3D || (typeof Trace3D !== "undefined" ? Trace3D : null);
if (!T) throw new Error("preview: Trace3D did not load");

if (input.tuning) {
  Object.keys(input.tuning).forEach(function (key) { T.TUNING[key] = input.tuning[key]; });
}

var width = input.width || 620;
var height = input.height || 380;

var scene = T.buildScene(input.topology, {});
T.settle(scene, input.settle == null ? 260 : input.settle);
if (input.pipeline) T.applyRun(scene, input.pipeline, { instant: !!input.instant });
if (input.activity && input.activity.length) {
  // The browser may vary packet speeds; a regression image may not. Inject the
  // engine's deterministic PRNG so identical inputs produce identical pixels.
  T.spawnPackets(scene, input.activity, { random: T.seeded(0x51a7c0de) });
}

// Advance the animation to the requested moment, at a fixed 60Hz step so the
// captured frame is deterministic and reproducible.
var seconds = input.seconds == null ? 2.5 : input.seconds;
for (var t = 0; t < seconds; t += 1 / 60) {
  T.advance(scene, 1 / 60);
  T.movePackets(scene, 1 / 60);
}

var cam = T.makeCamera(input.cam || {});
if (input.frame !== false) T.frame(scene, cam, width, height, input.margin);

var canvas = makeCanvas(width, height, OPS);
var renderer = new T.Renderer(canvas, { document: documentStub, dpr: 1 });
renderer.resize(width, height);
if (input.quality != null) renderer.quality = input.quality;

var highlight = null, soft = false;
if (input.highlight) {
  scene.nodes.forEach(function (n) { if (n.id === input.highlight) highlight = n; });
}
if (!highlight && input.focus) {
  // The panel's own default: soft focus on the step being executed.
  scene.steps.forEach(function (step) {
    if (step.status === "active" || step.heat > 0.35) { highlight = step; soft = true; }
  });
}
renderer.draw(scene, cam, {
  highlight: highlight,
  soft: soft,
  selected: input.selected || null,
  now: input.now == null ? 1755790000000 : input.now
});

// A compact description of what was drawn, for metrics that need to know what a
// blob *is* rather than only where it landed.
var nodes = scene.nodes.map(function (n) {
  var p = T.project(n, cam, width, height);
  return {
    id: n.id, kind: n.kind, label: n.label, module: n.module, status: n.status,
    reveal: Math.round(n.reveal * 1000) / 1000, heat: Math.round(n.heat * 1000) / 1000,
    x: Math.round(p.x * 10) / 10, y: Math.round(p.y * 10) / 10,
    depth: Math.round(p.depth), scale: Math.round(p.scale * 1000) / 1000,
    visible: !!p.visible, r: Math.round(T.nodeRadius(n) * p.scale * 100) / 100
  };
});

var payload = JSON.stringify({
  width: width, height: height,
  cam: { yaw: cam.yaw, pitch: cam.pitch, dist: cam.dist, target: cam.target },
  ops: OPS,
  nodes: nodes,
  counts: {
    nodes: scene.nodes.length, edges: scene.edges.length,
    packets: scene.packets.length, rings: scene.rings.length,
    lit: scene.nodes.filter(function (n) { return n.reveal > 0.6; }).length
  },
  stats: scene.stats || null
});

// Written to a file rather than stdout: a captured frame is a few hundred KB of
// JSON and osascript's stdout is not a reliable pipe for that.
if (input.out) {
  $.NSString.alloc.initWithUTF8String(payload)
    .writeToFileAtomicallyEncodingError(input.out, true, $.NSUTF8StringEncoding, null);
  "wrote " + OPS.length + " ops";
} else {
  payload;
}

