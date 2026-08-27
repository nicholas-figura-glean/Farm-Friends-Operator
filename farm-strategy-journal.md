# Farm Friends strategy journal

## 2026-08-20T20:57:40Z

- Rank: #1
- Lifetime produce: 961
- Gap to second (Moe): 713
- Observed latest collection: 115 eggs / 115 units over the latest observed interval (about 0.804 egg units per pre-existing chicken; new chickens had not produced yet)
- Rival lifetime produce / estimated output: Moe 248 / ~20 units per tick; John 45 / ~0; Aaron 11 / ~0; Guillermo G. 7 / ~0; Neill 6 / ~0
- Threat check: no rival near 50% of Nick's observed output; Moe is ~17% of the latest observed 115-unit interval.
- Animal counts: 162 chickens, 2 pigs, 1 beehive; 165 total
- Coins: 8
- Feed: 345
- Maximum hunger: 30 (new chickens at 0; no feeding warranted)
- Fields: one blooming wildflower plot; no food crops
- Per-kind units/coin/tick table (settled measured rates; no contrary live evidence): chicken 0.0895, beehive 0.0235, pig 0.0210. The live tools expose aggregate production and counts, not per-animal lifetime/age, so an exact fresh lifetime-normalized recomputation was not possible this run.
- Actions: collected 115 eggs; harvested (no crops); sold 115 eggs for 230 coins; bought 38 feed; adopted 19 chickens.
- Trades: 3 existing offers retained (5 feed for 10 coins to Guillermo G., Neill, and Aaron); no new offers, acceptances, declines, incoming trades, or gifts.
- Feed cost share: 38 / 230 = 16.5% of this run's sale revenue; purchase funded expansion reserve.
- Call-volume pressure: 19 paced adoption calls, all succeeded; no rate limiting or errors.
- Rule changes: none. Continue chicken-only compounding, feed reserve protection, 36-hunger threshold, and no food crops.

## 2026-08-20T21:45Z — migration to deterministic execution

- **Rule change (execution, not strategy):** the run loop moved from an LLM
  automation to `run.py --cycle`, executed by launchd every 5 minutes and
  aligned to :35s so collection follows the :23-25s tick. Strategy now lives in
  `farm/rules.py`; the settled facts (chicken-only, 36-hunger feeding, 2-feed
  per-animal reserve plus committed offer feed, no food crops, sell everything,
  three honest 5-feed-for-10-coin offers) are unchanged and encoded as code.
- **Rule change:** `MAX_ADOPTIONS_PER_RUN` raised 40 -> 120. The old cap existed
  only to stop an LLM run from overrunning the 5-minute window. Measured: 40
  paced adoptions take 44s including all other calls, so 120 stays inside the
  window with minutes of headroom. Run 2 hit the old cap with 147 coins idle,
  which was throttling compounding.
- **Cost:** the LLM loop cost roughly 150k-600k billed input tokens per 5-minute
  run (measured across 46 runs: 62k/run of raw tool text, 59k of thinking,
  re-sent over ~21 turns). Execution is now 0 tokens. Supervision is one hourly
  review of a ~600 character digest plus a 4x/hour one-line health check.
- **Observed at migration:** rank #1, lifetime produce 2778, 420 animals
  (417 chickens, 2 pigs, 1 beehive), feed 855 exactly at reserve, Moe second at
  478. Latest measured rate 0.753 units/chicken/tick, within the 0.50-1.30 band.
- **Escalation is now explicit:** rank loss, chicken rate outside 0.50-1.30,
  hunger >= 42, feed below reserve, adopt failures, tools/list changes, incoming
  trades, transport retries, production stalls, and any rival gaining >= 50% of
  our produce all raise an ANOMALY and set `needs_llm: true`.

## 2026-08-20T21:51:39Z - runs 1-4 (generated)

- Rank: #1, lifetime produce 3450 (+1217 this window)
- Animals: 545 (beehive 1, chicken 542, pig 2), +205 this window
- Output: 0.748 units/chicken/tick mean (min 0.732, max 0.760) over 3 producing runs
- Economy: 1936 coins revenue, 330 spent on feed (17.0%), 165 chickens adopted of 0 planned
- Husbandry: peak hunger 24 against threshold 36 (stop 70); feed 1105 vs reserve 1105
- Throughput: 203 calls, 44s mean / 65s max per run, pacing None
- Rivals:
  - Moe: 610 lifetime (+228 this window, 18.7% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 3 lifetime (+2 this window, 0.2% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: tick gap 10 min since last run - possible downtime
- Nothing anomalous; rules unchanged.
- Active rules: engine chicken, feed at 36, reserve 2/animal, budget 210s, pacing floor 0.10s, verify every 6 runs, food crops banned

## 2026-08-20T22:30Z — corrections from live operation (runs 1-8)

Four things the LLM-era model of this game had wrong, all found by running the
deterministic loop and reading the raw responses:

1. **Production is not a global 5-minute tick.** The event feed shows per-animal
   timers, and 980 units appeared 2.5 minutes after a collection that returned
   nothing. All tick-aligned scheduling and all "units per chicken per tick"
   arithmetic was therefore measuring noise. Throughput is now measured as
   units/chicken/**minute** over the real interval since the last collection,
   and the loop no longer tries to align to anything. `--align` and the
   tick-drift detector are gone.
2. **Uncollected produce is not lost, it accumulates.** So collecting more often
   is purely a reinvestment-speed play. Cadence moved from 300s to 180s, and the
   starvation backstop no longer collects at all (it was splitting intervals and
   making throughput look like it had collapsed).
3. **`feed_animals(animal_id='all')` does not feed everyone.** After a bulk feed,
   1176 chickens sat at hunger 0 while the beehive, both pigs, and one chicken
   sat at 48. Those stragglers would have drifted to the hunger-70 production
   stop invisibly. The loop now re-reads after feeding and feeds leftovers
   individually by id (`MAX_INDIVIDUAL_FEEDS = 25`).
4. **`farm_events` is not worth a call.** With hundreds of adoptions per run the
   50-event window is entirely adoption spam, so it told us nothing about
   production. That call was removed; the stall detector now watches for
   consecutive empty collections instead, which is what actually matters.

Operational changes in the same window:

- **Global rate limiter (6 calls/s ceiling, shared across all threads).** Growth
  made call volume the binding constraint: run 8 issued 548 calls. Adoption runs
  on 6 workers, but total pressure is capped in one place and halves itself on
  any server error, recovering 10% per clean run.
- **Wall-clock budget (150s) instead of an adoption cap**, plus a 240s hard
  self-timeout. A stuck run held the lock for nine minutes and cost three cycles
  of compounding; a process now kills itself before its slot expires.
- **Rate limiter bug worth remembering:** the first version re-checked the clock
  after sleeping and advanced the reservation pointer each iteration, so waiters
  pushed each other into the future and throughput decayed to nothing. Two
  timing assertions in `--self-test` now cover it.
- **Atomic versioned releases.** launchd runs `release/`, a symlink flipped with
  a single rename. Two incidents forced this: it once executed a half-edited
  working tree, and a naive swap deleted the tree out from under a running cycle.
- **Journal entries are generated in Python** from history.ndjson, so the LLM no
  longer writes prose. The hourly automation now only runs `--alerts`, which
  prints one line when nothing is wrong.

State at the end of this window: rank #1, lifetime produce 13,290, 2102 animals
(2099 chickens, 2 pigs, 1 beehive), Moe second at 2451. Measured throughput
0.285-0.291 units/chicken/min, stable across runs. Feed cost is running ~17-20%
of revenue. Adoption is budget-limited, which is the expected steady state:
coins roll forward and are spent next run.

## 2026-08-20T23:10Z — throughput ceiling found by measurement (runs 11-15)

- **The server queues instead of scaling.** At 6 adoption workers, mean adopt
  latency was 0.684s and effective throughput 4.19 calls/s. At 8 workers latency
  rose to 1.637s for 4.67 calls/s — essentially the same throughput at 2.4x the
  per-call cost. `MAX_CALLS_PER_SECOND` is therefore set to 5.0 with 6 workers:
  the measured ceiling, not an aspiration. The latency-based courtesy ease-off
  detected this by itself and had already backed the rate down to 4.8/s.
- **Feed frequency, not feed threshold, was the cost driver.** Bulk feeding costs
  ~1.1-1.5 feed per fed animal regardless of that animal's hunger, so feeding
  every run spent 40% of revenue (3799 of 9568 coins). With the cooldown
  (`FEED_COOLDOWN_RUNS = 2`, urgent override at hunger 60) feed share fell to
  ~29% and then ~13% on runs that skip feeding.
- **Adoption now precedes the feed top-up.** Buying feed first pre-committed
  coins for a planned adoption count that the wall-clock budget then cut short,
  which could leave the reserve under target. Feed is now sized to the number
  actually adopted, and a property test asserts that partial adoption can never
  worsen the reserve shortfall.
- **Parallel adoption raced the coin balance.** Six workers each passed the
  "enough adopted?" check on the last affordable chicken, so five extra calls
  went out and came back "That costs 10 coins and you have 8". Fixed by claiming
  a slot before calling; that message is now classified as a normal stop, not an
  error, so it no longer halves the rate limiter or raises five alerts.
- **Alert hygiene:** a single transport retry among 700 calls and a
  self-recovering rate backoff were both firing as anomalies. Both now require
  real evidence (>=5 retries or >2% of calls; backoff only counts with failures
  in the same run). False alerts cost tokens, which is the one thing this system
  exists to avoid.

State: rank #1, lifetime produce 48,049 against Moe's 8,468. 6200 animals.
Adoption is budget-limited at ~700/run with coins accumulating, which is the
expected equilibrium — leftover coins roll forward and chickens remain the best
engine per coin, so there is nothing better to spend them on.

## 2026-08-20T23:40Z — the feed reversal, cadence, and where the ceiling now is

**The cooldown was wrong and measurement caught it within three runs.** Feeding
less often is cheaper per run and much worse overall, because production scales
with how recently the herd was fed:

| run | fed | u/chicken/min | revenue | feed cost | net per chicken |
|-----|-----|---------------|---------|-----------|-----------------|
| 14  | yes | 0.294         | 15,956  | 5,171     | 1.96 |
| 15  | no  | 0.177         | 10,842  | 1,404     | 1.52 |
| 16  | no  | 0.056         | 3,886   | 1,216     | 0.39 |
| 17  | yes | 0.222         | 38,746  | 11,672    | 3.75 |

Two skipped feeds cut net revenue per chicken by 80%; restoring feed-every-run
recovered it tenfold in one cycle. `FEED_COOLDOWN_RUNS` is 0 and
`FEED_AT_HUNGER` is 6, i.e. feed whenever anything is hungry. Feed is an input to
maximise, not a cost to minimise.

**Phase timing changed the cadence decision.** Per-phase wall clock showed ~65-85s
of every run is inherent server work that no client-side parallelism can shrink:
collect 29-49s and bulk feed 32-36s at 7-8k animals, against list_farm at only
1.3-2.7s. At a 180s cadence that overhead left just 82s for adoption (406
chickens) while 33k coins sat idle. At 300s the same overhead leaves ~173s
(858 chickens) and feeding still happens every ~5.5 minutes, the interval that
measures 0.29-0.30 u/chicken/min. Adoption throughput per hour improved ~2x.

**Throughput must be measured collect-to-collect.** The rate was being computed
against run-end timestamps, so each run's own duration (up to 262s) leaked into
the interval and under-reported production. That produced one false alert at
0.098. With `collect_ts` recorded and used, the same conditions read 0.295.

**Where the ceiling is now.** Adoption is budget-limited every run with coins
accumulating (68,815 idle at run 19). The next real lever is overlapping the
adoption phase with collect/feed/sell: those two calls block for 65-85s while the
rate limiter sits idle, so adopting concurrently against a shared, lock-protected
coin budget would reclaim roughly a third of every window. It is deliberately not
built yet: the system is stable, dominant, and costs nothing to run, so the
risk/benefit favoured leaving a working loop alone.

State: rank #1, lifetime produce 98,503 against Moe's 16,970 — a 5.8x lead.
8,754 animals, feed exactly at reserve, zero pending alerts, and the last several
runs required no LLM involvement at all.

## 2026-08-20T23:48:53Z - runs 1-20 (generated)

- Rank: #1, lifetime produce 114311 (+112078 this window)
- Animals: 9332 (beehive 1, chicken 9329, pig 2), +8992 this window
- Output: 0.245 units/chicken/min mean (min 0.056, max 0.398) over 17 measurable runs
- Economy: 247868 coins revenue, 65626 spent on feed (26.5%), 8571 chickens adopted of 17765 planned
- Husbandry: peak hunger 42 against threshold 6 (stop 70); feed 18679 vs reserve 18679
- Throughput: 8774 calls, 133s mean / 263s max per run, rate limit 2.56/s
- Rivals:
  - Moe: 19512 lifetime (+19130 this window, 17.1% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+30 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: tick gap 10 min since last run - possible downtime; chicken rate 0.103 outside band 0.50-1.30; hunger 42 at/above alarm 42 (production stops at 70); 5 adopt call failures; call rate backed off to 2.00/s - server pushing back
- Adoption hit the 260s wall-clock budget in 12/20 runs (8571 of 17765 planned chickens bought); coins roll forward, so this throttles compounding. Levers are MAX_CALLS_PER_SECOND and ADOPT_WORKERS.
- Peak hunger 42 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T03:00:16Z - runs 21-40 (generated)

- Rank: #1, lifetime produce 379210 (+251318 this window)
- Animals: 11344 (beehive 1, chicken 11341, pig 2), +1577 this window
- Output: 0.083 units/chicken/min mean (min 0.000, max 0.155) over 19 measurable runs
- Economy: 557116 coins revenue, 119659 spent on feed (21.5%), 2012 chickens adopted of 16300 planned
- Husbandry: peak hunger 18 against threshold 6 (stop 70); feed 22703 vs reserve 22703
- Throughput: 2203 calls, 146s mean / 263s max per run, rate limit 4.0/s
- Rivals:
  - Moe: 45149 lifetime (+23428 this window, 9.3% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: call rate at 2.05/s with failures this run - server pushing back; throughput 0.000 units/chicken/min outside band 0.10-1.00 over 9.4 min; throughput 0.044 units/chicken/min outside band 0.10-1.00 over 15.6 min; throughput 0.002 units/chicken/min outside band 0.10-1.00 over 30.2 min; throughput 0.007 units/chicken/min outside band 0.10-1.00 over 30.1 min
- Adoption hit the 150s wall-clock budget in 8/20 runs (2012 of 16300 planned chickens bought); coins roll forward, so this throttles compounding. Levers are MAX_CALLS_PER_SECOND and ADOPT_WORKERS.
- 12 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Mean throughput 0.083 units/chicken/min sits outside the 0.10-1.00 band; the band or the husbandry assumption needs revisiting.
- Peak hunger 18 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T04:15Z — the ceiling is per farm, not per kind (goal audit, runs 48-56)

Audited the loop against the three standing goals. Two passed unchanged; one had
a real gap, and closing it meant testing an assumption rather than tuning code.

**Goal 1 (win, no mistakes): passing.** Rank #1 with 419,415 produce against
Moe's 45,240 (9.3x). 74 self-test checks green, zero pending alerts, zero LLM
escalations, $0.00 all-time cost, working tree in sync with the live release.

**Goal 3 (headless, self-healing): passing.** Both agents loaded, last exit 0.
Runs 31-33 backed the call ceiling 5.0 -> 2.05/s on their own and every knob has
since relaxed back to default with no intervention. During this audit the
supervisor also healed a feed-reserve shortfall caused by the experiment below
(adopt cap 25 -> 12) without being asked.

**Goal 2 (maximize total produce): was NOT passing.** Output had been flat at
~1,550/min for 20+ runs, adoption gated to 0, and 570,172 coins were sitting idle
and growing ~8,145/run with nothing to spend them on. The gate was correct but
nothing had replaced it, and the reason it was correct rested on an untested
inference.

**The open question.** The per-farm ceiling was measured by varying chicken count
only. A per-KIND ceiling fits the same data: while the egg rate per chicken fell
0.25 -> 0.134 units/min, the 2 pigs and 1 beehive held 0.25-0.29 units/animal/min
throughout - undiluted, which is exactly what a per-kind cap looks like. If that
were true, total produce was capped near a fifth of the farm's potential and the
idle coins were the fix.

**Test (`experiments/species_probe.py`, run 50).** 100 each of pig, sheep, cow and
beehive in one batch - one interval measures four rates independently because each
kind produces a distinct item. 9,517 coins, 1.7% of idle coins, run under the
cycle's own flock in chunks so it could never double-act or hold the lock across a
slot. Result over the next six runs (25 min): sheep 0 wool ever, cow 0 milk ever,
pig 1-2 truffles/run (same as with 2 pigs), beehive 1-2 honey/run (same as with 1).
Total output unchanged at ~1,550/min, new animals at hunger 0 with nothing ready,
so not a warm-up artifact.

- **Rule change:** `ADOPTABLE_KINDS = ("chicken",)`. The cap is per farm and
  already saturated; an animal of any kind added past saturation produces nothing
  and costs feed forever. The growth gate stands, now on experiment.
- **Rule change:** crops stay banned, for a better reason. One wheat, one corn and
  one pumpkin plot were planted in the same pass; 27 minutes later all three still
  read "0% grown, about 15/20/30 min left". The timers never advance. `plant()`
  creates unlimited plots, so this would have scaled badly.
- **Cost of being wrong:** ~9,500 coins and ~500 feed/run for 400 permanent
  non-producers. Against 15k/run revenue that is noise, and the question is closed.

**Two measurement bugs found while reading the raw responses.**

1. `collect_produce` answers "Nothing to collect right now ... or make sure your
   animals are fed" whenever any hunger is present, and the produce then banks
   during `feed_animals`. Runs 50 and 51 recorded `collected={}` and then sold
   11,597 and 7,934 eggs **in the same run**. So `units_collected` is not
   production, and the loop collects before it feeds.
2. Lifetime produce accrues as animals produce, not when we collect (run 25:
   +41,207 produce, 572 units collected). Combined with (1), every
   collection-derived metric understates output.

- **New detector:** score rate = leaderboard produce delta per minute, floored at
  `PRODUCE_FLOOR_PER_ANIMAL` (0.05/animal/min, capped at 600/min) so a small farm
  is never judged against an 11k-animal plateau. It requires **two consecutive**
  low windows: replaying all 56 historical runs, runs 40, 46 and 55 each read
  105-246/min immediately before a 1,600-2,000/min window, so a one-window rule
  would have woken a model three times for nothing. Two windows fires on none of
  them, and a simulated hunger-70 stall is still caught one cycle later.
  `PRODUCTION` has no remedy and escalates by design.
- **Existing detectors softened, not loosened:** a below-band units/chicken/min
  reading and a zero-collect streak are now soft notes when the score rate proves
  production is fine - which is precisely the run-50 signature. Replay over 56
  runs: zero new alerts, so this costs nothing in tokens.
- `--self-test` is 74 -> 80 checks, covering the kind ban, the scaled floor, the
  one-window/two-window boundary, and the banked-produce case.

**What this means for the strategy.** Because production accrues whether or not we
call anything, and the herd is at the farm ceiling, collecting, selling and
adopting can no longer raise the score. The only remaining lever is feed uptime:
keep the herd fed and never stall long enough for hunger to reach 70. That is now
what the loop is instrumented to detect, and it is the one thing that could still
lose a 9x lead.

## 2026-08-21T04:20:36Z - runs 41-60 (generated)

- Rank: #1, lifetime produce 488951 (+102110 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +500 this window
- Output: 0.126 units/chicken/min mean (min 0.070, max 0.160) over 17 measurable runs
- Economy: 235072 coins revenue, 83443 spent on feed (35.5%), 125 chickens adopted of 125 planned
- Husbandry: peak hunger 12 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 308 calls, 63s mean / 111s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 50089 lifetime (+4940 this window, 4.8% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: feed 21086 below reserve target 23753
- 5 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 12 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T05:20Z — Coop Rush becomes an idle game (dashboard, no farm impact)

Rebuilt the game tab as an idle/incremental game (AdVenture Capitalist shape)
rather than the 60-second arcade round it was. Researched the genre's numbers
first: per-business cost curves of `base * coeff^owned` with coefficients in the
1.07-1.15 band, cycle times halved at 25/50/100/200/300/400 owned, managers as the
mechanic that actually makes a game idle, and a prestige currency drawn from a
sqrt of lifetime earnings that stacks multiplicatively with prestige-bought
upgrades.

Shipped: 7 producers, x1/x10/x100/Max buying, per-producer managers, 6 milestones
each, 14 one-off coin upgrades, 7 permanent heirloom upgrades, a rebuild/prestige
loop with a real 1M floor, and 4h offline banking. All named after the real farm's
findings - the four species that produced nothing, the crop timers that never
advanced, the ceiling that turned out to be real.

**Architecture change.** The game moved out of `monitor.py`'s HTML string into
`game/` as real .js/.css/.html files. The old canvas game was ~200 lines and fine
inside a string; a simulation with a cost curve and a prestige economy is not
something you can test inside a string literal. `monitor.py` composes the tab from
those files at import and `deploy/export_game.py` composes the standalone build
from the same ones, so the two cannot drift.

**Testing, and what it caught.** There is no npm here and no browser automation, so
the engine is deliberately DOM-free and network-free and the suites run in
JavaScriptCore via `osascript -l JavaScript`: 38 mechanics checks, a UI smoke test
that drives the real delegated click handler against a DOM stub, and a 6-hour
auto-player balance simulation. `deploy/test_game.sh` runs all of it.

Three bugs came from running it rather than reading it:

- **The game was unstartable.** No coins, no producers, so there was no first move.
  The auto-player made zero purchases in 40 simulated minutes. Fixed by starting
  with one coop, as AdCap starts you with one lemonade stand.
- **The prestige button lied.** `floor(12*sqrt(lifetime/1e6))` awards its first
  heirloom at ~7k produce while the button claimed 1M was required, and a
  1-heirloom reset is a trap. Added a real `PRESTIGE_MIN_UNITS` floor and made the
  locked button show progress toward it.
- **A rebuild flatlined the farm.** Prestige returned you to one hand-clicked coop
  with no coins; a 6h simulation showed produce stuck at 0 for the entire run once
  clicking stopped. `Farmhand` (25 heirlooms, now the cheapest perk) starts every
  rebuild with the coop manager hired. Post-fix the loop compounds properly: 26
  rebuilds in 6h, all 7 producers in use, rebuild intervals lengthening from 5min
  to 355min as they should.

A fourth was found by structural checks rather than tests: installing the markup
placeholder deleted the `<div id="tab-game">` panel wrapper, which would have
broken tab switching on the dashboard. Caught by asserting all four tab panels
exist in the composed page.

Also fixed while here: the number formatter rendered `Infinity` as `0`, which in an
idle game reads as a broken save rather than a very good one.

No farm code touched. 80 self-test checks green, release unchanged, run 74 at rank
#1 with 563,342 produce.

## 2026-08-21T05:38:58Z - runs 61-80 (generated)

- Rank: #1, lifetime produce 597358 (+103578 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.150 units/chicken/min mean (min 0.133, max 0.164) over 20 measurable runs
- Economy: 240790 coins revenue, 82745 spent on feed (34.4%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 12 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 177 calls, 55s mean / 78s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 53666 lifetime (+2939 this window, 2.8% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: none
- Peak hunger 12 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T07:02:09Z - runs 81-100 (generated)

- Rank: #1, lifetime produce 722495 (+118212 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.115 units/chicken/min mean (min 0.007, max 0.157) over 18 measurable runs
- Economy: 261068 coins revenue, 101617 spent on feed (38.9%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 18 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 186 calls, 69s mean / 130s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 54568 lifetime (+813 this window, 0.7% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: none
- Peak hunger 18 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T08:27:21Z - runs 101-120 (generated)

- Rank: #1, lifetime produce 844566 (+114346 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.131 units/chicken/min mean (min 0.004, max 0.219) over 20 measurable runs
- Economy: 278294 coins revenue, 93822 spent on feed (33.7%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 24 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 187 calls, 66s mean / 84s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 55004 lifetime (+436 this window, 0.4% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: none
- Peak hunger 24 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T09:48:38Z - runs 121-140 (generated)

- Rank: #1, lifetime produce 960427 (+114325 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.143 units/chicken/min mean (min 0.116, max 0.163) over 18 measurable runs
- Economy: 241976 coins revenue, 91125 spent on feed (37.7%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 18 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 186 calls, 64s mean / 91s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 55222 lifetime (+218 this window, 0.2% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: none
- Peak hunger 18 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T11:09:32Z - runs 141-160 (generated)

- Rank: #1, lifetime produce 1072853 (+106092 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.131 units/chicken/min mean (min 0.074, max 0.193) over 19 measurable runs
- Economy: 240954 coins revenue, 89903 spent on feed (37.3%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 12 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 182 calls, 62s mean / 84s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 55292 lifetime (+70 this window, 0.1% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: none
- Peak hunger 12 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T12:30:26Z - runs 161-180 (generated)

- Rank: #1, lifetime produce 1185791 (+105666 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.135 units/chicken/min mean (min 0.056, max 0.189) over 19 measurable runs
- Economy: 242604 coins revenue, 91361 spent on feed (37.7%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 24 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 187 calls, 62s mean / 81s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 55879 lifetime (+582 this window, 0.6% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: none
- Peak hunger 24 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

### 2026-08-21T05:35Z — Coop Rush layout fixes (and a way to actually see the layout)

Reported: the producer card's milestone label was sitting on top of the flavour
text. It was `position:absolute; top:-16px` on a 4px bar, so it had nowhere to go
but over the row above. Bar and label now share one flex row.

The real problem was that nothing here could render the game: it builds its DOM in
JavaScript and the only available browser can't run JavaScript, so the layout had
never actually been looked at. `deploy/preview_game.py` fixes that - it lifts the
real card templates out of `game/coop_rush_ui.js`, fills them with deliberately
hostile fixtures (0 owned, 8.42Qi costs, long flavour text, all-milestones-done),
inlines the real stylesheet and writes a static page. `--desktop` strips the
breakpoint so the wide layout can be inspected in a narrow panel; `--fit` scales it.

Looking at it immediately turned up two more defects the report hadn't mentioned:

- The **Buy/Manager stack drifted against every card title**, by a different amount
  per card, because the grid was `align-items:center` and the button column was
  shorter than the body. Now `stretch`, with the buttons flexing to fill, so the
  column lines up top and bottom on every card.
- The **cycle timer was unreadable below ~50% progress**: dark text chosen for
  contrast against the green fill, sitting on the dark unfilled track. Now light
  text with a shadow, legible over both.

Also widened the heirloom upgrade columns (300px -> 330px) so descriptions stop
wrapping into their own cost, and stopped `5.00K 🐤` breaking across lines.

The preview script found a bug in itself, too: it prepended an opening tag that its
own extractor already returned, producing `class="cr-prod<div class="cr-prod` -
which silently disabled the grid and made every card look stacked. It now guards
against that shape.

Verified: game suites pass, standalone export carries the fixes, dashboard reserving
on :8766 serves them, 80 farm self-test checks green, farm code untouched.

## 2026-08-21T14:00:43Z - runs 181-200 (generated)

- Rank: #1, lifetime produce 1310465 (+114640 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.112 units/chicken/min mean (min 0.007, max 0.178) over 19 measurable runs
- Economy: 257152 coins revenue, 91656 spent on feed (35.6%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 12 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 188 calls, 81s mean / 163s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 56096 lifetime (+217 this window, 0.2% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: feed 21075 below reserve target 23753
- Peak hunger 12 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T15:28:20Z - runs 201-220 (generated)

- Rank: #1, lifetime produce 1433558 (+116612 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.119 units/chicken/min mean (min 0.002, max 0.176) over 19 measurable runs
- Economy: 267888 coins revenue, 92532 spent on feed (34.5%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 18 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 183 calls, 69s mean / 87s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 56172 lifetime (+76 this window, 0.1% of our gain)
  - John: 45 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 31 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: none
- Peak hunger 18 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T16:49:32Z - runs 221-240 (generated)

- Rank: #1, lifetime produce 1548885 (+108244 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.144 units/chicken/min mean (min 0.086, max 0.209) over 19 measurable runs
- Economy: 277798 coins revenue, 91863 spent on feed (33.1%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 18 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 180 calls, 63s mean / 84s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 70002 lifetime (+13716 this window, 12.7% of our gain)
  - John: 56 lifetime (+11 this window, 0.0% of our gain)
  - Jason: 37 lifetime (+6 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: THREAT: Moe gained 122 vs our 238 (>= 50%)
- Peak hunger 18 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T18:08:43Z - runs 241-260 (generated)

- Rank: #1, lifetime produce 1658600 (+103699 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.148 units/chicken/min mean (min 0.128, max 0.168) over 20 measurable runs
- Economy: 225365 coins revenue, 87190 spent on feed (38.7%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 12 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 185 calls, 57s mean / 76s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 75362 lifetime (+4341 this window, 4.2% of our gain)
  - John: 68 lifetime (+11 this window, 0.0% of our gain)
  - Jason: 41 lifetime (+1 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: none
- Peak hunger 12 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-21T20:24:59Z - runs 261-280 (generated)

- Rank: #1, lifetime produce 1902424 (+236445 this window)
- Animals: 11869 (beehive 101, chicken 11466, cow 100, pig 102, sheep 100), +0 this window
- Output: 0.092 units/chicken/min mean (min 0.022, max 0.158) over 9 measurable runs
- Economy: 545391 coins revenue, 118426 spent on feed (21.7%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 12 against threshold 6 (stop 70); feed 23753 vs reserve 23753
- Throughput: 222 calls, 101s mean / 147s max per run, rate limit 5.0/s
- Rivals:
  - Moe: 109278 lifetime (+33700 this window, 14.3% of our gain)
  - John: 118 lifetime (+50 this window, 0.0% of our gain)
  - Jason: 64 lifetime (+23 this window, 0.0% of our gain)
  - Aaron: 11 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+6 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: feed 16977 below reserve target 23753; feed 17401 below reserve target 23753; feed 17328 below reserve target 23753; feed 17430 below reserve target 23753
- Mean throughput 0.092 units/chicken/min sits outside the 0.10-1.00 band; the band or the husbandry assumption needs revisiting.
- Peak hunger 12 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 2/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-23T05:16:02Z - runs 281-300 (generated)

- Rank: #2, lifetime produce 3306989 (+1395187 this window)
- Animals: 14468 (beehive 101, chicken 14065, cow 100, pig 102, sheep 100), +2599 this window
- Output: 0.034 units/chicken/min mean (min 0.000, max 0.065) over 12 measurable runs
- Economy: 1315124 coins revenue, 89855 spent on feed (6.8%), 599 chickens adopted of 1615 planned
- Husbandry: peak hunger 100 against threshold 6 (stop 70); feed 558540 vs reserve 434055
- Throughput: 848 calls, 188s mean / 1014s max per run, rate limit 3.2/s
- Rivals:
  - John: 5526755 lifetime (+5526637 this window, 396.1% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 11548 lifetime (+11542 this window, 0.8% of our gain)
  - Jason: 113 lifetime (+49 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+9 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: John exceeded 50% of our gain
- Alerts this window: feed 17513 below reserve target 23753; RANK LOST: now #2; no produce collected in 4 consecutive runs - production may have stopped; hunger 100 at/above alarm 66 (production stops at 70); could not feed chicken #7 at hunger 100: feed_animals returned isError: 🚫 You're out of feed! Use buy_feed.
- Adoption hit the 150s wall-clock budget in 4/20 runs (599 of 1615 planned chickens bought); coins roll forward, so this throttles compounding. Levers are MAX_CALLS_PER_SECOND and ADOPT_WORKERS.
- 1 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Mean throughput 0.034 units/chicken/min sits outside the 0.10-1.00 band; the band or the husbandry assumption needs revisiting.
- Peak hunger 100 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 150s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-23T09:35:47Z - runs 301-320 (generated)

- Rank: #2, lifetime produce 4196402 (+865329 this window)
- Animals: 24500 (beehive 101, chicken 24097, cow 100, pig 102, sheep 100), +8753 this window
- Output: 0.013 units/chicken/min mean (min 0.004, max 0.020) over 9 measurable runs
- Economy: 654942 coins revenue, 149403 spent on feed (22.8%), 947 chickens adopted of 2525 planned
- Husbandry: peak hunger 6 against threshold 6 (stop 70); feed 735015 vs reserve 735015
- Throughput: 1223 calls, 136s mean / 246s max per run, rate limit 1.05/s
- Rivals:
  - John: 6753274 lifetime (+1159664 this window, 134.0% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 17217 lifetime (+5461 this window, 0.6% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: John exceeded 50% of our gain
- Alerts this window: RANK LOST: now #2; THREAT: John gained 66855 vs our 24084 (>= 50%); John has passed us on lifetime produce; RANK LOST: now #2; THREAT: John gained 33820 vs our 12819 (>= 50%)
- Adoption hit the 260s wall-clock budget in 5/20 runs (947 of 2525 planned chickens bought); coins roll forward, so this throttles compounding. Levers are MAX_CALLS_PER_SECOND and ADOPT_WORKERS.
- 15 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Mean throughput 0.013 units/chicken/min sits outside the 0.10-1.00 band; the band or the husbandry assumption needs revisiting.
- Peak hunger 6 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-23T13:50:23Z - runs 321-340 (generated)

- Rank: #2, lifetime produce 5224323 (+949233 this window)
- Animals: 28411 (beehive 101, chicken 28008, cow 100, pig 102, sheep 100), +3861 this window
- Output: 0.017 units/chicken/min mean (min 0.007, max 0.040) over 6 measurable runs
- Economy: 384030 coins revenue, 171190 spent on feed (44.6%), 1700 chickens adopted of 1700 planned
- Husbandry: peak hunger 18 against threshold 6 (stop 70); feed 851775 vs reserve 852345
- Throughput: 1924 calls, 137s mean / 255s max per run, rate limit 3.2/s
- Rivals:
  - John: 7863453 lifetime (+962040 this window, 101.3% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18673 lifetime (+1092 this window, 0.1% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: John exceeded 50% of our gain
- Alerts this window: RANK LOST: now #2; THREAT: John gained 148139 vs our 78688 (>= 50%); John has passed us on lifetime produce; RANK LOST: now #2; THREAT: John gained 31801 vs our 19678 (>= 50%)
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Mean throughput 0.017 units/chicken/min sits outside the 0.10-1.00 band; the band or the husbandry assumption needs revisiting.
- Peak hunger 18 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-23T16:52:07Z - runs 341-360 (generated)

- Rank: #2, lifetime produce 6401732 (+1131764 this window)
- Animals: 49135 (beehive 101, chicken 48732, cow 100, pig 102, sheep 100), +19569 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 604052 spent on feed (0.0%), 7159 chickens adopted of 8000 planned
- Husbandry: peak hunger 30 against threshold 6 (stop 70); feed 1474065 vs reserve 1474065
- Throughput: 7345 calls, 234s mean / 273s max per run, rate limit 2.56/s
- Rivals:
  - John: 8672977 lifetime (+792682 this window, 70.0% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18673 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: John exceeded 50% of our gain
- Alerts this window: RANK LOST: now #2; feed 886215 below reserve target 886995; bulk feed transport failure (continuing): transport failure after 3 tries: <HTTPError 504: 'Gateway Timeout'>; feed reserve still short after reconciliation: 886215/886995; John has passed us on lifetime produce
- Adoption hit the 260s wall-clock budget in 4/20 runs (7159 of 8000 planned chickens bought); coins roll forward, so this throttles compounding. Levers are MAX_CALLS_PER_SECOND and ADOPT_WORKERS.
- 16 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 30 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-23T19:59:05Z - runs 361-380 (generated)

- Rank: #2, lifetime produce 8116234 (+1635025 this window)
- Animals: 64599 (beehive 101, chicken 64196, cow 100, pig 102, sheep 100), +14565 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 348240 spent on feed (0.0%), 3389 chickens adopted of 7800 planned
- Husbandry: peak hunger 36 against threshold 6 (stop 70); feed 2222305 vs reserve 1937985
- Throughput: 3571 calls, 260s mean / 281s max per run, rate limit 5.0/s
- Rivals:
  - John: 9648916 lifetime (+969122 this window, 59.3% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18673 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: John exceeded 50% of our gain
- Alerts this window: RANK LOST: now #2; 6 transport retries across 130 calls; call rate pinned at 2.50/s with sustained failures - server pushing back; John has passed us on lifetime produce; RANK LOST: now #2
- Adoption hit the 260s wall-clock budget in 17/20 runs (3389 of 7800 planned chickens bought); coins roll forward, so this throttles compounding. Levers are MAX_CALLS_PER_SECOND and ADOPT_WORKERS.
- 3 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 36 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-23T22:52:44Z - runs 381-400 (generated)

- Rank: #2, lifetime produce 10265216 (+2045462 this window)
- Animals: 85387 (beehive 101, chicken 84984, cow 100, pig 102, sheep 100), +19572 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 339320 spent on feed (0.0%), 7763 chickens adopted of 8000 planned
- Husbandry: peak hunger 42 against threshold 6 (stop 70); feed 2561625 vs reserve 2561625
- Throughput: 7909 calls, 221s mean / 266s max per run, rate limit 5.0/s
- Rivals:
  - John: 11154930 lifetime (+1421379 this window, 69.5% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18673 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: John exceeded 50% of our gain
- Alerts this window: RANK LOST: now #2; THREAT: John gained 84635 vs our 103520 (>= 50%); John has passed us on lifetime produce; feed reserve still short after reconciliation: 2346975/2347605; feed reserve still short after reconciliation: 2374185/2374905
- Adoption hit the 260s wall-clock budget in 1/20 runs (7763 of 8000 planned chickens bought); coins roll forward, so this throttles compounding. Levers are MAX_CALLS_PER_SECOND and ADOPT_WORKERS.
- 19 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 42 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T01:49:15Z - runs 401-420 (generated)

- Rank: #1, lifetime produce 12940528 (+2538341 this window)
- Animals: 106604 (beehive 101, chicken 106201, cow 100, pig 102, sheep 100), +20162 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 636510 spent on feed (0.0%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 54 against threshold 6 (stop 70); feed 3198135 vs reserve 3198135
- Throughput: 8186 calls, 229s mean / 247s max per run, rate limit 5.0/s
- Rivals:
  - John: 12071476 lifetime (+812466 this window, 32.0% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18673 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: feed reserve still short after reconciliation: 2620245/2621505; feed reserve still short after reconciliation: 2648835/2650185; feed reserve still short after reconciliation: 2680335/2681055; feed reserve still short after reconciliation: 2711535/2712015; feed reserve still short after reconciliation: 2909835/2910915
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 54 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T04:39:16Z - runs 421-440 (generated)

- Rank: #1, lifetime produce 16126634 (+3015379 this window)
- Animals: 120127 (beehive 101, chicken 119724, cow 100, pig 102, sheep 100), +12471 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 405690 spent on feed (0.0%), 5007 chickens adopted of 5007 planned
- Husbandry: peak hunger 66 against threshold 6 (stop 70); feed 3603825 vs reserve 3603825
- Throughput: 5174 calls, 209s mean / 251s max per run, rate limit 5.0/s
- Rivals:
  - John: 12071476 lifetime (+0 this window, 0.0% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18673 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: feed reserve still short after reconciliation: 3229455/3229695; feed reserve still short after reconciliation: 3256575/3257355; feed reserve still short after reconciliation: 3284805/3285915; hunger 66 at/above alarm 66 (production stops at 70); bulk feed unconfirmed AND hunger 60 is high - feeding may be failing
- 18 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 66 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T07:03:40Z - runs 441-460 (generated)

- Rank: #1, lifetime produce 18982900 (+2662927 this window)
- Animals: 120127 (beehive 101, chicken 119724, cow 100, pig 102, sheep 100), +0 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 0 spent on feed (0.0%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 72 against threshold 6 (stop 70); feed 3603825 vs reserve 3603825
- Throughput: 100 calls, 132s mean / 156s max per run, rate limit 5.0/s
- Rivals:
  - John: 12071476 lifetime (+0 this window, 0.0% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18673 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: bulk feed unconfirmed AND hunger 60 is high - feeding may be failing; herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; hunger 66 at/above alarm 66 (production stops at 70); bulk feed unconfirmed AND hunger 60 is high - feeding may be failing
- Peak hunger 72 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T09:29:44Z - runs 461-480 (generated)

- Rank: #1, lifetime produce 21707727 (+2551058 this window)
- Animals: 120127 (beehive 101, chicken 119724, cow 100, pig 102, sheep 100), +0 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 0 spent on feed (0.0%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 78 against threshold 6 (stop 70); feed 3603825 vs reserve 3603825
- Throughput: 101 calls, 137s mean / 239s max per run, rate limit 5.0/s
- Rivals:
  - John: 12071476 lifetime (+0 this window, 0.0% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18673 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: hunger 78 at/above alarm 66 (production stops at 70); bulk feed unconfirmed AND hunger 78 is high - feeding may be failing; herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; bulk feed unconfirmed AND hunger 60 is high - feeding may be failing; herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting
- Peak hunger 78 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T11:53:21Z - runs 481-500 (generated)

- Rank: #1, lifetime produce 24570147 (+2676467 this window)
- Animals: 120127 (beehive 101, chicken 119724, cow 100, pig 102, sheep 100), +0 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 0 spent on feed (0.0%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 78 against threshold 6 (stop 70); feed 3603825 vs reserve 3603825
- Throughput: 100 calls, 130s mean / 149s max per run, rate limit 5.0/s
- Rivals:
  - John: 12071476 lifetime (+0 this window, 0.0% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18673 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+24 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: hunger 66 at/above alarm 66 (production stops at 70); bulk feed unconfirmed AND hunger 66 is high - feeding may be failing; herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; KNOB AGE: individual_feeds=100 unchanged for 42 runs (since run 439); bulk feed unconfirmed AND hunger 72 is high - feeding may be failing
- Peak hunger 78 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T14:18:41Z - runs 501-520 (generated)

- Rank: #1, lifetime produce 27416285 (+2752561 this window)
- Animals: 120127 (beehive 101, chicken 119724, cow 100, pig 102, sheep 100), +0 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 0 spent on feed (0.0%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 72 against threshold 6 (stop 70); feed 3603825 vs reserve 3603825
- Throughput: 102 calls, 135s mean / 221s max per run, rate limit 5.0/s
- Rivals:
  - John: 12071476 lifetime (+0 this window, 0.0% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 18771 lifetime (+98 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: hunger 66 at/above alarm 66 (production stops at 70); bulk feed unconfirmed AND hunger 66 is high - feeding may be failing; herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; KNOB AGE: adopt_cap=30 unchanged for 42 runs (since run 459); KNOB AGE: individual_feeds=100 unchanged for 42 runs (since run 459)
- Peak hunger 72 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T16:46:57Z - runs 521-540 (generated)

- Rank: #1, lifetime produce 30409049 (+2799544 this window)
- Animals: 120127 (beehive 101, chicken 119724, cow 100, pig 102, sheep 100), +0 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 0 spent on feed (0.0%), 0 chickens adopted of 0 planned
- Husbandry: peak hunger 66 against threshold 6 (stop 70); feed 3603825 vs reserve 3603825
- Throughput: 113 calls, 137s mean / 148s max per run, rate limit 5.0/s
- Rivals:
  - John: 12071476 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 160102 lifetime (+141103 this window, 5.0% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 113 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 35 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Deep: 23 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 1 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; KNOB AGE: adopt_cap=30 unchanged for 42 runs (since run 479); KNOB AGE: individual_feeds=100 unchanged for 42 runs (since run 479); bulk feed unconfirmed AND hunger 60 is high - feeding may be failing; herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting
- Peak hunger 66 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T18:59:30Z - runs 541-560 (generated)

- Rank: #1, lifetime produce 32920326 (+2414792 this window)
- Animals: 120152 (beehive 101, chicken 119749, cow 100, pig 102, sheep 100), +25 this window
- Output: 33.352 units/chicken/min mean (min 33.352, max 33.352) over 1 measurable runs
- Economy: 40385324 coins revenue, 961766 spent on feed (2.4%), 25 chickens adopted of 25 planned
- Husbandry: peak hunger 66 against threshold 6 (stop 70); feed 3604575 vs reserve 3604575
- Throughput: 128 calls, 97s mean / 157s max per run, rate limit 5.0/s
- Rivals:
  - John: 12524371 lifetime (+452895 this window, 18.8% of our gain)
  - Neill: 662435 lifetime (+490122 this window, 20.3% of our gain)
  - Moe: 109278 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 161 lifetime (+47 this window, 0.0% of our gain)
  - Deep: 47 lifetime (+24 this window, 0.0% of our gain)
  - Chuck: 44 lifetime (+8 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 20 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+5 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: bulk feed unconfirmed AND hunger 60 is high - feeding may be failing; herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; bulk feed unconfirmed AND hunger 60 is high - feeding may be failing; herd 120127 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; bulk feed unconfirmed AND hunger 60 is high - feeding may be failing
- 1 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Mean throughput 33.352 units/chicken/min sits outside the 0.10-1.00 band; the band or the husbandry assumption needs revisiting.
- Peak hunger 66 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T20:56:38Z - runs 561-580 (generated)

- Rank: #1, lifetime produce 35191366 (+2174183 this window)
- Animals: 120838 (beehive 101, chicken 120435, cow 100, pig 102, sheep 100), +661 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 2437143 spent on feed (0.0%), 875 chickens adopted of 875 planned
- Husbandry: peak hunger 6 against threshold 6 (stop 70); feed 3625155 vs reserve 3625155
- Throughput: 1030 calls, 51s mean / 146s max per run, rate limit 5.0/s
- Rivals:
  - John: 12660415 lifetime (+84311 this window, 3.9% of our gain)
  - Neill: 1305763 lifetime (+616076 this window, 28.3% of our gain)
  - Moe: 151200 lifetime (+41922 this window, 1.9% of our gain)
  - Jason: 161 lifetime (+0 this window, 0.0% of our gain)
  - Deep: 96 lifetime (+47 this window, 0.0% of our gain)
  - Chuck: 44 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 32 lifetime (+12 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: herd 120177 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; STRATEGY STALE: herd 120127-120177 across 30 runs while coins grew 39302156 (97% of 40385324 revenue; balance 39302189); THREAT: John gained 51733 vs our 96857 (>= 50%); herd 120188 past the hunger-safe ceiling 119120 (projected max hunger 67 of 70) - stop adopting; STRATEGY STALE: herd 120127-120188 across 30 runs while coins grew 39181008 (97% of 40385324 revenue; balance 39181041)
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 6 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-24T22:53:38Z - runs 581-600 (generated)

- Rank: #1, lifetime produce 37465683 (+2274317 this window)
- Animals: 130877 (beehive 101, chicken 130477, cow 99, pig 102, sheep 98), +9956 this window
- Output: 0.156 units/chicken/min mean (min 0.102, max 0.420) over 20 measurable runs
- Economy: 9557904 coins revenue, 2925488 spent on feed (30.6%), 500 chickens adopted of 500 planned
- Husbandry: peak hunger 0 against threshold 6 (stop 70); feed 3926325 vs reserve 3926325
- Throughput: 814 calls, 52s mean / 74s max per run, rate limit 5.0/s
- Rivals:
  - John: 12633643 lifetime (+-26772 this window, -1.2% of our gain)
  - Neill: 1420571 lifetime (+114808 this window, 5.0% of our gain)
  - Moe: 151022 lifetime (+-178 this window, -0.0% of our gain)
  - Jason: 161 lifetime (+0 this window, 0.0% of our gain)
  - Deep: 103 lifetime (+7 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+29 this window, 0.0% of our gain)
  - Aaron: 35 lifetime (+3 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: RIVAL GROWING: Neill herd 66938 -> 67370 (+432) on 1601246 coins; RIVAL GROWING: Neill herd 67370 -> 68721 (+1351) on 1567332 coins; IDLE CAPITAL: 41744589 coins equal 211.9x a typical run while growth cap is 25; RIVAL GROWING: Neill herd 68721 -> 70073 (+1352) on 1533368 coins; IDLE CAPITAL: 41874623 coins equal 106.1x a typical run while growth cap is 25
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-25T01:15:26Z - runs 601-620 (generated)

- Rank: #1, lifetime produce 40722490 (+3155285 this window)
- Animals: 147810 (beehive 101, chicken 147411, cow 98, pig 102, sheep 98), +16260 this window
- Output: 0.171 units/chicken/min mean (min 0.104, max 0.310) over 20 measurable runs
- Economy: 6732124 coins revenue, 3286890 spent on feed (48.8%), 5750 chickens adopted of 5750 planned
- Husbandry: peak hunger 6 against threshold 6 (stop 70); feed 4433175 vs reserve 4434315
- Throughput: 6078 calls, 124s mean / 171s max per run, rate limit 5.0/s
- Rivals:
  - John: 12608668 lifetime (+-24261 this window, -0.8% of our gain)
  - Neill: 1418599 lifetime (+-1876 this window, -0.1% of our gain)
  - Moe: 150698 lifetime (+-324 this window, -0.0% of our gain)
  - Deep: 415 lifetime (+312 this window, 0.0% of our gain)
  - Jason: 161 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 35 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: IDLE CAPITAL: 43534021 coins equal 208.7x a typical run while growth cap is 25; animal count 131693 below expected 131704; IDLE CAPITAL: 43607217 coins equal 207.0x a typical run while growth cap is 25; IDLE CAPITAL: 43866924 coins equal 207.7x a typical run while growth cap is 25; IDLE CAPITAL: 43934453 coins equal 208.0x a typical run while growth cap is 25
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 6 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-25T03:53:02Z - runs 621-640 (generated)

- Rank: #1, lifetime produce 44697616 (+3743682 this window)
- Animals: 169340 (beehive 1101, chicken 167942, cow 98, pig 101, sheep 98), +20565 this window
- Output: 0.159 units/chicken/min mean (min 0.101, max 0.218) over 20 measurable runs
- Economy: 7883418 coins revenue, 3815888 spent on feed (48.4%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 6 against threshold 6 (stop 70); feed 5078865 vs reserve 5080215
- Throughput: 8328 calls, 157s mean / 199s max per run, rate limit 5.0/s
- Rivals:
  - John: 12580797 lifetime (+-26548 this window, -0.7% of our gain)
  - Neill: 1417399 lifetime (+-1039 this window, -0.0% of our gain)
  - Moe: 150415 lifetime (+-283 this window, -0.0% of our gain)
  - Deep: 752 lifetime (+337 this window, 0.0% of our gain)
  - Jason: 161 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 35 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: RIVAL GROWING: Neill herd 78424 -> 78690 (+266) on 1307882 coins; RIVAL WAKE: Neill recent 1.502/min vs base 0.000/min over 6 intervals (lifetime 1418507); animal count 157856 below expected 157867; RIVAL WAKE: Deep recent 6.754/min vs base 1.852/min over 6 intervals (lifetime 752); animal count 161792 below expected 161805
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 6 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-25T06:25:38Z - runs 641-660 (generated)

- Rank: #1, lifetime produce 48915159 (+4084839 this window)
- Animals: 189348 (beehive 1097, chicken 187955, cow 97, pig 101, sheep 98), +19211 this window
- Output: 0.170 units/chicken/min mean (min 0.102, max 0.405) over 20 measurable runs
- Economy: 9403319 coins revenue, 4198305 spent on feed (44.6%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 6 against threshold 6 (stop 70); feed 5679318 vs reserve 5680455
- Throughput: 8330 calls, 157s mean / 179s max per run, rate limit 5.0/s
- Rivals:
  - John: 12550211 lifetime (+-29763 this window, -0.7% of our gain)
  - Neill: 1413983 lifetime (+-3373 this window, -0.1% of our gain)
  - Moe: 150113 lifetime (+-245 this window, -0.0% of our gain)
  - Deep: 1381 lifetime (+629 this window, 0.0% of our gain)
  - Jason: 161 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 35 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: RIVAL WAKE: Deep recent 8.428/min vs base 1.181/min over 6 intervals (lifetime 1138); animal count 182377 below expected 182387; animal count 186236 below expected 186251
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 6 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-25T09:01:54Z - runs 661-680 (generated)

- Rank: #1, lifetime produce 53932046 (+4715695 this window)
- Animals: 209354 (beehive 1094, chicken 207964, cow 97, pig 101, sheep 98), +19218 this window
- Output: 0.155 units/chicken/min mean (min 0.083, max 0.213) over 20 measurable runs
- Economy: 10076593 coins revenue, 4780304 spent on feed (47.4%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 6 against threshold 6 (stop 70); feed 6279405 vs reserve 6280635
- Throughput: 8328 calls, 162s mean / 191s max per run, rate limit 5.0/s
- Rivals:
  - John: 12519327 lifetime (+-29443 this window, -0.6% of our gain)
  - Neill: 1410570 lifetime (+-3245 this window, -0.1% of our gain)
  - Moe: 149650 lifetime (+-463 this window, -0.0% of our gain)
  - Deep: 2562 lifetime (+1181 this window, 0.0% of our gain)
  - Jason: 161 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 35 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 191318 below expected 191337; animal count 196398 below expected 196419; RIVAL WAKE: Deep recent 2.344/min vs base 0.000/min over 6 intervals (lifetime 1491); animal count 205309 below expected 205322
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 6 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-25T11:36:59Z - runs 681-700 (generated)

- Rank: #1, lifetime produce 61702797 (+7605780 this window)
- Animals: 229042 (beehive 1088, chicken 227661, cow 96, pig 100, sheep 97), +18697 this window
- Output: 0.229 units/chicken/min mean (min 0.104, max 0.429) over 20 measurable runs
- Economy: 16105597 coins revenue, 5204653 spent on feed (32.3%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 18 against threshold 6 (stop 70); feed 6871275 vs reserve 6871275
- Throughput: 8332 calls, 159s mean / 193s max per run, rate limit 5.0/s
- Rivals:
  - John: 12477823 lifetime (+-40641 this window, -0.5% of our gain)
  - Neill: 1405296 lifetime (+-5179 this window, -0.1% of our gain)
  - Moe: 149315 lifetime (+-335 this window, -0.0% of our gain)
  - Deep: 3158 lifetime (+596 this window, 0.0% of our gain)
  - Jason: 161 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 35 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 211507 below expected 211530; animal count 220318 below expected 220348; animal count 225274 below expected 225319; animal count 229042 below expected 229059; RIVAL WAKE: Deep recent 12.636/min vs base 0.000/min over 6 intervals (lifetime 3158)
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 18 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-25T14:09:34Z - runs 701-720 (generated)

- Rank: #1, lifetime produce 65898586 (+4013682 this window)
- Animals: 249078 (beehive 1088, chicken 247697, cow 96, pig 100, sheep 97), +18880 this window
- Output: 0.251 units/chicken/min mean (min 0.088, max 0.520) over 8 measurable runs
- Economy: 9114256 coins revenue, 2918520 spent on feed (32.0%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 12 against threshold 6 (stop 70); feed 7471455 vs reserve 7472355
- Throughput: 8246 calls, 152s mean / 194s max per run, rate limit 5.0/s
- Rivals:
  - John: 12453438 lifetime (+-23505 this window, -0.6% of our gain)
  - Neill: 2987028 lifetime (+1581845 this window, 39.4% of our gain)
  - Moe: 149262 lifetime (+-53 this window, -0.0% of our gain)
  - Deep: 7179 lifetime (+3836 this window, 0.1% of our gain)
  - Jason: 161 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 35 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: RIVAL WAKE: Neill recent 902.723/min vs base 0.000/min over 6 intervals (lifetime 1449247); animal count 235018 below expected 235062; RIVAL GROWING: Neill herd 77827 -> 347844 (+270017) on 14 coins; RIVAL GROWING: Neill herd 347844 -> 381622 (+33778) on 0 coins; THREAT: Neill gained 421594 vs our 366647 (>= 50%)
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 12 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-25T16:08:19Z - runs 721-740 (generated)

- Rank: #1, lifetime produce 65898586 (+0 this window)
- Animals: 251352 (beehive 1088, chicken 249971, cow 96, pig 100, sheep 97), +1529 this window
- Output: n/a units/chicken/min mean (min n/a, max n/a) over 0 measurable runs
- Economy: 0 coins revenue, 56370 spent on feed (0.0%), 875 chickens adopted of 875 planned
- Husbandry: peak hunger 0 against threshold 6 (stop 70); feed 7540575 vs reserve 7540575
- Throughput: 976 calls, 41s mean / 151s max per run, rate limit 5.0/s
- Rivals:
  - John: 12453438 lifetime (+0 this window, 0.0% of our gain)
  - Neill: 2987028 lifetime (+0 this window, 0.0% of our gain)
  - Moe: 149262 lifetime (+0 this window, 0.0% of our gain)
  - Deep: 7179 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 161 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 35 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: PRODUCTION: 0 produce/min over 7.6 min, below the 600/min floor for two runs running (hunger 0, 249823 animals); no produce collected in 13 consecutive runs - production may have stopped; PRODUCTION: 0 produce/min over 7.3 min, below the 600/min floor for two runs running (hunger 0, 250605 animals); no produce collected in 14 consecutive runs - production may have stopped; PRODUCTION: 0 produce/min over 9.6 min, below the 600/min floor for two runs running (hunger 0, 251252 animals)
- 5 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-25T17:58:17Z - runs 741-760 (generated)

- Rank: #1, lifetime produce 67216704 (+1318118 this window)
- Animals: 251285 (beehive 1088, chicken 249904, cow 96, pig 100, sheep 97), +-67 this window
- Output: 0.327 units/chicken/min mean (min 0.253, max 0.385) over 3 measurable runs
- Economy: 2708191 coins revenue, 738925 spent on feed (27.3%), 50 chickens adopted of 50 planned
- Husbandry: peak hunger 12 against threshold 6 (stop 70); feed 7538565 vs reserve 7538565
- Throughput: 160 calls, 34s mean / 79s max per run, rate limit 5.0/s
- Rivals:
  - John: 12449090 lifetime (+-4348 this window, -0.3% of our gain)
  - Neill: 5613132 lifetime (+2626104 this window, 199.2% of our gain)
  - Moe: 149167 lifetime (+-95 this window, -0.0% of our gain)
  - Deep: 11330 lifetime (+4151 this window, 0.3% of our gain)
  - Jason: 196 lifetime (+35 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 35 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: Neill exceeded 50% of our gain
- Alerts this window: PRODUCTION: 0 produce/min over 5.7 min, below the 600/min floor for two runs running (hunger 0, 251352 animals); no produce collected in 33 consecutive runs - production may have stopped; PRODUCTION: 0 produce/min over 5.5 min, below the 600/min floor for two runs running (hunger 0, 251352 animals); no produce collected in 34 consecutive runs - production may have stopped; PRODUCTION: 0 produce/min over 5.3 min, below the 600/min floor for two runs running (hunger 0, 251352 animals)
- 2 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 12 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-25T21:23:56Z - runs 761-780 (generated)

- Rank: #1, lifetime produce 73406456 (+5234186 this window)
- Animals: 222406 (beehive 1081, chicken 221066, cow 80, pig 83, sheep 96), +-29026 this window
- Output: 0.352 units/chicken/min mean (min 0.009, max 0.698) over 20 measurable runs
- Economy: 35454375 coins revenue, 4583924 spent on feed (12.9%), 7847 chickens adopted of 8000 planned
- Husbandry: peak hunger 30 against threshold 6 (stop 70); feed 7009433 vs reserve 6672195
- Throughput: 8139 calls, 172s mean / 266s max per run, rate limit 5.0/s
- Rivals:
  - John: 12358370 lifetime (+-84992 this window, -1.6% of our gain)
  - Neill: 7479247 lifetime (+238910 this window, 4.6% of our gain)
  - Deep: 153753 lifetime (+136974 this window, 2.6% of our gain)
  - Moe: 147969 lifetime (+-1150 this window, -0.0% of our gain)
  - Jason: 226 lifetime (+10 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 30 lifetime (+-5 this window, -0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 9 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 251432 below expected 251487; STRATEGY STALE: herd 251285-251432 across 30 runs while coins grew 2525182 (72% of 3526240 revenue; balance 79728011); THREAT: Neill gained 1627205 vs our 955566 (>= 50%); STRATEGY STALE: herd 251285-251782 across 30 runs while coins grew 4639596 (79% of 5907212 revenue; balance 81842425); STRATEGY STALE: herd 251285-252091 across 30 runs while coins grew 5193505 (77% of 6728558 revenue; balance 82396334)
- Adoption hit the 260s wall-clock budget in 1/20 runs (7847 of 8000 planned chickens bought); coins roll forward, so this throttles compounding. Levers are MAX_CALLS_PER_SECOND and ADOPT_WORKERS.
- 19 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 30 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T00:03:43Z - runs 781-800 (generated)

- Rank: #1, lifetime produce 89305874 (+15089280 this window)
- Animals: 241590 (beehive 1075, chicken 240258, cow 79, pig 83, sheep 95), +18030 this window
- Output: 0.410 units/chicken/min mean (min 0.088, max 0.619) over 20 measurable runs
- Economy: 32591570 coins revenue, 4953635 spent on feed (15.2%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 36 against threshold 6 (stop 70); feed 7246995 vs reserve 7247715
- Throughput: 8326 calls, 169s mean / 223s max per run, rate limit 5.0/s
- Rivals:
  - John: 12284068 lifetime (+-70218 this window, -0.5% of our gain)
  - Neill: 7431962 lifetime (+-44550 this window, -0.3% of our gain)
  - Deep: 152251 lifetime (+-1447 this window, -0.0% of our gain)
  - Moe: 147230 lifetime (+-739 this window, -0.0% of our gain)
  - Jason: 226 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 30 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 9 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 7 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 230384 below expected 230404; animal count 234020 below expected 234033; animal count 237638 below expected 237668
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 36 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T02:42:13Z - runs 801-820 (generated)

- Rank: #1, lifetime produce 106393039 (+16028033 this window)
- Animals: 254220 (beehive 1072, chicken 252894, cow 79, pig 80, sheep 95), +11831 this window
- Output: 0.431 units/chicken/min mean (min 0.017, max 0.657) over 20 measurable runs
- Economy: 33616784 coins revenue, 5425846 spent on feed (16.1%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 36 against threshold 6 (stop 70); feed 7626615 vs reserve 7626615
- Throughput: 8310 calls, 165s mean / 226s max per run, rate limit 5.0/s
- Rivals:
  - John: 12204693 lifetime (+-73362 this window, -0.5% of our gain)
  - Neill: 7383851 lifetime (+-45471 this window, -0.3% of our gain)
  - Deep: 273995 lifetime (+104210 this window, 0.7% of our gain)
  - Moe: 146256 lifetime (+-868 this window, -0.0% of our gain)
  - Jason: 226 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 30 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 9 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+-7 this window, -0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 242389 below expected 242433; RIVAL WAKE: Deep recent 366.467/min vs base 0.000/min over 6 intervals (lifetime 169785); animal count 248265 below expected 248281; animal count 250648 below expected 250699; animal count 251281 below expected 251323
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 36 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T05:19:48Z - runs 821-840 (generated)

- Rank: #1, lifetime produce 123345012 (+16354621 this window)
- Animals: 260686 (beehive 1065, chicken 259367, cow 79, pig 80, sheep 95), +6107 this window
- Output: 0.449 units/chicken/min mean (min 0.234, max 0.621) over 20 measurable runs
- Economy: 37018636 coins revenue, 5401644 spent on feed (14.6%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 36 against threshold 6 (stop 70); feed 7820595 vs reserve 7820595
- Throughput: 8312 calls, 158s mean / 189s max per run, rate limit 5.0/s
- Rivals:
  - John: 12119570 lifetime (+-81455 this window, -0.5% of our gain)
  - Neill: 7338881 lifetime (+-43293 this window, -0.3% of our gain)
  - Deep: 464928 lifetime (+191000 this window, 1.2% of our gain)
  - Moe: 145252 lifetime (+-952 this window, -0.0% of our gain)
  - Jason: 226 lifetime (+0 this window, 0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 30 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 9 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 6 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 6 lifetime (+0 this window, 0.0% of our gain)
  - john: 6 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 254862 below expected 254922; animal count 255524 below expected 255547; animal count 255858 below expected 255871; animal count 256504 below expected 256516; animal count 256835 below expected 256855
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 36 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T07:55:43Z - runs 841-860 (generated)

- Rank: #1, lifetime produce 141365890 (+16820300 this window)
- Animals: 266991 (beehive 1054, chicken 265684, cow 79, pig 79, sheep 95), +6023 this window
- Output: 0.460 units/chicken/min mean (min 0.214, max 0.643) over 20 measurable runs
- Economy: 38194605 coins revenue, 5530395 spent on feed (14.5%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 36 against threshold 6 (stop 70); feed 8010195 vs reserve 8009745
- Throughput: 8315 calls, 162s mean / 184s max per run, rate limit 5.0/s
- Rivals:
  - John: 12042637 lifetime (+-71629 this window, -0.4% of our gain)
  - Neill: 7290923 lifetime (+-44855 this window, -0.3% of our gain)
  - Deep: 351408 lifetime (+-113404 this window, -0.7% of our gain)
  - Moe: 109042 lifetime (+-36129 this window, -0.2% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+-194 this window, -0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 6 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+-30 this window, -0.0% of our gain)
  - Alexander: 0 lifetime (+-9 this window, -0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+-6 this window, -0.0% of our gain)
  - john: 0 lifetime (+-6 this window, -0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 260968 below expected 261015; animal count 261624 below expected 261634; animal count 261949 below expected 261977; animal count 262250 below expected 262266; animal count 262562 below expected 262605
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 36 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T10:30:21Z - runs 861-880 (generated)

- Rank: #1, lifetime produce 159458709 (+17354907 this window)
- Animals: 273280 (beehive 1043, chicken 271985, cow 79, pig 78, sheep 95), +5973 this window
- Output: 0.460 units/chicken/min mean (min 0.217, max 0.652) over 20 measurable runs
- Economy: 38803236 coins revenue, 5652018 spent on feed (14.6%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 36 against threshold 6 (stop 70); feed 8198415 vs reserve 8198415
- Throughput: 8308 calls, 158s mean / 189s max per run, rate limit 5.0/s
- Rivals:
  - John: 11971368 lifetime (+-69407 this window, -0.4% of our gain)
  - Neill: 7245173 lifetime (+-43885 this window, -0.3% of our gain)
  - Deep: 609971 lifetime (+258654 this window, 1.5% of our gain)
  - Moe: 108320 lifetime (+-648 this window, -0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 0 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 0 lifetime (+-6 this window, -0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 267307 below expected 267331; animal count 267615 below expected 267663; RIVAL WAKE: Deep recent 550.285/min vs base 0.000/min over 6 intervals (lifetime 378682); animal count 268225 below expected 268278; animal count 268849 below expected 268898
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 36 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T13:06:25Z - runs 881-900 (generated)

- Rank: #1, lifetime produce 176261263 (+15936715 this window)
- Animals: 279705 (beehive 1038, chicken 278419, cow 79, pig 77, sheep 92), +6107 this window
- Output: 0.413 units/chicken/min mean (min 0.267, max 0.617) over 19 measurable runs
- Economy: 37728174 coins revenue, 5419119 spent on feed (14.4%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 30 against threshold 6 (stop 70); feed 8391795 vs reserve 8391165
- Throughput: 8288 calls, 163s mean / 247s max per run, rate limit 4.0/s
- Rivals:
  - John: 11894840 lifetime (+-70899 this window, -0.4% of our gain)
  - Neill: 7201631 lifetime (+-41510 this window, -0.3% of our gain)
  - Deep: 1399751 lifetime (+759757 this window, 4.8% of our gain)
  - Moe: 107446 lifetime (+-757 this window, -0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 0 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 0 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: bulk feed unconfirmed: feed_animals returned isError: 🚫 Failed query: 
    WITH inv AS (
      SELECT qty FROM inventory WH; animal count 273598 below expected 273605; animal count 273896 below expected 273950; RIVAL WAKE: Deep recent 4479.616/min vs base 238.965/min over 6 intervals (lifetime 824324); animal count 274545 below expected 274568
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 30 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T15:45:33Z - runs 901-920 (generated)

- Rank: #1, lifetime produce 190184809 (+13152069 this window)
- Animals: 286764 (beehive 1032, chicken 285487, cow 77, pig 77, sheep 91), +6728 this window
- Output: 0.328 units/chicken/min mean (min 0.140, max 0.531) over 20 measurable runs
- Economy: 29470041 coins revenue, 5196253 spent on feed (17.6%), 8000 chickens adopted of 8000 planned
- Husbandry: peak hunger 36 against threshold 6 (stop 70); feed 8602935 vs reserve 8602935
- Throughput: 8264 calls, 165s mean / 180s max per run, rate limit 4.0/s
- Rivals:
  - John: 11921465 lifetime (+30172 this window, 0.2% of our gain)
  - Neill: 7162197 lifetime (+-37094 this window, -0.3% of our gain)
  - Deep: 2250088 lifetime (+849733 this window, 6.5% of our gain)
  - Moe: 106846 lifetime (+-600 this window, -0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 30 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 0 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 0 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 280340 below expected 280392; animal count 281000 below expected 281044; animal count 281627 below expected 281678; RIVAL GROWING: Deep herd 131216 -> 199129 (+67913) on 300057 coins; animal count 282308 below expected 282330
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 36 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T18:04:04Z - runs 921-940 (generated)

- Rank: #1, lifetime produce 199959560 (+8921548 this window)
- Animals: 289319 (beehive 1030, chicken 288047, cow 75, pig 76, sheep 91), +2244 this window
- Output: 0.261 units/chicken/min mean (min 0.140, max 0.491) over 20 measurable runs
- Economy: 21476116 coins revenue, 4397194 spent on feed (20.5%), 3500 chickens adopted of 3500 planned
- Husbandry: peak hunger 36 against threshold 6 (stop 70); feed 8679585 vs reserve 8679585
- Throughput: 3760 calls, 115s mean / 183s max per run, rate limit 4.0/s
- Rivals:
  - John: 12298609 lifetime (+94104 this window, 1.1% of our gain)
  - Neill: 7767679 lifetime (+608190 this window, 6.8% of our gain)
  - Deep: 2630510 lifetime (+378978 this window, 4.2% of our gain)
  - Moe: 136062 lifetime (+29216 this window, 0.3% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 19 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 0 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 0 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: no rival above 50% of our gain
- Alerts this window: animal count 287075 below expected 287098; animal count 288121 below expected 288150; RIVAL GROWING: Deep herd 249911 -> 282159 (+32248) on 260013 coins; animal count 288793 below expected 288834; animal count 289499 below expected 289515
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 36 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T20:19:37Z - runs 941-960 (generated)

- Rank: #1, lifetime produce 203868747 (+3754356 this window)
- Animals: 289432 (beehive 1028, chicken 288163, cow 75, pig 75, sheep 91), +99 this window
- Output: 0.103 units/chicken/min mean (min 0.047, max 0.170) over 20 measurable runs
- Economy: 8914667 coins revenue, 3091243 spent on feed (34.7%), 500 chickens adopted of 500 planned
- Husbandry: peak hunger 18 against threshold 6 (stop 70); feed 8682975 vs reserve 8682975
- Throughput: 762 calls, 88s mean / 178s max per run, rate limit 5.0/s
- Rivals:
  - John: 12258151 lifetime (+-38565 this window, -1.0% of our gain)
  - Neill: 10500047 lifetime (+2625542 this window, 69.9% of our gain)
  - Deep: 2633098 lifetime (+2573 this window, 0.1% of our gain)
  - Moe: 135801 lifetime (+-261 this window, -0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 19 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 0 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 0 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: Neill exceeded 50% of our gain
- Alerts this window: IDLE CAPITAL: 254981071 coins equal 327.3x a typical run while growth cap is 25; KNOB AGE: rate_ceiling=4.0 unchanged for 41 runs (since run 900); THREAT: Neill gained 106826 vs our 154831 (>= 50%); RIVAL GROWING: Deep herd 307715 -> 308415 (+700) on 138213 coins; IDLE CAPITAL: 255173608 coins equal 327.6x a typical run while growth cap is 25
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 18 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-26T22:07:30Z - runs 961-980 (generated)

- Rank: #1, lifetime produce 207400017 (+3318443 this window)
- Animals: 289567 (beehive 1028, chicken 288301, cow 72, pig 75, sheep 91), +127 this window
- Output: 0.125 units/chicken/min mean (min 0.049, max 0.245) over 19 measurable runs
- Economy: 8087858 coins revenue, 2864290 spent on feed (35.4%), 500 chickens adopted of 500 planned
- Husbandry: peak hunger 18 against threshold 6 (stop 70); feed 8687025 vs reserve 8687025
- Throughput: 753 calls, 76s mean / 170s max per run, rate limit 5.0/s
- Rivals:
  - Neill: 12988044 lifetime (+2353517 this window, 70.9% of our gain)
  - John: 12225447 lifetime (+-30616 this window, -0.9% of our gain)
  - Deep: 2634797 lifetime (+1463 this window, 0.0% of our gain)
  - Moe: 135429 lifetime (+-372 this window, -0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 19 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 0 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 0 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: Neill exceeded 50% of our gain
- Alerts this window: STRATEGY STALE: herd 289319-289447 across 30 runs while coins grew 11369806 (70% of 16188534 revenue; balance 260636808); IDLE CAPITAL: 260636808 coins equal 561.1x a typical run while growth cap is 25; THREAT: Neill gained 134480 vs our 212827 (>= 50%); animal count 289443 below expected 289457; STRATEGY STALE: herd 289319-289443 across 30 runs while coins grew 11071980 (70% of 15811154 revenue; balance 260775906)
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 18 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-27T00:13:49Z - runs 981-1000 (generated)

- Rank: #1, lifetime produce 211059364 (+3505086 this window)
- Animals: 289679 (beehive 1026, chicken 288415, cow 72, pig 75, sheep 91), +98 this window
- Output: 0.114 units/chicken/min mean (min 0.047, max 0.221) over 20 measurable runs
- Economy: 8293190 coins revenue, 2918322 spent on feed (35.2%), 500 chickens adopted of 500 planned
- Husbandry: peak hunger 6 against threshold 6 (stop 70); feed 8690385 vs reserve 8690385
- Throughput: 760 calls, 77s mean / 130s max per run, rate limit 5.0/s
- Rivals:
  - Neill: 15333379 lifetime (+2232816 this window, 63.7% of our gain)
  - John: 12188289 lifetime (+-35820 this window, -1.0% of our gain)
  - Deep: 2635872 lifetime (+1075 this window, 0.0% of our gain)
  - Moe: 134936 lifetime (+-443 this window, -0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 19 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 0 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 0 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: Neill exceeded 50% of our gain
- Alerts this window: STRATEGY STALE: herd 289383-289581 across 30 runs while coins grew 8227827 (66% of 12527729 revenue; balance 265803056); IDLE CAPITAL: 265803056 coins equal 855.3x a typical run while growth cap is 25; THREAT: Neill gained 112519 vs our 154261 (>= 50%); STRATEGY STALE: herd 289383-289594 across 30 runs while coins grew 8243857 (66% of 12544013 revenue; balance 265886768); IDLE CAPITAL: 265886768 coins equal 861.4x a typical run while growth cap is 25
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 6 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-27T02:17:49Z - runs 1001-1020 (generated)

- Rank: #1, lifetime produce 214974059 (+3835282 this window)
- Animals: 289761 (beehive 1025, chicken 288499, cow 71, pig 75, sheep 91), +67 this window
- Output: 0.120 units/chicken/min mean (min 0.048, max 0.245) over 20 measurable runs
- Economy: 8725814 coins revenue, 3405401 spent on feed (39.0%), 500 chickens adopted of 500 planned
- Husbandry: peak hunger 12 against threshold 6 (stop 70); feed 8692845 vs reserve 8692845
- Throughput: 760 calls, 71s mean / 132s max per run, rate limit 5.0/s
- Rivals:
  - Neill: 18203231 lifetime (+2798647 this window, 73.0% of our gain)
  - John: 12152141 lifetime (+-35766 this window, -0.9% of our gain)
  - Deep: 2638012 lifetime (+2124 this window, 0.1% of our gain)
  - Moe: 134451 lifetime (+-485 this window, -0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 19 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 0 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 0 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: Neill exceeded 50% of our gain
- Alerts this window: STRATEGY STALE: herd 289475-289704 across 30 runs while coins grew 6803403 (63% of 10772495 revenue; balance 271019884); IDLE CAPITAL: 271019884 coins equal 585.0x a typical run while growth cap is 25; THREAT: Neill gained 71205 vs our 79413 (>= 50%); STRATEGY STALE: herd 289487-289707 across 30 runs while coins grew 6781848 (63% of 10827594 revenue; balance 271137701); IDLE CAPITAL: 271137701 coins equal 585.2x a typical run while growth cap is 25
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 12 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned

## 2026-08-27T05:52:21Z - runs 1021-1040 (generated)

- Rank: #1, lifetime produce 222443046 (+7295605 this window)
- Animals: 289502 (beehive 1019, chicken 288248, cow 71, pig 74, sheep 90), +-272 this window
- Output: 0.138 units/chicken/min mean (min 0.074, max 0.239) over 18 measurable runs
- Economy: 16362775 coins revenue, 3535727 spent on feed (21.6%), 500 chickens adopted of 500 planned
- Husbandry: peak hunger 6 against threshold 6 (stop 70); feed 8685075 vs reserve 8685075
- Throughput: 756 calls, 71s mean / 97s max per run, rate limit 5.0/s
- Rivals:
  - Neill: 22988676 lifetime (+4689328 this window, 64.3% of our gain)
  - John: 12086301 lifetime (+-64419 this window, -0.9% of our gain)
  - Deep: 2640455 lifetime (+2443 this window, 0.0% of our gain)
  - Moe: 133744 lifetime (+-707 this window, -0.0% of our gain)
  - Chuck: 73 lifetime (+0 this window, 0.0% of our gain)
  - Jason: 32 lifetime (+0 this window, 0.0% of our gain)
  - Vijay: 19 lifetime (+0 this window, 0.0% of our gain)
  - Guillermo G.: 7 lifetime (+0 this window, 0.0% of our gain)
  - Aaron: 0 lifetime (+0 this window, 0.0% of our gain)
  - Alexander: 0 lifetime (+0 this window, 0.0% of our gain)
  - Brendan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Kannan: 0 lifetime (+0 this window, 0.0% of our gain)
  - Matthew: 0 lifetime (+0 this window, 0.0% of our gain)
  - john: 0 lifetime (+0 this window, 0.0% of our gain)
- Threat check: Neill exceeded 50% of our gain
- Alerts this window: STRATEGY STALE: herd 289666-289774 across 30 runs while coins grew 8072844 (63% of 12860174 revenue; balance 276541071); IDLE CAPITAL: 276541071 coins equal 599.8x a typical run while growth cap is 25; STRATEGY STALE: herd 289670-289780 across 30 runs while coins grew 8118497 (63% of 12973501 revenue; balance 276716495); IDLE CAPITAL: 276716495 coins equal 600.2x a typical run while growth cap is 25; animal count 289777 below expected 289787
- 20 run(s) ended with >=10 idle coins after a complete adoption plan, which means the feed reserve is binding rather than coins.
- Peak hunger 6 reached the 6 feeding threshold, so feeding is now actually firing rather than sitting idle.
- Active rules: engine chicken, feed at 6, reserve 30/animal, budget 260s, call rate 5.0/s, 6 adopt workers, verify every 6 runs, food crops banned
