/* MCP Switchboard: a playful, honest picture of the process boundary.
 *
 * The trace explorer answers "what happened when" and the tool matrix answers
 * "how is this wired". Neither shows the thing that is actually most surprising
 * about this loop: the *shape of the traffic*. One cycle fires hundreds of
 * JSON-RPC calls at a game server over a few seconds, most of them from one
 * step, in bursts, with wildly different latencies. That is a picture, not a
 * table, so this tab draws it.
 *
 * The rules it plays by, because a pretty view that lies is worse than no view:
 *
 *   - every packet is one row of state/tool_calls.ndjson. Nothing is generated,
 *     interpolated or looped to look busy. If the run made 3 calls, 3 fly.
 *   - flight time is the measured duration_ms, divided by an explicit replay
 *     speed the operator chooses. Very short calls are padded to a visible
 *     minimum, and the legend says so.
 *   - a call still in flight is drawn as in flight (pulsing, no landing), never
 *     as a completed round trip.
 *   - tools that were reachable but never called this run are listed as silent,
 *     not hidden, so absence stays visible.
 *   - when the deployed client predates boundary tracing, coverage says so.
 *
 * Everything above the panel section is DOM-free and deterministic so the same
 * arithmetic the page runs can be tested in JavaScriptCore.
 */
(function (root) {
  "use strict";

  // Which server tools change farm state. This is a property of the Farm Friends
  // API, not a guess about a name: mutations are the calls whose failure or
  // duplication costs coins or animals, so they are worth marking in red-orange.
  var WRITES = {
    collect_produce: 1, harvest: 1, feed_animals: 1, adopt_animal: 1,
    buy_feed: 1, sell: 1, propose_trade: 1, respond_to_trade: 1
  };

  var ICONS = {
    collect_produce: "\uD83E\uDD5A", harvest: "\uD83C\uDF3E", feed_animals: "\uD83C\uDF7D",
    adopt_animal: "\uD83D\uDC23", buy_feed: "\uD83D\uDED2", sell: "\uD83D\uDCB0",
    propose_trade: "\uD83E\uDD1D", respond_to_trade: "\uD83D\uDCEC",
    list_farm: "\uD83D\uDCDC", leaderboard: "\uD83C\uDFC6", farm_events: "\uD83D\uDCF0",
    "tools/list": "\uD83D\uDD0C"
  };

  var SPEEDS = [1, 4, 12];
  var PACKET_CAP = 240;         // drawn packets; the count is always stated
  var MIN_FLIGHT = 0.5;         // seconds on screen for a sub-frame call
  var MAX_LOOP = 26;            // a replay nobody waits through is not a replay
  var MIN_LOOP = 3.5;
  var BURST_WINDOW = 30;        // seconds of run replayed when the run is long
  var WINDOW_FLOOR = 45;        // below this a run is short enough to replay whole

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function clamp(value, low, high) {
    return value < low ? low : (value > high ? high : value);
  }

  function epoch(value) {
    if (!value) return null;
    var parsed = new Date(value).getTime();
    return isFinite(parsed) ? parsed / 1000 : null;
  }

  function seconds(value) {
    if (value == null || !isFinite(Number(value))) return "\u2014";
    value = Number(value);
    if (value < 0.001) return "<1ms";
    if (value < 1) return Math.round(value * 1000) + "ms";
    if (value < 10) return value.toFixed(2) + "s";
    if (value < 100) return value.toFixed(1) + "s";
    return Math.round(value) + "s";
  }

  function count(value) {
    value = Number(value) || 0;
    return value >= 10000 ? (value / 1000).toFixed(1) + "k" : String(value);
  }

  /* Stable colour per name so a step keeps its hue between polls and between
   * the rail, the packets and the lane summary. */
  function hue(name) {
    var text = String(name || ""), total = 0;
    for (var i = 0; i < text.length; i++) total = (total * 31 + text.charCodeAt(i)) % 360;
    return total;
  }

  function icon(tool) {
    if (ICONS[tool]) return ICONS[tool];
    return WRITES[tool] ? "\u2699\uFE0F" : "\uD83D\uDCE1";
  }

  function kind(tool) {
    return WRITES[tool] ? "write" : "read";
  }

  function percentile(sorted, fraction) {
    if (!sorted.length) return null;
    var at = clamp(Math.floor(fraction * (sorted.length - 1)), 0, sorted.length - 1);
    return sorted[at];
  }

  function short(value, limit) {
    var text = value == null ? "" : String(value);
    if (typeof value === "object") {
      try { text = JSON.stringify(value); } catch (error) { text = String(value); }
    }
    text = text.replace(/\s+/g, " ").trim();
    limit = limit || 68;
    return text.length > limit ? text.slice(0, limit - 1) + "\u2026" : text;
  }

  // ------------------------------------------------------------------- model

  function normalise(trace, origin, now, closedAt) {
    trace = trace || {};
    var raw = trace.calls || [];
    var fromActivity = false;
    if (!raw.length && trace.activity && trace.activity.length) {
      fromActivity = true;
      raw = trace.activity.map(function (row) {
        return {
          id: row.key, tool: row.tool, step: row.step, started_ts: row.ts,
          ended_ts: null, duration_ms: null, status: "event", arguments: {}, source: "activity"
        };
      });
    }
    return raw.map(function (call, index) {
      var start = epoch(call.started_ts);
      var end = epoch(call.ended_ts);
      if (end == null && call.duration_ms != null && start != null) {
        end = start + Number(call.duration_ms) / 1000;
      }
      var event = call.status === "event" || fromActivity;
      // A call with no end row cannot still be in flight once the run itself has
      // finished: the end row was never written (killed process, or the read
      // window cut it). Calling that "active" would invent a duration that grows
      // forever, which is how the first version reported 2,300s of boundary work
      // inside a 300s run.
      var unterminated = !event && end == null && closedAt != null;
      var active = !event && !unterminated && (call.status === "active" || end == null);
      var duration = unterminated ? null
        : (call.duration_ms != null ? Number(call.duration_ms) / 1000
          : (start != null && end != null ? Math.max(0, end - start)
            : (active && start != null ? Math.max(0, now - start) : null)));
      return {
        id: String(call.id == null ? "call-" + index : call.id),
        tool: String(call.tool || "unknown"),
        step: call.step || null,
        start: start,
        end: end,
        duration: duration,
        status: event ? "event"
          : (call.status === "error" ? "error" : (unterminated ? "unterminated" : (active ? "active" : "ok"))),
        active: active,
        event: event,
        unterminated: unterminated,
        arguments: call.arguments || {},
        result: call.result,
        error: call.error,
        startOffset: start == null || origin == null ? null : Math.max(0, start - origin),
        endOffset: end == null || origin == null ? null : Math.max(0, end - origin)
      };
    }).sort(function (a, b) {
      return (a.startOffset == null ? 1e18 : a.startOffset) - (b.startOffset == null ? 1e18 : b.startOffset)
        || a.tool.localeCompare(b.tool);
    });
  }

  /* Real concurrency from real timestamps: +1 at each start, -1 at each end.
   * This is the number the eye cannot get from a list of 455 rows, and it is the
   * honest measure of how parallel the boundary actually is. */
  function concurrency(calls, span) {
    var events = [];
    calls.forEach(function (call) {
      if (call.startOffset == null) return;
      events.push({ at: call.startOffset, delta: 1 });
      // Open until the run's end only if the call is genuinely still running. An
      // unterminated span closes at its start: its length is unknown, and holding
      // it open would draw concurrency that was never measured.
      var close = call.endOffset != null ? call.endOffset
        : (call.active ? span : call.startOffset);
      events.push({ at: close, delta: -1 });
    });
    events.sort(function (a, b) { return a.at - b.at || a.delta - b.delta; });
    var open = 0, peak = 0, peakAt = 0, series = [];
    events.forEach(function (event) {
      open += event.delta;
      if (open > peak) { peak = open; peakAt = event.at; }
      series.push({ at: event.at, open: Math.max(0, open) });
    });
    // Downsample to a fixed number of buckets so the chart is the same size for
    // 3 calls and for 900, and each bucket keeps the worst case it contains.
    var buckets = 96, width = span > 0 ? span / buckets : 1, samples = [];
    var cursor = 0, level = 0;
    for (var b = 0; b < buckets; b++) {
      var edge = (b + 1) * width, high = level;
      while (cursor < series.length && series[cursor].at <= edge) {
        level = series[cursor].open;
        if (level > high) high = level;
        cursor++;
      }
      samples.push({ at: b * width, open: high });
    }
    return { peak: peak, peakAt: peakAt, samples: samples };
  }

  function lanes(calls, toolNames, span, win) {
    function inWindow(call) {
      return !win || (call.startOffset != null && call.startOffset >= win.start && call.startOffset <= win.end);
    }
    var byTool = {};
    toolNames.forEach(function (name) {
      byTool[name] = {
        name: name, icon: icon(name), kind: kind(name), hue: hue(name),
        calls: [], durations: [], count: 0, errors: 0, active: 0, total: 0,
        first: null, last: null, steps: {}
      };
    });
    calls.forEach(function (call) {
      var lane = byTool[call.tool];
      if (!lane) {
        lane = byTool[call.tool] = {
          name: call.tool, icon: icon(call.tool), kind: kind(call.tool), hue: hue(call.tool),
          calls: [], durations: [], count: 0, errors: 0, active: 0, total: 0,
          first: null, last: null, steps: {}, unexpected: true
        };
        toolNames.push(call.tool);
      }
      lane.calls.push(call);
      lane.count++;
      if (inWindow(call)) lane.windowCount = (lane.windowCount || 0) + 1;
      if (call.status === "error") lane.errors++;
      if (call.active) lane.active++;
      if (call.duration != null) { lane.durations.push(call.duration); lane.total += call.duration; }
      if (call.step) lane.steps[call.step] = (lane.steps[call.step] || 0) + 1;
      if (call.startOffset != null && (lane.first == null || call.startOffset < lane.first)) lane.first = call.startOffset;
      if (call.startOffset != null && (lane.last == null || call.startOffset > lane.last)) lane.last = call.startOffset;
    });
    var busiest = 0;
    toolNames.forEach(function (name) { busiest = Math.max(busiest, byTool[name].count); });
    return toolNames.map(function (name) {
      var lane = byTool[name];
      lane.windowCount = lane.windowCount || 0;
      var sorted = lane.durations.slice().sort(function (a, b) { return a - b; });
      lane.median = percentile(sorted, 0.5);
      lane.p95 = percentile(sorted, 0.95);
      lane.slowest = sorted.length ? sorted[sorted.length - 1] : null;
      lane.fastest = sorted.length ? sorted[0] : null;
      lane.share = busiest ? lane.count / busiest : 0;
      lane.silent = lane.count === 0;
      lane.rate = span > 0 ? lane.count / span : 0;
      lane.topStep = Object.keys(lane.steps).sort(function (a, b) {
        return lane.steps[b] - lane.steps[a] || a.localeCompare(b);
      })[0] || null;
      return lane;
    }).sort(function (a, b) {
      return b.count - a.count || a.name.localeCompare(b.name);
    });
  }

  /* Drawn packets, thinned per lane rather than globally.
   *
   * A global stride is proportional but visually useless here: 86% of a cycle's
   * calls are adopt_animal, so it spent the whole budget on one lane and drew
   * nothing at all on lanes with one or two calls. Per-lane quotas keep the
   * relative density while guaranteeing that any lane with traffic shows traffic,
   * and errors, in-flight and unterminated calls are never the ones dropped. */
  function packets(calls, cap) {
    if (calls.length <= cap) return { drawn: calls.slice(), thinned: false };
    function interesting(call) {
      return call.status === "error" || call.active || call.unterminated;
    }
    var byTool = {}, order = [];
    calls.forEach(function (call) {
      if (!byTool[call.tool]) { byTool[call.tool] = []; order.push(call.tool); }
      byTool[call.tool].push(call);
    });
    var drawn = [];
    order.forEach(function (tool) {
      var list = byTool[tool];
      var quota = Math.max(1, Math.round(cap * list.length / calls.length));
      if (quota >= list.length) { drawn = drawn.concat(list); return; }
      var must = list.filter(interesting), rest = list.filter(function (call) { return !interesting(call); });
      var room = Math.max(0, quota - must.length);
      var stride = room > 0 ? rest.length / room : rest.length + 1;
      var kept = [];
      for (var at = 0; at < rest.length && kept.length < room; at += stride) {
        kept.push(rest[Math.floor(at)]);
      }
      drawn = drawn.concat(must, kept);
    });
    drawn.sort(function (a, b) { return (a.startOffset || 0) - (b.startOffset || 0); });
    return { drawn: drawn, thinned: true };
  }

  /* The densest BURST_WINDOW seconds of the run, by call starts. A 300-second
   * cycle compressed into a 26-second loop leaves ~3 packets in the air at once,
   * which reads as a broken panel; the burst is where the boundary is actually
   * interesting, and the header states which slice is on screen. */
  function busiestWindow(calls, width, span) {
    var starts = calls.map(function (call) { return call.startOffset; })
      .filter(function (at) { return at != null; }).sort(function (a, b) { return a - b; });
    if (!starts.length) return { start: 0, end: Math.min(width, span), count: 0 };
    var best = 0, bestCount = 0, tail = 0;
    for (var head = 0; head < starts.length; head++) {
      while (starts[head] - starts[tail] > width) tail++;
      if (head - tail + 1 > bestCount) { bestCount = head - tail + 1; best = starts[tail]; }
    }
    var start = clamp(best - 0.4, 0, Math.max(0, span - width));
    return { start: start, end: Math.min(span, start + width), count: bestCount };
  }


  function derive(topology, pipeline, trace, nowMs, state) {
    topology = topology || {};
    pipeline = pipeline || {};
    trace = trace || {};
    state = state || {};
    var now = (nowMs == null ? Date.now() : nowMs) / 1000;
    // x1 is real time: the honest default, and dense enough once the stage
    // replays a burst window rather than the whole cycle.
    var speed = SPEEDS.indexOf(Number(state.speed)) >= 0 ? Number(state.speed) : 1;

    var steps = (pipeline.steps || []).slice();
    var origin = epoch(pipeline.started_ts);
    if (origin == null) {
      for (var i = 0; i < steps.length && origin == null; i++) origin = epoch(steps[i].started_ts);
    }
    // "The run is over" is what makes an unpaired start row unterminated rather
    // than in flight, so it has to be decided before the calls are normalised.
    var closedAt = pipeline.status === "running" ? null : epoch(pipeline.finished_ts);
    var calls = normalise(trace, origin, now, closedAt);
    if (origin == null && calls.length) {
      origin = calls[0].start;
      calls = normalise(trace, origin, now, closedAt);
    }
    if (origin == null) origin = now;

    var end = epoch(pipeline.finished_ts) || now;
    calls.forEach(function (call) { if (call.end != null && call.end > end) end = call.end; });
    steps.forEach(function (step) {
      var stepEnd = epoch(step.ended_ts);
      if (stepEnd != null && stepEnd > end) end = stepEnd;
    });
    var span = Math.max(0.001, end - origin);

    // The stage replays a window of the run; the stat strip always describes the
    // whole run. Both are labelled, and the window is chosen from real call
    // starts rather than from a guess about where the interesting part is.
    var windowed = state.window !== "run" && span > WINDOW_FLOOR;
    var win = windowed ? busiestWindow(calls, BURST_WINDOW, span)
      : { start: 0, end: span, count: calls.length };
    var windowSpan = Math.max(0.001, win.end - win.start);

    var toolNames = (topology.nodes || []).filter(function (node) {
      return node.kind === "tool";
    }).map(function (node) { return node.label; });
    if (!toolNames.length) {
      var seen = {};
      calls.forEach(function (call) { seen[call.tool] = 1; });
      toolNames = Object.keys(seen);
    }
    var laneRows = lanes(calls, toolNames.slice(), span, windowed ? win : null);

    var stepRows = steps.map(function (step, order) {
      var mine = calls.filter(function (call) { return call.step === step.name; });
      var tools = {};
      mine.forEach(function (call) { tools[call.tool] = (tools[call.tool] || 0) + 1; });
      return {
        name: step.name,
        label: step.label || step.name,
        status: step.status || "pending",
        order: order,
        hue: hue(step.name),
        count: mine.length,
        tools: Object.keys(tools).sort(),
        startOffset: epoch(step.started_ts) == null ? null : Math.max(0, epoch(step.started_ts) - origin)
      };
    });
    var stepHue = {};
    stepRows.forEach(function (step) { stepHue[step.name] = step.hue; });

    var durations = calls.filter(function (call) { return call.duration != null && !call.active; })
      .map(function (call) { return call.duration; }).sort(function (a, b) { return a - b; });
    var boundarySeconds = 0;
    calls.forEach(function (call) { if (call.duration != null) boundarySeconds += call.duration; });
    var inFlight = calls.filter(function (call) { return call.active; });
    var stranded = calls.filter(function (call) { return call.unterminated; });
    var errors = calls.filter(function (call) { return call.status === "error"; });
    var flow = concurrency(calls, span);

    var slowest = null, fastest = null;
    calls.forEach(function (call) {
      if (call.duration == null || call.active) return;
      if (!slowest || call.duration > slowest.duration) slowest = call;
      if (!fastest || call.duration < fastest.duration) fastest = call;
    });
    var chattiest = stepRows.slice().sort(function (a, b) { return b.count - a.count; })[0] || null;
    var silent = laneRows.filter(function (lane) { return lane.silent; });

    // The stage replays a window; the stat strip always describes the whole run.
    var inWindow = calls.filter(function (call) {
      return call.startOffset != null && call.startOffset >= win.start && call.startOffset <= win.end;
    });
    var selection = packets(inWindow, PACKET_CAP);
    var loop = clamp(windowSpan / speed, MIN_LOOP, MAX_LOOP);
    // The replay is only honest if the whole window fits in the loop: when the raw
    // scale would overflow MAX_LOOP the effective speed is reported, not the chip.
    var effectiveSpeed = windowSpan / loop;

    var drawn = selection.drawn.map(function (call) {
      var lane = null;
      laneRows.forEach(function (candidate) { if (candidate.name === call.tool) lane = candidate; });
      var flight = call.duration == null ? MIN_FLIGHT : Math.max(MIN_FLIGHT, call.duration / effectiveSpeed);
      var reference = lane && lane.median ? lane.median : (durations.length ? percentile(durations, 0.5) : 1);
      return {
        id: call.id,
        tool: call.tool,
        step: call.step,
        status: call.status,
        active: call.active,
        unterminated: call.unterminated,
        duration: call.duration,
        startOffset: call.startOffset,
        launchAt: Math.max(0, (call.startOffset == null ? 0 : call.startOffset) - win.start) / effectiveSpeed,
        flight: flight,
        padded: call.duration != null && call.duration / effectiveSpeed < MIN_FLIGHT,
        // An unfinished call has no landing: park it at how far through a typical
        // call of its kind it currently is, and pulse there. An unterminated one
        // stops mid-wire and stays dim: nothing is known about where it got to.
        progress: call.unterminated ? 0.55
          : (call.active ? clamp(reference ? (call.duration || 0) / (reference * 2) : 0.5, 0.08, 0.92) : 1),
        hue: stepHue[call.step] == null ? hue(call.tool) : stepHue[call.step],
        summary: short(call.error || call.result || call.arguments)
      };
    });

    return {
      speed: speed,
      effectiveSpeed: effectiveSpeed,
      loop: loop,
      windowed: windowed,
      window: { start: win.start, end: win.end, span: windowSpan, calls: inWindow.length },
      paused: !!state.paused,
      focus: state.focus || null,
      run: pipeline.run == null ? null : pipeline.run,
      status: pipeline.status || "idle",
      running: pipeline.status === "running",
      origin: origin,
      span: span,
      steps: stepRows,
      lanes: laneRows,
      calls: calls,
      packets: drawn,
      thinned: selection.thinned,
      flow: flow,
      stats: {
        calls: calls.length,
        drawn: drawn.length,
        tools: laneRows.filter(function (lane) { return !lane.silent; }).length,
        silent: silent.length,
        errors: errors.length,
        inFlight: inFlight.length,
        unterminated: stranded.length,
        peak: flow.peak,
        peakAt: flow.peakAt,
        median: percentile(durations, 0.5),
        p95: percentile(durations, 0.95),
        boundarySeconds: boundarySeconds,
        wallSeconds: span,
        perMinute: span > 0 ? calls.length / span * 60 : 0,
        // >1 means calls genuinely overlapped: the loop spent less wall time at
        // the boundary than the calls themselves took.
        parallelism: span > 0 ? boundarySeconds / span : 0
      },
      hall: {
        slowest: slowest, fastest: fastest, busiest: laneRows[0] && laneRows[0].count ? laneRows[0] : null,
        chattiest: chattiest && chattiest.count ? chattiest : null, silent: silent
      },
      coverage: trace.coverage || (calls.length ? "full" : "unavailable")
    };
  }

  // -------------------------------------------------------------------- views

  function statsHtml(model) {
    var stats = model.stats;
    var coverage = model.coverage === "full" ? "every MCP call"
      : model.coverage === "mutations_only" ? "mutations only" : "no call telemetry";
    var cells = [
      ["Calls this run", count(stats.calls), stats.drawn < stats.calls
        ? "drawing " + stats.drawn + " of " + (model.windowed ? model.window.calls + " in the window" : stats.calls)
        : stats.tools + " of " + (stats.tools + stats.silent) + " tools used"],
      ["In flight now", String(stats.inFlight), stats.unterminated
        ? "peak " + stats.peak + " \u00b7 " + stats.unterminated + " unterminated span" + (stats.unterminated === 1 ? "" : "s")
        : "peak " + stats.peak + " concurrent"],
      ["Call rate", stats.perMinute >= 1 ? Math.round(stats.perMinute) + "/min" : stats.perMinute.toFixed(1) + "/min",
        seconds(stats.wallSeconds) + " of wall clock"],
      ["Median round trip", seconds(stats.median), "p95 " + seconds(stats.p95)],
      ["Boundary time", seconds(stats.boundarySeconds), stats.parallelism > 1.05
        ? "\u00d7" + stats.parallelism.toFixed(1) + " overlapped" : "mostly serial"],
      [stats.errors ? "Errors" : "Clean calls", stats.errors ? String(stats.errors) : count(stats.calls - stats.errors),
        "coverage: " + coverage]
    ];
    return '<div class="mw-stats" id="mw-stats">' + cells.map(function (cell, index) {
      var tone = index === 5 ? (stats.errors ? " bad" : " good") : "";
      return '<div class="mw-stat' + tone + '"><small>' + esc(cell[0]) + '</small><b>' + esc(cell[1])
        + '</b><span>' + esc(cell[2]) + '</span></div>';
    }).join("") + '</div>';
  }

  function railHtml(model) {
    var rows = model.steps.map(function (step) {
      var lit = step.count > 0;
      return '<button class="mw-pad ' + esc(step.status) + (lit ? " lit" : "")
        + (model.focus === "step:" + step.name ? " focus" : "") + '"'
        + ' style="--hue:' + step.hue + '" data-wire-focus="step:' + esc(step.name) + '"'
        + ' title="' + esc(step.label + " \u00b7 " + step.count + " MCP call(s)") + '">'
        + '<i class="mw-pad-dot"></i><span class="mw-pad-name">' + esc(step.label) + '</span>'
        + '<span class="mw-pad-n">' + (step.count ? count(step.count) : "\u2014") + '</span></button>';
    }).join("");
    return '<div class="mw-rail"><div class="mw-rail-head"><b>\uD83D\uDC0D farm/*.py</b><small>'
      + model.steps.length + ' pipeline steps</small></div>' + (rows
        || '<div class="mw-empty-small">No step timing recorded yet.</div>') + '</div>';
  }

  function laneTrackHtml(model, lane) {
    var mine = model.packets.filter(function (packet) { return packet.tool === lane.name; });
    var body = mine.map(function (packet) {
      // --x is where this call really started inside the replay window. It is what
      // a reduced-motion browser draws instead of a flight: the same data, still.
      var style = "--hue:" + packet.hue + ";--t0:" + packet.launchAt.toFixed(3)
        + "s;--dur:" + packet.flight.toFixed(3) + "s;--reach:" + (packet.progress * 100).toFixed(1)
        + "%;--x:" + clamp(model.loop > 0 ? packet.launchAt / model.loop * 100 : 0, 0, 100).toFixed(2) + "%";
      var label = packet.tool + " \u00b7 " + (packet.step || "unassigned") + " \u00b7 "
        + (packet.unterminated ? "no end row recorded" : (packet.active ? "in flight" : seconds(packet.duration)));
      return '<i class="mw-packet ' + esc(packet.status)
        + (packet.active ? " flying" : "") + (packet.unterminated ? " stranded" : "")
        + (packet.padded ? " padded" : "") + '" style="' + style + '" title="' + esc(label) + '"></i>';
    }).join("");
    return '<span class="mw-track">' + (lane.silent
      ? '<span class="mw-silent-note">reachable in code \u00b7 not called this run</span>' : body) + '</span>';
  }

  function laneRowHtml(model, lane) {
    var detail = lane.silent ? "silent"
      : count(lane.count) + " call" + (lane.count === 1 ? "" : "s")
        + (model.windowed ? " \u00b7 " + count(lane.windowCount) + " in window" : "")
        + " \u00b7 med " + seconds(lane.median)
        + (lane.errors ? " \u00b7 " + lane.errors + " error" + (lane.errors === 1 ? "" : "s") : "")
        + (lane.topStep ? " \u00b7 from " + lane.topStep : "");
    return '<div class="mw-lane' + (lane.silent ? " silent" : "") + (lane.errors ? " failing" : "")
      + (model.focus === "tool:" + lane.name ? " focus" : "") + '" style="--hue:' + lane.hue + '">'
      + '<button class="mw-lane-label" data-wire-focus="tool:' + esc(lane.name) + '">'
      + '<span class="mw-ico">' + lane.icon + '</span>'
      + '<span class="mw-lane-copy"><b>' + esc(lane.name)
      + '<i class="mw-kind ' + esc(lane.kind) + '">' + (lane.kind === "write" ? "mutates" : "reads") + '</i></b>'
      + '<small>' + esc(detail) + '</small></span>'
      + '</button>' + laneTrackHtml(model, lane)
      + '<span class="mw-lane-meter"><i style="width:' + (lane.share * 100).toFixed(1) + '%"></i></span></div>';
  }

  function stageHtml(model) {
    var visible = model.lanes.filter(function (lane) {
      if (!model.focus) return true;
      if (model.focus.indexOf("tool:") === 0) return lane.name === model.focus.slice(5);
      var step = model.focus.slice(5);
      return !!lane.steps[step];
    });
    if (!visible.length) visible = model.lanes;
    return '<div class="mw-stage' + (model.paused ? " paused" : "") + '" id="mw-stage"'
      + ' style="--loop:' + model.loop.toFixed(2) + 's">'
      + railHtml(model)
      + '<div class="mw-lanes" id="mw-lanes">' + visible.map(function (lane) {
        return laneRowHtml(model, lane);
      }).join("") + '</div>'
      + '<div class="mw-server"><div class="mw-server-tower' + (model.stats.inFlight ? " busy" : "") + '">'
        + '<b>\uD83C\uDFE1</b><span>Farm Friends</span><small>MCP server</small>'
        + '<span class="mw-server-pulse"></span></div>'
        + '<div class="mw-server-note">JSON-RPC over HTTP<br>' + count(model.stats.calls) + ' calls landed here</div></div>'
      + '</div>';
  }

  function flowHtml(model) {
    var samples = model.flow.samples, peak = Math.max(1, model.flow.peak);
    var width = 1000, height = 130;
    var points = samples.map(function (sample, index) {
      var x = samples.length <= 1 ? 0 : index / (samples.length - 1) * width;
      var y = height - (sample.open / peak) * (height - 12) - 4;
      return x.toFixed(1) + "," + y.toFixed(1);
    });
    var area = points.length
      ? "0," + height + " " + points.join(" ") + " " + width + "," + height
      : "0," + height + " " + width + "," + height;
    var marks = model.steps.filter(function (step) {
      return step.startOffset != null && step.count > 0;
    }).map(function (step) {
      return { step: step, x: clamp(step.startOffset / model.span, 0, 1) * width };
    });
    // Steps that start within a second of each other collide into unreadable
    // mush, so keep every line and drop only the colliding labels.
    var lastLabel = -1e9;
    var markHtml = marks.map(function (mark) {
      var label = "";
      if (mark.x - lastLabel > 52) {
        lastLabel = mark.x;
        label = '<text x="' + clamp(mark.x + 4, 0, width - 60).toFixed(1) + '" y="15">'
          + esc(mark.step.name) + '</text>';
      }
      return '<g class="mw-flow-mark" style="--hue:' + mark.step.hue + '"><line x1="' + mark.x.toFixed(1)
        + '" y1="6" x2="' + mark.x.toFixed(1) + '" y2="' + height + '"></line>' + label + '</g>';
    }).join("");
    // The slice the stage is replaying, marked on the run it came from.
    var band = model.windowed
      ? '<rect class="mw-flow-window" x="' + (model.window.start / model.span * width).toFixed(1)
        + '" y="0" width="' + (model.window.span / model.span * width).toFixed(1)
        + '" height="' + height + '"></rect>'
      : '';
    return '<div class="mw-flow" id="mw-flow"><svg viewBox="0 0 ' + width + ' ' + height
      + '" preserveAspectRatio="none" role="img" aria-label="Concurrent MCP calls over the run">'
      + band
      + '<polygon class="mw-flow-area" points="' + area + '"></polygon>'
      + '<polyline class="mw-flow-line" points="' + points.join(" ") + '"></polyline>'
      + markHtml + '</svg>'
      + '<div class="mw-flow-axis"><span>run start</span><span>peak ' + model.flow.peak
      + ' concurrent at +' + seconds(model.flow.peakAt) + '</span><span>+' + seconds(model.span) + '</span></div>'
      + (model.windowed ? '<div class="mw-flow-note">The lit band is the ' + seconds(model.window.span)
        + ' the switchboard is replaying.</div>' : '') + '</div>';
  }

  function hallHtml(model) {
    var hall = model.hall;
    function card(title, headline, detail, tone) {
      return '<div class="mw-card' + (tone ? " " + tone : "") + '"><small>' + esc(title) + '</small><b>'
        + esc(headline) + '</b><span>' + esc(detail) + '</span></div>';
    }
    var cards = [];
    cards.push(hall.busiest
      ? card("\uD83D\uDD01 Busiest wire", hall.busiest.icon + " " + hall.busiest.name,
          count(hall.busiest.count) + " calls \u00b7 " + seconds(hall.busiest.total) + " total \u00b7 med "
          + seconds(hall.busiest.median))
      : card("\uD83D\uDD01 Busiest wire", "\u2014", "no calls observed in this run"));
    cards.push(hall.slowest
      ? card("\uD83D\uDC0C Slowest round trip", seconds(hall.slowest.duration),
          hall.slowest.tool + " at +" + seconds(hall.slowest.startOffset)
          + " \u00b7 " + short(hall.slowest.arguments && Object.keys(hall.slowest.arguments).length
            ? hall.slowest.arguments : hall.slowest.result, 40),
          "warn")
      : card("\uD83D\uDC0C Slowest round trip", "\u2014", "nothing completed yet"));
    cards.push(hall.fastest
      ? card("\u26A1 Fastest round trip", seconds(hall.fastest.duration),
          hall.fastest.tool + " at +" + seconds(hall.fastest.startOffset), "good")
      : card("\u26A1 Fastest round trip", "\u2014", "nothing completed yet"));
    cards.push(hall.chattiest
      ? card("\uD83D\uDCE3 Chattiest step", hall.chattiest.label,
          count(hall.chattiest.count) + " calls \u00b7 " + (hall.chattiest.tools.join(", ") || "\u2014"))
      : card("\uD83D\uDCE3 Chattiest step", "\u2014", "no step issued a call"));
    cards.push(card("\uD83D\uDD07 Silent tools", String(hall.silent.length),
      hall.silent.length
        ? hall.silent.map(function (lane) { return lane.name; }).slice(0, 6).join(", ")
        : "every reachable tool was exercised"));
    cards.push(card("\uD83E\uDDEE Overlap", "\u00d7" + model.stats.parallelism.toFixed(1),
      seconds(model.stats.boundarySeconds) + " of call time inside " + seconds(model.stats.wallSeconds) + " of run"));
    return '<div class="mw-hall" id="mw-hall">' + cards.join("") + '</div>';
  }

  function toolbarHtml(model) {
    var chips = SPEEDS.map(function (speed) {
      return '<button data-wire-speed="' + speed + '" aria-pressed="' + (model.speed === speed) + '">\u00d7'
        + speed + '</button>';
    }).join("");
    var windows = '<div class="mw-chips" role="group" aria-label="Replay window">'
      + '<button data-wire-window="burst" aria-pressed="' + model.windowed + '">Busiest '
      + Math.round(BURST_WINDOW) + 's</button>'
      + '<button data-wire-window="run" aria-pressed="' + (!model.windowed) + '">Whole run</button></div>';
    var scope = model.windowed
      ? 'busiest ' + seconds(model.window.span) + ' of ' + seconds(model.span)
        + ' (+' + seconds(model.window.start) + ' \u2192 +' + seconds(model.window.end) + ')'
      : 'all ' + seconds(model.span);
    return '<div class="mw-toolbar"><div class="mw-title"><b>\uD83D\uDEF0 Live wire</b><span>run '
      + (model.run == null ? "\u2014" : "#" + esc(model.run)) + ' \u00b7 ' + esc(model.status)
      + ' \u00b7 replaying ' + scope + ' in ' + seconds(model.loop) + '</span></div>'
      + '<div class="mw-controls">' + windows
      + '<div class="mw-chips" role="group" aria-label="Replay speed">' + chips + '</div>'
      + '<button class="mw-toggle" data-wire-pause="1" aria-pressed="' + model.paused + '">'
      + (model.paused ? "\u25B6 Resume" : "\u23F8 Freeze") + '</button>'
      + (model.focus ? '<button class="mw-toggle" data-wire-focus="">\u2715 ' + esc(model.focus.split(":")[1]) + '</button>' : '')
      + '</div></div>';
  }

  function legendHtml(model) {
    var drawn = model.windowed
      ? model.stats.drawn + ' of the ' + model.window.calls + ' calls in this window ('
        + model.stats.calls + ' in the run)'
      : model.stats.drawn + ' of ' + model.stats.calls + ' calls';
    return '<div class="mw-legend"><span><i class="ok"></i> completed call</span>'
      + '<span><i class="flying"></i> in flight</span><span><i class="error"></i> error</span>'
      + (model.stats.unterminated ? '<span><i class="stranded"></i> start row with no end recorded</span>' : '')
      + '<span><i class="padded"></i> padded to ' + MIN_FLIGHT.toFixed(2) + 's to stay visible</span>'
      + '<span class="mw-legend-note">Packet colour is the pipeline step that issued the call. '
      + 'Flight time is the measured duration at \u00d7' + model.effectiveSpeed.toFixed(1)
      + ' replay speed. Drawing ' + drawn
      + (model.thinned ? ', thinned per lane so every busy wire keeps its shape' : '')
      + '. Lane counts and the numbers above cover the whole run.</span></div>';
  }

  function render(topology, pipeline, trace, state, nowMs) {
    var model = derive(topology, pipeline, trace, nowMs, state);
    if (model.coverage === "unavailable" && !model.calls.length) {
      return {
        model: model,
        html: '<div class="mw-empty">No MCP boundary telemetry yet.<br>'
          + '<small>state/tool_calls.ndjson is written by farm/mcp.py at Client.call(). '
          + 'The switchboard stays empty rather than animating calls that were never observed.</small></div>'
      };
    }
    return {
      model: model,
      html: toolbarHtml(model) + statsHtml(model) + stageHtml(model) + legendHtml(model)
        + '<div class="mw-split">' + flowHtml(model) + hallHtml(model) + '</div>'
    };
  }

  var Wire = {
    esc: esc, seconds: seconds, hue: hue, icon: icon, kind: kind,
    concurrency: concurrency, busiestWindow: busiestWindow, derive: derive, render: render,
    SPEEDS: SPEEDS, PACKET_CAP: PACKET_CAP, MIN_FLIGHT: MIN_FLIGHT, BURST_WINDOW: BURST_WINDOW
  };

  // -------------------------------------------------------------------- panel

  /* The panel repaints on the dashboard's 1s ticker, and innerHTML resets CSS
   * animations. So the animated stage is rebuilt only when the traffic actually
   * changed or when the replay loop has run out, while the numbers around it are
   * patched every tick. Without that split every packet would be stuck in its
   * first frame forever, which is exactly how the first version looked.
   */
  var panel = {
    root: null, topology: null, payload: null, fingerprint: null, fetching: null,
    signature: null, cycleStart: 0, built: false, drawn: 0,
    state: { speed: 1, paused: false, focus: null, window: "burst", model: null }
  };

  function byId(id) {
    return typeof document !== "undefined" && document.getElementById ? document.getElementById(id) : null;
  }

  function find(selector) {
    if (!panel.root || typeof panel.root.querySelector !== "function") return null;
    return panel.root.querySelector(selector);
  }

  /* An unseen panel gets no layout, and the 1s ticker would otherwise keep
   * rebuilding a replay nobody is watching. activateTab() rebuilds on entry. */
  function hidden() {
    var host = byId("tab-wire");
    if (host && host.hidden) return true;
    if (typeof document !== "undefined" && document.hidden) return true;
    return false;
  }

  function signature(model) {
    // Deliberately excludes the call set. A running cycle adds calls on every poll,
    // and rebuilding the stage for each one restarted every CSS animation, so no
    // packet ever finished its flight. New traffic is picked up at the next loop
    // boundary instead; the numbers around the stage still update every tick.
    return [model.run, model.status, model.speed, model.paused ? 1 : 0, model.focus || "",
      model.windowed ? "burst" : "run"].join("|");
  }

  function mount(options) {
    options = options || {};
    panel.root = byId(options.rootId || "mcp-wire");
    if (!panel.root) return false;
    if (typeof panel.root.addEventListener === "function") {
      panel.root.addEventListener("click", function (event) {
        var target = event.target && event.target.closest ? event.target : null;
        if (!target) return;
        var speed = target.closest("[data-wire-speed]");
        if (speed) {
          panel.state.speed = Number(speed.getAttribute("data-wire-speed")) || 1;
          rebuild();
          return;
        }
        if (target.closest("[data-wire-pause]")) { panel.state.paused = !panel.state.paused; rebuild(); return; }
        var window_ = target.closest("[data-wire-window]");
        if (window_) {
          panel.state.window = window_.getAttribute("data-wire-window") === "run" ? "run" : "burst";
          rebuild();
          return;
        }
        var focus = target.closest("[data-wire-focus]");
        if (focus) {
          var value = focus.getAttribute("data-wire-focus") || "";
          panel.state.focus = (!value || value === panel.state.focus) ? null : value;
          rebuild();
        }
      });
    }
    panel.root.innerHTML = '<div class="mw-empty">Waiting for MCP boundary telemetry\u2026</div>';
    return true;
  }

  function fetchTopology(fingerprint) {
    if (typeof fetch !== "function" || panel.fetching) return;
    panel.fetching = fetch("/api/topology", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("topology " + response.status);
        return response.json();
      })
      .then(function (graph) { panel.fetching = null; panel.fingerprint = fingerprint || null; setTopology(graph); })
      .catch(function (error) {
        panel.fetching = null;
        // The switchboard only needs topology to list silent tools, so a failed
        // fetch degrades to "tools observed this run" instead of blanking.
        if (!panel.topology) { panel.topology = { nodes: [], steps: [] }; paint(); }
        if (typeof console !== "undefined" && console.error) console.error("wire topology failed", error);
      });
  }

  function setTopology(graph) {
    panel.topology = graph || {};
    rebuild();
  }

  function update(payload) {
    panel.payload = payload || {};
    var trace = panel.payload.trace || {};
    if (!panel.topology || (trace.fingerprint && trace.fingerprint !== panel.fingerprint)) {
      fetchTopology(trace.fingerprint);
    }
    paint();
  }

  function rebuild() {
    panel.signature = null;
    panel.built = false;
    panel.drawn = 0;
    paint();
  }

  function paint() {
    if (!panel.root) return;
    if (panel.built && hidden()) return;
    var payload = panel.payload || {};
    var output = render(panel.topology || {}, payload.pipeline, payload.trace, panel.state, Date.now());
    var model = output.model;
    panel.state.model = model;
    var current = signature(model);
    var loopExpired = !panel.state.paused
      && Date.now() - panel.cycleStart > (model.loop + 0.6) * 1000;
    // First traffic of a run should not wait for a loop boundary: an empty stage
    // that stays empty for 26 seconds reads as a broken panel.
    var firstTraffic = panel.drawn === 0 && model.stats.drawn > 0;

    if (!panel.built || current !== panel.signature || loopExpired || firstTraffic) {
      panel.root.innerHTML = output.html;
      panel.signature = current;
      panel.cycleStart = Date.now();
      panel.drawn = model.stats.drawn;
      panel.built = true;
      return;
    }
    // Numbers only: leave the stage (and every running animation) alone.
    var stats = find("#mw-stats");
    if (stats) stats.outerHTML = statsHtml(model);
    var flow = find("#mw-flow");
    if (flow) flow.outerHTML = flowHtml(model);
    var hall = find("#mw-hall");
    if (hall) hall.outerHTML = hallHtml(model);
  }

  var WirePanel = {
    state: panel.state,
    mount: mount,
    setTopology: setTopology,
    update: update,
    paint: paint,
    rebuild: rebuild,
    setSpeed: function (speed) { panel.state.speed = Number(speed) || 1; rebuild(); },
    setWindow: function (value) { panel.state.window = value === "run" ? "run" : "burst"; rebuild(); },
    setPaused: function (paused) { panel.state.paused = !!paused; rebuild(); },
    focus: function (value) { panel.state.focus = value || null; rebuild(); },
    reset: function () {
      panel.state.speed = 1; panel.state.paused = false; panel.state.focus = null;
      panel.state.window = "burst"; rebuild();
    }
  };

  root.MCPWire = Wire;
  root.MCPWirePanel = WirePanel;
  if (typeof module !== "undefined" && module.exports) module.exports = Wire;
})(typeof globalThis !== "undefined" ? globalThis : this);
