/* Headless current-run renderer for deploy/preview_trace_explorer.py. */
ObjC.import("Foundation");
function read(path) {
  return $.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null).js;
}
var args = $.NSProcessInfo.processInfo.arguments.js;
var input = JSON.parse(read(args[4].js));
var globalThisRef = this;
eval(read("dashboard/trace_explorer.js"));
var T = globalThisRef.TraceExplorer || TraceExplorer;
function count(text, needle) { return String(text).split(needle).length - 1; }
var trace = T.render(input.topology, input.pipeline, input.trace,
  { view: "trace", selected: input.selected || null }, input.now);
var matrix = T.render(input.topology, input.pipeline, input.trace,
  { view: "matrix", selected: input.selected || null }, input.now);
var result = {
  traceHtml: trace.html,
  matrixHtml: matrix.html,
  summary: {
    run: trace.model.run,
    status: trace.model.status,
    active: trace.model.active ? trace.model.active.name : null,
    steps: trace.model.steps.length,
    calls: trace.model.calls.length,
    callGroups: trace.model.groups.length,
    tools: trace.model.tools.length,
    coverage: trace.model.coverage,
    elapsed: trace.model.elapsed,
    boundarySeconds: trace.model.boundarySeconds
  },
  metrics: {
    stepRows: count(trace.html, "te-row te-step"),
    callRows: count(trace.html, "te-row te-call"),
    callSpans: count(trace.html, "te-call-span "),
    toolColumns: count(matrix.html, "te-tool-head"),
    observedCells: count(matrix.html, " called"),
    traceBytes: trace.html.length,
    matrixBytes: matrix.html.length,
    hasCanvas: trace.html.indexOf("canvas") >= 0 || matrix.html.indexOf("canvas") >= 0
  }
};
var payload = JSON.stringify(result);
$.NSString.alloc.initWithUTF8String(payload)
  .writeToFileAtomicallyEncodingError(input.resultPath, true, $.NSUTF8StringEncoding, null);
"wrote trace explorer preview";
