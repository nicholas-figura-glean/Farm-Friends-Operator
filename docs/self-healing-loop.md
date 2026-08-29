# The self-healing loop

Three specialized change agents sit inside the nine-service unattended runtime.
They are deliberately separated by capability: one can only *look*, one can only
*propose*, one can only *edit*. `farm/control.py::SERVICES` is authoritative for
the complete process set.

```
  contract_watch (15m) ---- direct mechanic ----> capability-policy order --┐
       |                                                                    |
       `---- uncertain capability ----> bounded probe ---- supported result |
                                                |                           |
  research_agent (1h) --------------------------+---- implementation order --+
                                                                            v
                         author_agent (10m) -> gates -> release -> canary -> runtime
                                                                            |
                         farm/mechanics.py <--- literal policy declaration --'
                         validates contract, calls/cost, lock, and outcome
```

A work order or probe definition is not implementation. Directly documented
progression/crisis tools target the literal policy surface immediately. Uncertain
tools require a causal result, and the research agent consumes that result to file
a separate implementation order. The protected cycle/executor never executes code
from the editable policy file: it parses one literal assignment with `ast`, validates
it against the current contract, and owns the actual one-shot call and verification.

## Periodic whole-system review

Every 20 completed runs, the supervisor executes `farm/governance.py`. This is a
deterministic local-state review rather than a model call. It checks execution,
strategy position, all services, canary bounds, compaction, policy/claims,
dashboard freshness, question/probe closure, repair flow, and provenance. Missed
boundaries retry on the next supervisor pass. Reviews are append-only and compare
with the previous review so regressions and autonomous recoveries are explicit.
Known safe remedies reuse scheduler, compaction, stale-claim, and question/probe
paths; the reviewer cannot grant a model permission to rewrite the safety kernel.

## Why the split

A single agent that both detects a problem and rewrites code to fix it has no
check on its own judgement: a noisy detector becomes a code change, and a bad
code change hides the evidence that it was bad. Splitting detection from mutation
means every edit has a durable, reviewable reason attached (`state/workorders.ndjson`)
and can be traced back to the observation that caused it.

## capability policies — the formerly missing action handoff

The original split was safe but incomplete: every new tool became an opportunity
probe, mutating probes were non-autonomous, and no component converted a supported
result into executable behavior. The system could therefore detect a mandatory
mechanic forever while truthfully changing nothing.

`experiments/capability_policies.py` is the narrow editable seam. It contains only
literal records for direct progression and active-crisis mechanics. `farm/mechanics.py`
rejects executable syntax, stale description fingerprints, tools with required
arguments, routine/social tools, missing evidence, excessive call counts, excessive
coin fractions, and incomplete outcome checks. The cycle coordinates these actions
against the expansion lock and treats a failed post-state invariant as decisive
canary breakage.

The same classifier now drives both contract and research routing:

- direct server mechanism -> implementation policy order;
- uncertain objective effect -> bounded probe;
- supported probe result -> implementation policy order;
- active validated policy -> old probe-only order is retired.

Prestige efficacy is judged on the server's actual lexicographic objective. A
verified league increase with lifetime produce preserved and capacity nondecreasing
dominates the intentional herd reset; canary still requires post-reset production
to resume. Tier changes may retain the current major-league cap.

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
dumps the cycle already writes to `state/raw/latest/`, which are at most 300s
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
  tree; launchd fires every 300s and would happily execute a half-applied change.
* **Two backends.** An argument rename is derivable from the diff, so it is done in
  Python with no model and no variance. The model is reserved for changes needing
  judgement (reparsing, fallbacks for a removed capability).
* **Strict edit format.** The model returns SEARCH/REPLACE blocks. A SEARCH string
  matching zero or several times is refused rather than guessed at.
* **It cannot weaken its own supervision.** The model cannot edit the cycle,
  evidence readers, compactor, provenance graph, policy compiler, efficacy judge,
  canary, budgets, release script, work queue, or either agent's own source. A
  mechanically derived endpoint-keyword rename remains allowed through a separate
  narrow backend; it cannot make a judgement or rewrite arbitrary code. Orders
  needing protected model edits are safely contained on the last verified release.
* **Rationed.** Passes per day, dollars per day, minimum runs between changes, and
  a hard rule that no pass starts while a canary is still watching the last one —
  two unproven changes at once make an unhealthy canary impossible to attribute.

## canary — autonomy is safe because it is reversible

The gate matrix proves a change is *correct*. It cannot prove it is good for the
lexicographic objective: league level first, lifetime produce second.
POSTMORTEM-run377 is exactly that failure for the secondary score: three
individually reasonable throttles together nearly lost first place with every
suite green.

So a flip is provisional. The supervisor adjudicates on every 60s pass with two
separate gates:

1. **Safety brake.** A hard failure or a herd-normalized loss beyond the loose 25%
   emergency floor reverts immediately. This catches a confirmed three-run zero
   streak or a feed transport failure without mistaking one accelerated pre-tick
   cycle, wolves, sickness, or abduction for broken code.
2. **Efficacy.** After ten clean runs, reliability repairs must remain inside a
   5% operational equivalence band; ordinary strategy candidates must clear their
   pre-declared gain with a 90% lower confidence bound. A prestige is different:
   verified level growth with lifetime produce preserved and capacity nondecreasing is direct primary-
   objective evidence, but it is accepted only after post-reset production resumes.

Accepted releases advance a durable champion ledger. Each candidate projects its
measured ratio through the prior releases; crossing a cumulative 5% regression
budget reverts even when every individual loss was smaller. Prestige transitions
exclude both the reset row and first lagging leaderboard interval from rate
baselines—the retiring herd must never be divided by the replacement herd. A
reliability release armed during the next six recovery runs resolves inconclusive
once production resumes, keeping the fix without pretending the rebuilding herd is
an efficacy comparison.

Safety properties: never revert to a pruned tree, never revert twice, never
overlap candidates, and never let evaluator bookkeeping delay the runtime pointer
flip. Every release records its source SHA; source rollback applies the complete
base..candidate range in one inverse commit, not merely the final commit in a
multi-commit candidate.

## research_agent — whether the strategy should change at all

Hypothesis sources, cheapest first:

1. **Unused capability.** Tools the server exposes that our code has never called.
   Free to detect, and the most concrete unexplored strategy space. Directly
   documented progression/crisis mechanics route to implementation; uncertain
   tools such as `gift` retain the probe-first path (`name_animal` is cosmetic).
2. **Parameter sensitivity.** `research.counterfactual_sweep` replays history
   against alternative constants. Zero MCP calls.
3. **Outcome correlation.** For sensitive parameters, compare realised produce rate
   across the runs an alternative would have changed. Correlational, not causal —
   it prioritises probes, it does not justify a change.
4. **Model hypotheses.** Capped at once per 24h and only when the free sources are
   exhausted. Must return falsifiable proposals with a bounded probe design.

A sensitive parameter with no claim backing its value becomes a durable *question*,
not a change. Every proposal is pre-registered by semantic hypothesis, null,
falsifier, primary metric, discovery cohort, and expected gain. The same evidence
cannot both discover and validate it; observational association may prioritize a
probe but cannot promote behavior. Failed hypotheses require novel evidence, the
lineage graph must remain acyclic, and a policy sequence that returns A→B→A pauses.

The research agent may not edit constants directly: it files a lineage-carrying
work order, the author proves correctness, and the provisional deployment must
prove efficacy against the champion before promotion. That gives strategy changes
the property the postmortems say they lacked — reversible and independently judged
before a human notices.

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

## The isolation bug that kept recurring

Four bugs in this system had the same shape, and it is worth naming because three of
them were introduced while building the safety machinery itself:

**Code that believes it is isolated while reaching a module-level real path.**

| where | what it touched | consequence |
|---|---|---|
| `test_author.py` budget checks | `spend_today()` reads the real pass log | assertions silently tested the wrong branch, then failed once the agent had really run |
| `test_vcs.py` final assertion | required no `author/` branch in the real repo | would fail on every genuine authoring pass, and the pre-existing-failure attribution would then stand the agent down permanently |
| `test_contract_watch.py` | `journal.ALERTS` points at the real `state/alerts.ndjson` | 34 fictional "feed_animals now requires batch_id" breaking alerts reached live operations; the 60s supervisor escalated each with `needs_llm: true` |
| `canary.revert()` | did git work on the real repo despite a `project` argument | the author suite rewrote live `main` and reverted a real production commit, with all checks green |

The last one is the instructive one. `canary.revert()` accepted `project` so it could
flip a symlink inside a temp directory, and the git side effect quietly ignored it.
Worse, `vcs.revert_commit()` moves `main` through a temp worktree and `update-ref`,
which never touches the working tree -- and `deploy/release.sh` builds from the
working tree. So the deployment kept running correct code while `main` had silently
lost it. Nothing failed. That is precisely why it survived: the only symptom was that
the next release cut after any checkout would ship something different.

Three defences came out of it:

* a function whose job is to flip a symlink flips a symlink; the consequential side
  effect moved to `canary.record_inverse_commit()`, called from the one place that
  has actually decided to take it, and only when `project` is the real root
* `release.sh` warns when the working tree and `main` disagree, because a release is
  built from the working tree and `main` should describe what is running
* each of the four has a regression assertion now, and two of them assert about the
  *real* paths staying untouched rather than about the sandbox behaving

All four were found by running the system, not by reading it. The gate matrix was
green for every one of them.
