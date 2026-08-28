/* Shared operator narrative --------------------------------------------------
 * The charts remain owned by their tab modules. This layer answers the questions
 * those charts cannot answer alone: what is happening, what changed, what the
 * system decided, what it did, and which autonomous subsystem owns recovery.
 */

var OP_AUTONOMY = null;
var OP_AUTONOMY_LOADING = false;
var OP_AUTONOMY_LAST_FETCH_MS = null;
var OP_AUTONOMY_REFRESH_MS = 30000;
var FINDINGS_CLAIM_FILTER = "accepted";
var FINDINGS_QUESTIONS_EXPANDED = false;
var HISTORY_CHANGE_INDEX = 1;

function opNode(id) { return typeof document !== "undefined" && document.getElementById ? document.getElementById(id) : null; }
function opText(id, value) { var node = opNode(id); if (node) node.textContent = value == null ? "—" : String(value); }
function opHtml(id, value) { var node = opNode(id); if (node) node.innerHTML = value == null ? "" : String(value); }
function opClass(id, base, tone) { var node = opNode(id); if (node) node.className = base + (tone ? " " + tone : ""); }
function opList(value) { return Array.isArray(value) ? value : []; }
function opN(value) { var parsed = Number(value); return Number.isFinite(parsed) ? parsed : null; }
function opTrim(value, limit) {
  var text = String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  return text.length > limit ? text.slice(0, Math.max(1, limit - 1)) + "…" : text;
}
function opAgoSeconds(seconds) {
  var n = opN(seconds);
  if (n == null) return "unknown age";
  if (n < 60) return Math.round(n) + "s ago";
  if (n < 3600) return Math.round(n / 60) + "m ago";
  if (n < 86400) return Math.round(n / 3600) + "h ago";
  return Math.round(n / 86400) + "d ago";
}
function opAgoTs(ts) {
  if (!ts) return "—";
  var ms = new Date(ts).getTime();
  return Number.isFinite(ms) ? opAgoSeconds(Math.max(0, (Date.now() - ms) / 1000)) : String(ts);
}
function opTone(status) {
  status = String(status || "").toLowerCase();
  if (/error|failed|critical|regressed|stalled|offline|recovering/.test(status)) return "recovering";
  if (/warn|watch|probation|open|claimed|active|running/.test(status)) return "watch";
  return "";
}
function opSigned(value, suffix) {
  var n = opN(value);
  if (n == null) return "—";
  return (n > 0 ? "+" : "") + num(Math.round(n)) + (suffix || "");
}
function opDelta(rows, key) {
  rows = opList(rows).filter(function (row) { return opN(row && row[key]) != null; });
  if (rows.length < 2) return null;
  return opN(rows[rows.length - 1][key]) - opN(rows[rows.length - 2][key]);
}
function opPill(label, tone) {
  return '<span class="delta' + (tone ? " " + tone : "") + '"><b>' + esc(label) + '</b></span>';
}
function opLifecycle(rows) {
  return '<div class="lifecycle">' + rows.map(function (row) {
    return '<div class="life-step ' + esc(row.status || "") + '"><small>' + esc(row.phase) + '</small><div><b>'
      + esc(row.title) + '</b><span>' + esc(row.detail) + '</span></div></div>';
  }).join("") + '</div>';
}
function opStepState(pipeline, names) {
  var steps = opList((pipeline || {}).steps).filter(function (step) { return names.indexOf(step.name) >= 0; });
  if (!steps.length) return "pending";
  if (steps.some(function (step) { return step.status === "failed"; })) return "failed";
  if (steps.some(function (step) { return step.status === "active"; })) return "active";
  if (steps.every(function (step) { return step.status === "done" || step.status === "skipped"; })) return "done";
  return "pending";
}

function operatorOverall(data, autonomy) {
  data = data || {};
  autonomy = autonomy || {};
  var farmBlockers = opList(data.blockers);
  var autoBlockers = opList(autonomy.blockers);
  var pipelineStatus = String((data.pipeline || {}).effective_status || (data.pipeline || {}).status || "");
  var critical = data.health === "offline" || data.health === "error" || /failed|stalled/.test(pipelineStatus)
    || autoBlockers.some(function (row) { return row.severity === "critical"; })
    || farmBlockers.some(function (row) {
      return (row.level === "critical" || row.level === "error" || row.level === "recovering")
        && /production (stopped|stalled)|hard failure|cycle failed|agent .*not loaded|unrecoverable/i.test(String(row.text || ""));
    });
  var healthNormal = data.health === "healthy" || data.health === "running";
  var warning = farmBlockers.length > 0 || autoBlockers.length > 0 || !healthNormal;
  var tone = critical ? "recovering" : warning ? "watch" : "";
  var label = critical ? "Self-healing" : warning ? "Autonomy watching" : "Autonomous";
  var count = farmBlockers.length + autoBlockers.length;
  var detail = critical ? "Recovery guardrails are containing the condition and scheduled agents own the next action" : warning
    ? count + " bounded condition" + (count === 1 ? "" : "s") + " visible; routine production continues"
    : "Routine operation is handling itself";
  opText("global-status", label);
  opClass("global-status", "system-state", tone);
  opText("autonomy-state-title", label);
  opText("autonomy-state-detail", detail);
  opClass("autonomy-primary", "auto-cell primary", tone);

  var agents = autonomy.agents || {};
  var live = agents.live, expected = agents.expected;
  opText("autonomy-agents", live == null || expected == null ? "Loading services" : live + "/" + expected + " services loaded");
  opText("autonomy-agents-detail", opList(agents.down).length ? opList(agents.down).join(", ") + " unavailable" : "All scheduled control loops available");
  opClass("autonomy-services", "auto-cell", opList(agents.down).length ? "recovering" : "");

  var pipeline = data.pipeline || {};
  var canary = autonomy.canary || {};
  var runLabel = pipeline.run == null ? "Waiting for first run" : "Run #" + pipeline.run + " · " + (pipeline.status || "idle");
  var cycleDetail = pipeline.status === "running" ? "Executing " + (pipeline.active || "startup")
    : canary.status === "watching" ? "Canary has observed " + (canary.runs_observed || 0) + " verified run" + (Number(canary.runs_observed || 0) === 1 ? "" : "s")
    : "Next cycle follows the deterministic cadence";
  opText("autonomy-cycle", runLabel);
  opText("autonomy-cycle-detail", cycleDetail);
  opClass("autonomy-cycle-cell", "auto-cell", opTone(pipeline.status === "running" ? "active" : canary.status));

  var activity = opList((autonomy.activity || {}).events);
  var latest = activity[0];
  opText("autonomy-action", latest ? latest.title : "No control-plane change");
  opText("autonomy-action-detail", latest ? latest.actor + " · " + opAgoTs(latest.ts) : "Existing ledgers are quiet");
  opClass("autonomy-action-cell", "auto-cell", latest ? opTone(latest.status) : "");
  return {tone:tone, label:label, detail:detail, farmBlockers:farmBlockers, autoBlockers:autoBlockers};
}

function operatorOverview(data, autonomy, overall) {
  var latest = data.latest || {};
  var pipeline = data.pipeline || {};
  var growth = data.growth || {};
  var trend = opList(data.trend);
  var producing = !(data.signals || {}).below_floor && opN(latest.produce_per_min) !== 0;
  var state = pipeline.status === "running" ? "Cycle in progress" : producing ? "Growing autonomously" : overall.label;
  var tone = pipeline.status === "running" ? "watch" : overall.tone;
  opText("overview-verdict", state);
  opText("overview-verdict-detail", pipeline.status === "running"
    ? "Run #" + (pipeline.run == null ? "—" : pipeline.run) + " is executing " + (pipeline.active || "startup")
    : "Last verified run #" + (latest.run == null ? "—" : latest.run) + " · " + opAgoTs(latest.ts));
  opClass("overview-verdict-box", "hero-verdict", tone);

  var produceGain = opN(latest.our_produce_gain);
  if (produceGain == null) produceGain = opDelta(trend, "produce");
  var herdGain = opDelta(trend, "animals");
  var adaptive = data.adaptive || {};
  var adaptiveDomains = opList(adaptive.blocked_domains);
  var adaptiveLabel = adaptiveDomains.length ? "holding " + adaptiveDomains.join(" + ") : "clear";
  var deltas = [
    '<span class="delta good">Produce <b>' + esc(opSigned(produceGain)) + '</b></span>',
    '<span class="delta good">Herd <b>' + esc(opSigned(herdGain)) + '</b></span>',
    '<span class="delta">Adopted <b>' + esc(num(latest.adopted)) + '</b></span>',
    '<span class="delta">Feed restored <b>' + esc(num(latest.feed_bought)) + '</b></span>',
    '<span class="delta ' + (adaptiveDomains.length ? "watch" : "good") + '">Adaptive guard <b>' + esc(adaptiveLabel) + '</b></span>',
    '<span class="delta ' + (opList(latest.anomalies).length ? "watch" : "good") + '">Verification <b>'
      + (opList(latest.anomalies).length ? opList(latest.anomalies).length + " explained" : "clean") + '</b></span>',
    '<span class="delta good">Routine tokens <b>0</b></span>'
  ];
  opHtml("overview-deltas", deltas.join(""));

  var selected = ((latest.decision_trace || {}).selected) || latest.plan || {};
  var riskCount = Object.values ? Object.values(latest.risk_event_counts || {}).reduce(function (sum, value) { return sum + (opN(value) || 0); }, 0) : 0;
  var lifecycle = [
    {phase:"Observe", title:num(latest.animals) + " animals · hunger " + (latest.max_hunger == null ? "—" : latest.max_hunger),
      detail:(riskCount ? riskCount + " stochastic risk event(s) normalized" : "Farm, events, market and leaderboard read"),
      status:opStepState(pipeline,["tools","read","events","board"])},
    {phase:"Decide", title:selected.adopt == null ? "Plan derived from executable policy" : "Adopt " + num(selected.adopt) + " · buy " + num(selected.buy_feed) + " feed",
      detail:opTrim((growth || {}).reason || "Reserve, hunger and growth gates evaluated", 102), status:opStepState(pipeline,["plan"])},
    {phase:"Act", title:num(latest.units_collected) + " collected · " + num(latest.adopted) + " adopted",
      detail:(latest.fed ? "Herd fed" : "Feed not due") + " · " + num(latest.feed_bought) + " feed purchased",
      status:opStepState(pipeline,["collect","feed","buy_feed","adopt","sell"])},
    {phase:"Verify", title:latest.verified === false ? "Outcome not verified" : "Rank #" + (latest.rank == null ? "—" : latest.rank) + " · " + num(Math.round(opN(latest.produce_per_min) || 0)) + "/min",
      detail:opList(latest.anomalies).length ? opList(latest.anomalies).length + " explained anomaly signal(s) retained" : "Observed state matched the bounded plan",
      status:latest.verified === false ? "failed" : opStepState(pipeline,["verify","finish"])}
  ];
  opHtml("cycle-story", opLifecycle(lifecycle));
  opText("cycle-story-summary", pipeline.status === "running" ? "Live run" : "Last completed cycle");
}

function operatorPipeline(data) {
  data = data || {};
  var pipeline = data.pipeline || {};
  var latest = data.latest || {};
  var signals = data.signals || {};
  var status = pipeline.status || "idle";
  var active = pipeline.active || "between runs";
  opText("pipeline-hero-state", status === "running" ? "Executing " + active : status === "failed" ? "Run failed" : "Last run verified");
  opText("pipeline-hero-detail", status === "running"
    ? "The current step is measured live; guardrails remain active through finish."
    : "The workspace is showing run #" + (pipeline.run == null ? "—" : pipeline.run) + " while the next cadence slot approaches.");
  opClass("pipeline-hero-verdict", "hero-verdict", opTone(status));

  var selected = ((latest.decision_trace || {}).selected) || latest.plan || {};
  var decision = selected.adopt == null
    ? "Policy evaluated the current farm state"
    : "Grow by " + num(selected.adopt) + " while restoring " + num(selected.buy_feed) + " feed";
  var reason = ((latest.decision_trace || {}).growth_evidence || {}).reason || (data.growth || {}).reason
    || "Executable rules selected the bounded plan from current reserve, hunger, evidence and cycle-budget inputs.";
  opText("pipe-decision-title", decision);
  opText("pipe-decision-body", opTrim(reason, 240));

  var lifecycle = [
    {phase:"Observe", title:"Read world + boundary", detail:"Contract, farm, events and leaderboard", status:opStepState(pipeline,["tools","read","events","board"])},
    {phase:"Decide", title:"Compile bounded plan", detail:"Claims, reserves and alternatives", status:opStepState(pipeline,["plan"])},
    {phase:"Act", title:"Mutate within budget", detail:"Collect, feed, sell, buy and adopt", status:opStepState(pipeline,["collect","feed","sell","buy_feed","adopt"])},
    {phase:"Verify", title:"Re-read + record", detail:"Outcome, rank, anomalies and ledger", status:opStepState(pipeline,["verify","finish"])}
  ];
  opHtml("pipe-lifecycle", opLifecycle(lifecycle));

  var rate = opN(signals.produce_per_min), floor = opN(signals.floor);
  var hunger = opN(signals.hunger), stop = opN(signals.hunger_stop);
  var feed = opN(signals.feed), reserve = opN(signals.reserve_target);
  var started = pipeline.started_ts ? new Date(pipeline.started_ts).getTime() : null;
  var end = status === "running" ? Date.now() : (pipeline.finished_ts ? new Date(pipeline.finished_ts).getTime() : null);
  var elapsed = started && end ? Math.max(0,(end-started)/1000) : null;
  var budget = opN(pipeline.budget_s);
  var guardrails = [
    {label:"Production", value:rate == null ? "—" : num(Math.round(rate)) + "/min", detail:floor == null ? "floor unavailable" : "floor " + num(Math.round(floor)), tone:rate != null && floor != null && rate < floor ? "watch" : "good"},
    {label:"Hunger", value:hunger == null ? "—" : hunger + " / " + stop, detail:hunger == null || stop == null ? "stop unavailable" : Math.max(0,stop-hunger) + " points headroom", tone:hunger != null && stop != null && hunger >= stop ? "bad" : "good"},
    {label:"Feed reserve", value:feed == null ? "—" : short(feed), detail:reserve == null ? "target unavailable" : "target " + short(reserve), tone:feed != null && reserve != null && feed < reserve ? "watch" : "good"},
    {label:"Cycle budget", value:elapsed == null ? "—" : secs(elapsed), detail:budget == null ? "budget unavailable" : Math.max(0,budget-elapsed).toFixed(0) + "s headroom", tone:elapsed != null && budget != null && elapsed > budget ? "bad" : "good"}
  ];
  opHtml("pipe-guardrails", guardrails.map(function (g) {
    return '<div class="guardrail ' + g.tone + '"><small>' + esc(g.label) + '</small><b>' + esc(g.value)
      + '</b><span>' + esc(g.detail) + '</span></div>';
  }).join(""));
}

function operatorAdaptive(data, evidence) {
  data = data || {};
  evidence = evidence || {};
  var adaptive = data.adaptive || {};
  var domains = opList(adaptive.blocked_domains);
  var blocks = opList(adaptive.active_blocks);
  var events = opList(adaptive.recent_events);
  var compatibilityOrders = opList(((adaptive.compatibility || {}).orders));
  var noveltyQuestions = opList(((evidence.questions || {}).questions)).filter(function (row) {
    return String(row && row.class || "").indexOf("activity_novelty_") === 0;
  }).sort(function (a,b) {
    return Number(b.last_seen_run || b.closed_run || 0) - Number(a.last_seen_run || a.closed_run || 0);
  });
  var question = noveltyQuestions[0] || null;
  var repairing = compatibilityOrders.length > 0;
  var holding = domains.length > 0 || repairing;
  var state = repairing ? "Repairing server format" : domains.length ? "Containing novel activity" : question && (question.status === "open" || question.status === "probing")
    ? "Investigating new activity" : "Adaptive guard clear";
  var detail = repairing
    ? "Captured " + (compatibilityOrders[0].tool || "server") + " drift · repair " + (compatibilityOrders[0].status || "open")
    : domains.length ? "Holding " + domains.join(", ") + " while evidence is gathered"
    : question ? "Latest question " + question.status + " · run #" + (question.last_seen_run == null ? "—" : question.last_seen_run)
    : "No unexplained strategic behavior is active";
  opText("adaptive-state", state);
  opText("adaptive-detail", detail);
  opClass("adaptive-verdict", "adaptive-verdict", holding ? "watch" : "");
  opClass("adaptive-card", "card adaptive-card", holding ? "watch" : "");

  var latestEvent = events[0] || {};
  opHtml("adaptive-metrics", [
    ["Domains held", domains.length ? domains.join(", ") : "none", holding ? "watch" : "good"],
    ["Coin outflow", num(adaptive.trade_coin_outflow || 0), Number(adaptive.trade_coin_outflow || 0) ? "bad" : "good"],
    ["Coins protected", num(adaptive.trade_coin_outflow_blocked || 0), Number(adaptive.trade_coin_outflow_blocked || 0) ? "watch" : ""],
    ["Format repair", repairing ? (compatibilityOrders[0].tool || "open") : "none", repairing ? "watch" : "good"]
  ].map(function (row) {
    return '<div class="adaptive-metric ' + esc(row[2]) + '"><small>' + esc(row[0]) + '</small><b>' + esc(row[1]) + '</b></div>';
  }).join(""));

  opHtml("adaptive-holds", blocks.length ? blocks.map(function (block) {
    return '<article class="adaptive-hold"><div><b>' + esc(block.subject || block.class || "Novel activity") + '</b><p>'
      + esc(opTrim(block.alert || ((block.evidence || {}).detail) || "Evidence review is active", 220)) + '</p></div><div class="adaptive-tags">'
      + opList(block.domains).map(function (domain) { return opPill(domain, "watch"); }).join("")
      + '<span class="delta">runs <b>' + esc(block.first_run == null ? "—" : block.first_run) + '–' + esc(block.last_run == null ? "—" : block.last_run) + '</b></span></div></article>';
  }).join("") : '<div class="adaptive-safe"><b>No strategic domains held</b><span>Known husbandry and growth actions may continue under promoted policy.</span></div>');

  if (!question) {
    opHtml("adaptive-question", '<div class="adaptive-safe"><b>No novelty question yet</b><span>The next material rising edge will create one automatically.</span></div>');
  } else {
    var probe = question.active_probe_id || question.probe_result_status || "not started";
    opHtml("adaptive-question", '<article class="adaptive-question ' + esc(opTone(question.status)) + '"><div class="adaptive-question-head"><b>'
      + esc(question.id || question.class) + '</b>' + opPill(question.status || "open", opTone(question.status)) + '</div><p>'
      + esc(opTrim(question.hypothesis || question.answer || "Evidence question recorded", 230)) + '</p><div class="adaptive-tags"><span class="delta">class <b>'
      + esc(question.class) + '</b></span><span class="delta">probe <b>' + esc(probe) + '</b></span><span class="delta">generation <b>'
      + esc(question.generation == null ? 1 : question.generation) + '</b></span></div></article>');
  }

  opHtml("adaptive-events", events.length ? events.map(function (event) {
    var held = event.kind === "signal";
    return '<article class="adaptive-event ' + (held ? "holding" : "resolved") + '"><span class="adaptive-event-mark">'
      + (held ? "!" : "✓") + '</span><div><b>' + esc(event.subject || event.class || "Adaptive event") + '</b><p>'
      + esc(opTrim(event.detail || "No detail recorded", 240)) + '</p><div class="adaptive-tags"><span class="delta">run <b>#'
      + esc(event.run) + '</b></span><span class="delta"><b>' + esc(event.status || event.kind) + '</b></span>'
      + opList(event.domains).map(function (domain) { return opPill(domain, "watch"); }).join("") + '</div></div><time>'
      + esc(opAgoTs(event.ts)) + '</time></article>';
  }).join("") : '<div class="adaptive-safe"><b>No novelty events recorded</b><span>The sentinel is armed before strategic mutation.</span></div>');
}

function operatorHealing(data) {
  data = data || {};
  var heal = data.heal || {};
  var tokens = data.tokens || {};
  var cost = data.cost || {};
  var recent = opList(heal.recent);
  var latest = recent[0];
  var overrides = ((heal.knobs || {}).overrides) || {};
  var active = Object.keys(overrides).length;
  var escalations = opN(tokens.total_escalations) || 0;
  var healed = opN(tokens.total_healed) || 0;
  var label = latest ? (latest.class === "relax" ? "Safeguards returning to default" : "Latest condition handled locally") : "No recovery work queued";
  opText("healing-verdict", label);
  opText("healing-verdict-detail", latest ? "Run #" + (latest.run == null ? "—" : latest.run) + " · " + opAgoTs(latest.ts) : "Routine execution remains on default settings");
  opClass("healing-hero-verdict", "hero-verdict", active ? "watch" : "");
  opText("heal-active-count", String(active));
  opText("heal-active-detail", active ? Object.keys(overrides).join(", ") : "All safeguards at defaults");
  opText("heal-local-count", num(healed));
  opText("heal-model-count", num(escalations));
  opText("heal-zero-count", num(cost.free_runs));

  if (!latest) {
    opHtml("healing-latest", '<div class="empty">No bounded remedy has been needed.</div>');
    opHtml("healing-loop", opLifecycle([
      {phase:"Detect",title:"No surviving alert",detail:"Detectors remain armed",status:"done"},
      {phase:"Diagnose",title:"No condition to classify",detail:"No state change",status:"done"},
      {phase:"Remedy",title:"Defaults preserved",detail:"No mutation or model wake-up",status:"done"},
      {phase:"Verify",title:"Routine health continues",detail:"Next supervisor pass will re-check",status:"done"}
    ]));
    return;
  }
  var relaxing = latest.class === "relax";
  opHtml("healing-latest", '<div class="remedy-feature"><span class="remedy-icon">' + (relaxing ? "↘" : "↻")
    + '</span><div><h3>' + esc(relaxing ? "Quiet-run relaxation" : (latest.class || "Bounded remedy")) + '</h3><p>'
    + esc(latest.action || "No action text recorded") + '</p><div class="remedy-meta"><span class="delta">run <b>#'
    + esc(latest.run) + '</b></span><span class="delta">observed <b>' + esc(opAgoTs(latest.ts)) + '</b></span>'
    + (latest.alert ? '<span class="delta watch">trigger <b>' + esc(opTrim(latest.alert,110)) + '</b></span>' : '')
    + '</div></div></div>');
  opHtml("healing-loop", opLifecycle([
    {phase:"Detect",title:latest.alert ? "Detector raised a bounded condition" : "Quiet streak met relaxation gate",detail:opTrim(latest.alert || "No active alert remained",100),status:"done"},
    {phase:"Diagnose",title:"Classified as " + (latest.class || "remedy"),detail:"Supervisor selected a clamped local response",status:"done"},
    {phase:"Remedy",title:opTrim(latest.action || "No mutation required",78),detail:"No coins, animals or strategy can be changed here",status:"done"},
    {phase:"Verify",title:relaxing ? "Default restored incrementally" : "Override remains observable",detail:"Future quiet runs relax the knob automatically",status:relaxing ? "done" : "active"}
  ]));
}

function operatorHistory(history) {
  history = history || {};
  var stats = history.stats || {};
  var reduction = opN(stats.reduction_pct);
  opText("history-verdict", reduction == null ? "Measured economics loading" : fixed(reduction,3) + "% lower routine cost");
  opText("history-verdict-detail", num(stats.zero_runs) + " of " + num(stats.ledger_runs) + " audited runs explicitly booked at zero");
  opClass("history-hero-verdict", "hero-verdict", "");
  opHtml("history-impact-deltas", [
    '<span class="delta good">Actual exception estimate <b>' + esc(money(stats.actual_cost,2)) + '</b></span>',
    '<span class="delta watch">Old-loop midpoint <b>' + esc(money(stats.counterfactual_cost_mid,0)) + '</b></span>',
    '<span class="delta good">Local dispositions <b>' + esc(num(stats.healed)) + '</b></span>',
    '<span class="delta ' + (Number(stats.escalations||0) ? "watch" : "good") + '">Model wake-ups <b>' + esc(num(stats.escalations)) + '</b></span>'
  ].join(""));
}

function operatorWire(data) {
  data = data || {};
  var calls = opList((data.trace || {}).calls);
  var completed = calls.filter(function (call) { return opN(call.duration_ms) != null; });
  var errors = calls.filter(function (call) { return call.status === "error" || call.error; });
  var active = calls.filter(function (call) { return !call.ended_ts || call.status === "active"; });
  var slowest = completed.slice().sort(function (a,b) { return Number(b.duration_ms)-Number(a.duration_ms); })[0];
  var model = typeof window !== "undefined" && window.MCPWirePanel && window.MCPWirePanel.state
    ? window.MCPWirePanel.state.model : null;
  var clean = !errors.length;
  opText("wire-hero-state", calls.length ? (clean ? "Boundary healthy" : errors.length + " boundary error" + (errors.length === 1 ? "" : "s")) : "Waiting for boundary traffic");
  var detail = calls.length ? num(calls.length) + " observed calls"
    + (model && model.stats ? " · " + model.stats.peak + " peak concurrency · ×" + model.stats.parallelism.toFixed(1) + " overlap" : "")
    : "The view never synthesizes calls that were not recorded.";
  opText("wire-hero-detail", detail);
  opClass("wire-hero-verdict", "hero-verdict", clean ? "" : "recovering");
  opHtml("wire-deltas", [
    '<span class="delta ' + (clean ? "good" : "bad") + '">Clean <b>' + esc(num(calls.length-errors.length)) + '/' + esc(num(calls.length)) + '</b></span>',
    '<span class="delta">In flight <b>' + esc(num(active.length)) + '</b></span>',
    '<span class="delta">Coverage <b>' + esc((data.trace || {}).coverage || "unavailable") + '</b></span>',
    '<span class="delta watch">Slowest <b>' + esc(slowest ? slowest.tool + " · " + secs(Number(slowest.duration_ms)/1000) : "—") + '</b></span>'
  ].join(""));
}

function operatorActivityHtml(events) {
  events = opList(events);
  if (!events.length) return '<div class="empty">No recent autonomous control-plane activity.</div>';
  var icons = {observe:"⌁",decide:"◇",act:"↻",verify:"✓",research:"⌕"};
  return '<div class="activity-feed">' + events.map(function (event) {
    var tone = opTone(event.status);
    return '<article class="activity-item ' + esc(event.phase || "") + (tone ? " " + tone : "") + '"><span class="activity-phase">'
      + esc(icons[event.phase] || "•") + '</span><div><b>' + esc(event.title) + '</b><p>' + esc(opTrim(event.detail,170))
      + '</p></div><time>' + esc(opAgoTs(event.ts)) + '</time></article>';
  }).join("") + '</div>';
}

function operatorFindings(evidence, autonomy) {
  evidence = evidence || {};
  autonomy = autonomy || OP_AUTONOMY || {};
  var registry = evidence.claims || {};
  var claims = opList(registry.claims);
  var questionRows = opList((evidence.questions || {}).questions).filter(function (row) { return row.status === "open" || row.status === "probing"; });
  var runtime = ((evidence.policy || {}).runtime) || {};
  var semantic = ((evidence.research || {}).semantic_audit) || {};
  var accepted = claims.filter(function (row) { return row.status === "accepted"; });
  var superseded = claims.filter(function (row) { return row.status === "superseded"; });
  var overdue = claims.filter(function (row) { return row.refresh && row.refresh.state === "overdue"; });
  var healthy = runtime.compatible && semantic.ok && !overdue.length;
  opText("findings-verdict", healthy ? "Strategy is evidence-backed" : overdue.length ? overdue.length + " claim recheck" + (overdue.length === 1 ? " is" : "s are") + " overdue" : "Knowledge control plane is watching");
  opText("findings-verdict-detail", "Policy " + (runtime.policy_id || "unversioned") + " · " + accepted.length + " accepted claims · " + questionRows.length + " open questions");
  opClass("findings-hero-verdict", "hero-verdict", healthy ? "" : "watch");
  var probeCount = opList((evidence.probes || {}).probes).length || opList((evidence.experiments || {}).experiments).length
    || (((evidence.species || {}).probe || {}).status ? 1 : 0);
  opHtml("knowledge-flow", [
    ["Questions",questionRows.length,"prioritized uncertainty"],
    ["Probes",probeCount,"bounded tests"],
    ["Claims",accepted.length,"accepted + fresh"],
    ["Policy",runtime.policy_id ? 1 : 0,runtime.compatible ? "compatible" : "not promoted"],
    ["Runtime",runtime.compatible && semantic.ok ? "✓" : "!",semantic.ok ? "semantic contract passing" : "audit queued for autonomous verification"]
  ].map(function (row) {
    return '<div class="knowledge-node"><small>' + esc(row[0]) + '</small><b>' + esc(row[1]) + '</b><span>' + esc(row[2]) + '</span></div>';
  }).join(""));

  var ceiling = evidence.ceiling || {};
  var scaling = ceiling.scaling || {};
  var strategicClaim = accepted.filter(function (row) { return row.id === "mechanic.output_linear_with_herd"; })[0] || accepted[0] || {};
  var statement = strategicClaim.statement || ceiling.claim || "No accepted strategy claim yet.";
  opHtml("ev-strategy-brief", '<div class="strategy-verdict"><strong>' + (runtime.compatible ? "Continue current policy" : "Hold policy changes")
    + '</strong><p>' + esc(opTrim(statement,220)) + '</p></div><div class="kv strategy-facts">'
    + kv([
      ["Scaling exponent",scaling.exponent == null ? "—" : fixed(scaling.exponent,3)],
      ["Confidence",esc((strategicClaim.confidence || {}).level || "—")],
      ["Freshness",overdue.length ? '<span class="warn">' + overdue.length + ' overdue</span>' : '<span class="good">current</span>'],
      ["Runtime",runtime.compatible ? '<span class="good">compatible</span>' : '<span class="bad">not compatible</span>']
    ]) + '</div>');

  var activitySeen = {};
  var activity = opList((autonomy.activity || {}).events).filter(function (event) {
    if (!(event.phase === "research" || event.phase === "observe" || event.phase === "decide")) return false;
    var key = event.actor + "|" + event.title;
    if (activitySeen[key]) return false;
    activitySeen[key] = true;
    return true;
  }).slice(0,6);
  opHtml("research-activity", operatorActivityHtml(activity));
  operatorApplyFindingsFilters();
}

function operatorApplyFindingsFilters() {
  var claims = opNode("ev-claims");
  if (claims) claims.className = "claim-list filter-" + FINDINGS_CLAIM_FILTER;
  if (typeof document !== "undefined" && document.querySelectorAll) {
    var buttons = document.querySelectorAll("[data-claim-filter]");
    for (var i=0;i<buttons.length;i++) buttons[i].setAttribute("aria-pressed", String(buttons[i].getAttribute("data-claim-filter") === FINDINGS_CLAIM_FILTER));
  }
  var questions = opNode("ev-questions");
  if (questions) questions.className = "question-list" + (FINDINGS_QUESTIONS_EXPANDED ? " expanded" : "");
  var toggle = opNode("question-toggle");
  if (toggle) toggle.textContent = FINDINGS_QUESTIONS_EXPANDED ? "Show highest-priority questions only" : "Show every open question";
}

function renderOperator(data, autonomy) {
  data = data || {};
  var overall = operatorOverall(data, autonomy || OP_AUTONOMY || {});
  operatorOverview(data, autonomy || OP_AUTONOMY || {}, overall);
  operatorPipeline(data);
  operatorAdaptive(data, typeof EVIDENCE !== "undefined" ? EVIDENCE : null);
  operatorHealing(data);
  operatorWire(data);
  if (typeof EVIDENCE !== "undefined" && EVIDENCE) operatorFindings(EVIDENCE, autonomy || OP_AUTONOMY || {});
}

function renderOperatorTick(data) {
  if (!data) return;
  operatorOverall(data, OP_AUTONOMY || {});
  operatorPipeline(data);
  operatorAdaptive(data, typeof EVIDENCE !== "undefined" ? EVIDENCE : null);
  operatorWire(data);
}

async function loadOperatorAutonomy(force) {
  if ((!force && OP_AUTONOMY) || OP_AUTONOMY_LOADING) return;
  OP_AUTONOMY_LOADING = true;
  try {
    var response = await fetch('/api/autonomy?t=' + Date.now(), {cache:"no-store"});
    if (!response.ok) throw new Error("HTTP " + response.status);
    OP_AUTONOMY = await response.json();
    OP_AUTONOMY_LAST_FETCH_MS = Date.now();
    // Architecture owns its rendering but can reuse the same truthful payload on
    // first entry. Its force-refresh behavior remains unchanged.
    if (typeof AUTONOMY !== "undefined") AUTONOMY = OP_AUTONOMY;
  } catch (error) {
    OP_AUTONOMY = {error:error && error.message ? error.message : String(error), blockers:[{
      severity:"warn", what:"autonomy view unavailable", why:error && error.message ? error.message : String(error)
    }]};
    OP_AUTONOMY_LAST_FETCH_MS = Date.now();
  } finally {
    OP_AUTONOMY_LOADING = false;
    if (typeof LAST !== "undefined" && LAST) safe("operator narrative", function () { renderOperator(LAST, OP_AUTONOMY); });
    if (typeof EVIDENCE !== "undefined" && EVIDENCE) safe("findings narrative", function () { operatorFindings(EVIDENCE, OP_AUTONOMY); });
  }
}

if (typeof document !== "undefined" && document.addEventListener) document.addEventListener("click", function (event) {
  var target = event.target && event.target.closest ? event.target : null;
  if (!target) return;
  var filter = target.closest("[data-claim-filter]");
  if (filter) {
    FINDINGS_CLAIM_FILTER = filter.getAttribute("data-claim-filter") || "accepted";
    operatorApplyFindingsFilters();
    return;
  }
  var questions = target.closest("[data-question-toggle]");
  if (questions) {
    FINDINGS_QUESTIONS_EXPANDED = !FINDINGS_QUESTIONS_EXPANDED;
    operatorApplyFindingsFilters();
    return;
  }
  var change = target.closest("[data-history-change]");
  if (change) {
    HISTORY_CHANGE_INDEX = Number(change.getAttribute("data-history-change")) || 0;
    if (typeof EVIDENCE !== "undefined" && EVIDENCE) renderCostHistory(EVIDENCE.cost_history || {});
  }
});
