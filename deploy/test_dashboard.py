#!/usr/bin/env python3
"""Headless checks for the dashboard's own JavaScript.

The game had test suites; the dashboard did not, which is how two silent
freeze modes survived in monitor.py:

1. render() was one unguarded chain. The pipeline, signals, chart and log tail
   were painted LAST, so a throw in any earlier panel left them frozen at their
   previous values while the overview kept refreshing. A stuck page that still
   looks alive is worse than a blank one.
2. The poll bootstrap (`load(); setInterval(...)`) ran AFTER the injected game
   bundle, so a top-level throw in the game froze every tab at "connecting".

Both are pure front-end behaviour, and the browser available here cannot run
JavaScript, so this runs the real page script in JavaScriptCore against a DOM
stub - the same trick game/test_ui.js uses.

Run: python3 deploy/test_dashboard.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402


def _page_script() -> str:
    """The dashboard's own JS, with the game bundle and bootstrap removed.

    The game is excluded on purpose: it has its own suites, and this file is
    about the dashboard surviving the game (and every panel) misbehaving.
    """
    match = re.search(r"<script>(.*)</script>", monitor.HTML, re.S)
    if not match:
        raise SystemExit("test_dashboard: no <script> block found in monitor.HTML")
    js = match.group(1)
    js = re.sub(r"try\s*\{\s*/\*GAME_JS_START\*/.*?/\*GAME_JS_END\*/\s*\}\s*catch\s*\(error\)\s*\{.*?\n\}",
                "", js, flags=re.S)
    js = re.sub(r"/\*GAME_JS_START\*/.*?/\*GAME_JS_END\*/", "", js, flags=re.S)
    js = js.replace("load(); setInterval(load, 2000); setInterval(tick, 1000);", "")
    js = js.replace("load(); setInterval(load, 2000);", "")
    if "renderPipeline" not in js:
        raise SystemExit("test_dashboard: extracted script is missing renderPipeline")
    return js


def _payload() -> dict:
    """A realistic payload, shaped like /api/state during a live run."""
    return {
        "app": "farmfriends-monitor",
        "updated_at": "2026-08-21T15:11:20Z",
        "cadence_seconds": 180,
        "health": "healthy",
        "latest": {"run": 216, "ts": "2026-08-21T15:11:56Z", "rank": 1, "animals": 11869,
                   "produce": 1410000, "units_per_chicken_min": 0.12, "max_hunger": 18,
                   "adopted": 0, "anomalies": [], "trade_coin_outflow": 0,
                   "trade_coin_outflow_blocked": 400000,
                   "novelty": {"blocked_domains": ["offers", "trades"],
                               "active_blocks": [{"class": "activity_novelty_rival",
                                                  "subject": "competitive activity",
                                                  "domains": ["offers", "trades"],
                                                  "first_run": 216, "last_run": 216,
                                                  "alert": "John entered a new capital regime"}],
                               "signals": [{"class": "activity_novelty_rival",
                                            "subject": "competitive activity",
                                            "domains": ["offers", "trades"],
                                            "detail": "John coins increased by 823,576"}],
                               "resolved_blocks": []}},
        "current": {"active": True, "stage": "feed"},
        "launchd": {"cycle": {"state": "running"}, "supervisor": {"state": "ok"}},
        "blockers": [],
        "leaderboard": [
            {"name": "Moe", "produce": 56166, "delta": 166, "gap": 1353834},
            {"name": "John", "produce": 1210000, "delta": 10000, "gap": 200000},
        ],
        "leaderboard_history": [
            {"run": 214, "ts": "2026-08-21T15:03:40Z",
             "scores": {"Nick": 1380000, "John": 1195000, "Moe": 55800, "Aaron": 30}},
            {"run": 215, "ts": "2026-08-21T15:07:51Z",
             "scores": {"Nick": 1398509, "John": 1200000, "Moe": 56000, "Aaron": 32}},
            {"run": 216, "ts": "2026-08-21T15:11:56Z",
             "scores": {"Nick": 1410000, "John": 1210000, "Moe": 56166, "Aaron": 35}},
        ],
        "trend": [{"run": 215, "ts": "2026-08-21T15:07:51Z", "rank": 1, "animals": 11869,
                   "produce": 1398509, "units_per_chicken_min": 0.1, "max_hunger": 18,
                   "adopted": 0, "anomalies": []},
                  {"run": 216, "ts": "2026-08-21T15:11:56Z", "rank": 1, "animals": 11869,
                   "produce": 1410000, "units_per_chicken_min": 0.12, "max_hunger": 18,
                   "adopted": 0, "anomalies": []}],
        "tokens": {"per_run": {}, "all_time": {"tokens": 0, "cost_usd": 0.0}},
        "cost": {"all_time_usd": 0.0, "avoided_usd": 0.3048},
        "heal": {"knobs": {}, "classes": [], "log": []},
        "growth": {"cap": 0, "saturated": True, "herd": 11869, "plateau": 1604.77,
                   "recent_samples": 172, "reason": "the marginal herd buys no output"},
        "signals": {"produce_per_min": 1788.5, "floor": 593.5, "below_floor": False,
                    "prev_below_floor": False, "hunger": 18, "hunger_stop": 70,
                    "hunger_alarm": 66, "feed": 23753, "reserve_target": 23753, "soft": []},
        "scene": {"animals": 11869, "by_kind": {"chicken": 11669, "sheep": 100, "cow": 100},
                  "idle_kinds": ["sheep", "cow"], "feed": 23753, "reserve_target": 23753,
                  "feed_fill": 1.0, "hunger": 18, "hunger_stop": 70, "hunger_fill": 18/70,
                  "ready_units": 3303, "rank": 1, "produce": 1410000,
                  "produce_per_sec": 29.8, "produce_delta": 11491,
                  "ts": "2026-08-21T15:11:56Z"},
        "adaptive": {"status": "holding", "blocked_domains": ["offers", "trades"],
                     "compatibility": {"status": "clear", "orders": []},
                     "active_blocks": [{"class": "activity_novelty_rival",
                                        "subject": "competitive activity",
                                        "domains": ["offers", "trades"],
                                        "first_run": 216, "last_run": 216,
                                        "alert": "John entered a new capital regime"}],
                     "signals": [{"class": "activity_novelty_rival"}],
                     "resolved_blocks": [], "trade_coin_outflow": 0,
                     "trade_coin_outflow_blocked": 400000,
                     "recent_events": [{"run": 216, "ts": "2026-08-21T15:11:56Z",
                                        "kind": "signal", "class": "activity_novelty_rival",
                                        "subject": "competitive activity",
                                        "detail": "John coins increased by 823,576",
                                        "domains": ["offers", "trades"], "status": "holding"}]},
        "log_tail": ["FARM 2026-08-21T15:11:56Z run=216 ok"],
        "release": {"revision": "20260821T150327Z", "stale": False},
        "trace": {
            "fingerprint": "deadbeef1234",
            "run_started_ts": "2026-08-21T15:10:52Z",
            "coverage": "full",
            "calls": [
                {"id": "a1", "tool": "collect_produce", "step": "collect",
                 "started_ts": "2026-08-21T15:10:58.000Z", "ended_ts": "2026-08-21T15:10:58.800Z",
                 "duration_ms": 800, "status": "ok", "arguments": {"pass": 1},
                 "result": "5000 units", "error": None, "source": "boundary"},
                {"id": "a2", "tool": "feed_animals", "step": "feed",
                 "started_ts": "2026-08-21T15:11:12.000Z", "ended_ts": None,
                 "duration_ms": None, "status": "active", "arguments": {"max_hunger": 18},
                 "result": None, "error": None, "source": "boundary"},
            ],
            "activity": [],
        },
        "pipeline": {
            "run": 216, "status": "running", "dry": False, "active": "feed",
            "started_ts": "2026-08-21T15:10:52Z", "updated_ts": "2026-08-21T15:11:10Z",
            "finished_ts": None, "budget_s": 150, "timeout_s": 170,
            "baseline": {"collect": 25.9, "feed": 33.4},
            "steps": [
                {"name": "tools", "label": "Handshake", "hint": "h", "status": "done",
                 "started_ts": "2026-08-21T15:10:52Z", "ended_ts": "2026-08-21T15:10:55Z",
                 "seconds": 3.1, "detail": {"count": 15}, "note": None},
                {"name": "collect", "label": "Collect produce", "hint": "h", "status": "done",
                 "started_ts": "2026-08-21T15:10:55Z", "ended_ts": "2026-08-21T15:11:07Z",
                 "seconds": 12.0, "detail": {"units": 5000}, "note": None},
                {"name": "feed", "label": "Feed herd", "hint": "h", "status": "active",
                 "started_ts": "2026-08-21T15:11:10Z", "ended_ts": None, "seconds": None,
                 "detail": {}, "note": None},
                {"name": "harvest", "label": "Harvest crops", "hint": "h", "status": "skipped",
                 "started_ts": None, "ended_ts": "2026-08-21T15:11:07Z", "seconds": None,
                 "detail": {}, "note": "no harvestable food crops"},
                {"name": "finish", "label": "Record run", "hint": "h", "status": "pending",
                 "started_ts": None, "ended_ts": None, "seconds": None, "detail": {}, "note": None},
            ],
        },
    }


HARNESS = r"""
var RESULT = (function () {
  // --- DOM stub -------------------------------------------------------------
  function El(id) { this.id = id; this._html = ""; this._text = ""; this.className = ""; this.hidden = false; this.dataset = {}; }
  Object.defineProperty(El.prototype, "innerHTML", { get: function () { return this._html; }, set: function (v) { this._html = String(v); } });
  Object.defineProperty(El.prototype, "textContent", { get: function () { return this._text; }, set: function (v) { this._text = String(v); } });
  El.prototype.setAttribute = function () {}; El.prototype.getAttribute = function () { return null; };
  El.prototype.addEventListener = function () {};
  var STORE = {};
  var document = {
    getElementById: function (id) { if (!STORE[id]) STORE[id] = new El(id); return STORE[id]; },
    querySelectorAll: function () { return []; }
  };
  var SESSION = {}, RELOADS = 0;
  var sessionStorage = {
    getItem: function (key) { return Object.prototype.hasOwnProperty.call(SESSION, key) ? SESSION[key] : null; },
    setItem: function (key, value) { SESSION[key] = String(value); }
  };
  var window = { location: { reload: function () { RELOADS += 1; } } };
  var console = { error: function () {}, log: function () {} };

  __PAGE_SCRIPT__

  // --- assertions -----------------------------------------------------------
  var checks = [], failures = [];
  function ok(name, cond, detail) {
    checks.push({name: name, pass: !!cond, detail: detail || ""});
    if (!cond) failures.push(name + (detail ? " [" + detail + "]" : ""));
  }
  function txt(id) { return STORE[id] ? STORE[id].textContent : null; }
  function html(id) { return STORE[id] ? STORE[id].innerHTML : null; }
  function reset() { STORE = {}; }

  var LIVE = __PAYLOAD__;
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  // Freeze "now" just after the live payload's last progress write.
  var NOW = new Date("2026-08-21T15:11:22Z").getTime();
  var RealDate = Date;
  Date.now = function () { return NOW; };

  // A page is immutable UI code, not just a view over mutable JSON. It must reload
  // once when publish or rollback changes the release pointer, without looping if
  // the monitor restart is delayed.
  var alignedRelease = {release: {pointer_revision: VIEW_REVISION}};
  ok("release refresh: matching code stays loaded",
     refreshForRelease(alignedRelease) === false && RELOADS === 0);
  var restartingRelease = {release: {pointer_revision: "next-release", serving_revision: VIEW_REVISION}};
  ok("release refresh: pointer flip waits for the restarted monitor",
     refreshForRelease(restartingRelease) === false && RELOADS === 0, "reloads=" + RELOADS);
  var changedRelease = {release: {pointer_revision: "next-release", serving_revision: "next-release"}};
  ok("release refresh: a new pointer reloads the page",
     refreshForRelease(changedRelease) === true && RELOADS === 1, "reloads=" + RELOADS);
  ok("release refresh: the same transition cannot loop",
     refreshForRelease(changedRelease) === false && RELOADS === 1, "reloads=" + RELOADS);

  // 1. A live run paints every panel.
  reset();
  render(clone(LIVE));
  ok("live run: pipeline steps painted", (html("pipe-steps") || "").length > 100, "len=" + (html("pipe-steps") || "").length);
  ok("live run: active step shown", txt("pipe-active") === "feed", "got " + txt("pipe-active"));
  ok("live run: elapsed ticks off wall clock", txt("pipe-elapsed") === "30.0s", "got " + txt("pipe-elapsed"));
  ok("live run: status pill names the step", (html("pipe-status") || "").indexOf("running - feed") >= 0, html("pipe-status"));
  ok("live run: next run deferred", txt("pipe-next") === "after this run", "got " + txt("pipe-next"));
  ok("live run: log tail painted", (txt("log") || "").indexOf("run=216") >= 0, txt("log"));
  ok("live run: chart painted", (html("chart") || "").indexOf("<svg") >= 0);
  ok("live run: signals painted", (html("signals") || "").length > 20);
  ok("live run: overview painted", txt("last-run") === "#216", "got " + txt("last-run"));
  ok("operator shell: healthy routine operation reads autonomous", txt("global-status") === "Autonomous", txt("global-status"));
  ok("operator shell: overview explains what changed", (html("overview-deltas") || "").indexOf("Produce") >= 0 && (html("overview-deltas") || "").indexOf("Routine tokens") >= 0);
  ok("operator shell: latest cycle shows observe through verify", (html("cycle-story") || "").indexOf("Observe") >= 0 && (html("cycle-story") || "").indexOf("Verify") >= 0);
  ok("operator shell: pipeline surfaces its decision and guardrails", (txt("pipe-decision-title") || "").length > 10 && (html("pipe-guardrails") || "").indexOf("Production") >= 0);
  ok("adaptive guard: overview chip names held domains", (html("overview-deltas") || "").indexOf("Adaptive guard") >= 0 && (html("overview-deltas") || "").indexOf("offers + trades") >= 0, html("overview-deltas"));
  ok("adaptive guard: pipeline verdict surfaces containment", txt("adaptive-state") === "Containing novel activity", txt("adaptive-state"));
  ok("adaptive guard: held domains and protected coins are visible", (html("adaptive-holds") || "").indexOf("competitive activity") >= 0 && (html("adaptive-metrics") || "").indexOf("400,000") >= 0, html("adaptive-holds"));
  ok("adaptive guard: recent evidence identifies the rival change", (html("adaptive-events") || "").indexOf("John coins increased by 823,576") >= 0, html("adaptive-events"));
  operatorAdaptive(LIVE,{questions:{questions:[{id:"q-rival",class:"activity_novelty_rival",status:"answered",generation:2,last_seen_run:216,probe_result_status:"passed",hypothesis:"John entered a materially different capital regime"}]}});
  ok("adaptive guard: durable question and probe are linked", (html("adaptive-question") || "").indexOf("q-rival") >= 0 && (html("adaptive-question") || "").indexOf("passed") >= 0 && (html("adaptive-question") || "").indexOf("generation") >= 0, html("adaptive-question"));
  var REPAIRING = clone(LIVE);
  REPAIRING.adaptive.compatibility = {status:"repairing",orders:[{id:"runtime-parse-list-farm",tool:"list_farm",status:"open",sample:"list_farm_state.txt"}]};
  operatorAdaptive(REPAIRING,{});
  ok("adaptive guard: parser drift is visibly self-healing", txt("adaptive-state") === "Repairing server format" && (html("adaptive-metrics") || "").indexOf("list_farm") >= 0, txt("adaptive-state") + " " + html("adaptive-metrics"));
  operatorAdaptive(LIVE,{});
  ok("operator shell: healing remains a closed loop when quiet", (html("healing-loop") || "").indexOf("Defaults preserved") >= 0);
  var HEALING = {knobs:{},classes:[
    {class:"threat",count:4,last_run:216,last_action:"contained wolf",alerts:["wolf"]},
    {class:"idle_capital",count:2,last_run:215,last_action:"adopted bounded herd",alerts:["idle capital"]}
  ],recent:[]};
  OPEN_HEAL_CLASSES = {};
  captureHealClassState({querySelectorAll:function () { return [
    {dataset:{healClass:"threat"},open:true},
    {dataset:{healClass:"idle_capital"},open:true}
  ]; }});
  renderHealing(HEALING);
  ok("healing disclosures stay expanded across a state refresh",
     (html("heal-classes") || "").indexOf('data-heal-class="threat" open') >= 0 &&
     (html("heal-classes") || "").indexOf('data-heal-class="idle_capital" open') >= 0,
     html("heal-classes"));
  captureHealClassState({querySelectorAll:function () { return [
    {dataset:{healClass:"threat"},open:false},
    {dataset:{healClass:"idle_capital"},open:true}
  ]; }});
  renderHealing(HEALING);
  ok("each healing disclosure retains its own open or closed state",
     (html("heal-classes") || "").indexOf('data-heal-class="threat" open') < 0 &&
     (html("heal-classes") || "").indexOf('data-heal-class="idle_capital" open') >= 0,
     html("heal-classes"));
  ok("operator shell: boundary health is summarized outside the animation", (txt("wire-hero-state") || "").indexOf("Boundary") >= 0);
  ok("live run: hero paints the live estimate", (txt("hero-produce") || "").indexOf("1,410,000") >= 0,
     txt("hero-produce"));
  ok("live run: hero carries recent sparklines", (html("spark-produce") || "").indexOf("<svg") >= 0);
  ok("live run: farm scene is telemetry, not a placeholder", (html("farm-scene") || "").indexOf("Feed silo") >= 0,
     html("farm-scene"));
  ok("live run: alternative species are not falsely labelled zero", (html("farm-scene") || "").indexOf("measured output: zero") < 0 && (html("farm-scene") || "").indexOf("nonzero observed") >= 0);
  ok("live run: leaderboard is a race", (html("leaderboard") || "").indexOf("race-track") >= 0);
  ok("live run: Grand Prix plots every recorded racer", (html("gp-chart") || "").indexOf("<svg") >= 0 && (html("gp-legend") || "").indexOf("Aaron") >= 0, html("gp-legend"));
  ok("live run: Grand Prix exposes exact sampled scores", (html("gp-chart") || "").indexOf("run 216") >= 0 && (html("gp-chart") || "").indexOf("1,410,000") >= 0, html("gp-chart"));
  RACE_MODE="gain"; RACE_RANGE=20; renderLeaderboardHistory(LIVE.leaderboard_history);
  ok("Grand Prix switches to window gain without another poll", (html("gp-chart") || "").indexOf("gained") >= 0 && (txt("gp-note") || "").indexOf("4 racers") >= 0, txt("gp-note"));
  RACE_MODE="absolute"; RACE_RANGE=100; renderLeaderboardHistory(LIVE.leaderboard_history);
  ok("live run: chart is an area chart with hover targets", (html("chart") || "").indexOf("dot-hit") >= 0);
  CHART_METRIC = "animals"; renderChart(LIVE.trend);
  ok("chart metric can be switched without another poll", txt("chart-title") === "Animals trend", txt("chart-title"));
  CHART_METRIC = "produce"; renderChart(LIVE.trend);
  ACTIVE_RUN = "216"; renderRuns(LIVE.trend, {});
  ok("run rows expand into phase/action/evidence detail", (html("runs") || "").indexOf("Decision evidence") >= 0);
  ACTIVE_RUN = null;
  ok("live run: no panel errors reported", (html("pipe-heartbeat") || "").indexOf("panel error") < 0, html("pipe-heartbeat"));

  // 2. The tab is live BETWEEN runs: a finished run counts down to the next one.
  reset();
  var idle = clone(LIVE);
  idle.pipeline.status = "ok";
  idle.pipeline.active = null;
  idle.pipeline.finished_ts = "2026-08-21T15:11:12Z";  // 10s before NOW
  idle.pipeline.updated_ts = "2026-08-21T15:11:12Z";
  render(idle);
  ok("idle: pill says waiting, not a bare status", (html("pipe-status") || "").indexOf("waiting for next run") >= 0, html("pipe-status"));
  ok("idle: countdown is populated", (txt("pipe-next") || "").indexOf("in ") === 0, "got " + txt("pipe-next"));
  var firstCountdown = txt("pipe-next");
  NOW += 5000;                     // the 1s local ticker, five seconds later
  tick();
  ok("idle: countdown advances without a new poll", txt("pipe-next") !== firstCountdown,
     firstCountdown + " -> " + txt("pipe-next"));
  ok("idle: heartbeat reports data age", (html("pipe-heartbeat") || "").indexOf("s ago") >= 0, html("pipe-heartbeat"));
  NOW -= 5000;

  // 3. An overdue run is called out rather than looking merely quiet.
  reset();
  var overdue = clone(LIVE);
  overdue.pipeline.status = "ok";
  overdue.pipeline.active = null;
  overdue.pipeline.finished_ts = "2026-08-21T15:04:00Z";  // > cadence ago
  overdue.pipeline.updated_ts = "2026-08-21T15:04:00Z";
  render(overdue);
  ok("overdue: pill warns", (html("pipe-status") || "").indexOf("overdue") >= 0, html("pipe-status"));
  ok("overdue: next-run field warns", (txt("pipe-next") || "").indexOf("overdue") === 0, "got " + txt("pipe-next"));

  // 4. A hard-killed run leaves status=running forever; that must read as stalled,
  //    not as a run whose clock is still ticking.
  reset();
  var stalled = clone(LIVE);
  stalled.pipeline.updated_ts = "2026-08-21T15:05:00Z";   // no progress write for ~6m
  render(stalled);
  ok("stalled: pill says stalled", (html("pipe-status") || "").indexOf("stalled") >= 0, html("pipe-status"));
  ok("stalled: pill is red", (html("pipe-status") || "").indexOf("offline") >= 0, html("pipe-status"));

  // 5. THE REGRESSION: a throwing panel must not silence the panels after it.
  reset();
  var realCost = renderCost, realGrowth = renderGrowth;
  renderCost = function () { throw new Error("boom in cost"); };
  renderGrowth = function () { throw new Error("boom in growth"); };
  render(clone(LIVE));
  ok("isolation: pipeline still paints", (html("pipe-steps") || "").length > 100, "len=" + (html("pipe-steps") || "").length);
  ok("isolation: active step still correct", txt("pipe-active") === "feed", "got " + txt("pipe-active"));
  ok("isolation: signals still paint", (html("signals") || "").length > 20);
  ok("isolation: chart still paints", (html("chart") || "").indexOf("<svg") >= 0);
  ok("isolation: log tail still paints", (txt("log") || "").indexOf("run=216") >= 0);
  ok("isolation: failures are surfaced, not swallowed", (html("pipe-heartbeat") || "").indexOf("2 panel error") >= 0, html("pipe-heartbeat"));
  renderCost = realCost; renderGrowth = realGrowth;

  // 6. An empty payload must not throw (first paint, or state/ wiped).
  reset();
  var threw = null;
  try { render({}); } catch (e) { threw = e.message || String(e); }
  ok("empty payload does not throw", threw === null, threw || "");
  ok("empty payload: pipeline degrades to idle", txt("pipe-active") === "idle", "got " + txt("pipe-active"));

  // 7. Findings are independently renderable: a malformed operational payload
  //    cannot take the measured evidence down with it.
  document.getElementById("ev-runs-slider").value = 288;
  var TEST_EVIDENCE = __EVIDENCE__, evThrew = null;
  try { renderEvidence(TEST_EVIDENCE); } catch (e) { evThrew = e.message || String(e); }
  ok("findings render does not throw", evThrew === null, evThrew || "");
  ok("findings: ceiling evidence is visual", (html("ev-ceiling-chart") || "").indexOf("<svg") >= 0);
  ok("findings: the scientific claim has a plain-language lead", (txt("ev-ceiling-summary") || "").indexOf("Growth is") === 0, txt("ev-ceiling-summary"));
  ok("findings: the strategy card selects the growth claim", (html("ev-strategy-brief") || "").indexOf("scaling") >= 0, html("ev-strategy-brief"));
  ok("findings: knowledge lifecycle is visible", (html("knowledge-flow") || "").indexOf("Questions") >= 0 && (html("knowledge-flow") || "").indexOf("Runtime") >= 0);
  ok("findings: measured sample count is shown", (html("ev-ceiling-stats") || "").indexOf("samples") >= 0);
  // The growth decision rests on the exponent, so it must be visible on the tab and
  // not merely present in the payload.
  ok("findings: scaling exponent is shown", (html("ev-ceiling-stats") || "").indexOf("scaling exponent") >= 0);
  ok("findings: unweighted and weighted bucket r are shown side by side",
     (html("ev-ceiling-stats") || "").indexOf("unweighted vs weighted") >= 0);
  ok("findings: the method note states the association is not identified",
     (txt("ev-ceiling-method") || "").indexOf("cannot be separated") >= 0);
  ok("findings: every species gets a bar", (html("ev-species") || "").indexOf("sheep") >= 0);
  ok("findings: cost model produces a monthly number", (txt("ev-old-cost") || "").indexOf("$") === 0,
     txt("ev-old-cost"));
  ok("findings: detector redesigns are present", (html("ev-detectors") || "").indexOf("throughput") >= 0);
  ok("findings: experiment timeline is present", (html("ev-timeline") || "").indexOf("Run 50") >= 0);
  ok("findings: semantic contract is visible and passing",
     (html("ev-knowledge-stats") || "").indexOf("semantic contract") >= 0
     && (html("ev-knowledge-stats") || "").indexOf("passing") >= 0);
  ok("findings: accepted linear claim is visible",
     (html("ev-claims") || "").indexOf("mechanic.output_linear_with_herd") >= 0
     && (html("ev-claims") || "").indexOf("accepted") >= 0);
  ok("findings: superseded plateau remains explicit",
     (html("ev-claims") || "").indexOf("mechanic.per_farm_output_plateau") >= 0
     && (html("ev-claims") || "").indexOf("superseded") >= 0);
  ok("findings: policy identity and zero-call replay are visible",
     (txt("ev-policy") || "").indexOf("Runtime policy") >= 0
     && (txt("ev-policy") || "").indexOf("0 MCP calls") >= 0,
     txt("ev-policy"));
  ok("cost history: actual ledger cost is explicit",
     txt("hist-actual-cost") === "$" + TEST_EVIDENCE.cost_history.stats.actual_cost.toFixed(2),
     txt("hist-actual-cost"));
  ok("cost history: counterfactual avoided cost is shown", (txt("hist-avoided") || "").indexOf("$") === 0,
     txt("hist-avoided"));
  ok("cost history: actual and counterfactual curves render", (html("hist-chart") || "").indexOf("cost-actual") >= 0
     && (html("hist-chart") || "").indexOf("cost-old") >= 0);
  ok("cost history: execution changes tell the Python story", (html("hist-changes") || "").indexOf("Deterministic Python cycle") >= 0);
  ok("cost history: token sources are decomposed", (html("hist-sources") || "").indexOf("raw tool text") >= 0);
  ok("cost history: every audited run gets a proof square", (html("hist-ledger") || "").indexOf("ledger-dot") >= 0);
  COST_HISTORY_METRIC = "tokens"; renderCostHistoryChart(TEST_EVIDENCE.cost_history);
  ok("cost history: metric switches without another fetch", txt("hist-chart-title") === "Cumulative tokens over audited runs",
     txt("hist-chart-title"));
  COST_HISTORY_RANGE = "50"; renderCostHistoryChart(TEST_EVIDENCE.cost_history);
  ok("cost history: range switches to the last 50 runs", (html("hist-chart") || "").indexOf("run " + TEST_EVIDENCE.cost_history.points.slice(-50)[0].run) >= 0);
  COST_HISTORY_METRIC = "cost"; COST_HISTORY_RANGE = "all";

  // 8. tick() before any poll must be a no-op rather than an error.
  ok("tick before first poll is safe", (function () {
    try { LAST = null; tick(); return true; } catch (e) { return false; }
  })());

  // 9. The 2D execution trace, driven through its real API and real source graph.
  // The DOM stub intentionally provides no canvas: this replacement does not
  // need one, so its primary representation is also its accessible text form.
  reset();
  var TOPOLOGY = __TOPOLOGY__;
  ok("trace: panel is exposed on the page", typeof TracePanel === "object" && !!TracePanel);
  ok("trace: engine is exposed on the page", typeof TraceExplorer === "object" && !!TraceExplorer);
  var mounted = TracePanel.mount({rootId: "trace-explorer"});
  ok("trace: mounts in a DOM with no canvas", mounted === true);

  var traceThrew = null;
  try {
    TracePanel.setTopology(TOPOLOGY);
    TracePanel.update(clone(LIVE));
  } catch (e) { traceThrew = e.message || String(e); }
  ok("trace: real topology plus a live payload does not throw", traceThrew === null, traceThrew || "");
  var traceHtml = html("trace-explorer") || "";
  ok("trace: run view names measured steps and server tools",
     traceHtml.indexOf("Collect produce") >= 0 && traceHtml.indexOf("collect_produce") >= 0, traceHtml.slice(0, 220));
  ok("trace: real tool calls are represented as spans",
     (traceHtml.split("te-call-span ").length - 1) === 2);
  ok("trace: full MCP coverage is explicit", traceHtml.indexOf("all MCP calls") >= 0);
  ok("trace: static functions are not mislabelled as runtime spans",
     traceHtml.indexOf("reachability, not measured time") >= 0);

  var traceModel = TracePanel.state.model;
  ok("trace: model preserves every payload step", !!traceModel && traceModel.steps.length === 5,
     traceModel ? "steps=" + traceModel.steps.length : "none");
  ok("trace: model preserves every observed call", traceModel && traceModel.calls.length === 2);
  ok("trace: active step timing grows from the shared clock",
     traceModel && traceModel.active && traceModel.active.name === "feed" && traceModel.active.duration > 0);
  ok("trace: pending step has no invented duration",
     traceModel && traceModel.steps[4].duration === null);

  TracePanel.setView("matrix");
  traceHtml = html("trace-explorer") || "";
  ok("trace: matrix names the MCP process boundary",
     traceHtml.indexOf("MCP boundary") >= 0 && traceHtml.indexOf("Farm Friends server") >= 0);
  ok("trace: matrix overlays observed counts on source reachability",
     traceHtml.indexOf("observed this run") >= 0 && traceHtml.indexOf("reachable in code") >= 0);

  TracePanel.select("call:collect|collect_produce");
  traceHtml = html("trace-explorer") || "";
  ok("trace: selecting a tool exposes arguments and result",
     traceHtml.indexOf("&quot;pass&quot;:1") >= 0 && traceHtml.indexOf("5000 units") >= 0);
  ok("trace: tool inspector exposes the Python-to-server path",
     traceHtml.indexOf("Client.call") >= 0 && traceHtml.indexOf("collect_produce") >= 0);

  TracePanel.select("node:" + encodeURIComponent("cycle:Cycle.feed_if_needed"));
  traceHtml = html("trace-explorer") || "";
  ok("trace: selecting a function names source and line",
     traceHtml.indexOf("STATIC CODE") >= 0 && traceHtml.indexOf("cycle.py:") >= 0, traceHtml.slice(-400));
  ok("trace: function inspector states that timing is static",
     traceHtml.indexOf("not presented as a measured runtime span") >= 0);
  TracePanel.clearSelection();
  ok("trace: closing selection returns to ambient state", TracePanel.state.selected === null);

  // A malformed or missing payload must degrade, not throw: this panel shares a
  // script with the polling loop and is repainted by the one-second ticker.
  var degraded = null;
  try { TracePanel.update({}); TracePanel.setView("trace"); TracePanel.reset(); TracePanel.paint(); }
  catch (e) { degraded = e.message || String(e); }
  ok("trace: an empty payload and every control degrade safely", degraded === null, degraded || "");

  // 10. The MCP switchboard: the animated boundary view, driven through its real
  // API and the real source graph. It shares the poll and the 1s ticker with
  // every other panel, so the contract is that it degrades and never throws.
  reset();
  ok("wire: engine is exposed on the page", typeof MCPWire === "object" && !!MCPWire);
  ok("wire: panel is exposed on the page", typeof MCPWirePanel === "object" && !!MCPWirePanel);
  var wireMounted = MCPWirePanel.mount({rootId: "mcp-wire"});
  ok("wire: mounts in a DOM with no canvas", wireMounted === true);
  var wireThrew = null;
  try {
    MCPWirePanel.setTopology(TOPOLOGY);
    MCPWirePanel.update(clone(LIVE));
  } catch (e) { wireThrew = e.message || String(e); }
  ok("wire: real topology plus a live payload does not throw", wireThrew === null, wireThrew || "");
  var wireHtml = html("mcp-wire") || "";
  ok("wire: every observed call becomes exactly one packet",
     (wireHtml.split("mw-packet ").length - 1) === 2, wireHtml.slice(0, 200));
  ok("wire: packets carry measured start and duration",
     wireHtml.indexOf("--t0:") >= 0 && wireHtml.indexOf("--dur:") >= 0);
  ok("wire: the in-flight call is drawn as in flight, not landed", wireHtml.indexOf("flying") >= 0);
  ok("wire: silent-but-reachable tools stay visible", wireHtml.indexOf("not called this run") >= 0);
  ok("wire: mutating tools are marked as such", wireHtml.indexOf("mutates") >= 0);
  ok("wire: replay speed is disclosed rather than implied",
     wireHtml.indexOf("replay speed") >= 0 && wireHtml.indexOf("measured duration") >= 0);
  var wireModel = MCPWirePanel.state.model;
  ok("wire: model keeps the payload's calls and tools",
     !!wireModel && wireModel.calls.length === 2 && wireModel.lanes.length > 2,
     wireModel ? wireModel.calls.length + "/" + wireModel.lanes.length : "none");
  ok("wire: in-flight call is counted once", wireModel && wireModel.stats.inFlight === 1);
  var wireDegraded = null;
  try {
    MCPWirePanel.setSpeed(12); MCPWirePanel.setPaused(true); MCPWirePanel.focus("tool:feed_animals");
    MCPWirePanel.paint(); MCPWirePanel.reset(); MCPWirePanel.update({});
  } catch (e) { wireDegraded = e.message || String(e); }
  ok("wire: an empty payload and every control degrade safely", wireDegraded === null, wireDegraded || "");
  ok("wire: no telemetry means no animation at all",
     (html("mcp-wire") || "").indexOf("mw-packet") < 0, html("mcp-wire"));

  return {checks: checks, failures: failures};
})();
JSON.stringify(RESULT);
"""


def main() -> int:
    script = (HARNESS
              .replace("__PAGE_SCRIPT__", _page_script())
              .replace("__PAYLOAD__", json.dumps(_payload()))
              .replace("__TOPOLOGY__", json.dumps(monitor.topology.cached_graph()))
              .replace("__EVIDENCE__", json.dumps(monitor.evidence.report())))

    # The dashboard's bootstrap ordering is a property of the page text, not of
    # the render functions, so it is asserted here rather than in JS.
    html = monitor.HTML
    boot = html.find("load(); setInterval(load, 2000)")
    game = html.find("/*GAME_JS_START*/")
    snapshot_error = None
    try:
        monitor.snapshot()
    except Exception as exc:  # noqa: BLE001 - this check reports the real failure
        snapshot_error = "%s: %s" % (exc.__class__.__name__, exc)
    # The release gate must recognize modifier classes such as operator-tab and
    # architecture-tab instead of requiring the class attribute to equal "tab".
    packaged_panels = set()
    for tag in re.findall(r'<div\b[^>]*>', html):
        panel_id = re.search(r'\bid="tab-([a-z_]+)"', tag)
        classes = re.search(r'\bclass="([^"]*)"', tag)
        if panel_id and classes and "tab" in classes.group(1).split():
            packaged_panels.add(panel_id.group(1))
    expected_panels = {"overview", "pipeline", "cost", "history", "findings", "game", "wire", "architecture"}
    adaptive_projection = monitor._adaptive_summary([_payload()["latest"]])
    original_workorders_current = monitor.workorders.current
    monitor.workorders.current = lambda path: {
        "runtime-parse-list-farm": {
            "id": "runtime-parse-list-farm",
            "status": "open",
            "tool": "list_farm",
            "summary": "list_farm response changed",
            "detail": {"sample": "list_farm_state.txt"},
            "provenance": {"change_class": "compatibility"},
            "created_ts": "2026-08-28T17:00:00Z",
        }
    }
    try:
        compatibility_projection = monitor._adaptive_summary([_payload()["latest"]])
    finally:
        monitor.workorders.current = original_workorders_current
    static_checks = [
        ("real /api/state snapshot survives trace helpers", snapshot_error is None),
        ("bootstrap runs before the game bundle", boot != -1 and game != -1 and boot < game),
        ("local redraw ticker is installed", "setInterval(tick, 1000)" in html),
        ("game bundle is wrapped in try/catch", re.search(r"try\s*\{\s*/\*GAME_JS_START\*/", html) is not None),
        ("no unsubstituted template markers",
         not re.search(r"__(GAME|TRACE|WIRE|ARCH|OPERATOR)_(JS|CSS|MARKUP)__", html)),
        # A placeholder token mentioned anywhere in the template (even inside a
        # comment) is substituted textually, which silently injects a second copy
        # of the whole game bundle and breaks the page.
        ("game bundle is embedded exactly once", html.count(monitor.GAME_JS) == 1),
        ("operator bundle is embedded exactly once", html.count(monitor.OPERATOR_JS) == 1),
        ("operator stylesheet is embedded", ".autonomy-ribbon" in html and ".knowledge-flow" in html),
        ("adaptive activity is a first-class pipeline panel", 'id="adaptive-card"' in html and 'id="adaptive-events"' in html),
        ("adaptive panel styles disclose holding and resolved states", ".adaptive-card.watch" in html and ".adaptive-event.resolved" in html),
        ("adaptive server projection preserves holds and trade protection",
         adaptive_projection.get("blocked_domains") == ["offers", "trades"]
         and adaptive_projection.get("trade_coin_outflow") == 0
         and adaptive_projection.get("trade_coin_outflow_blocked") == 400000
         and len(adaptive_projection.get("recent_events") or []) == 1),
        ("adaptive server projection exposes compatibility self-healing",
         compatibility_projection.get("status") == "repairing"
         and compatibility_projection.get("compatibility", {}).get("orders", [{}])[0].get("tool") == "list_farm"
         and any(block.get("class") == "compatibility_repair"
                 for block in compatibility_projection.get("active_blocks") or [])),
        ("served page has no operator-attention state token",
         all(token not in html for token in (
             "system-state attention", "hero-verdict attention",
             ".system-state.attention", ".arch-warn.attention",
         ))),
        ("seven standard tabs retain the scoped operator system", html.count('class="tab operator-tab"') == 7),
        ("architecture composes the shared hierarchy through its specialized tab", 'class="tab architecture-tab" id="tab-architecture"' in html),
        ("architecture bundle is embedded exactly once", html.count(monitor.ARCH_JS) == 1),
        ("architecture stylesheet includes operator summary and inspector posture",
         ".arch-situation" in html and ".arch-component-posture" in html),
        ("architecture audit descriptions cannot inherit global detail-card spacing",
         'class="arch-ev-detail"' in monitor.ARCH_JS and ".arch-ev-detail" in monitor.ARCH_CSS
         and 'class="detail"' not in monitor.ARCH_JS),
        ("architecture answers the four headless-control questions before the graph",
         all(text in monitor.ARCH_JS for text in ("Happening now", "What changed", "Autonomous action", "Recovery ownership"))),
        ("architecture history defaults to progressive disclosure", 'arch-history audit-drawer' in monitor.ARCH_JS),
        ("release-compatible panel discovery accepts modifier classes", packaged_panels == expected_panels),
        ("autonomy loads outside Architecture on its own cadence", "loadOperatorAutonomy" in html and "OP_AUTONOMY_REFRESH_MS" in html),
        ("raw audit walls default to progressive disclosure", html.count("audit-drawer") >= 8),
        ("trace bundle is embedded exactly once", html.count(monitor.TRACE_JS) == 1),
        ("trace engine and panel are both embedded",
         "TraceExplorer" in html and "TracePanel" in html),
        # The panel must exist before the first payload can arrive, and its
        # failure must not be able to stop the poll: mounted in its own try/catch,
        # before the bootstrap line.
        ("trace bundle is mounted before the polling bootstrap",
         -1 < html.find("/*TRACE_JS_START*/") < boot),
        ("trace bundle is wrapped in try/catch",
         re.search(r"try\s*\{\s*/\*TRACE_JS_START\*/", html) is not None),
        ("trace stylesheet is embedded", ".te-workspace" in html),
        ("coverage state cannot inherit the host full-width card rule",
         "coverage-full" in monitor.TRACE_JS and 'class="te-coverage full"' not in monitor.TRACE_JS),
        ("matrix has explicit fixed column geometry",
         "table-layout:fixed" in monitor.TRACE_CSS and ".te-col-tool" in monitor.TRACE_CSS),
        ("explorer content cannot inherit host main sizing",
         'class="te-main"' in monitor.TRACE_JS and "<main>" not in monitor.TRACE_JS),
        ("tool headings override the host table nowrap rule",
         re.search(r"\.te-tool-head span\s*\{[^}]*white-space:normal", monitor.TRACE_CSS, re.S) is not None),
        ("pipeline tab hosts the execution trace", 'id="trace-explorer"' in html),
        ("trace needs no canvas or spatial fallback", 'id="trace-canvas"' not in html),
        ("trace explains measured spans versus static reachability",
         "never as invented runtime spans" in html),
        ("findings have a dedicated non-polled endpoint", 'path == "/api/evidence"' in open(monitor.__file__, encoding="utf-8").read()),
        ("hash routes every top-level experience", '"overview","pipeline","cost","history","findings","game","wire","architecture"' in html),
        ("keyboard shortcuts route all eight tabs", 'o:"overview",p:"pipeline",c:"cost",t:"history",f:"findings",g:"game",w:"wire",a:"architecture"' in html),
        # The switchboard is the only animated panel, and it is mounted next to the
        # trace: same isolation contract, same before-the-poll ordering.
        ("switchboard bundle is embedded exactly once", html.count(monitor.WIRE_JS) == 1),
        ("switchboard engine and panel are both embedded",
         "MCPWire" in html and "MCPWirePanel" in html),
        ("switchboard is mounted before the polling bootstrap",
         -1 < html.find("/*WIRE_JS_START*/") < boot),
        ("switchboard bundle is wrapped in try/catch",
         re.search(r"try\s*\{\s*/\*WIRE_JS_START\*/", html) is not None),
        ("switchboard stylesheet is embedded", ".mw-stage" in html and "@keyframes mw-fly" in html),
        ("switchboard has its own tab and root", 'id="tab-wire"' in html and 'id="mcp-wire"' in html),
        ("switchboard needs no canvas", "canvas" not in monitor.WIRE_JS),
        ("switchboard motion has a reduced-motion fallback",
         "prefers-reduced-motion" in monitor.WIRE_CSS and "--x" in monitor.WIRE_JS),
        ("switchboard cannot inherit host layout classes",
         "<main>" not in monitor.WIRE_JS and 'class="full"' not in monitor.WIRE_JS),
        ("switchboard is repainted by both the poll and the ticker",
         html.count('window.MCPWirePanel') >= 3),
    ]

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run(["osascript", "-l", "JavaScript", path],
                              capture_output=True, text=True)
    finally:
        os.unlink(path)

    if proc.returncode != 0:
        print("DASHBOARD TEST FAILED: the page script did not run")
        print((proc.stderr or proc.stdout).strip()[:2000])
        return 1

    result = json.loads(proc.stdout.strip())
    for name, passed in static_checks:
        print("  %-4s %s" % ("ok" if passed else "FAIL", name))
    for check in result["checks"]:
        detail = ("  [%s]" % check["detail"]) if (check["detail"] and not check["pass"]) else ""
        print("  %-4s %s%s" % ("ok" if check["pass"] else "FAIL", check["name"], detail))

    failures = [n for n, p in static_checks if not p] + result["failures"]
    total = len(static_checks) + len(result["checks"])
    print()
    if failures:
        print("DASHBOARD TEST FAILED: %d of %d checks" % (len(failures), total))
        for failure in failures:
            print("  - %s" % failure)
        return 1
    print("DASHBOARD TEST PASSED: %d checks" % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
