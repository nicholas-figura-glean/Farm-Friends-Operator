# Farm Friends operator

Deterministic Python runs the farm. An LLM is only involved when something breaks.

## Why

The loop used to be executed by an LLM automation every 5 minutes. Measured over
46 runs on 2026-08-20, each run cost roughly **150k-600k billed input tokens**:
62k/run of raw tool text, 59k of thinking, re-sent across ~21 assistant turns.
The largest single input was `list_farm`, a plain-text wall that grows ~65 bytes
per animal and was read three or four times per run. At 3,000 animals that alone
is ~200KB per run.

None of that reasoning belongs in routine execution. Actions are deterministic
arithmetic in `farm/rules.py`; bounded investigation, claim revision, and policy
promotion live off the mutation path.

## Current bulk and risk architecture (2026-08-24)

- `collect_produce` runs exactly once at the start of every cycle. It is a
  constant-time bulk operation; there is no cadence, retry loop, batching, or
  per-animal fan-out. Frequent draining limits inventory exposed to spoilage.
- `feed_animals(animal_id="all")` runs at most once per cycle when feeding is
  due. Individual-ID mop-up and its healing knob have been removed.
- `farm_events(limit=100)` runs every cycle. Wolves, sickness, storms, spoilage,
  and visible vet/repair charges are normalized, deduplicated, and written into
  the run ledger for the dashboard and anomaly evaluator.
- Expansion preserves `RISK_COIN_RESERVE` in liquid coins for automatic bills;
  the existing feed runway remains separate and is still restored after growth.
- Risk events are expected stochastic losses, not transport failures. A verified
  animal shortfall accompanied by a wolf/sickness event is recorded as explained
  telemetry rather than treated as unexplained state drift.

| | before | after |
|---|---|---|
| per cycle | 150k-600k billed input tokens | **0** |
| cadence | every 300s | every 300s |
| supervision | the loop itself | 60s deterministic supervisor + durable questions |
| scaling with herd size | re-read and re-reasoned every turn | parsed in Python, never surfaced |

## Layout

```
run.py                 execution, supervision, questions, audits, sweep, policy CLI
experiments/           declarative bounded probes; mutating probes require explicit use
farm/analysis.py       full-ledger cohorts, regimes, regression, immutable fingerprints
farm/claims.py         versioned claims, confidence, freshness, falsifiers, supersession
farm/questions.py      durable current question registry + transition events
farm/policy.py         content-addressed compile, semantic gate, explicit promotion
farm/research.py       replay audits, drift checks, counterfactuals, decision bundles
farm/ledger.py         normalized actor/run/policy/intervention observation stream
farm/probes.py         lock- and budget-enforced probe scheduler
farm/mcp.py            JSON-RPC, endpoint scrubbing, contextual boundary spans
farm/parse.py          plain text -> dataclasses; risk events normalized by category
farm/rules.py          pure executable arithmetic and replayable self-audit rules
farm/growth.py         reversible recent-evidence growth brake
farm/evidence.py       dashboard projection generated from estimators and claims
farm/cycle.py          deterministic loop, intents, predictions, outcome verification
farm/watch.py          operational + strategy detectors
farm/heal.py           operational remedies + durable-question disposition
farm/scheduler.py      launchd liveness and repair for both agents
farm/contract.py       full server contract capture, fingerprint, severity-classified drift
farm/workorders.py     append-only work queue between detection and repair
farm/canary.py         provisional releases: prove a flip helped, or revert it
farm/llm.py            the ONLY module that talks to a model (Glean llm_proxy gateway)
farm/progress.py       live pipeline position for the dashboard
farm/topology.py       static call graph (steps -> functions -> MCP tools), read from farm/*.py with ast
farm/tokens.py         token/cost ledger for the exception path
farm/report.py         compact rendering (cycle summary, review, dry-run plan)
farm/journal.py        generates journal entries and the alert queue
farm/release.py        release identity and staleness detection
monitor.py             read-only local dashboard (python3 monitor.py)
POSTMORTEM-run291.md   how first place was lost and what was changed; read this
                       before touching feed, the growth gate, or the healer
POSTMORTEM-run377.md   how it was nearly lost again: three throttles aimed at the
                       wrong variable, the collect timeout wall, and the measured
                       proof that output is linear in herd size. Read this before
                       touching transport errors, the reserve alert, or collect.
dashboard/             2D trace explorer, tool matrix, MCP switchboard, styles and headless tests
game/                  Coop Rush: engine, UI, styles, markup and headless tests
deploy/release.sh      publish an immutable release, flip the pointer atomically
deploy/test_dashboard.sh  topology, MCP spans, trace/switchboard models and page-render suites
deploy/install.sh      install/remove all launchd agents
releases/<rev>/        immutable published copies; `release` symlinks to the live one
state/history.ndjson   one machine-readable row per run
state/observations.ndjson normalized actor/run/policy/intervention outcomes
state/claims.json      current claim registry; transitions in claim_events.ndjson
state/questions.ndjson one current row per durable question identity
state/policy.json      explicitly promoted policy snapshot
state/audits.ndjson    semantic, drift, and counterfactual audit history
state/experiments.ndjson bounded probe execution history
state/alerts.ndjson    operational/strategy signal queue
state/heal.json        supervisor knobs (only farm/heal.py writes this)
state/heal.ndjson      every operational remedy the supervisor applied
state/progress.json    live pipeline position
state/tool_calls.ndjson paired MCP spans with actor/run/policy attribution
state/growth.json      recent reversible growth-brake verdict
state/contract.json    the server contract the current code was written against
state/contract.ndjson  one row per contract scan, with classified drift
state/workorders.ndjson code-change work orders; one current row per order id
state/canary.json      the release currently on probation, if any
state/canary.ndjson    every arm/resolve event, including reverts
state/research_findings.ndjson  research scans and model hypotheses
state/tokens.ndjson    token/cost ledger; routine runs record an explicit zero
state/raw/latest/      last raw responses, overwritten each run
state/intents.ndjson   legacy before/after mutation journal, retained for compatibility
```

Exit codes: `0` routine, `3` needs attention, `4` hard failure.

## Operating it

```bash
python3 run.py --dry-run      # read live state, print the decision, mutate nothing
python3 run.py --alerts       # only what survived healing; costs tokens when it prints
python3 run.py --supervise     # the self-healing pass (what the 60s agent runs)
python3 run.py --heal-status    # active knobs, recent remedies, token cost
python3 run.py --review 20    # trend across recent runs
python3 run.py --questions    # durable open strategic uncertainties
python3 run.py --research-audit # semantic contracts + drift + question updates
python3 run.py --sweep        # pure counterfactual replay; zero MCP calls
python3 run.py --knowledge-refresh # rebuild versioned claims
python3 run.py --policy-status # promoted/runtime compatibility
python3 run.py --probes       # list bounded probes and budgets
python3 run.py --self-test    # focused runtime regression suite
python3 run.py --contract-status # server contract baseline + recent drift
python3 run.py --orders        # the code-change work order queue
python3 run.py --canary-status # the release currently on probation
python3 run.py --llm-status    # headless model availability and authoring spend
deploy/release.sh             # publish the working tree (required after ANY edit)
deploy/install.sh             # install/refresh both agents
deploy/install.sh --uninstall # stop the schedule (boots out the supervisor first)
python3 monitor.py             # open the read-only live dashboard
python3 monitor.py --no-open    # serve it without opening a browser
python3 monitor.py --port 8877  # if 8765 is taken (it also auto-falls-back)
deploy/open_monitor.py          # reuse a running dashboard, else start one, then open it
deploy/export_game.py           # write coop-rush.html, a standalone playable file
deploy/test_game.sh             # run the game's headless test suites
deploy/test_dashboard.py        # 58 checks: panels, liveness, visuals, cost history, findings
python3 deploy/test_evidence.py  # 25 checks: derived findings and cost history stay ledger-faithful
deploy/make_app.sh              # build double-clickable Coop Rush.app / Farm Monitor.app
```

### Running the dashboard and the game

There is no native app and no build step: the dashboard is a local Python server
and the game is a dependency-free DOM app on one of its tabs. Three ways in, in
order of effort:

| | what it does |
| --- | --- |
| `python3 monitor.py` | serves the dashboard and opens it; the game is the **Coop Rush** tab, the live MCP traffic is the **MCP Switchboard** tab |
| `python3 deploy/export_game.py` then open `coop-rush.html` | the game alone, one self-contained file, no server and no network |
| `deploy/make_app.sh` | `apps/Coop Rush.app` and `apps/Farm Monitor.app`, double-clickable in Finder |

The `.app` bundles are real bundles (Info.plist + an executable) but not native
code: one re-exports and opens the HTML, the other calls `deploy/open_monitor.py`.
Nothing depends on them.

Two port details, both learned the hard way on this machine:

- **8765 was already taken** by an unrelated local app, so `monitor.py` now binds
  the first free port at or after the one requested and prints where it landed
  (`--strict-port` to refuse instead).
- **"Does something answer on 8765?" is the wrong liveness check.** The squatter
  returned HTTP 200 on `/`, so a status-code check reused someone else's page and
  never started the monitor. `deploy/open_monitor.py` identifies our dashboard by
  the `app: farmfriends-monitor` marker in `/api/state`, which is why running it
  twice reuses one server instead of starting a second.

The monitor is local and read-only, with six tabs. Use the tabs or the keyboard:
`O` overview, `P` pipeline, `C` cost, `T` token history, `F` findings, `G` game.

- **Overview** - a live-estimated hero counter with sparklines; a farm scene whose
  pens, silo, hunger pressure and barn are all real telemetry; current standings;
  and the **Produce Grand Prix**, a zero-extra-call multi-racer chart over 20, 50,
  or 100 recorded runs with total-score and window-gain views. It also shows six
  switchable farm trends, launchd status, current intent, growth policy, and
  expandable recent runs with phase timing, actions, and decision evidence.
- **Pipeline** - opens with a two-part **execution trace**, using the same model
  as production tracing tools instead of asking one spatial graph to explain
  everything. **Run trace** is a nested waterfall: the fourteen measured step
  spans share one clock, and every observed MCP call is grouped beneath its
  parent step. Repeated calls (for example 40 adoptions) occupy one lane with 40
  individually inspectable ticks, not 40 noisy rows. **Tool matrix** is the whole
  system at a glance: pipeline steps are rows, the 11 external MCP tools are
  columns, a hollow cell means the code can reach that tool, and a numbered green
  cell is how many calls this run actually made. A blank is meaningful: `plan`
  and `finish` have no server path. Selecting a step, call lane or matrix cell
  opens its measured start/duration/status, arguments, bounded result or error,
  and the source-derived Python path. Functions are explicitly labelled **static
  reachability, not measured time**; the UI never invents function spans.
- Below it, the same run step by step: status per step
  (pending / active / done / skipped / failed), duration against the recent
  median for that step, what each step found (units collected, revenue, adopted
  vs requested, rank), the reason any step was skipped, and elapsed time against
  the cycle budget and hard timeout. A run occupies ~80s of every ~260s, so
  between runs the tab counts down to the next expected start (last finish +
  cadence) and flags it when that is overdue; a run whose progress writes have
  stopped reads **stalled** rather than ticking forever, which is what a
  hard-killed process leaves behind. Plus **what the run is judged on**: the score
  rate against its floor, whether the previous window was also low (escalation
  needs two), hunger against the production stop, and feed against the reserve.
- **Cost & healing** - all-time LLM cost, tokens and wake-ups; how many runs were
  charged versus recorded at zero; the last 5 runs broken out (tokens in/out,
  cost, wake-ups, alerts healed, and the alerts each run raised); the cost avoided
  by healing; **what is being self-healed**, grouped by alert class with the last
  remedy and the alert text behind it; the active knobs with healed ones marked;
  and the full remedy log.
- **Token & cost history** - the longitudinal before/after story: actual cumulative
  ledger cost against the measured LLM-era low/mid/high counterfactual, switchable
  cost/token/per-run/healing views, 50/100/all-run ranges, the token composition of
  the old loop, projected monthly impact, a run-by-run zero-cost proof strip, and
  the six Python changes that moved the line to zero. Green is booked data; amber
  is explicitly labelled as the 46-run historical estimate rather than an invoice.
- **Findings** - the depth behind the operator: a ledger-derived herd/output curve,
  the run-50 species experiment, retired crop and collection hypotheses, a live
  old-loop cost counterfactual, detector redesigns, and the run-by-run timeline of
  how the model changed. It comes from `/api/evidence`, fetched once rather than
  repeated on the 2-second state poll.
- **Coop Rush** - a playable incremental farm game (see below).

It reads `state/` only and never calls the farm API.

The pipeline view is fed by `farm/progress.py`, which the loop updates as it
runs. Progress writes are atomic and best-effort: monitoring costs visibility
when it fails, never a cycle. The alternative (inferring position from
`state/raw/latest/` file mtimes) needed no instrumentation but could not tell
which run a file belonged to, could not see steps that make no server call, and
could not distinguish "skipped" from "not reached yet".

The execution trace combines two sources, and keeps their epistemic boundary
visible:

- **Measured spans.** `farm/progress.py` records the fourteen step intervals.
  `farm/mcp.py` records best-effort start/end rows around `Client.call()` and the
  `tools/list` handshake in `state/tool_calls.ndjson`: tool, bounded arguments,
  duration, success/error and a scrubbed 240-character result preview. Writes
  swallow their own errors, so a full disk costs observability rather than a
  cycle. Until that instrumentation is present in a deployed release, the panel
  falls back to mutation intent pairs and says **mutation calls only** rather
  than pretending read-only calls were captured.
- **Static reachability.** `farm/topology.py` parses `farm/*.py` with `ast` and
  resolves calls it can prove: `self.foo()`, `module.foo()`, `foo()` and
  `self.c.call("tool")`. Anything dynamic resolves to nothing rather than to a
  guess. Static extraction shows all possible paths, including a skipped step;
  measured boundary spans show which paths this run actually took. Profiling
  every Python function would slow the cycle and still say nothing about paths a
  particular run skipped, so functions stay in the inspector instead of the
  timing waterfall.
- **Time and architecture are separate views.** A waterfall is good at sequence,
  nesting and duration. A matrix is good at many-to-many reachability. The old
  spatial graph tried to encode both and produced crossing lines; the matrix has
  no lines to cross and preserves meaningful absence.
- **Static data stays off the poll.** `/api/topology` is fetched once and refreshed
  only when its fingerprint changes. `/api/state` carries the small live half:
  progress plus current-run call spans.
- **No dependency or build step.** `dashboard/trace_explorer.js` derives the span
  model, routes source paths, renders both views and owns interaction state with
  plain DOM/CSS. Its primary view is already structured text, so there is no
  canvas fallback or animation loop to fail.
- **Headless proof.** `dashboard/test_trace_explorer.js` verifies 50 model and HTML
  contracts; `dashboard/test_mcp_wire.js` verifies 71 switchboard contracts;
  `deploy/test_tool_trace.py` verifies 21 telemetry, pairing, error and
  redaction contracts; `deploy/test_dashboard.py` runs the real page script and
  snapshot against a DOM stub for 122 checks; topology adds 61. Run all 325 with
  `deploy/test_dashboard.sh`. Render the current real run without opening a
  browser using `python3 deploy/preview_trace_explorer.py --check` (or
  `--view matrix`). `run.py --self-test` additionally blocks a release whose
  topology no longer matches `progress.STEPS` or has lost a server call.

The dashboard's own JavaScript is tested headlessly by `deploy/test_dashboard.py`,
which runs the real page script in JavaScriptCore against a DOM stub. It exists
because the page had two ways to freeze while still looking alive, neither
visible from the server side:

- **`render()` was one unguarded chain.** The pipeline, signals, chart and log
  tail were painted last, so a throw in any earlier panel left them frozen at
  their previous values while the overview kept refreshing. Each panel is now
  painted through `safe()`, and the number of failed panels is shown on the page
  instead of only in the console.
- **The poll bootstrap ran after the injected game bundle.** A top-level throw in
  the game stopped `load()` and `setInterval` from ever running and froze every
  tab at "connecting". The refresh now starts first and the game is wrapped in
  its own try/catch: the game is a tab, not the product.

Time-dependent panels also redraw on a local 1s tick rather than only when a poll
lands, so elapsed clocks and the next-run countdown advance even when the payload
is unchanged. A related trap the suite now guards: template placeholders are
substituted textually, so merely *naming* the game placeholder in a comment
injected a second 31 KB copy of the bundle into the page.

### MCP switchboard

The trace tab answers *what happened when*. The **🛰️ MCP Switchboard** tab answers
*what the boundary feels like*: a cycle fires hundreds of JSON-RPC calls in bursts,
mostly from one step, with latencies spanning three orders of magnitude. That is a
picture, not a table.

Each dot is one row of `state/tool_calls.ndjson` flying from the pipeline step that
issued it to the server. Lanes are server tools; packet colour is the issuing step;
flight time is the measured `duration_ms` divided by the replay speed. Alongside it,
a concurrency curve built from real start/end timestamps shows how parallel the
boundary actually is — the number no list of 700 rows conveys.

An animated view is the easiest place to ship a lie, so the constraints are explicit:

- **No invented traffic.** No looped filler, no synthetic "typical" call. 3 calls, 3
  packets. With no telemetry at all the stage stays empty and says why.
- **In flight is drawn as in flight.** An unfinished call pulses mid-wire instead of
  landing. A start row with no end row on a *finished* run is `unterminated`, drawn dim
  and given no duration — treating those as active invented 2,300s of boundary work
  inside a 300s run in the first draft, which is how the class of bug was found.
- **Absence stays visible.** Tools that were reachable but never called keep their lane
  and say so.
- **Compression is disclosed.** A long cycle replays its densest 30s window (marked on
  the concurrency chart) because compressing 300s into a 26s loop leaves ~3 packets on
  screen; the header names the slice, the run length and the effective speed, and
  `Whole run` is one click away. Sub-frame calls are padded to a visible 0.5s and the
  legend says so.
- **Thinning is per lane.** 86% of a cycle's calls are `adopt_animal`, so a global
  stride drew nothing on the quiet lanes. Per-lane quotas keep relative density while
  guaranteeing every lane with traffic shows traffic, and errors, in-flight and
  unterminated calls are never the ones dropped.
- **Motion is declarative.** One CSS animation per packet with its own `--t0`/`--dur`:
  no rAF loop, no canvas, nothing to keep spinning behind a hidden tab.
  `prefers-reduced-motion` falls back to a static strip chart at each call's real
  launch time, and the stage is rebuilt only at loop boundaries so a 2s poll cannot
  reset every animation mid-flight.

```
dashboard/mcp_wire.js          model, HTML and panel; DOM-free arithmetic, testable in JSC
dashboard/mcp_wire.css         lanes, packets, keyframes and the reduced-motion fallback
dashboard/test_mcp_wire.js     71 model/HTML contracts, including every honesty rule above
deploy/preview_mcp_wire.py     renders the live run headlessly: --check, --speed, --focus
```

### Coop Rush

Coop Rush began as satire of the run-46 plateau conclusion. The full ledger later
falsified that conclusion, so the tab is now a time capsule of why negative
findings need scope, freshness, and supersession—not a statement of current game
mechanics. Its simulation remains intentionally independent and fully testable.

Genre mechanics follow AdVenture Capitalist, whose numbers are documented:

| mechanic | how it works here |
| --- | --- |
| cost curve | `cost = base * coeff^owned`, coefficients 1.07 (coop) to 1.13 (market) |
| buy amounts | x1 / x10 / x100 / Max, priced as a geometric series; keys `1-4` |
| manual play | clicking a producer **starts** a cycle; the tick finishes it |
| managers | one-time coin cost, then the producer runs itself - what makes it idle |
| milestones | 25 / 50 / 100 / 200 / 300 / 400 owned alternate x2 produce and halved cycles |
| one-off upgrades | 14 permanent-for-the-run purchases, wiped by a rebuild |
| prestige | rebuild for heirloom hens: `floor(12 * sqrt(produce / 1M))`, +2% each |
| heirloom upgrades | 7 permanent perks, multiplicative with the per-heirloom bonus |
| offline | managed producers bank up to 4h of output while the page is closed |
| achievements | 18 measured-finding badges, +1% produce each and permanent across rebuilds |
| advisor | ranks affordable purchases by payback time; `B` buys it and `M` hires the next manager |
| feedback | floating output, egg bursts, milestone celebrations, stacked toasts and number pops |

Seven producers retain names from the original experiment. `Ceiling Lift I/II`
and `The Run-50 Lesson` now explicitly refer to the **false** ceiling belief and
the architecture that allowed it to persist.

The game lives in `game/` as real files rather than inside `monitor.py`'s HTML
string, because a simulation worth testing is not something you can test inside a
string literal. Both the dashboard tab and the standalone export compose from the
same files, so they cannot drift.

```
game/coop_rush.js        the simulation: DOM-free and network-free, so it is testable
game/coop_rush_ui.js     rendering and input; structure rebuilds only when it changes
game/coop_rush.css       styles, sharing the dashboard's palette variables
game/coop_rush.html      the markup fragment
game/test_mechanics.js   costs, milestones, achievements, advisor, saves, offline, balance
game/test_ui.js          drives the real click handler against a DOM stub
game/test_balance.js     6h auto-player simulation, printed as a table
deploy/test_game.sh      runs all of it: deploy/test_game.sh [--balance]
deploy/preview_game.py   static render of the panels, for looking at spacing
```

`deploy/preview_game.py` also produces a hostile-fixture render (0 owned, `8.42Qi`
costs, long text, all milestones done). It lifts the real card templates out of
the UI source and inlines the real stylesheet, so layout regressions can be
inspected even without running the server. The live dashboard and game were also
verified in the in-app browser at the responsive 704px viewport.

```
python3 deploy/preview_game.py --serve            # serve it on :8790
python3 deploy/preview_game.py --desktop --fit    # wide layout, scaled to a narrow panel
```

Looking at the result caught two defects beyond the reported one: the Buy/Manager
stack drifted against every card title by a different amount (centred column,
shorter than the card body - now stretched), and the cycle timer was dark-on-dark
whenever the bar was under half full.

There is no npm here and no browser automation, so the suites run in
JavaScriptCore via `osascript -l JavaScript`. That constraint is why the engine is
kept DOM-free. It has already earned its keep - the headless runs caught three
bugs that reading the code did not:

- **The game was unstartable.** With no coins and no producers there was no first
  move; the auto-player sat at zero purchases for 40 simulated minutes. You now
  start with one coop, exactly as AdCap hands you one lemonade stand.
- **The prestige button lied.** The sqrt handed out its first heirloom at ~7k
  produce while the button claimed it needed 1M, and a 1-heirloom reset is a trap.
  There is now a real 1M floor, and the locked button shows progress toward it.
- **A rebuild flatlined the farm.** Resetting dropped you to one hand-clicked coop
  with no income, and a 6h simulation showed it never recovering once the player
  stopped clicking. `Farmhand` (25 heirlooms, the cheapest perk) starts every
  rebuild with the coop manager already hired.

## Self-healing

Two agents run and repair each other, so no single stopped job silences the farm:

| agent | cadence | job |
| --- | --- | --- |
| `com.nickfigura.farmfriends` | 180s | the full cycle |
| `com.nickfigura.farmfriends.supervisor` | 60s | keep the schedule alive, remediate alerts |

`--supervise` does three things in order, and makes **no** farm calls unless the
loop has actually gone stale:

1. **Repair the schedule.** A dead scheduler makes every other signal
   meaningless. This is the failure that started it: the agent was simply not
   loaded, `--alerts` looked calm, and nothing ran for half an hour. Repairs are
   capped at `HEAL_SCHEDULER_MAX_REPAIRS_PER_HOUR` so a broken plist cannot
   become a restart loop.
2. **Recover a stale loop** by running one cycle inline, under the same lock the
   scheduled runs use, so it can never double-run the farm.
3. **Remediate alerts** via `farm/heal.py`, then acknowledge only what it fixed.

The healer's constraints are the interesting part:

- **Remedies are bounded and conservative.** Knobs can throttle growth
  (`rate_ceiling`, `adopt_cap`, `adopt_workers`) or do bounded extra work
  (`collect_passes`, `individual_feeds`). None can spend coins, adopt, sell,
  trade, or gift. The worst a bad healing decision can do is slow the farm down,
  and `rules.py` clamps every knob on read, so a corrupt store cannot escape the
  measured bounds.
- **One remedy per class per pass.** Several queued copies of one alert describe
  one condition. Stepping per alert once dropped a 5.0/s ceiling to 2.05/s.
- **Knobs relax one step per quiet pass** (`HEAL_ATTEMPT_RESET_RUNS`), so one bad
  afternoon cannot throttle the farm permanently.
- **Strategy is never healed.** Rank loss, threats, a rival passing us, a changed
  `tools/list`, a shrinking herd, or hunger at the production stop always
  escalate. So does any class whose remedy stops working: silent self-healing
  that is not working is worse than a page.

## Cost

Routine runs cost nothing, and `state/tokens.ndjson` proves it rather than
asserting it. Every cycle records an explicit zero; the only rows with a cost are
LLM wake-ups, booked at the moment `--alerts` prints a payload. Tokens are
estimated as `chars/4` plus the fixed context a wake-up carries, priced by
`LLM_INPUT_COST_PER_MTOK` / `LLM_OUTPUT_COST_PER_MTOK`. `--heal-status` and the
dashboard show cost per run, 24h, all-time, and the cost avoided by healing. The
**Token & cost history** tab then joins that ledger to the measured pre-Python
baseline: 150k-600k billed input tokens plus 59k thinking/output tokens per cycle.
It preserves the full low/high range instead of collapsing historical estimates
into fake precision, and its actual series moves automatically if a future alert
really wakes a model.

The detectors matter more than the prices here. Two of them were firing almost
every run for non-incidents, and fixing them removed most of the spend:

- **Throughput** now consults the backlog. A low units/chicken/min reading is
  only an incident if produce is piling up: runs 27-29 collected 62-760 units but
  ended with only 666-3,303 ready, so the herd was drained, not broken.
- **Transport retries** need a call-volume floor. One retry in a 35-call run is
  2.9% and looked like an incident; it also convinced the cycle that the previous
  run was unclean, which pinned the rate limiter at its 0.5/s floor for runs on
  end. `rules.transport_trouble` is now the single definition of "trouble" shared
  by the detector and the cycle's rate recovery.

**launchd runs `release/`, never the working tree.** Editing a file changes
nothing until `deploy/release.sh` publishes it. `--alerts` and `--review` print
the live revision and warn when the working tree has diverged.

## Growth is gated on fresh evidence, not inherited conclusions

The run-46 conclusion that production was capped per farm was **wrong**. It mixed
a collection-limited proxy, partial feeding, a short herd range, and multiple
operating regimes. Encoding that result froze the herd at 11,869 for 246 runs
while 3.2M coins accumulated and ultimately cost first place.

The full regime-filtered leaderboard ledger later showed the opposite: healthy
lifetime-produce rate remained approximately linear with herd size above 8,000
animals. Restoring growth scaled the farm past 100,000 animals and retook rank 1
at run 416. `mechanic.per_farm_output_plateau` remains in `state/claims.json` only
as a **superseded** claim; `mechanic.output_linear_with_herd` is the accepted
replacement.

`rules.growth_verdict()` remains a reversible safety brake for a future mechanics
change:

- It compares the current herd with a nearby 70-90% cohort, never ancient rows.
- The marginal threshold is 2%, not the old 10% linear-scaling demand.
- Thin evidence holds the previous verdict instead of oscillating.
- A 25-adoption maintenance floor prevents a challenged verdict from freezing
  the experiment that could falsify it.
- Continuous counterfactual replay shows where neighbouring thresholds would
  have changed decisions.

The run-50 species probe still supports chickens as the best observed engine:
alternative species contributed a negligible share of collected inventory. It
did **not** prove a per-farm score ceiling. Species economics and total-score
scaling are separate claims with separate estimators.

The crop probe is also scoped correctly now: wheat, corn, and pumpkin remained at
0% after 27 minutes in the run-50 server regime. That direct negative result is
accepted but overdue for bounded revalidation; it is not immortal folklore.

## What actually scores

Units collected is not the score, and twice it has been badly misleading:

- **Produce accrues as animals produce, not when we collect.** Run 25 gained
  41,207 lifetime produce while a collect call returned 572 units.
- **`collect_produce` returns "Nothing to collect right now ... or make sure your
  animals are fed" while any hunger is present.** The produce then appears in the
  barn during `feed_animals`. Runs 50 and 51 recorded `collected={}` and sold
  11,597 and 7,934 eggs in the same run.

So the authoritative health signal is the **leaderboard produce delta per minute**,
and `watch.py` judges that (`rules.produce_floor`, scaled per animal so an early
1,177-animal farm is not compared against an 11k-animal plateau). It catches the
one failure that can actually lose the game - the herd reaching hunger 70, where
production stops while collection and every other signal still look ordinary.

It requires **two consecutive low windows**. Produce arrives in bursts, so single
windows are lumpy: replaying all 56 runs of history, runs 40, 46 and 55 each read
105-246/min immediately before a 1,600-2,000/min window. One window would have
woken a model three times for nothing; two windows fires on none of them and still
catches a real stall one cycle (~4 min) later. A `PRODUCTION` alert has no remedy
and escalates by design - production stopping is not something to self-heal past.

Because production accrues without collection, the loop's job is to **keep the
herd producing, preserve feed runway, and convert surplus capital into safe herd
growth while marginal score remains positive.** Collection banks coins; adoption
raises score; feeding prevents the score engine from stopping at hunger 70.

## What the game actually does (measured, not assumed)

Current accepted mechanics, each with scope and a falsifier in `state/claims.json`:

- **Lifetime produce is the objective.** It accrues as animals produce, not when
  inventory is collected.
- **Healthy output scales with herd size.** The full current fit above 8,000
  animals is strongly positive; the historical plateau is superseded.
- **Uncollected produce accumulates.** Collection is paced by coin/feed need and
  transport cost, currently every ten cycles unless a safety override fires.
- **Feeding is the score input, not merely a cost.** Skipping feeds sharply
  reduced net output, so the cooldown is zero and any meaningful hunger can fire
  the feed path.
- **Whole-herd calls are gateway-limited and ambiguous.** A 504 may still apply
  server-side, so post-call state reconciliation—not blind throttling—settles it.
- **Worst-case hunger rises with herd size under the current bulk-feed path.** A
  measured safety ceiling preserves headroom below the production stop.
- **`farm_events` is omitted from routine play.** Its bounded window is dominated
  by adoption spam at current scale.

## Safety and failure behaviour

- Unrecognized server wording raises `ParseDrift` **before** any mutating call.
- A global rate limiter (`MAX_CALLS_PER_SECOND`) bounds total pressure across all
  6 adoption workers, halves itself on a real server error, and recovers 10% per
  clean run. "You can't afford that" is classified as a normal stop, not an error.
- A 260s wall-clock budget governs the cycle; a 285s `SIGALRM` makes the process
  stop before the next 300s slot. (A stuck run once held the lock for nine
  minutes and cost three cycles of compounding.)
- `flock` on `state/.lock` means an overlapping run skips instead of double-acting.
- Every mutating call is bracketed in `state/intents.ndjson`.
- `--health` (4x/hour) runs a full recovery cycle itself if the last run is more
  than 11 minutes old, so a dead scheduler heals without a model.

## Secret handling

The MCP endpoint is a secret. It is read from `$FARM_MCP_URL` or
`~/.config/farm/endpoint` (mode 0600), never passed as a CLI argument (keeping it
out of `ps` and shell history), and scrubbed from every exception and log line by
`Client.scrub()`.

## Changing strategy

Do not edit a constant and call that learning. Refresh evidence, inspect the
claim transition and counterfactual sweep, compile a candidate policy, run every
release gate, then explicitly promote and publish:

```bash
python3 run.py --knowledge-refresh
python3 run.py --research-audit
python3 run.py --sweep
python3 run.py --promote-policy
# full deploy test matrix
deploy/release.sh
```

Routine arithmetic still belongs in `rules.py` or `watch.py`, not a prompt. The
corollary is now enforced: **a decision that is never re-checked belongs in the
question ledger.** See `docs/epistemic-control-plane.md`.

## Version control

The repository is the change-management layer for autonomous edits. It is not
optional decoration: without it an unattended change had no diff, no reviewable
record, and no way to be undone except by re-pointing at a whole previous release
directory, which also takes down anything good that shipped after it.

```
python3 run.py --vcs-status     # main, autonomous commits, release tags
git log --oneline               # every change, including machine-authored ones
git show release/<revision>     # the exact code a published release was built from
```

What is versioned, and what is deliberately not:

| path | tracked | why |
|---|---|---|
| code, docs, tests, plists | yes | this is the reviewable surface |
| `state/` | no | 322MB, rewritten every 180s; already append-only and immutable |
| `releases/`, `release` | no | outputs of a commit; a checkout must not resurrect a stale tree |

Autonomous changes never touch `main` directly. Each authoring pass gets its own
`git worktree` on an `author/<order-id>` branch, so the pass is isolated by the
tool rather than by a hand-maintained list of directories to copy. `main` only
moves after the full gate matrix has passed inside that worktree, and
`merge_to_main` refuses if `main` moved during the pass -- the gates that just
passed were run against a tree that no longer reflects reality.

When the canary rejects a release it does two separate things. The symlink flip is
what makes the farm healthy again in seconds. The inverse commit is what stops the
rejected change being silently re-published by the next release, and leaves a record
that it was tried and rejected. The rejected commit stays in history; nothing is
erased.
