# Postmortem: runs 337-377, three throttles pointed at the wrong variable

Read this with `POSTMORTEM-run291.md`. That one is about losing first place by
freezing the herd on bad evidence. This one is about nearly losing it again by
throttling the herd on *irrelevant* evidence, three separate times, while every
proxy said the farm was healthy.

Standing at the start of the session: **#2**, 41,293 animals, 2,455,082 lifetime
produce behind John, and the gap **reopening** at ~1,287 produce/min.

Standing four hours later: 61,586 animals, gap 1,563,780 and closing, projected
to retake first place in ~5 hours.

Nothing about the farm changed. What changed is that three throttles stopped
firing and one dead call stopped being made.

## What was actually wrong

### 1. Whole-herd 504s were read as "the server is pushing back"

`mcp.Client` kept **one global** `transport_errors` counter. Two calls in the
cycle are whole-herd calls that cannot be split (`collect_produce` takes no
arguments at all), and past ~25,000 animals both reliably exceed the gateway's
~97s limit and burn all three retries. So every run reported exactly `6
transport retries`, the detector called it sustained backpressure, and the healer
cut `adopt_workers` 8 -> 4 and `rate_ceiling` 5.0 -> 2.56.

Measured over the same window:

| tool | ok | failed |
|---|---|---|
| `adopt_animal` | **3,353** | **0** |
| `buy_feed` / `list_farm` / `leaderboard` | 51 | 0 |
| `feed_animals` | 0 | 5 |
| `collect_produce` | 0 | 5 |

Adoption was perfect. We halved the rate of the only call that scores because of
two calls that a lower rate cannot help - they are one request each.

Worse, `feed_animals` **completes server-side when it 504s**. The gateway hangs
up; the farm still gets fed. Proof: feed is debited, and max herd hunger stayed
between 6 and 24 out of a 70 starvation threshold while the herd grew 12k -> 58k.

**Fix.** `transport_errors_by_tool` attributes retries to the tool that caused
them, and `rules.core_transport_errors()` excludes `rules.HEAVY_HERD_TOOLS` from
the signal that moves the rate limiter. They still surface as a soft note,
because a *change* in them is interesting; they just cannot throttle anything.

### 2. A 0.05% feed shortfall halved the adopt cap

`feed < reserve_target` raised an incident, and its remedy halves the adopt cap.
But `reserve_target` is computed from the **final** animal count while `buy_feed`
was sized from an earlier one, and `experiments/expand.py` adopts throughout the
run. Growing fast therefore *guarantees* a small shortfall against a target that
moved underneath us.

Run 337 was short **390 feed out of 789,135** - 0.05% - with a completely healthy
288-minute runway. The cap went 400 -> 200 -> 100 -> 50 across runs 337-340.
Across the last 120 runs the alert fired 20 times and **15 were false alarms**.

**Fix.** Two layers, because this remedy is expensive:
- the detector tolerates a shortfall smaller than one cycle of concurrent
  adoption (`FEED_RESERVE_TOLERANCE_MIN/FRACTION`) and downgrades it to a note;
- the remedy re-checks the runway itself and refuses to throttle while it is at
  or above the 240-minute floor.

The runway detector, denominated in minutes, was always the real guard.

### 3. A collect that cannot finish was attempted every run

`collect_produce` measured ~97s flat at every herd size from 25k to 58k, returned
504, and banked **0 units** for 14+ consecutive runs. The barn held nothing but
feed, so it was not silently succeeding either.

Meanwhile bulk feed costs `0.001755 * herd + 23.4` seconds (linear fit, n=60) and
is **not** optional. Adding a dead 97s call on top crosses the 285s watchdog at
about **70,000 animals** - and we need ~106,000 to retake first place:

| herd | feed | + collect | verdict |
|---|---|---|---|
| 58,061 | 125s | 267s | over budget |
| 70,000 | 146s | 288s | **watchdog kill** |
| 106,000 | 209s | 351s | **watchdog kill** |
| 106,000 | 209s | *254s (no collect)* | OK |

So the cycle was on course to start dying from timeout roughly two hours before
the herd got big enough to win. Run 291 again: a call that gets slower with
success until success kills it.

Collection is also the one thing that is **free to drop**: lifetime produce - the
score - accrues whether or not produce is collected. It rose ~7,700/min for hours
while every single collect returned 504. Collection only converts ready produce
into coins, and the coin runway (~14h) already outlasts the projected win (~5h).

**Fix.** `rules.collect_fits_budget()` skips collect once it cannot fit alongside
the feed we actually need, with `COLLECT_REPROBE_RUNS = 40` forcing an occasional
attempt so "collect does not work at this herd size" stays a measurement instead
of becoming folklore. Reclaiming that time raised cycle adoption from ~130 to
**317 per run** immediately.

## What we measured while we were in there

`experiments/feed_economics.py` settles the feed numbers directly rather than
inferring them from noisy accounting, because the reserve is our single largest
idle asset (1.74M feed = 1.74M coins = ~174,000 unadopted chickens).

- **1 feed per animal fed**, exactly. Measured twice, single-animal.
- **Hunger rises 1.26/min**, in discrete steps of 6. Production stops at 70, so
  an animal has ~56 minutes from full to stopped.
- **Real burn is 0.060-0.070 feed/animal/min**, against
  `FEED_BURN_PER_ANIMAL_MIN = 0.104`. The model is ~1.7x conservative, so the
  30/animal reserve is worth 7-8 hours, not the 4.8 it was sized for.

The reserve was **deliberately left at 30**. It is the thing that lets us spend
coins down toward zero without repeating John's starvation spiral: feeding draws
on feed we already own, so a full barn is insurance that survives having no
money. Cutting it is a real option worth ~500k coins, but it is the exact
parameter that lost first place once, and coins were not the binding constraint.

### There is no output ceiling

The growth gate's founding evidence was a plateau in output above ~8,000
animals. With 371 samples spanning 648 to 58,000 animals:

- slope above 8k: **0.178** produce/min per animal (r = 0.908, n = 355)
- slope below 8k: **0.199** (r = 0.958, n = 16)

That is the same number, not a collapse. Output is **linear in herd size**, which
is what makes "adopt until we out-produce him" a plan rather than a hope. The
falling per-animal *collected* column that started all this is a flat numerator
over a growing herd - and the numerator is flat because it is capped by a
whole-herd call that now always times out.

`deploy/test_evidence.py` used to assert the plateau (`above < below * 0.35`).
It now asserts the finding it should have: that the marginal animal still pays.

## The gap that let all three of these run

Every one of these was invisible to every detector, because **nothing measured
the objective**. Hunger, runway, throughput, call rate and transport all read
healthy while the herd growth rate was cut in half, twice, and while the cycle
drifted toward a timeout wall.

`rules.win_projection()` now answers the only question that decides the game -
will we pass the leader, and when - from the measured linear-output model:

```
produce(t) = P + R*t + 0.5 * y * g * t**2      y = per-animal yield, g = adoption rate
```

It emits `NO PATH TO WIN` when there is no positive root, which is the one alert
that must never be healed by throttling anything. The leader's produce arrives in
lumpy windows (John measured 884-8,321/min with a frozen herd), so the rate is
EWMA-smoothed at alpha 0.35 - an unsmoothed sample swung the ETA 6-12h and would
have raised false alarms, and a false escalation costs real money.

We also now record **rival herd and coins**, not just produce. The leaderboard
reported all three the whole time. "John is frozen at 56,061 animals on 76 coins"
was the single most decision-relevant fact in the game and it was invisible to the
loop - it had to be re-derived by hand from a raw response. Produce alone cannot
distinguish *the rival got fed* (rate capped) from *the rival is adopting* (rate
uncapped), and only the second one can beat us.

That matters immediately: as of run 377 John holds **423,461 coins** (up from 76)
and his herd has resumed growing. `RIVAL GROWING` now fires on that.

## Lessons, in the form they should have been written the first time

13. **Attribute a failure before reacting to it.** A counter that aggregates two
    populations - retryable calls and structurally-impossible ones - will
    eventually make you throttle the healthy population.
14. **A remedy that costs score must re-check the number that justifies it.** The
    detector decides what is odd; the remedy decides what is worth paying for.
15. **Tolerate what concurrency makes inevitable.** Two agents mutating the same
    farm guarantee small accounting disagreements. Alerting on them turns your
    own parallelism into a self-inflicted throttle.
16. **Stop paying for a call that cannot succeed, but keep re-probing it.**
    Otherwise a measurement quietly becomes folklore.
17. **Measure the objective, or the proxies will all look fine while you lose.**
    Three throttles and a timeout wall hid behind five green detectors.
18. **Test the finding, not the snapshot.** `collected == 0` and
    `above < below * 0.35` both aged into false alarms and taught us to distrust
    a passing suite.

## Verification

```bash
python3 run.py --self-test         # 113 checks (was 94); 14 new ones pin the above
python3 deploy/test_evidence.py    # 27 checks
python3 deploy/test_dashboard.py   # 91 checks
```

Each new check was confirmed to fail when its fix is reverted - the three
throttle fixes were mutation-tested individually.

## Three more, found while verifying the first three

### 4. The rate limiter was throttling on its own latency

`cycle.py` eased the call rate whenever the mean adopt call exceeded
`SLOW_CALL_SECONDS` (1.2s) - "rising latency means the server is straining". But
`started` was captured before `client.call()`, which blocks inside
`LIMITER.acquire()`. With W workers sharing a limiter at R calls/s, a worker
waits about W/R for its slot, so the measurement was dominated by the limiter:

    6 workers / 2.5 calls per second = 2.4s     observed: 2.42s

Every run therefore measured "slow", cut the rate, and thereby lengthened the
wait that produced the measurement. Lower rate -> longer wait -> lower rate.

True server service time, measured single-threaded with the limiter wide open,
is **0.670s** - half the threshold. The server was never straining.

**Fix.** `mcp.Client.last_service_seconds` times only the request, excluding the
limiter wait, and the ease-off uses it. The adopt budget check still uses wall
time, because the limiter wait is real elapsed time even when it is not strain.
`MEASURED_SERVICE_SECONDS` records the measurement and a test asserts the
threshold keeps headroom over it.

Effect, immediately: rate 2.5 -> 4.0 -> 5.0/s, measured service 2.49s -> 0.59s,
cycle adoption 107 -> 400 per run, cycle duration 277s -> 201s.

### 5. The adoption floor charged coins for feed already in the barn

`expand.affordable()` reserved `FEED_PER_ANIMAL_RESERVE * herd` coins before
allowing any adoption. At run 379 that reserved **1,905,660 coins** against a barn
that already held **2,222,305 feed** - 538 minutes of runway. Coins and feed were
both counted as though only coins existed, so the agent reported that just
**1,048** more animals were affordable while 1.9M coins sat idle.

`experiments/endgame.py` simulates the remaining race under the real constraint
(adoption is one-off, feed is a flow, the barn offsets it) and the difference is
the whole game:

| policy | herd cap | outcome |
|---|---|---|
| old floor | ~64,570 | **never passes John** |
| counting the barn | ~112,000 | **passes John in 6.4h, never starves** |

**Fix.** `rules.affordable_adoptions()` floors coins at the *shortfall* between
the reserve the herd should have and the reserve it already has. New animals are
still charged a full reserve each, so the invariant that matters - never adopt an
animal we cannot also feed - is unchanged.

The reserve constant itself was **left at 30**, deliberately. Measured burn is
0.060-0.070 against the modelled 0.104, so 30/animal is worth 7-8 hours rather
than the 4.8 it was sized for, and cutting it is worth ~500k coins. But it is the
exact parameter that lost first place once, it is what lets us spend coins toward
zero without starving (feeding draws on feed we already own), and coins stopped
being the binding constraint once the double-count was removed. No reason to
touch it.

### 6. The hunger wall - starvation from the opposite direction

Because `feed_animals("all")` is a single gateway-limited call, it reaches a
shrinking *fraction* of the herd as the herd grows, and the unfed tail's hunger
climbs with herd size. Measured across 52 runs above 25,000 animals:

    max_hunger = 0.000614 * herd - 7.14

    herd 42,354 -> max hunger 18        herd 66,895 -> max hunger 36

Production stops at `HUNGER_STOP = 70`, which that line reaches at about
**125,700 animals**. `experiments/expand.py` was being launched with
`--target 250000` - twice the wall. Past it, every coin buys an animal that
produces nothing and still has to be fed: run 291's starvation arrived at from the
opposite direction, by succeeding too fast.

**Fix.** `rules.hunger_safe_herd_ceiling()` caps the herd at ~119,000, where
projected worst-case hunger is still only 66. That is above the ~112,000 needed to
retake first place, so it costs nothing and prevents a runaway. expand.py caps its
target to it, and a detector warns at 90% of the ceiling instead of relying on
`HUNGER_ALARM`, which fires at 66 of 70 - about three minutes of notice at the
measured 1.26 hunger/min.

## Cost: the alert set predated the objective

Four alerts fired on **every single run** and each escalated to an LLM "for
judgement": `RANK LOST: now #2`, `THREAT: John gained X vs our Y`, `John has
passed us on lifetime produce`, and (newly) `RIVAL GROWING`.

All four are *premises* of the race we are deliberately running. Worse, `THREAT`
fired while we were winning: it compares the rival's gain to ours at a 50%
threshold, so it is true even when we out-produce him (103,520 vs 84,635).

The judgement they asked for is now arithmetic. `watch.evaluate` holds them and
resolves them against the projection: soft notes while we are on track, escalated
the moment we are not. Being behind is not news; being behind with no path to the
front is.

Result: **3-4 escalations per run -> 0**, `needs_llm: false`.

## And one self-inflicted wound

Resetting the throttle knobs by hand, I appended a dict to `heal.json`'s `healed`
list, which holds alert-key strings. `set()` over it raised
`TypeError: unhashable type: 'dict'`, which failed the entire supervise pass and
set `needs_llm: true` - a de-duplication helper billed us for a model wake-up.

Fixed the data, and made `healed_keys()` skip what it cannot hash. A supervisor
must survive its own state file. Lesson 19: **the component that decides whether
to spend money must be the most defensive one in the system.**

## Where this left us

| | start of session | after |
|---|---|---|
| rank | #2 | #2, closing |
| herd | 41,293 | 66,895 |
| lead to close | 2,455,082 and **widening** | 1,500,145 and **closing** |
| our score rate | 7,696/min | 11,610/min (John ~9,000) |
| herd growth | 126/min, throttled to 72 | 119-145/min, unthrottled |
| cycle duration | 274s of a 285s watchdog | 201-245s |
| escalations/run | 3-4 | **0** |
| projected win | never (gap widening) | **~4.7 h** |
| self-test | 94 checks | **130 checks** |
