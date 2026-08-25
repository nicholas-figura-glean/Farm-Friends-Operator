/* Headless switchboard renderer for deploy/preview_mcp_wire.py.
 *
 * Runs the real model and HTML generator against the live run's telemetry, so a
 * change to the animation contract can be checked without a browser.
 */
ObjC.import("Foundation");
function read(path) {
  return $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null).js;
}
var args = $.NSProcessInfo.processInfo.arguments.js;
var input = JSON.parse(read(args[4].js));
var globalThisRef = this;
eval(read("dashboard/mcp_wire.js"));
var W = globalThisRef.MCPWire || MCPWire;
function count(text, needle) { return String(text).split(needle).length - 1; }

var state = { speed: input.speed || 4, paused: false, focus: input.focus || null };
var output = W.render(input.topology, input.pipeline, input.trace, state, input.now);
var model = output.model;
var result = {
  html: output.html,
  summary: {
    run: model.run,
    status: model.status,
    coverage: model.coverage,
    calls: model.stats.calls,
    drawn: model.stats.drawn,
    thinned: model.thinned,
    lanes: model.lanes.length,
    silent: model.stats.silent,
    errors: model.stats.errors,
    inFlight: model.stats.inFlight,
    peak: model.stats.peak,
    median: model.stats.median,
    p95: model.stats.p95,
    span: model.stats.wallSeconds,
    boundary: model.stats.boundarySeconds,
    parallelism: model.stats.parallelism,
    perMinute: model.stats.perMinute,
    loop: model.loop,
    effectiveSpeed: model.effectiveSpeed,
    busiest: model.hall.busiest ? model.hall.busiest.name : null,
    slowest: model.hall.slowest ? model.hall.slowest.duration : null
  },
  metrics: {
    packets: count(output.html, "mw-packet "),
    laneRows: count(output.html, "mw-lane-label"),
    stepPads: count(output.html, "mw-pad "),
    flying: count(output.html, "mw-packet active flying"),
    hasCanvas: output.html.indexOf("canvas") >= 0,
    bytes: output.html.length
  }
};
var payload = JSON.stringify(result);
$.NSString.alloc.initWithUTF8String(payload)
  .writeToFileAtomicallyEncodingError(input.resultPath, true, $.NSUTF8StringEncoding, null);
"wrote switchboard preview";
