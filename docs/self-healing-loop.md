# The self-healing loop

Three agents were added so the operator can keep playing a game that keeps
changing, without a human in the loop. They are deliberately separated by
capability: one can only *look*, one can only *propose*, one can only *edit*.

```
  contract_watch (15m)  ---- work orders ---->  author_agent (10m)  ---->  release + canary
   sees the server                                edits the code              supervisor (60s)
   never edits                                    never decides strategy      reverts on regression

  research_agent (1h)  ------ work orders ---->  (same path)
   measures strategy
   never edits
```

## Why the split

A single agent that both detects a problem and rewrites code to fix it has no
check on its own judgement: a noisy detector becomes a code change, and a bad
code change hides the evidence that it was bad. Splitting detection from mutation
means every edit has a durable, reviewable reason attached (`state/workorders.ndjson`)
and can be traced back to the observation that caused it.

## contract_watch — what the server looks like

The loop previously noticed exactly one kind of server change: the sorted list of
tool *names*, compared against the previous run (`cycle.py`). That could not see a
new required argument, a renamed field, a narrowed enum, or a changed response
format — all of which break the farm on the next call rather than at detection.

`farm/contract.py` captures the whole observable contract and classifies each
change by whether it breaks **us**, which is decidable because `reliance()` reads
our own call sites with `ast`:

| severity | meaning | example |
|---|---|---|
| `breaking` | the farm will fail, or is failing | `feed_animals` gained a required arg we do not pass |
| `shape` | a response format moved under the parser | `hunger` became `fullness` |
| `opportunity` | new capability we are not using | a new tool, or a new enum value |
| `additive` | real but harmless | an optional arg on a tool we never call |
| `cosmetic` | wording only | a reworded description |

The same schema change is `breaking` on a tool we call and `additive` on one we do
not. Only the first three file work orders.

### Cost

One MCP call per scan (`tools/list`). Response shapes are derived from the raw
dumps the cycle already writes to `state/raw/latest/`, which are at most 180s
stale and cover 10 of the 15 tools. `list_farm` alone is ~20MB on the wire;
re-fetching it every 15 minutes to check its format would add real load for no new
information. POSTMORTEM-run377 is the reason that constraint is taken seriously.

### Noise control

Two measured false-positive sources were removed during development, both worth
knowing about because they will recur if the shape extractor is edited:

1. **Moods are data, not structure.** Keeping lowercase words verbatim in a line
   template meant `is delighted.` vs `is starving.` registered as a format change
   on almost every scan. Templates are now a pure skeleton (`<w+>`, `#`,
   punctuation, symbols); word identity lives in `vocabulary` and `numeric_labels`.
2. **Names have a variable word count.** `Gold Rush the beehive` and `Buzzy the
   beehive` are the same structure but tokenize to different lengths, so adopting
   an animal with a two-word name looked like drift. Consecutive words collapse to
   one `<w+>`.

Schema facts from `tools/list` are authoritative immediately. Response-shape drift
must be seen in `CONFIRM_SCANS` consecutive scans before it becomes a work order.

### The baseline is pinned, not advanced

`state/contract.json` is the contract the current code was *written against*, not
the last thing observed. It advances only when a change needs no code (absorbed
immediately) or when a fix ships. If it advanced on sight, a breaking change would
be reported once, and a single failed repair would leave the farm broken with
nothing left to re-detect it.

## author_agent — the only thing that edits code

```
budget -> claim -> stage -> patch -> gate -> publish -> canary
```

* **Staging.** Patches are applied to a temp copy, never speculatively to the live
  tree; launchd fires every 180s and would happily execute a half-applied change.
* **Two backends.** An argument rename is derivable from the diff, so it is done in
  Python with no model and no variance. The model is reserved for changes needing
  judgement (reparsing, fallbacks for a removed capability).
* **Strict edit format.** The model returns SEARCH/REPLACE blocks. A SEARCH string
  matching zero or several times is refused rather than guessed at.
* **It cannot weaken its own supervision.** `PROTECTED` refuses edits to
  `farm/canary.py`, `farm/workorders.py`, `farm/llm.py`, `farm/rules.py` (every
  budget lives there), `deploy/release.sh`, and both agents' own source. An agent
  that can rewrite its gates is not supervised. Orders needing protected files are
  escalated, not attempted.
* **Rationed.** Passes per day, dollars per day, minimum runs between changes, and
  a hard rule that no pass starts while a canary is still watching the last one —
  two unproven changes at once make an unhealthy canary impossible to attribute.

## canary — autonomy is safe because it is reversible

The gate matrix proves a change is *correct*. It cannot prove it is *good for the
score*, and the score is the only thing that decides the game. POSTMORTEM-run377
is exactly that failure: three individually reasonable throttles that together
nearly lost first place, with every suite green.

So a flip is provisional. The supervisor adjudicates on every 60s pass:

* `watching` — fewer than `CANARY_MIN_RUNS` post-flip runs
* `healthy` — producing at least as fast as the pre-flip baseline; clear it
* `regressed` — revert the pointer to the previous revision and record why

The band is loose (25%) on purpose: `produce_per_min` moves with herd size,
wolves, sickness and latency. The canary catches a real break — a parser returning
zero, a feed step that stopped running — not a few percent of throughput. A run
that produces nothing is decisive on its own and does not wait for an average. A
wolf attack is explicitly **not** a regression.

Safety properties: never revert to a pruned tree, never revert twice (a resolved
canary is cleared, so a flapping metric cannot walk the pointer backwards), and
never block the farm — every failure path leaves the pointer alone.

## research_agent — whether the strategy should change at all

Hypothesis sources, cheapest first:

1. **Unused capability.** Tools the server exposes that our code has never called.
   Free to detect, and the most concrete unexplored strategy space. At the time of
   writing: `gift` and `visit_farm` (`name_animal` is ignored as cosmetic — it
   cannot move lifetime produce).
2. **Parameter sensitivity.** `research.counterfactual_sweep` replays history
   against alternative constants. Zero MCP calls.
3. **Outcome correlation.** For sensitive parameters, compare realised produce rate
   across the runs an alternative would have changed. Correlational, not causal —
   it prioritises probes, it does not justify a change.
4. **Model hypotheses.** Capped at once per 24h and only when the free sources are
   exhausted. Must return falsifiable proposals with a bounded probe design.

A sensitive parameter with no claim backing its value becomes a durable *question*,
not a change. And the research agent may not edit constants directly: it files a
work order, the author implements it, the gates prove correctness, and the canary
proves it did not cost production. That gives strategy changes the property the
postmortems say they lacked — reversible before a human notices.

## Model access

`farm/llm.py` is the only module that talks to a model. It uses the Glean Desktop
`llm_proxy` gateway already authorised on this machine — an OpenAI-compatible
endpoint read from `~/.glean/agent/auth.json` — so there is no second credential to
provision or rotate. `cursor-agent` was installed and then removed once the
gateway was confirmed: it would have needed its own API key.

Two measured properties of that endpoint shape the client:

* It implements the **Responses** API. `/chat/completions` returns 404.
* **Reasoning tokens consume the output budget**, so `max_output_tokens` is
  generous and `truncated` is always checked. A half-written patch is more
  dangerous than no patch.

### Dormancy is a normal state, not an error

The token's `refresh` field is `__desktop_managed__`: only Glean Desktop can renew
it. If Desktop stays closed past expiry the gateway becomes unavailable, and every
caller must degrade rather than fail. The author agent falls back to mechanical
repairs and leaves the order open with a reason; the farm keeps feeding. `run.py
--llm-status` reports remaining token lifetime, because "the author did nothing" is
otherwise indistinguishable from "there was nothing to do".

This is the one real caveat on "100% headless": autonomous *code repair* depends on
Glean Desktop running often enough to keep the token fresh. Autonomous *play*,
*monitoring*, *detection*, and *rollback* do not — they are all deterministic
Python.

## Case study: the first thing the loop investigated

The research pipeline's first real finding was not a game change. It was a broken
gate, and it is worth recording because it shows what the evidence path is for.

Two release suites asserted that the bucket-smoothed herd/output fit had
`r > 0.95`. It had fallen to 0.925, which blocked every release. The tempting fix
is to lower the number. That would have been wrong twice over.

**The statistic was unstable.** On the same live cohort, r of the eleven bucket
means measured 0.925 unweighted, 0.954 weighted by sample count, 0.917 weighted by
its square root, and 0.992 with the newest band dropped. It moved 0.954 to 0.943 on
the arrival of a single run. A 0.95 threshold sat inside that spread, so the gate's
verdict was decided by arbitrary methodology rather than by the farm.

**The statistic pointed the wrong way.** On synthetic data with a true scaling
exponent of 0.70 -- hard saturation, the one condition that should stop adoption --
straight-line r reads **0.993**. The gate passed most confidently exactly when
growth had stopped paying. It could never have detected the thing it existed to
catch, because r falls for saturation and for super-linear growth alike.

The replacement is the scaling exponent from a log-log fit: `rate = a * herd^b`,
where `b = 1` is proportional, `b < 0.95` is saturation. It recovered 1.00, 0.90,
0.70 and 0.00 from synthetic cohorts, and it is stable against bucketing (1.162 on
641 raw samples versus 1.131 on the eleven bucket means). `output_model` gained a
`saturating` verdict, so saturation is now detectable for the first time.

The measured answer: **the farm is not saturating.** The exponent is 1.162, and the
power-law form fits much better (r=0.903 raw, 0.985 bucketed) than the line
(r=0.916). Growth still pays.

Three further errors surfaced on the way, all recorded in
`state/research_findings.ndjson`:

* **The straight line carried a negative intercept** (-7,583 units/min), predicting
  negative output below ~24k animals. It is a symptom of forcing a line through a
  convex relation, and nobody noticed because the model is only evaluated above 8k.
* **The regressor is a clock.** Herd size only ever grows, so improvements in our
  own collection behaviour load onto the herd coefficient. At a constant herd of
  ~120,127 across roughly a hundred runs, per-animal rate fell to 0.111 and then
  climbed to 0.194 -- a large move in the response with no move in the regressor.
  The association is therefore **not causally identified**, and the model now says
  so in a `confound` field rather than implying a scaling law.
* **The metric is collection, not production.** Lifetime produce only advances when
  we collect, so runs 752-757 scored a rate of exactly zero while 251,352 animals
  were alive and `ready_units` was 0. The claim wording overstated what is measured.

An exponent above 1 implies per-animal output *improves* with scale, which is not
plausible for animals. That is the confound showing through, so the exponent is an
upper bound on the scale benefit, not evidence of increasing returns. The honest
summary is that growth is not saturating; the exact size of the scale benefit is
not identified from observational history.
