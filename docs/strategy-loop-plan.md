# Plan: self-audit and research loop

Status: **implemented and enforced by immutable-release gates.**
Written after the run 291 loss of first place and completed after the farm retook
rank 1 at run 416. The implementation is split across `farm/rules.py` (pure replay
detectors), `farm/questions.py`, `farm/research.py`, `farm/probes.py`, and the
versioned claims/policy layer documented in `docs/epistemic-control-plane.md`.

A deterministic governance review now verifies every 20 runs that aged strategic
questions are producing question-linked probe results. A stalled learning loop is
routed back into the existing `strategy_stale` probe path instead of waiting for a
person to notice it.

Every original acceptance target is replayed by `deploy/test_knowledge.py` against
the full ledger: stale strategy by run 80 with none in runs 1-50, John wake at run
241, strategy unreachable from remedies, one current question identity, and zero
MCP calls from counterfactual replay. Gap re-entry writes a normalized blind-window
observation instead of mixing a synthetic non-run row into `history.ndjson`.

## The post-mortem this comes from

John's observable trajectory, from `rivals.John` in history:

| runs | John lifetime produce | alerts raised |
| --- | --- | --- |
| 1-238 | flat at 45 | none |
| 239-290 | 45 → 53 → 63 → 84 → 118 → 139 | none |
| 290 → 291 | **19.3h blind gap** (no runs) | none until after |
| 291 | 3,081,121, #1, 42,859 animals | `RANK LOST`, `THREAT`, `has passed us` |

### Why every rival detector was silent

All three rival tests in `farm/watch.py` are ratios against **our** gain in the
same window:

- `THREAT` requires `gained >= rules.THREAT_SHARE * ours`. Ours was ~9,400 per
  window, so John needed ~4,700. He was gaining 3-7. The threshold was ~1,000x
  away for the entire climb.
- `has passed us` and `RANK LOST` are scoreboard readouts. They fire *after* the
  crossover, which is too late by construction.

Nothing measures a rival against **his own** baseline. A rival going 0.9/run to
5/run is a 5x regime change and 0.05% of our gain, so it sits below every
threshold until it doesn't.

### Extrapolation would NOT have saved us

Recorded so nobody rebuilds this. At run 274 John gained 0.53/min against our
1,873/min. Any crossover-ETA detector computes an ETA of centuries and stays
quiet. His real move was ~0.3/min → ~2,700/min, a ~9,000x discontinuity that
happened inside the window where we were not looking. **Do not build a
time-to-crossover forecast and call this fixed.** The rival signal worth having is
"a dormant rival woke up, go find out how" — reconnaissance, not regression.

### What actually lost the game was ours

The causal failure was `GROWTH_MIN_MARGINAL_GAIN = 0.10` freezing the herd at
11,869 for **246 runs** while coins climbed to 3.2M. Replaying a self-audit —
herd flat across 30 runs while coins accumulate faster than half of revenue —
**fires at run 80**: herd 11,869, coins 814,073, 229,611 coins banked over the
window against 382,368 revenue, John still asleep at 45.

That is **211 runs and roughly 40 hours of advance warning**, from data the loop
was already writing to its own history file. The bug was eventually found by a
human post-mortem after the loss. Nothing in the system could ask the question,
because every detector watches liveness and husbandry. Nothing watches whether a
standing decision is still paying.

## The three gaps

1. **No detector watches us.** A frozen strategy is invisible because nothing
   regressed — we merely stopped winning.
2. **Escalation has two dispositions and needs three.** `farm/heal.py` is binary:
   remedy, or escalate. Escalated alerts are acked and forgotten. `RANK LOST`
   fired identically for 7 consecutive runs (292-298) and produced no
   investigation.
3. **Research is off the cycle path.** `experiments/` is hand-run by design.
   `species_probe.py` closed the largest open question for ~9,500 coins against
   570k idle. With 3.2M idle coins, declining to spend on research is the
   expensive choice.

---

## A. Self-audit detectors

**Do first.** Pure functions over `state/history.ndjson`: no server calls, no
tokens, replayable against all 298 rows.

`watch.evaluate(row, prev)` only sees one prior row, so it cannot express any of
this. Extend it to `watch.evaluate(row, prev, history=None)` — optional and
defaulting to `None` so the ~12 existing `--self-test` call sites in `run.py`
(lines 492-733) keep working unchanged. `run.py:106` passes the trailing window.

New detectors, thresholds to live in `farm/rules.py` beside the existing ones:

- **`STRATEGY STALE`** — herd size flat (spread ≤0.5% of max) across
  `AUDIT_WINDOW_RUNS` while coin balance grows by more than
  `AUDIT_IDLE_SHARE` of windowed revenue, and the balance exceeds
  `AUDIT_MIN_IDLE_COINS`. Replay target: fires at run 80; must not fire during
  legitimate growth in runs 1-50.
- **`IDLE CAPITAL`** — balance above N multiples of per-run revenue while the
  adoption cap sits at `MAINTENANCE_ADOPTIONS`. Catches the same condition from
  the capital side when the herd is drifting slightly.
- **`KNOB AGE`** — any constant gating a decision that has not been re-litigated
  in `AUDIT_KNOB_MAX_AGE_RUNS`, reported as a hypothesis rather than a fact.
  Reads `state/growth.json` `changed_run` and the `knobs` recorded per history
  row; no new bookkeeping needed.

All three are **strategy class**: they open a question (section C). They must not
be healable — a knob that throttles the farm is exactly the wrong response.

## B. Rival wake detector

Rate over the trailing 6 runs against the same rival's own prior 12, with an
absolute floor to keep zero-baseline noise out:

```
recent >= RIVAL_WAKE_MIN_RATE and (base <= RIVAL_WAKE_FLAT_EPS or recent >= RIVAL_WAKE_RATIO * base)
```

Replay at `RIVAL_WAKE_MIN_RATE = 0.5/min`, `RIVAL_WAKE_RATIO = 3`:

- **John: first fires at run 241** (recent 0.500/min vs base 0.000/min, lifetime
  57) — roughly 13 hours before the loss.
- **Moe: fires at run 56** (1.923 → 55.142/min, lifetime 46,602). This is a
  genuine regime change and a **true** positive. It must open a question, not
  page anyone. Tune the ledger's noise budget, not this detector, if that proves
  chatty.

Output is a reconnaissance question — *how* is the rival growing: herd size,
composition, whether he is doing something we have ruled out — not a projection.

## C. Question ledger: `state/questions.ndjson`

The missing third disposition. Alerts are acked and disappear; questions persist
until answered.

Each record carries: opening run and ts, the alert that opened it, a hypothesis,
**the measurement that would settle it**, a cost bound, and status
(`open` / `probing` / `answered` / `abandoned`) with the evidence reference that
closed it.

- `farm/heal.py` gains a third return category alongside `healed` and
  `escalated`: `questions`. Strategy classes (`rank_lost`, `threat`,
  `overtaken`, plus the new `strategy_stale`, `idle_capital`, `knob_age`,
  `rival_wake`) open or update a question instead of only escalating. Preserve
  the existing rule that strategy is never healed.
- Re-alerting an already-open question bumps a counter rather than appending a
  duplicate. This is what stops the 7-identical-`RANK LOST`-runs pattern.
- `run.py --questions` prints the open set. Add it to `--heal-status` output and
  to the dashboard's Findings tab (`/api/evidence`).
- An escalation for a strategy class should carry the decision bundle, not one
  line of text: rival trajectory table, our standing knobs and their age, idle
  coins, open questions, and the counterfactual from section D. The difference
  between "wake up and go look" and "wake up and decide".

## D. Continuous counterfactual sweep

The run-46 sweep was a one-off. Make it recurring: replay the last N history rows
against perturbed values of the decision constants — `GROWTH_MIN_MARGINAL_GAIN`,
`THREAT_SHARE`, `FEED_COOLDOWN_RUNS`, the growth comparison window — and report
every case where the live value would have changed the decision.

Pure history replay, zero server calls, zero tokens. This is precisely how
`GROWTH_MIN_MARGINAL_GAIN = 0.10` would have announced itself: the sweep would
have shown that 0.02 kept adopting while 0.10 froze the herd, every run, for 246
runs. A constant whose neighbours disagree with it is a question.

Belongs in `farm/evidence.py` (it is derived evidence) with a `--sweep` flag, and
its output feeds the strategy escalation bundle.

## E. Promote probes to schedulable

A registry in `experiments/` where each probe declares hypothesis, bounded cost
(coins, calls, wall time), stop condition, and evidence destination. The
supervisor may run at most one probe per N runs when a question is open and the
budget allows, under the **existing** `state/.lock` so it can never double-act on
the farm.

Keep the `species_probe.py` precedent as the cost model: bounded, one batch,
recorded in `rules.py` so nothing re-litigates it later.

## F. Treat gap re-entry as reconnaissance

John's takeoff happened entirely inside 19.3h of no observation.
`SCHEDULE GAP` now reports this after the fact, but launchd `StartInterval` does
not fire while the Mac is asleep, so the blind window is a standing condition and
not a bug to fix.

On re-entry from a gap longer than `GAP_RECON_MINUTES`: read the board and run
the trajectory detectors against the **pre-gap** row before anything else, and
write a synthetic blind-window marker row so the gap cannot be silently averaged
into the rate series.

---

## Acceptance criteria

Same discipline the produce detector earned (two-window rule, three false
wake-ups removed from 56 runs). Every detector replays against all 298 rows in
`state/history.ndjson`, wired into `run.py --self-test` so a release cannot ship
a regression:

1. Rival wake: **zero** fires on John for runs 1-238; fires by run 241.
2. `STRATEGY STALE`: fires by run 80; **zero** fires during runs 1-50.
3. No new detector is reachable from any `heal.py` remedy that spends coins,
   adopts, sells, trades, or gifts.
4. A question opened twice by the same condition produces one ledger entry.
5. The counterfactual sweep makes zero MCP calls, provable from
   `state/tool_calls.ndjson`.

Sequencing: **A + D first** (free, pure, replay-testable), then C, then B, then
E, then F.

## The rule to codify

`README.md` already says: if you find yourself doing the same arithmetic or
judgement call every run, that belongs in `rules.py` or `watch.py`, not in a
prompt. The missing corollary:

> **If you find yourself never re-checking a decision, that decision belongs in
> the question ledger.**

A model only ever sees what the alert queue hands it. This has to be
deterministic Python — a better prompt would not have caught John.
