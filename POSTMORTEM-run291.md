# Post-mortem: how we lost #1 (runs 290-293)

**Date:** 2026-08-23 · **Impact:** ~19h of stopped production, first place lost to John

## Summary

At run 290 we led with 2,015,006 lifetime produce. John had **139**. Nineteen hours
later John had 3,081,121 and we were #2. We never recovered during the outage
because the loop had entered a crash loop it could not heal itself out of.

None of this was a strategy failure in the game. It was three engineering faults
that compounded, plus one wrong belief that kept us from competing afterwards.

## Fault 1 - the feed reserve was 20 minutes, not a buffer

`FEED_PER_ANIMAL_RESERVE = 2` gave 23,753 feed at 11,869 animals. The herd burns
~1,235 feed/minute, so the entire reserve was **~20 minutes** of runway. It read
as healthy because the detector compared feed against an absolute count, and
23,753 looks like a lot.

Feed costs 1 coin. We were holding 2.9M idle coins.

**Fixed:** reserve is now 30/animal (~290 min). `rules.feed_buffer_minutes()`
expresses the buffer in **minutes**, and the detector alerts below a 240-minute
floor, because minutes are what you compare against a possible outage.

## Fault 2 - launchd does not fire while the Mac is asleep

`StartInterval` 180s does not accumulate across sleep, and `RunAtLoad` was
`false`. The gap between run 290 and 291 was **19.3 hours** and nothing said so:
`interval_min` was recorded but never checked.

**Fixed:** `RunAtLoad` is now `true`, and a run gap over
`RUN_GAP_ALARM_MINUTES` (20) is now itself an alert.

## Fault 3 - the deadlock: `feed` ran before `buy_feed`, and threw

This is why it never recovered. Pipeline order was:

```
collect -> read -> feed -> ... -> buy_feed
```

`feed_animals` raises `ToolError` when feed is 0. That exception escaped `run()`,
so the cycle aborted at the `feed` step - **before reaching `buy_feed`, the one
step that would have fixed it.** Feed stayed 0, so the next run failed
identically. Every cycle and every supervisor pass crashed on the same line:

```
farm.mcp.ToolError: feed_animals returned isError: You're out of feed! Use buy_feed.
```

The self-healing supervisor could not help: it calls `do_cycle()`, so it crashed
in exactly the same place. A loop cannot heal a fault that lives upstream of its
own remedy.

**Fixed, two layers:**
1. `ensure_feed_on_hand()` buys before feeding whenever the larder cannot cover a
   bulk feed. Same fix applied to `backstop()`.
2. `feed_if_needed()` treats "out of feed" as recoverable: buy, then retry once.
   Any other `ToolError` still raises, because that is real drift.

Both layers are pinned by regression tests, including one asserting `buy_feed` is
called **before** `feed_animals`.

## Fault 4 - the growth gate was a one-way ratchet that froze the herd

The expensive one. `GROWTH_MIN_MARGINAL_GAIN = 0.10` required output to rise 10%
versus a cohort only 11-43% smaller - effectively demanding **linear** scaling.
Our measured curve is sub-linear but strictly rising:

| herd | produce/min | per animal/min |
|---|---|---|
| 1,500-2,999 | 642 | 0.285 |
| 4,500-5,999 | 1,014 | 0.193 |
| 7,500-8,999 | 1,450 | 0.176 |
| 10,500-11,999 | 1,629 | 0.145 |

Total output never plateaued; only efficiency per animal fell. The gate read that
as a ceiling (1,645 vs 1,553/min = +5.9%), set the cap to `MAINTENANCE_ADOPTIONS`
(**0**), and froze the herd at 11,869 for **246 runs** while coins piled to 3.2M.

It was unescapable by construction: resuming required more output from the same
herd, but the herd was frozen at the rate that triggered the freeze.

Meanwhile John simply never stopped adopting.

**Fixed:** margin is 0.02 (a noise floor, not an economic hurdle),
`MAINTENANCE_ADOPTIONS` is 25 so growth can never latch fully off, and
`MAX_ADOPTIONS_PER_RUN` is 400. Coins are not the score; an idle coin is waste.

## Fault 5 - stale alerts throttled a healthy farm

Healing consumes an alert *queue* but acts on the *latest* row. At run 295 a
queued "hunger 100" alert from run 291 - the already-fixed starvation - cut the
adoption cap 25 -> 15 on a farm sitting at hunger 0, throttling the recovery.

**Fixed:** `HEAL_ALERT_STALE_RUNS = 5`; older alerts are recorded and ignored.
Also removed the duration-based adoption throttle, which was a feedback loop:
adopting made runs longer, which cut the adoption cap.

## What the rival was actually doing

`leaderboard` reports animals, coins and wildflowers, and `visit_farm` exists.
We had never looked. John: **43,631 animals**, we had 11,869.

Measured per-animal output once both farms were fed:

| | herd | produce/min | per animal/min |
|---|---|---|---|
| John | 43,631 | 8,185 | 0.187 |
| Nick | 16,005 | 2,992 | 0.187 |

**Identical.** There is no secret engine and no per-farm ceiling. His entire lead
was herd size. Wildflowers (3 coins, permanent, boost beehive honey) are real but
marginal here: honey ran ~4/min against ~600/min of eggs.

## The recovery

- Herd growth is throughput-bound, not cap-bound: collect (~55s) and bulk feed
  (~77s) consume the 150s cycle budget, leaving ~8s to adopt (~14 animals). The
  server is not the constraint - the cycle issues ~27 calls in 150s against a
  ~5/s ceiling.
- So adoption moved to its own agent, `experiments/expand.py`
  (`com.nickfigura.farmfriends.expand`): a bounded 240s sprint every 300s, ~3/s,
  refusing to spend below the coin floor that keeps the target herd fed.
  Measured 2.6-3.2 adoptions/s with zero failures, and **coins still rose** while
  it ran - expansion is self-funding.
- Sustainability holds at every scale; revenue runs ~3.6x feed burn:

| herd | produce/min | feed burn/min | revenue/min |
|---|---|---|---|
| 30,000 | 5,610 | 3,120 | ~11,220 |
| 60,000 | 11,220 | 6,240 | ~22,440 |
| 90,000 | 16,830 | 9,360 | ~33,660 |

## Lessons

1. **A buffer must be expressed in the unit of the threat.** "23,753 feed" hid a
   20-minute runway. Time-to-empty is the number that matters.
2. **Never put a fatal call upstream of its own remedy.** The recovery step must
   not be unreachable from the failure.
3. **A self-healing loop cannot heal its own crash path.** The supervisor shared
   the failing code, so it inherited the failure.
4. **Optimise the objective, not a proxy.** The gate maximised output per animal;
   the score is total produce. Falling efficiency is not a ceiling.
5. **Any automatic throttle needs a path back up.** A cap of 0 with an escape
   condition that the cap itself prevents is a permanent stop.
6. **Look at the opponent.** `leaderboard` and `visit_farm` had the answer the
   whole time, and it was mundane: he kept growing.
---

# Addendum: the recovery build (runs 293-311)

Fixing the faults above restored production. Winning needed a second pass,
because once the herd was growing the constraints moved.

## Output is linear in herd, and there is no per-farm ceiling

The old model said output plateaued at ~1,645/min. Measured against a healthy,
fully-fed herd, output is simply **linear in animals**, and our efficiency matches
the leader's exactly:

| | herd | produce/min | per animal/min |
|---|---|---|---|
| John | 45,161 | ~7,900 | 0.175 |
| Nick | 18,281 | ~3,290 | 0.180 |

The old "plateau" was an artifact of measuring while a 2-feed/animal reserve left
bulk feeds partial. So herd size is the whole game, and adoption throughput - not
coins - is the binding constraint. Revenue runs far ahead of feed burn plus
adoption spend, so growth is self-funding: coins *rose* during most sprints.

## The cycle could not physically fit its own budget

At ~17,000 animals `collect_produce` takes ~75s and `feed_animals("all")` ~83s.
That is ~158s of unavoidable server work against a 150s budget and a 170s
watchdog, so runs died on `exceeded 170s hard timeout` - each death costing a
feeding. Both calls scale with herd size, so this was going to get worse.

**Fixed:** budget 260s, watchdog 285s, cadence 300s, MCP timeout 30s -> 120s.

## Collection is not score, so it moved to a cadence

`collect_produce` was the largest block of server time in the cycle, and it only
converts produce into coins - lifetime produce accrues whether or not the barn is
drained. Coins were never the constraint. `COLLECT_EVERY = 3` (with a backlog
override) cut a non-collecting run from ~216s to ~23s, freeing server capacity for
the thing that actually scores.

## Adoption: a separate agent, and the concurrency lesson

Adoption is latency-bound, not rate-bound: the cycle issued only ~27 calls in 150s
against a ~5/s ceiling, but had ~8s left for adopting (~14 animals). So adoption
became its own bounded agent.

Measured in isolation it saturates at the server: 4 workers 4.40/s, 8 workers
4.89/s, 16 workers 4.88/s - all with zero adoption failures. But the cost of
pushing hard never appeared on adoption; it appeared on the **cycle**, as 504
Gateway Timeout:

| setting | adopt rate | cycle |
|---|---|---|
| 8 workers @3.0/s | ~2.3/s | clean |
| 10 workers @3.5/s | ~2.8-3.1/s | occasional 504 |
| 12 workers @4.0/s | ~3.6/s | repeated 504s |
| 16 workers @5.0/s | collapsed | sustained 504s, growth stopped |

The 16-worker collapse had a specific cause worth remembering: **the sprint had no
lock.** A sprint can stall inside a slow call (120s timeout x 3 retries), so it
outlives its own `--max-seconds` deadline, which is only checked between calls.
launchd then starts the next sprint on schedule and they overlap - 16 workers
becomes 32, then 48. Herd growth fell to +25 animals in 13 minutes, worse than
running nothing.

**Fixed:** `expand.py` takes its own flock and arms a `SIGALRM` it cannot block
past. Settled at 10 workers / 3.5/s - one step below where the cycle starts
failing.

## Lessons, part two

7. **A bounded job needs a lock and a signal, not just a deadline check.** A
   deadline tested between calls does nothing when the process is blocked inside
   one, and an unlocked periodic job will happily overlap itself into a stampede.
8. **Load limits show up somewhere other than where you apply them.** Adoption
   never failed; the cycle did. Tune against the victim, not the actor.
9. **Re-measure a "ceiling" after fixing an unrelated bug.** The production
   plateau that justified freezing the herd was really the feed reserve.
10. **Spend the non-binding resource.** Collection made coins we already had, at
    the cost of the one resource that was scarce.

---

# Addendum 2: the second stall (runs 313-344)

Six hours after the recovery build the leader was pulling ahead again. Herd growth
had fallen to **+50 animals per run** while John went 45,179 -> 56,048. Three
distinct faults, none of them strategy.

## Fault 6 - the expansion agent died and nothing was watching it

The last sprint was killed mid-run (no `DONE` line) and the label ended up
unloaded. `launchctl print` returned nothing at all. The supervisor only ensured
the cycle and itself, so the one agent responsible for the score was the one agent
with no keeper.

**Fixed:** `scheduler.EXPAND_LABEL`, and `do_supervise()` now ensures it too.

## Fault 7 - the healer throttled us into irrelevance

Knobs on inspection: `adopt_cap: 50`, `rate_ceiling: 1.05`. The ledger shows a
pure ratchet driven by transient 504s:

```
5.00 -> 4.00 -> 3.20 -> 2.56 -> 2.05 -> 1.64 -> 1.31 -> 1.05 calls/s
adopt cap 400 -> 200 -> 100 -> 50
```

`HEAL_MAX_ATTEMPTS["transport"]` was 4 and `HEAL_ATTEMPT_RESET_RUNS` cleared the
counter after 4 quiet runs, so a flaky server could throttle indefinitely, while
`relax()` recovered at only 1.25x per quiet window against 0.8x per throttle.

**Fixed:** `MIN_CALLS_PER_SECOND` 0.5 -> 2.5, transport attempts 4 -> 1, relax
1.25x -> 1.6x.

## Fault 8 - the remedy was aimed at the wrong variable

The measurement that settled it, over ~3,000 spans:

| tool | calls | failures | median |
|---|---|---|---|
| `adopt_animal` | 1,483 | **0** | 2.6s |
| `list_farm` | 8 | 0 | 3.4s |
| `collect_produce` | 2 | **2** | 97.7s |
| `feed_animals` | 2 | **1** | 97.3s |

Failures are confined to the two whole-herd calls. A 504 there is about call
**weight**, not call **rate** - lowering the rate cannot make a 97s call shorter,
so every throttle was pure loss. Worse, an escaping `McpError` from the bulk feed
killed the whole run, so adopt/sell/buy_feed never executed: intervals stretched
to ~24 minutes and hunger climbed, which made the next run worse.

**Fixed:** a transport failure on the bulk feed is now recorded and the run
continues. Pinned by a regression test.

## Fault 9 - the collect override cancelled its own cadence

`COLLECT_EVERY` was set to 3, then 10, and collection still ran ~97s on *every*
run. The override was barn backlog, which is self-defeating: collecting rarely
grows the backlog, and the backlog then forces a collect. Meanwhile coins sat at
3.67M against a reserve need of 852k - 4.3x covered.

Uncollected produce is already scored, so a backlog costs nothing but unbanked
coins. **Fixed:** the override is now coin adequacy (`COLLECT_COIN_COVER = 2.0`),
which self-regulates because `reserve_target` grows with the herd. A generous
`COLLECT_READY_CEILING` bounds the unknown.

## Outcome

| | produce | herd | rate | coins |
|---|---|---|---|---|
| John | 7,963,106 | 56,057 (frozen) | 3,625/min | **38** |
| Nick | 5,443,030 | 32,508 (+7k/hr) | **6,098/min** | 3.58M |

John is bankrupt and cannot feed 56,057 animals; his per-animal rate has fallen
from 0.175 to 0.065/min - the same starvation spiral that cost us first place,
which is a fair reminder that the feed buffer is the whole ballgame. Our runs
341-344 are clean, intervals steady at ~8.7 min, collect skipping.

## Lessons, part three

11. **Whatever produces the score needs a keeper.** The expansion agent was the
    only unsupervised component and it was the one that silently died.
12. **An automatic throttle needs a floor you could still live at.** A 0.5/s floor
    meant "healing" could take the farm to a tenth of working speed.
13. **Diagnose before remediating.** One latency table showed adoption was
    blameless at 1,483/0 failures; months of throttling it was aimed at noise.
14. **A failure in one step must not delete the other thirteen.** Losing the feed
    call cost the adopt, sell and buy_feed steps too, which is what made it spiral.
15. **An override can silently repeal the rule it guards.** Backlog forcing
    collection made `COLLECT_EVERY` decorative; gate on the reason, not a symptom.
