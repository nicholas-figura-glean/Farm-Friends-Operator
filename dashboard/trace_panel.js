/* trace_panel.js - the dashboard side of the 3D trace: DOM, input, animation.
 *
 * Split from trace3d.js on purpose. Everything in that file is pure arithmetic
 * and is unit-tested in JavaScriptCore; everything in this file needs a browser.
 * Keeping the boundary sharp is what makes the engine testable at all on a
 * machine with no node and no headless browser.
 *
 * Every entry point here is defensive, because this panel shares a <script> with
 * the monitor's polling loop: an exception thrown at mount time on some future
 * browser must cost the 3D view and nothing else. The dashboard's own `safe()`
 * wrapper catches per-panel throws, and `mount()` additionally falls back to an
 * accessible table when there is no 2D context to draw into.
 */
(function (root) {
  "use strict";

  var Engine = root.Trace3D;
  var panel = {
    scene: null, cam: null, renderer: null, canvas: null,
    topology: null, fingerprint: null, pipeline: null,
    highlight: null, selected: null, mode: "orbit", flat: false,
    lastFrame: 0, fps: 0, running: false, dragging: false,
    reduceMotion: false, replay: null, pointer: null, fetching: false
  };

  function $(id) {
    return typeof document !== "undefined" && document.getElementById
      ? document.getElementById(id) : null;
  }

  function canAnimate() {
    return typeof requestAnimationFrame === "function";
  }

  function hidden() {
    var host = $("tab-pipeline");
    if (host && host.hidden) return true;                 // another tab is open
    if (typeof document !== "undefined" && document.hidden) return true;  // window not visible
    return false;
  }

  // ------------------------------------------------------------------ mounting

  /* No drawing surface: hide the stage, open the text equivalent, and say so.
   * The tab keeps working; it just stops being three-dimensional. */
  function flatten() {
    panel.flat = true;
    if (panel.stage && panel.stage.classList) panel.stage.classList.add("flat");
    var details = $("trace-details");
    if (details) details.open = true;
    renderFallback();
    return false;
  }

  function mount(options) {
    options = options || {};
    if (!Engine) return false;
    var canvas = $(options.canvasId || "trace-canvas");
    panel.stage = $("trace-stage");
    if (!canvas || typeof canvas.getContext !== "function") {
      // No canvas API at all (or a DOM stub in the test harness): the panel
      // becomes a table. This is the path that keeps the tab honest instead of
      // blank on a browser that cannot draw.
      return flatten();
    }
    var ctx = null;
    try { ctx = canvas.getContext("2d"); } catch (error) { ctx = null; }
    if (!ctx) return flatten();
    panel.canvas = canvas;
    panel.reduceMotion = typeof matchMedia === "function"
      && matchMedia("(prefers-reduced-motion: reduce)").matches;
    panel.renderer = new Engine.Renderer(canvas, {
      document: document,
      dpr: Math.min(2, (root.devicePixelRatio || 1))
    });
    panel.cam = Engine.makeCamera();
    resize();
    bindInput(canvas);
    if (typeof addEventListener === "function") {
      addEventListener("resize", resize);
      addEventListener("visibilitychange", function () { if (!hidden()) start(); });
    }
    return true;
  }

  function resize() {
    if (!panel.renderer || !panel.canvas) return;
    var width = panel.canvas.clientWidth || 900;
    var height = panel.canvas.clientHeight || 520;
    panel.renderer.resize(width, height);
    // Re-frame on resize: the fit depends on the viewport, so a panel that was
    // framed at 1400px is either cropped or half empty at 900px.
    if (panel.scene && panel.mode !== "follow") {
      Engine.frame(panel.scene, panel.cam, width, height);
    }
    draw();
  }

  // --------------------------------------------------------------------- input

  function bindInput(canvas) {
    if (typeof canvas.addEventListener !== "function") return;
    var last = null;

    canvas.addEventListener("pointerdown", function (event) {
      last = { x: event.clientX, y: event.clientY };
      panel.dragging = true;
      panel.mode = "free";                    // a drag means the operator is steering
      syncModeChips();
      if (canvas.setPointerCapture) { try { canvas.setPointerCapture(event.pointerId); } catch (e) {} }
      if (canvas.classList) canvas.classList.add("dragging");
    });

    canvas.addEventListener("pointermove", function (event) {
      var rect = canvas.getBoundingClientRect ? canvas.getBoundingClientRect() : { left: 0, top: 0 };
      panel.pointer = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      if (panel.dragging && last) {
        Engine.orbit(panel.cam, (event.clientX - last.x) * 0.006, (event.clientY - last.y) * -0.005);
        last = { x: event.clientX, y: event.clientY };
        start();
        return;
      }
      hover();
    });

    function release() {
      panel.dragging = false; last = null;
      if (canvas.classList) canvas.classList.remove("dragging");
    }
    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("pointercancel", release);
    canvas.addEventListener("pointerleave", function () {
      release(); panel.pointer = null; panel.highlight = null; renderReadout(); draw();
    });

    canvas.addEventListener("wheel", function (event) {
      if (!panel.cam) return;
      event.preventDefault();
      Engine.zoom(panel.cam, event.deltaY > 0 ? 1.09 : 0.92);
      start();
    }, { passive: false });

    canvas.addEventListener("click", function () {
      if (!panel.scene || !panel.pointer) return;
      var node = Engine.pick(panel.scene, panel.cam, panel.renderer.width, panel.renderer.height,
                             panel.pointer.x, panel.pointer.y);
      panel.selected = node ? node.id : null;
      renderInspector();
      draw();
    });

    canvas.addEventListener("dblclick", function () { reset(); });
  }

  function hover() {
    if (!panel.scene || !panel.pointer || !panel.renderer) return;
    var node = Engine.pick(panel.scene, panel.cam, panel.renderer.width, panel.renderer.height,
                           panel.pointer.x, panel.pointer.y);
    var changed = (node ? node.id : null) !== (panel.highlight ? panel.highlight.id : null);
    panel.highlight = node;
    if (changed) { renderReadout(); draw(); }
  }

  function reset() {
    if (!panel.scene) return;
    panel.cam = Engine.makeCamera();
    if (panel.renderer) {
      Engine.frame(panel.scene, panel.cam, panel.renderer.width, panel.renderer.height);
    }
    panel.mode = "orbit";
    panel.selected = null;
    syncModeChips();
    renderInspector();
    start();
  }

  // ---------------------------------------------------------------- topology

  /* The graph is static between releases, so it is fetched from its own endpoint
   * once and re-fetched only when the fingerprint in /api/state changes (which
   * happens when a farm/*.py file is edited). Shipping 25KB of unchanging graph
   * inside a 2-second poll would be 45MB an hour for no new information. */
  function setTopology(topology) {
    if (!Engine || !topology || !topology.nodes || !topology.nodes.length) return;
    panel.topology = topology;
    panel.fingerprint = topology.fingerprint;
    panel.scene = Engine.buildScene(topology);
    Engine.settle(panel.scene, panel.reduceMotion ? 260 : 90);
    if (panel.pipeline) Engine.applyRun(panel.scene, panel.pipeline);
    panel.cam = Engine.makeCamera();
    if (panel.renderer) {
      Engine.frame(panel.scene, panel.cam, panel.renderer.width, panel.renderer.height);
    } else {
      panel.cam.dist = Engine.extent(panel.scene) * 2.2;
    }
    renderLegend();
    renderFallback();
    start();
  }

  function needsTopology(fingerprint) {
    return !panel.topology || (fingerprint && fingerprint !== panel.fingerprint);
  }

  // -------------------------------------------------------------------- update

  /* Called on every poll with the whole /api/state payload. */
  function update(data) {
    data = data || {};
    var trace = data.trace || {};
    panel.pipeline = data.pipeline || null;

    if (needsTopology(trace.fingerprint) && !panel.fetching) fetchTopology();
    if (!panel.scene) { renderFallback(); return; }

    if (!panel.replay) Engine.applyRun(panel.scene, panel.pipeline);
    if (!panel.reduceMotion && !panel.replay) {
      Engine.spawnPackets(panel.scene, trace.activity || []);
    }
    renderLegend();
    renderReadout();
    renderFallback();
    renderInspector();
    start();
  }

  function fetchTopology() {
    if (typeof fetch !== "function") return;
    panel.fetching = true;
    fetch("/api/topology", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (topology) { setTopology(topology); })
      .catch(function (error) {
        var host = $("trace-fallback");
        if (host) host.innerHTML = '<div class="empty">Topology unavailable: '
          + Engine.escapeHtml(error && error.message ? error.message : error) + "</div>";
      })
      .then(function () { panel.fetching = false; });
  }

  // -------------------------------------------------------------------- replay

  /* Replay the last completed run's build-out using its *recorded* per-step
   * timestamps, compressed into a few seconds. It is the same growth animation a
   * live run produces, which is the point: between runs you can still see the
   * shape of the last cycle instead of a static picture.
   */
  function startReplay(seconds) {
    if (!panel.pipeline || !panel.pipeline.steps || !panel.scene) return;
    var steps = panel.pipeline.steps;
    var base = null, end = null;
    steps.forEach(function (step) {
      var s = step.started_ts ? Date.parse(step.started_ts) : null;
      var e = step.ended_ts ? Date.parse(step.ended_ts) : null;
      if (s != null && (base == null || s < base)) base = s;
      if (e != null && (end == null || e > end)) end = e;
    });
    if (base == null || end == null || end <= base) return;
    panel.replay = { base: base, span: end - base, t0: Date.now(), duration: (seconds || 9) * 1000 };
    // Reset the web to unbuilt so the growth is visible from the first frame.
    panel.scene.nodes.forEach(function (node) {
      node.status = "pending"; node.reveal = 0; node.revealTarget = 0; node.heat = 0;
    });
    panel.scene.rings = [];
    panel.scene.packets = [];
    panel.scene.seenActivity = {};
    start();
  }

  /* The synthetic pipeline for a replay frame: a step is done/active/pending
   * according to where the replay clock sits inside its recorded window. */
  function replayPipeline(now) {
    var replay = panel.replay, source = panel.pipeline;
    var progress = (now - replay.t0) / replay.duration;
    if (progress >= 1) { panel.replay = null; return source; }
    var at = replay.base + progress * replay.span;
    var steps = (source.steps || []).map(function (step) {
      var s = step.started_ts ? Date.parse(step.started_ts) : null;
      var e = step.ended_ts ? Date.parse(step.ended_ts) : null;
      var status = "pending";
      if (step.status === "skipped") status = (e != null && at >= e) ? "skipped" : "pending";
      else if (s != null && e != null) status = at >= e ? step.status : (at >= s ? "active" : "pending");
      else if (s != null) status = at >= s ? "active" : "pending";
      return {
        name: step.name, status: status, seconds: status === "done" ? step.seconds : null,
        started_ts: step.started_ts, ended_ts: step.ended_ts
      };
    });
    return {
      status: "running", active: (steps.filter(function (s) { return s.status === "active"; })[0] || {}).name,
      steps: steps, baseline: source.baseline, summary: source.summary
    };
  }

  // --------------------------------------------------------------- animation

  /* Scheduling is deliberately paranoid.
   *
   * requestAnimationFrame is the right clock for an animation, but it is only
   * serviced when the page is actually being composited. Inside an occluded or
   * embedded webview (the Glean Desktop browser panel is one, and this was found
   * there, not theorised) callbacks never arrive at all - so a panel that trusts
   * rAF alone shows a permanently blank canvas with no way to recover. The
   * watchdog notices the frame that never came and switches the panel to a
   * timer-driven loop for the rest of the session.
   */
  function schedule() {
    if (panel.timerMode || !canAnimate()) {
      if (typeof setTimeout === "function") setTimeout(frame, 55);
      return;
    }
    panel.scheduledAt = Date.now();
    requestAnimationFrame(frame);
    if (panel.watchdog == null && typeof setTimeout === "function") {
      panel.watchdog = setTimeout(function () {
        panel.watchdog = null;
        if (panel.lastFrameAt == null || panel.lastFrameAt < panel.scheduledAt) {
          panel.timerMode = true;
          panel.running = false;
          start();
        }
      }, 1400);
    }
  }

  function start() {
    if (!panel.renderer || panel.flat) { renderFallback(); return; }
    if (panel.running) return;
    panel.running = true;
    panel.lastFrame = Date.now();
    schedule();
  }

  function frame() {
    panel.running = false;
    panel.lastFrameAt = Date.now();
    if (panel.watchdog != null && typeof clearTimeout === "function") {
      clearTimeout(panel.watchdog);
      panel.watchdog = null;
    }
    if (!panel.scene || !panel.renderer) return;
    var now = panel.lastFrameAt;
    var dt = Math.min(0.08, Math.max(0.001, (now - panel.lastFrame) / 1000));
    panel.lastFrame = now;
    panel.fps = panel.fps ? panel.fps * 0.9 + (1 / dt) * 0.1 : 1 / dt;

    if (hidden()) return;                     // no work at all for an unseen tab

    if (panel.replay) {
      Engine.applyRun(panel.scene, replayPipeline(now));
      if (!panel.replay) Engine.applyRun(panel.scene, panel.pipeline);
    }

    // The layout keeps a little residual motion so the web breathes, but the
    // heavy settling only happens while alpha is high (first paint, or a rebuild).
    if (!panel.reduceMotion || panel.scene.alpha > 0.2) Engine.relax(panel.scene);
    Engine.advance(panel.scene, dt);
    Engine.movePackets(panel.scene, dt);

    if (panel.mode === "orbit" && !panel.dragging && !panel.reduceMotion) {
      panel.cam.yaw += panel.cam.spin * dt;
      // Keep the fit while orbiting: the silhouette of a 3D graph changes as it
      // turns, so a distance that framed it at one angle crops it at another.
      if (!panel.framedAt || now - panel.framedAt > 400) {
        Engine.frame(panel.scene, panel.cam, panel.renderer.width, panel.renderer.height);
        panel.framedAt = now;
      }
    }
    if (panel.mode === "follow" && panel.scene.activeStep) {
      var node = panel.scene.byId["step:" + panel.scene.activeStep];
      if (node) {
        panel.cam.target.x += (node.x - panel.cam.target.x) * Math.min(1, dt * 2.2);
        panel.cam.target.y += (node.y - panel.cam.target.y) * Math.min(1, dt * 2.2);
        panel.cam.target.z += (node.z - panel.cam.target.z) * Math.min(1, dt * 2.2);
        panel.cam.yaw += panel.cam.spin * dt * 0.6;
      }
    } else if (panel.mode !== "follow") {
      panel.cam.target.x += (0 - panel.cam.target.x) * Math.min(1, dt * 2);
      panel.cam.target.y += (0 - panel.cam.target.y) * Math.min(1, dt * 2);
      panel.cam.target.z += (0 - panel.cam.target.z) * Math.min(1, dt * 2);
    }

    draw();
    renderReadout();

    // Keep animating only while something is actually moving: a settled, idle,
    // reduced-motion panel drops to zero CPU instead of burning a core.
    var busy = panel.scene.packets.length || panel.scene.rings.length
      || panel.scene.alpha > 0.12 || panel.replay
      || (panel.mode === "orbit" && !panel.reduceMotion)
      || panel.mode === "follow" || panel.dragging
      || anyMoving(panel.scene);
    if (busy) { panel.running = true; schedule(); }
  }

  function anyMoving(scene) {
    for (var i = 0; i < scene.nodes.length; i++) {
      var node = scene.nodes[i];
      if (Math.abs(node.reveal - node.revealTarget) > 0.004) return true;
      if (Math.abs(node.heat - (node.heatTarget || 0)) > 0.004) return true;
      if (node.heat > 0.02) return true;                    // an active step pulses
    }
    return false;
  }

  function draw() {
    if (!panel.renderer || !panel.scene) return;
    // With nothing under the pointer, focus softly on the step being executed:
    // its own subtree at full strength, the rest of the web as context. A picture
    // of 78 equally important things is a picture of nothing in particular, and
    // during a run there is always one thing that is actually happening.
    var highlight = panel.highlight;
    var soft = false;
    if (!highlight && !panel.selected) {
      highlight = activeStep();
      soft = !!highlight;
    }
    panel.renderer.draw(panel.scene, panel.cam, {
      highlight: highlight,
      soft: soft,
      selected: panel.selected
    });
  }

  function activeStep() {
    if (!panel.scene) return null;
    var found = null;
    panel.scene.steps.forEach(function (step) {
      if (step.status === "active" || step.heat > 0.35) found = step;
    });
    return found;
  }

  // ----------------------------------------------------------------- overlays

  function renderLegend() {
    var host = $("trace-legend");
    if (!host || !panel.scene) return;
    var modules = panel.scene.modules || [];
    var rows = modules.filter(function (m) { return m.nodes > 0; }).map(function (m) {
      return '<div class="row" style="color:hsl(' + Engine.hue(m.name) + ',72%,62%)">'
        + '<i class="swatch"></i><span style="color:var(--muted)">' + Engine.escapeHtml(m.name)
        + '.py</span><span class="n">' + Engine.escapeHtml(m.nodes) + '</span></div>';
    }).join("");
    var stats = panel.scene.stats || {};
    host.innerHTML = '<b>Module</b>' + rows
      + '<div class="kindrow" style="margin-top:6px">spine <i>steps</i> · mid <i>functions</i> · shell <i>server tools</i></div>'
      + '<div class="kindrow">size <i>lines of code</i> · ' + Engine.escapeHtml(stats.edges || 0) + ' call edges</div>';
  }

  function renderReadout() {
    var host = $("trace-readout");
    if (!host || !panel.scene) return;
    var node = panel.highlight;
    var stats = panel.scene.stats || {};
    var lit = panel.scene.nodes.filter(function (n) { return n.reveal > 0.6; }).length;
    if (node) {
      host.innerHTML = '<b>' + Engine.escapeHtml(node.label) + '</b>'
        + '<small>' + Engine.escapeHtml(node.kind === "tool" ? "server tool"
            : node.kind === "step" ? "pipeline step" : node.module + ".py:" + node.line) + '</small>'
        + '<small>' + Engine.escapeHtml(node.status) + (node.loc ? " · " + node.loc + " lines" : "") + '</small>';
      return;
    }
    host.innerHTML = '<b>' + lit + " / " + panel.scene.nodes.length + '</b>'
      + '<small>units built this run</small>'
      + '<small>' + Engine.escapeHtml(stats.functions || 0) + ' functions · '
      + Engine.escapeHtml(stats.tools || 0) + ' tools</small>'
      + '<div class="fps">' + (panel.replay ? "replaying · " : "")
      + Math.round(panel.fps || 0) + ' fps</div>';
  }

  function renderInspector() {
    var host = $("trace-inspect");
    if (!host) return;
    if (!panel.selected || !panel.scene || !panel.scene.byId[panel.selected]) {
      host.innerHTML = "";
      host.hidden = true;
      return;
    }
    var node = panel.scene.byId[panel.selected];
    var rows = [];
    if (node.kind === "step") {
      rows.push(["state", Engine.escapeHtml(node.status)]);
      if (node.seconds != null) rows.push(["duration", Number(node.seconds).toFixed(1) + "s"]);
      if (node.baseline != null) rows.push(["median", Number(node.baseline).toFixed(1) + "s"]);
      var step = ((panel.topology || {}).steps || []).filter(function (s) { return s.name === node.label; })[0];
      if (step) {
        rows.push(["functions reached", step.functions]);
        rows.push(["server tools", (step.tools || []).length]);
        rows.push(["modules", (step.modules || []).join(", ") || "—"]);
      }
    } else if (node.kind === "tool") {
      rows.push(["kind", "MCP server call"]);
      rows.push(["reached by", (node.steps || []).join(", ") || "—"]);
      rows.push(["callers", node.in || 0]);
    } else {
      rows.push(["source", node.module + ".py:" + node.line]);
      rows.push(["size", node.loc + " lines"]);
      rows.push(["calls out", node.out || 0]);
      rows.push(["called by", node.in || 0]);
      rows.push(["hops from step", node.depth]);
      rows.push(["steps that reach it", (node.steps || []).join(", ") || "—"]);
    }
    host.hidden = false;
    host.innerHTML = '<button class="close" data-trace-close="1" aria-label="Close">×</button>'
      + '<h3>' + Engine.escapeHtml(node.label) + '</h3>'
      + '<div class="qual">' + Engine.escapeHtml(node.qual || node.id) + '</div>'
      + (node.doc ? '<div class="doc">' + Engine.escapeHtml(node.doc) + '</div>' : "")
      + '<div class="kv">' + rows.map(function (row) {
        return '<div><span>' + Engine.escapeHtml(row[0]) + '</span><span>' + Engine.escapeHtml(row[1]) + '</span></div>';
      }).join("") + '</div>';
  }

  /* Always populated, whether or not the canvas exists: it is the panel's
   * accessible text equivalent as well as its no-canvas fallback. */
  function renderFallback() {
    var host = $("trace-fallback");
    if (!host || !Engine) return;
    if (!panel.topology) {
      if (!host.innerHTML) host.innerHTML = '<div class="empty">Deriving the call graph…</div>';
      return;
    }
    host.innerHTML = Engine.fallbackHtml(panel.topology, panel.pipeline);
  }

  function syncModeChips() {
    if (typeof document === "undefined" || !document.querySelectorAll) return;
    var chips = document.querySelectorAll("#trace-modes [data-tracemode]");
    if (!chips || !chips.forEach) return;
    chips.forEach(function (chip) {
      chip.setAttribute("aria-pressed", String(chip.dataset.tracemode === panel.mode));
    });
  }

  function setMode(mode) {
    panel.mode = mode === "follow" ? "follow" : (mode === "free" ? "free" : "orbit");
    syncModeChips();
    start();
  }

  root.TracePanel = {
    mount: mount,
    update: update,
    setTopology: setTopology,
    setMode: setMode,
    replay: startReplay,
    reset: reset,
    paint: function () { resize(); start(); },
    select: function (id) { panel.selected = id; renderInspector(); draw(); },
    clearSelection: function () { panel.selected = null; renderInspector(); draw(); },
    state: panel
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
