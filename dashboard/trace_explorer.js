/* Farm Friends execution trace explorer.
 *
 * This deliberately is not a graph renderer. A single spatial picture was being
 * asked to communicate sequence, duration, hierarchy, source code and network
 * traffic at once, and the result was a hairball even after aggressive tuning.
 * Established trace viewers separate those questions:
 *
 *   - a nested timeline answers what happened, in what order, and for how long;
 *   - a dependency matrix answers which step can reach which external tool;
 *   - an inspector answers what arguments, result and Python path are behind one
 *     row or cell.
 *
 * The model functions are DOM-free and deterministic so JavaScriptCore can test
 * the same arithmetic the page uses. The panel layer only fetches topology,
 * stores interaction state and replaces one small DOM subtree on each poll.
 */
(function (root) {
  "use strict";

  var MODULE_ORDER = ["cycle", "rules", "parse", "growth", "mcp"];

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function toolLabel(value) {
    return esc(value).replace(/\//g, "/<wbr>").replace(/_/g, " ");
  }

  function clamp(value, low, high) {
    return value < low ? low : (value > high ? high : value);
  }

  function time(value) {
    if (!value) return null;
    var parsed = new Date(value).getTime();
    return isFinite(parsed) ? parsed / 1000 : null;
  }

  function seconds(value) {
    if (value == null || !isFinite(Number(value))) return "—";
    value = Number(value);
    if (value < 0.001) return "<1ms";
    if (value < 1) return Math.round(value * 1000) + "ms";
    if (value < 10) return value.toFixed(2) + "s";
    if (value < 100) return value.toFixed(1) + "s";
    return Math.round(value) + "s";
  }

  function offset(value) {
    if (value == null || !isFinite(value)) return "—";
    return "+" + seconds(Math.max(0, value));
  }

  function pct(value, total) {
    if (!total) return 0;
    return clamp(value / total * 100, 0, 100);
  }

  function json(value) {
    if (value == null || value === "") return "—";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch (error) { return String(value); }
  }

  function detailRows(detail) {
    var rows = [];
    if (!detail || typeof detail !== "object") return rows;
    Object.keys(detail).sort().forEach(function (key) {
      var value = detail[key];
      if (value == null || typeof value === "object") value = json(value);
      rows.push({ key: key, value: value });
    });
    return rows;
  }

  function prepare(topology) {
    topology = topology || {};
    var nodes = topology.nodes || [];
    var edges = topology.edges || [];
    var byId = {}, outgoing = {}, incoming = {};
    nodes.forEach(function (node) { byId[node.id] = node; });
    edges.forEach(function (edge) {
      (outgoing[edge.source] = outgoing[edge.source] || []).push(edge.target);
      (incoming[edge.target] = incoming[edge.target] || []).push(edge.source);
    });

    var stepOrder = {};
    (topology.steps || []).forEach(function (step, index) { stepOrder[step.name] = index; });
    var tools = nodes.filter(function (node) { return node.kind === "tool"; });
    tools.sort(function (a, b) {
      var ao = Math.min.apply(Math, (a.steps || []).map(function (name) {
        return stepOrder[name] == null ? 999 : stepOrder[name];
      }).concat([999]));
      var bo = Math.min.apply(Math, (b.steps || []).map(function (name) {
        return stepOrder[name] == null ? 999 : stepOrder[name];
      }).concat([999]));
      return ao - bo || a.label.localeCompare(b.label);
    });

    var functionsByStep = {};
    nodes.forEach(function (node) {
      if (node.kind !== "func") return;
      (node.steps || []).forEach(function (step) {
        (functionsByStep[step] = functionsByStep[step] || []).push(node);
      });
    });
    Object.keys(functionsByStep).forEach(function (step) {
      functionsByStep[step].sort(function (a, b) {
        return (a.depth || 0) - (b.depth || 0)
          || MODULE_ORDER.indexOf(a.module) - MODULE_ORDER.indexOf(b.module)
          || (a.line || 0) - (b.line || 0);
      });
    });

    return {
      topology: topology,
      nodes: nodes,
      edges: edges,
      byId: byId,
      outgoing: outgoing,
      incoming: incoming,
      tools: tools,
      functionsByStep: functionsByStep,
      stepOrder: stepOrder,
      stats: topology.stats || {}
    };
  }

  function route(index, stepName, toolName) {
    var start = "step:" + stepName, target = "tool:" + toolName;
    if (!index.byId[start] || !index.byId[target]) return [];

    function idsBetween(from, to) {
      var queue = [from], previous = {}, seen = {};
      seen[from] = true;
      while (queue.length) {
        var id = queue.shift();
        if (id === to) break;
        (index.outgoing[id] || []).forEach(function (next) {
          if (seen[next]) return;
          seen[next] = true;
          previous[next] = id;
          queue.push(next);
        });
      }
      if (!seen[to]) return [];
      var ids = [], cursor = to;
      while (cursor) {
        ids.unshift(cursor);
        if (cursor === from) break;
        cursor = previous[cursor];
      }
      return ids;
    }

    var ids = idsBetween(start, target);
    if (!ids.length) return [];
    // farm/topology.py emits a semantic edge from the calling cycle method to the
    // MCP tool *and* ordinary code edges through Client.call/rpc/_post. Shortest
    // path alone therefore skips the transport. Prefer the route through _post
    // when the direct caller can reach it: that is the explanation an operator
    // needs when a call is slow or fails at the boundary.
    var post = "mcp:Client._post";
    if (ids.length >= 2 && index.byId[post] && ids.indexOf(post) < 0) {
      var parent = ids[ids.length - 2];
      var transport = idsBetween(parent, post);
      if (transport.length > 1) {
        ids = ids.slice(0, -1).concat(transport.slice(1)).concat([target]);
      }
    }
    return ids.map(function (id) { return index.byId[id]; }).filter(Boolean);
  }

  function ownerForCall(call, steps, index) {
    if (call.step) return call.step;
    var at = time(call.started_ts);
    if (at != null) {
      for (var i = 0; i < steps.length; i++) {
        var start = time(steps[i].started_ts);
        var end = time(steps[i].ended_ts);
        if (start != null && at >= start - 0.2 && (end == null || at <= end + 0.5)) return steps[i].name;
      }
    }
    var node = index.byId["tool:" + call.tool];
    return node && node.steps && node.steps.length === 1 ? node.steps[0] : null;
  }

  function normaliseCalls(trace, steps, index, origin, now, openedAt, closedAt) {
    trace = trace || {};
    var raw = trace.calls || [];
    if (!raw.length && trace.activity && trace.activity.length) {
      raw = trace.activity.map(function (row) {
        return {
          id: row.key, tool: row.tool, step: row.step,
          started_ts: row.ts, ended_ts: null, duration_ms: null,
          status: "event", arguments: {}, result: null, error: null, source: "activity"
        };
      });
    }
    return raw.map(function (call, indexInRun) {
      var start = time(call.started_ts);
      var end = time(call.ended_ts);
      if (end == null && call.duration_ms != null && start != null) end = start + Number(call.duration_ms) / 1000;
      // The boundary log is a rolling tail and can already contain traffic from
      // the next process after this run has closed. Backend run timestamps are
      // authoritative: never attach those calls to the completed run.
      if ((openedAt != null && start != null && start < openedAt)
          || (closedAt != null && start != null && start > closedAt)) return null;
      var event = call.status === "event";
      var unterminated = !event && end == null && closedAt != null;
      var active = !event && !unterminated
        && (call.status === "active" || end == null);
      var duration = unterminated ? null
        : (call.duration_ms != null ? Number(call.duration_ms) / 1000
          : (start != null && end != null ? Math.max(0, end - start)
            : (active && start != null ? Math.max(0, now - start) : null)));
      var result = {
        id: call.id || ("call-" + indexInRun),
        tool: String(call.tool || "unknown"),
        step: null,
        started_ts: call.started_ts || null,
        ended_ts: call.ended_ts || null,
        start: start,
        end: end,
        duration: duration,
        status: unterminated ? "unterminated" : (call.status || (active ? "active" : "ok")),
        active: active,
        unterminated: unterminated,
        arguments: call.arguments || {},
        result: call.result,
        error: call.error,
        source: call.source || "boundary"
      };
      result.step = ownerForCall(call, steps, index);
      result.startOffset = start == null || origin == null ? null : Math.max(0, start - origin);
      result.endOffset = end == null || origin == null ? (active ? Math.max(0, now - origin) : result.startOffset)
        : Math.max(0, end - origin);
      return result;
    }).filter(Boolean).sort(function (a, b) {
      return (a.start == null ? 1e30 : a.start) - (b.start == null ? 1e30 : b.start)
        || a.tool.localeCompare(b.tool);
    });
  }

  function groupCalls(calls) {
    var groups = {}, order = [];
    calls.forEach(function (call) {
      var key = (call.step || "unassigned") + "|" + call.tool;
      if (!groups[key]) {
        groups[key] = {
          key: key, step: call.step, tool: call.tool, instances: [],
          errors: 0, active: 0, duration: 0, firstOffset: call.startOffset
        };
        order.push(key);
      }
      var group = groups[key];
      group.instances.push(call);
      if (call.status === "error") group.errors++;
      if (call.active) group.active++;
      if (call.duration != null) group.duration += call.duration;
      if (group.firstOffset == null || (call.startOffset != null && call.startOffset < group.firstOffset)) {
        group.firstOffset = call.startOffset;
      }
    });
    return order.map(function (key) { return groups[key]; });
  }

  function metadataValue(trace, names) {
    var sources = [trace || {}, trace && trace.metadata, trace && trace.meta];
    for (var si = 0; si < sources.length; si++) {
      var source = sources[si];
      if (!source || typeof source !== "object") continue;
      for (var ni = 0; ni < names.length; ni++) {
        if (source[names[ni]] != null) return source[names[ni]];
      }
    }
    return null;
  }

  function coverageFor(trace, calls) {
    trace = trace || {};
    var declared = trace.coverage || (calls.length ? "full" : "unavailable");
    var truncated = metadataValue(trace, ["calls_truncated", "truncated", "has_more"]) === true;
    var partial = metadataValue(trace, ["calls_partial", "partial"]) === true
      || metadataValue(trace, ["complete"]) === false;
    var totalValue = metadataValue(trace, ["calls_total", "total_calls", "total"]);
    var returnedValue = metadataValue(trace, ["calls_returned", "returned_calls", "returned"]);
    var total = totalValue == null || !isFinite(Number(totalValue)) ? null : Number(totalValue);
    var returned = returnedValue == null || !isFinite(Number(returnedValue)) ? null : Number(returnedValue);
    if (total != null && returned != null && returned < total) truncated = true;
    if (declared === "partial" || truncated || partial) declared = "partial";
    return {
      name: declared,
      truncated: truncated,
      partial: partial,
      total: total,
      returned: returned
    };
  }

  function derive(topology, pipeline, trace, nowMs) {
    var index = prepare(topology);
    pipeline = pipeline || {};
    trace = trace || {};
    var now = (nowMs == null ? Date.now() : nowMs) / 1000;
    var steps = (pipeline.steps || []).slice();
    if (!steps.length) {
      steps = (topology && topology.steps || []).map(function (step) {
        return { name: step.name, label: step.name, hint: "", status: "pending", detail: {} };
      });
    }
    var origin = time(trace.run_started_ts);
    if (origin == null) origin = time(pipeline.started_ts);
    if (origin == null) {
      for (var oi = 0; oi < steps.length && origin == null; oi++) origin = time(steps[oi].started_ts);
    }
    var status = trace.effective_status || pipeline.effective_status || pipeline.status || "idle";
    var running = status === "running";
    var finished = time(trace.run_finished_ts);
    if (finished == null) finished = time(pipeline.finished_ts);
    // A backend can mark a stale raw `running` state as effectively closed. In
    // that case its last progress timestamp is the only defensible endpoint.
    var closedAt = running ? null : finished;
    if (closedAt == null && !running) {
      closedAt = time(trace.run_updated_ts);
      if (closedAt == null) closedAt = time(pipeline.updated_ts);
    }
    var calls = normaliseCalls(trace, steps, index, origin, now, origin, closedAt);
    if (origin == null && calls.length) {
      origin = calls[0].start;
      calls = normaliseCalls(trace, steps, index, origin, now, origin, closedAt);
    }
    if (origin == null) origin = now;

    // A closed run ends at the backend boundary even if a rolling call payload
    // contains later timestamps. Only live runs expand to observed step/call ends.
    var observedEnd = closedAt != null ? closedAt : now;
    if (running || closedAt == null) {
      steps.forEach(function (step) {
        var end = time(step.ended_ts);
        if (end != null && end > observedEnd) observedEnd = end;
      });
      calls.forEach(function (call) {
        if (call.end != null && call.end > observedEnd) observedEnd = call.end;
      });
    }
    var elapsed = Math.max(0, observedEnd - origin);
    var baseline = pipeline.baseline || {};
    var expected = 0;
    Object.keys(baseline).forEach(function (name) { expected += Number(baseline[name]) || 0; });
    var horizon = running ? Math.max(15, expected, elapsed * 1.08) : Math.max(1, elapsed);

    var stepMeta = {};
    (topology && topology.steps || []).forEach(function (step) { stepMeta[step.name] = step; });
    var groups = groupCalls(calls);
    var groupsByStep = {};
    groups.forEach(function (group) {
      (groupsByStep[group.step || "unassigned"] = groupsByStep[group.step || "unassigned"] || []).push(group);
    });

    var modelSteps = steps.map(function (step, order) {
      var start = time(step.started_ts), end = time(step.ended_ts);
      var startOffset = start == null ? null : Math.max(0, start - origin);
      var endOffset = end == null ? (step.status === "active" ? elapsed : startOffset) : Math.max(0, end - origin);
      var duration = step.seconds != null ? Number(step.seconds)
        : (startOffset != null && endOffset != null ? Math.max(0, endOffset - startOffset) : null);
      var meta = stepMeta[step.name] || {};
      return {
        name: step.name,
        label: step.label || step.name,
        hint: step.hint || "",
        note: step.note || null,
        detail: step.detail || {},
        status: step.status || "pending",
        order: order,
        start: start,
        end: end,
        startOffset: startOffset,
        endOffset: endOffset,
        duration: duration,
        left: startOffset == null ? 0 : pct(startOffset, horizon),
        width: startOffset == null || endOffset == null ? 0 : Math.max(0.25, pct(endOffset - startOffset, horizon)),
        functions: index.functionsByStep[step.name] || [],
        tools: meta.tools || [],
        modules: meta.modules || [],
        calls: groupsByStep[step.name] || []
      };
    });

    var toolRows = index.tools.map(function (tool) {
      var count = 0, errors = 0;
      calls.forEach(function (call) {
        if (call.tool === tool.label) { count++; if (call.status === "error") errors++; }
      });
      return { name: tool.label, steps: tool.steps || [], count: count, errors: errors };
    });

    var boundarySeconds = 0;
    calls.forEach(function (call) { if (call.duration != null) boundarySeconds += call.duration; });
    var done = modelSteps.filter(function (step) {
      return step.status === "done" || step.status === "skipped";
    }).length;
    var active = null;
    if (running) {
      modelSteps.forEach(function (step) { if (step.status === "active") active = step; });
    }
    var coverage = coverageFor(trace, calls);

    return {
      index: index,
      pipeline: pipeline,
      trace: trace,
      steps: modelSteps,
      calls: calls,
      groups: groups,
      groupsByStep: groupsByStep,
      tools: toolRows,
      origin: origin,
      elapsed: elapsed,
      horizon: horizon,
      expected: expected,
      running: running,
      status: status,
      run: pipeline.run,
      done: done,
      active: active,
      boundarySeconds: boundarySeconds,
      coverage: coverage.name,
      coverageMeta: coverage
    };
  }

  function styleSpan(left, width) {
    return "--left:" + clamp(left || 0, 0, 100).toFixed(3) + "%;--width:"
      + clamp(width || 0, 0, 100).toFixed(3) + "%";
  }

  function statusLabel(status) {
    return status === "done" ? "completed" : status;
  }

  function ruler(model) {
    var ticks = [0, 0.25, 0.5, 0.75, 1];
    return '<div class="te-row te-ruler"><div class="te-label"><b>Span</b><span>measured execution</span></div>'
      + '<div class="te-track">' + ticks.map(function (part) {
        return '<span class="te-ruler-tick" style="left:' + (part * 100) + '%"><i></i><b>'
          + esc(offset(model.horizon * part)) + '</b></span>';
      }).join("") + '</div></div>';
  }

  function callTicks(group, model) {
    return group.instances.map(function (call, index) {
      var left = call.startOffset == null ? 0 : pct(call.startOffset, model.horizon);
      var width = call.duration == null ? 0 : Math.max(0.18, pct(call.duration, model.horizon));
      var title = call.tool + " " + offset(call.startOffset) + " · " + seconds(call.duration);
      return '<span class="te-call-span ' + esc(call.status) + (call.active ? ' active' : '')
        + '" style="' + styleSpan(left, width) + '" title="' + esc(title)
        + '" aria-label="' + esc(title) + '" data-call-index="' + index + '"></span>';
    }).join("");
  }

  function traceView(model, state) {
    var html = '<div class="te-trace-scroll" id="trace-main-scroll">' + ruler(model);
    model.steps.forEach(function (step) {
      var selected = state.selected === "step:" + encodeURIComponent(step.name);
      var duration = step.status === "skipped" ? "skipped" : seconds(step.duration);
      html += '<button class="te-row te-step ' + esc(step.status) + (selected ? ' selected' : '')
        + '" data-trace-select="step:' + encodeURIComponent(step.name) + '">'
        + '<span class="te-label"><i class="te-status"></i><span class="te-step-copy"><b>'
        + (step.order + 1) + '. ' + esc(step.label) + '</b><small>'
        + esc(duration) + ' · ' + step.functions.length + ' functions'
        + (step.tools.length ? ' · ' + step.tools.length + ' tool' + (step.tools.length === 1 ? '' : 's') : ' · local only')
        + '</small></span></span><span class="te-track">';
      if (step.startOffset != null && step.status !== "skipped") {
        html += '<i class="te-step-span" style="' + styleSpan(step.left, step.width) + '"></i>';
      } else if (step.status === "skipped" && step.endOffset != null) {
        html += '<i class="te-skip-mark" style="left:' + pct(step.endOffset, model.horizon).toFixed(3) + '%"></i>';
      }
      html += '</span></button>';

      step.calls.forEach(function (group) {
        var id = "call:" + encodeURIComponent(group.step || "") + "|" + encodeURIComponent(group.tool);
        var callSelected = state.selected === id;
        html += '<button class="te-row te-call ' + (group.errors ? 'error' : (group.active ? 'active' : 'ok'))
          + (callSelected ? ' selected' : '') + '" data-trace-select="' + id + '">'
          + '<span class="te-label"><i class="te-boundary">↗</i><span class="te-step-copy"><b>'
          + esc(group.tool) + (group.instances.length > 1 ? ' ×' + group.instances.length : '')
          + '</b><small>MCP boundary · ' + seconds(group.duration)
          + (group.errors ? ' · ' + group.errors + ' error' + (group.errors === 1 ? '' : 's') : '')
          + '</small></span></span><span class="te-track">' + callTicks(group, model) + '</span></button>';
      });
    });
    if (model.groupsByStep.unassigned) {
      model.groupsByStep.unassigned.forEach(function (group) {
        var id = "call:|" + encodeURIComponent(group.tool);
        html += '<button class="te-row te-call unassigned" data-trace-select="' + id + '"><span class="te-label">'
          + '<i class="te-boundary">?</i><span class="te-step-copy"><b>' + esc(group.tool) + '</b>'
          + '<small>could not assign to a step</small></span></span><span class="te-track">'
          + callTicks(group, model) + '</span></button>';
      });
    }
    return html + '</div>';
  }

  function matrixView(model, state) {
    var html = '<div class="te-matrix-scroll" id="trace-matrix-scroll"><table class="te-matrix">'
      + '<colgroup><col class="te-col-step"><col class="te-col-internal">'
      + model.tools.map(function () { return '<col class="te-col-tool">'; }).join('') + '</colgroup>'
      + '<thead><tr><th class="te-step-head">Pipeline step</th><th class="te-internal-head">Internal work</th>'
      + '<th class="te-boundary-head" colspan="' + model.tools.length + '">MCP boundary → Farm Friends server</th></tr>'
      + '<tr><th></th><th>functions · modules</th>';
    model.tools.forEach(function (tool, index) {
      var label = toolLabel(tool.name);
      html += '<th class="te-tool-head' + (index === 0 ? ' boundary' : '') + '"><span>'
        + label + '</span>'
        + (tool.count ? '<b>' + tool.count + '</b>' : '') + '</th>';
    });
    html += '</tr></thead><tbody>';
    model.steps.forEach(function (step) {
      html += '<tr class="' + esc(step.status) + '"><th><button data-trace-select="step:'
        + encodeURIComponent(step.name) + '"><i class="te-status"></i><span>' + esc(step.label)
        + '</span></button></th><td class="te-internal"><b>' + step.functions.length + '</b><span>'
        + esc(step.modules.join(" · ") || "—") + '</span></td>';
      model.tools.forEach(function (tool, index) {
        var reachable = tool.steps.indexOf(step.name) >= 0;
        var group = null;
        (model.groupsByStep[step.name] || []).forEach(function (candidate) {
          if (candidate.tool === tool.name) group = candidate;
        });
        var id = "call:" + encodeURIComponent(step.name) + "|" + encodeURIComponent(tool.name);
        var title = step.label + " → " + tool.name + (group ? ": " + group.instances.length + " call(s) this run" : reachable ? ": reachable in code" : ": no path");
        html += '<td class="te-matrix-cell' + (index === 0 ? ' boundary' : '') + '">';
        if (reachable || group) {
          html += '<button class="' + (reachable ? 'reachable' : '') + (group ? ' called' : '')
            + (group && group.errors ? ' error' : '') + (state.selected === id ? ' selected' : '')
            + '" data-trace-select="' + id + '" title="' + esc(title) + '">'
            + (group ? '<b>' + group.instances.length + '</b>' : '<i></i>') + '</button>';
        }
        html += '</td>';
      });
      html += '</tr>';
    });
    return html + '</tbody></table></div><div class="te-matrix-key"><span><i class="reachable"></i> reachable in code</span>'
      + '<span><i class="called">1</i> observed this run</span><span class="muted">Blank means the step has no path to that tool.</span></div>';
  }

  function tags(values, kind) {
    return (values || []).map(function (value) {
      return '<span class="te-tag ' + esc(kind || '') + '">' + esc(value) + '</span>';
    }).join("");
  }

  function pathHtml(path) {
    if (!path.length) return '<div class="te-empty-small">No static path resolved.</div>';
    return '<div class="te-path">' + path.map(function (node, index) {
      var label = node.kind === "step" ? node.label : (node.qual || node.label);
      return (index ? '<i>→</i>' : '') + '<span class="' + esc(node.kind) + '">' + esc(label) + '</span>';
    }).join("") + '</div>';
  }

  function stepInspector(step, model) {
    var rows = detailRows(step.detail);
    var calls = [];
    step.calls.forEach(function (group) { calls = calls.concat(group.instances); });
    var modules = {};
    step.functions.forEach(function (fn) { modules[fn.module] = (modules[fn.module] || 0) + 1; });
    return '<div class="te-inspect-kind">PIPELINE SPAN</div><h3>' + esc(step.label) + '</h3>'
      + '<div class="te-inspect-sub"><span class="te-pill ' + esc(step.status) + '">' + esc(statusLabel(step.status))
      + '</span><span>' + offset(step.startOffset) + '</span><span>' + seconds(step.duration) + '</span></div>'
      + (step.hint ? '<p>' + esc(step.hint) + '</p>' : '')
      + (step.note ? '<div class="te-note">' + esc(step.note) + '</div>' : '')
      + (rows.length ? '<dl>' + rows.map(function (row) {
        return '<div><dt>' + esc(row.key) + '</dt><dd>' + esc(row.value) + '</dd></div>';
      }).join("") + '</dl>' : '')
      + '<section><h4>Server boundary</h4><div class="te-inspect-stat"><b>' + calls.length + '</b> observed call'
      + (calls.length === 1 ? '' : 's') + ' · ' + (step.tools.length ? esc(step.tools.join(", ")) : 'no reachable MCP tool') + '</div></section>'
      + '<section><h4>Static Python path <small>reachability, not measured time</small></h4>'
      + '<div class="te-module-summary">' + Object.keys(modules).sort().map(function (module) {
        return '<span><b>' + modules[module] + '</b> ' + esc(module) + '.py</span>';
      }).join("") + '</div><div class="te-function-list">'
      + step.functions.map(function (fn) {
        return '<button data-trace-select="node:' + encodeURIComponent(fn.id) + '" title="' + esc(fn.qual) + '">'
          + esc(fn.label) + '<small>' + esc(fn.module) + ':' + fn.line + '</small></button>';
      }).join("") + '</div></section>';
  }

  function callInspector(stepName, toolName, model) {
    var group = null;
    (model.groupsByStep[stepName || "unassigned"] || []).forEach(function (candidate) {
      if (candidate.tool === toolName) group = candidate;
    });
    var instances = group ? group.instances : [];
    var step = null;
    model.steps.forEach(function (candidate) { if (candidate.name === stepName) step = candidate; });
    var path = route(model.index, stepName, toolName);
    return '<div class="te-inspect-kind">MCP BOUNDARY</div><h3>' + esc(toolName) + '</h3>'
      + '<div class="te-inspect-sub"><span class="te-pill tool">external</span><span>'
      + (step ? esc(step.label) : 'unassigned') + '</span><span>' + instances.length + ' call'
      + (instances.length === 1 ? '' : 's') + '</span></div>'
      + '<section><h4>Call path <small>derived from farm/*.py</small></h4>' + pathHtml(path) + '</section>'
      + '<section><h4>Observed instances</h4>'
      + (instances.length ? '<div class="te-instances">' + instances.map(function (call, index) {
        var output = call.error || call.result;
        return '<details' + (instances.length <= 3 || call.status === 'error' ? ' open' : '') + '><summary>'
          + '<span>#' + (index + 1) + ' ' + offset(call.startOffset) + '</span><b>' + seconds(call.duration) + '</b>'
          + '<i class="' + esc(call.status) + '">' + esc(call.status) + '</i></summary>'
          + '<div><label>arguments</label><pre>' + esc(json(call.arguments)) + '</pre>'
          + '<label>' + (call.error ? 'error' : 'result') + '</label><pre>' + esc(json(output)) + '</pre>'
          + '<small>source: ' + esc(call.source) + '</small></div></details>';
      }).join("") + '</div>' : '<div class="te-note">Reachable in code; no call was observed in this run.</div>')
      + '</section>';
  }

  function nodeInspector(id, model) {
    var node = model.index.byId[id];
    if (!node) return '<div class="te-empty-small">Source node not found.</div>';
    var outgoing = (model.index.outgoing[id] || []).map(function (next) {
      var n = model.index.byId[next]; return n ? (n.qual || n.label) : next;
    });
    return '<div class="te-inspect-kind">STATIC CODE</div><h3>' + esc(node.label) + '</h3>'
      + '<div class="te-inspect-sub"><span class="te-pill code">' + esc(node.kind) + '</span><span>'
      + esc(node.module) + '.py:' + (node.line || '—') + '</span><span>' + (node.loc || 0) + ' LOC</span></div>'
      + '<p class="mono">' + esc(node.qual || node.id) + '</p>'
      + (node.doc ? '<p>' + esc(node.doc) + '</p>' : '')
      + '<section><h4>Steps that reach it</h4><div class="te-tags">' + tags(node.steps || [], "step") + '</div></section>'
      + '<section><h4>Calls from here</h4><div class="te-tags">' + tags(outgoing, "code") + '</div></section>'
      + '<div class="te-note">Static reachability only. This function is not presented as a measured runtime span.</div>';
  }

  function inspector(model, state) {
    var selected = state.selected;
    if (!selected && model.active) selected = "step:" + encodeURIComponent(model.active.name);
    var body = '';
    if (selected && selected.indexOf("step:") === 0) {
      var name = decodeURIComponent(selected.slice(5));
      var step = null;
      model.steps.forEach(function (candidate) { if (candidate.name === name) step = candidate; });
      body = step ? stepInspector(step, model) : '';
    } else if (selected && selected.indexOf("call:") === 0) {
      var pieces = selected.slice(5).split("|");
      body = callInspector(decodeURIComponent(pieces[0] || ""), decodeURIComponent(pieces[1] || ""), model);
    } else if (selected && selected.indexOf("node:") === 0) {
      body = nodeInspector(decodeURIComponent(selected.slice(5)), model);
    }
    if (!body) {
      body = '<div class="te-inspect-kind">TRACE INSPECTOR</div><h3>Select a span or matrix cell</h3>'
        + '<p>Measured step timing, tool arguments and results appear here. Python functions are labelled as static reachability.</p>';
    }
    return '<aside class="te-inspector" id="trace-inspect"><button class="te-close" data-trace-close aria-label="Close inspector">×</button>'
      + body + '</aside>';
  }

  function summary(model) {
    var coverage = model.coverage === "full" ? "all MCP calls"
      : model.coverage === "partial" ? "partial MCP call data"
      : model.coverage === "mutations_only" ? "mutation calls only" : "call telemetry unavailable";
    var coverageDetail = model.coverage === "full" ? "instrumented at Client.call()"
      : model.coverage === "partial" && model.coverageMeta.truncated
        ? (model.coverageMeta.returned != null && model.coverageMeta.total != null
          ? "truncated payload · " + model.coverageMeta.returned + " of " + model.coverageMeta.total + " calls returned"
          : "backend reports a truncated call payload")
      : model.coverage === "partial" ? "backend reports partial call telemetry"
      : "deploy the current farm/ release for complete tracing";
    // Do not use generic state classes such as `full`: the host dashboard uses
    // `.full { grid-column:1/-1 }` for cards. Keep explorer layout state scoped.
    var coverageClass = model.coverage === "full" ? "coverage-full" : "coverage-partial";
    return '<div class="te-summary">'
      + '<div><small>Run</small><b>' + (model.run == null ? '—' : '#' + esc(model.run)) + '</b><span class="te-pill '
      + esc(model.status) + '">' + esc(model.status) + '</span></div>'
      + '<div><small>Now executing</small><b>' + esc(model.active ? model.active.label : (model.running ? 'between steps' : 'complete')) + '</b><span>'
      + model.done + ' / ' + model.steps.length + ' steps reached</span></div>'
      + '<div><small>Elapsed</small><b>' + seconds(model.elapsed) + '</b><span>timeline to ' + seconds(model.horizon) + '</span></div>'
      + '<div><small>MCP boundary</small><b>' + model.calls.length + ' calls</b><span>' + seconds(model.boundarySeconds) + ' measured</span></div>'
      + '<div class="te-coverage ' + coverageClass + '"><small>Coverage</small><b>' + esc(coverage) + '</b><span>'
      + esc(coverageDetail) + '</span></div></div>';
  }

  function render(topology, pipeline, trace, state, nowMs) {
    state = state || { view: "trace", selected: null };
    var model = derive(topology, pipeline, trace, nowMs);
    var view = state.view === "matrix" ? "matrix" : "trace";
    var main = view === "matrix" ? matrixView(model, state) : traceView(model, state);
    var html = '<div class="te-toolbar"><div class="te-view-switch" role="group" aria-label="Execution view">'
      + '<button data-trace-view="trace" aria-pressed="' + (view === 'trace') + '">Run trace</button>'
      + '<button data-trace-view="matrix" aria-pressed="' + (view === 'matrix') + '">Tool matrix</button></div>'
      + '<div class="te-legend"><span><i class="step"></i> pipeline span</span><span><i class="call"></i> observed MCP call</span>'
      + '<span><i class="static"></i> static reachability</span></div></div>'
      + summary(model) + '<div class="te-workspace"><div class="te-main">' + main + '</div>' + inspector(model, state) + '</div>';
    return { html: html, model: model };
  }

  var Explorer = {
    esc: esc,
    toolLabel: toolLabel,
    time: time,
    seconds: seconds,
    prepare: prepare,
    route: route,
    derive: derive,
    render: render
  };

  // ---------------------------------------------------------------- panel

  var panel = {
    root: null,
    topology: null,
    payload: null,
    fingerprint: null,
    fetching: null,
    state: { view: "trace", selected: null, model: null }
  };

  function fetchTopology(fingerprint) {
    if (typeof fetch !== "function" || panel.fetching) return;
    panel.fetching = fetch("/api/topology", { cache: "no-store" })
      .then(function (response) { if (!response.ok) throw new Error("topology " + response.status); return response.json(); })
      .then(function (graph) { panel.fetching = null; setTopology(graph); panel.fingerprint = fingerprint || null; })
      .catch(function (error) {
        panel.fetching = null;
        if (panel.root) panel.root.innerHTML = '<div class="te-empty">Could not load execution topology: ' + esc(error.message || error) + '</div>';
      });
  }

  function mount(options) {
    options = options || {};
    panel.root = typeof document !== "undefined" ? document.getElementById(options.rootId || "trace-explorer") : null;
    if (!panel.root) return false;
    if (typeof panel.root.addEventListener === "function") {
      panel.root.addEventListener("click", function (event) {
        var target = event.target && event.target.closest ? event.target : null;
        if (!target) return;
        var view = target.closest("[data-trace-view]");
        if (view) { setView(view.getAttribute("data-trace-view") || view.dataset.traceView); return; }
        var select = target.closest("[data-trace-select]");
        if (select) { choose(select.getAttribute("data-trace-select") || select.dataset.traceSelect); return; }
        if (target.closest("[data-trace-close]")) { clearSelection(); }
      });
    }
    panel.root.innerHTML = '<div class="te-empty">Loading execution trace…</div>';
    return true;
  }

  function setTopology(graph) {
    panel.topology = graph || {};
    paint();
  }

  function update(payload) {
    panel.payload = payload || {};
    var trace = panel.payload.trace || {};
    if (!panel.topology || (trace.fingerprint && trace.fingerprint !== panel.fingerprint)) {
      fetchTopology(trace.fingerprint);
    }
    paint();
  }

  function paint() {
    if (!panel.root) return;
    if (!panel.topology) {
      panel.root.innerHTML = '<div class="te-empty">Loading execution topology…</div>';
      return;
    }
    var scroll = null, matrixScroll = null;
    if (typeof panel.root.querySelector === "function") {
      scroll = panel.root.querySelector("#trace-main-scroll");
      matrixScroll = panel.root.querySelector("#trace-matrix-scroll");
    }
    var scrollTop = scroll ? scroll.scrollTop : 0;
    var matrixLeft = matrixScroll ? matrixScroll.scrollLeft : 0;
    var output = render(panel.topology, panel.payload && panel.payload.pipeline,
      panel.payload && panel.payload.trace, panel.state, Date.now());
    panel.state.model = output.model;
    panel.root.innerHTML = output.html;
    if (typeof panel.root.querySelector === "function") {
      scroll = panel.root.querySelector("#trace-main-scroll");
      matrixScroll = panel.root.querySelector("#trace-matrix-scroll");
      if (scroll) scroll.scrollTop = scrollTop;
      if (matrixScroll) matrixScroll.scrollLeft = matrixLeft;
    }
  }

  function setView(view) {
    panel.state.view = view === "matrix" ? "matrix" : "trace";
    paint();
  }

  function choose(id) {
    panel.state.selected = id || null;
    paint();
  }

  function clearSelection() {
    panel.state.selected = null;
    paint();
  }

  function reset() {
    panel.state.view = "trace";
    panel.state.selected = null;
    paint();
  }

  var TracePanel = {
    state: panel.state,
    mount: mount,
    setTopology: setTopology,
    update: update,
    paint: paint,
    setView: setView,
    setMode: setView,       // harmless compatibility with a cached monitor page
    select: choose,
    clearSelection: clearSelection,
    reset: reset,
    replay: function () { reset(); }
  };

  root.TraceExplorer = Explorer;
  root.TracePanel = TracePanel;
  if (typeof module !== "undefined" && module.exports) module.exports = Explorer;
})(typeof globalThis !== "undefined" ? globalThis : this);
