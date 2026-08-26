# Epistemic control plane

Status: implemented additively in the working tree. The live release does not consume this design until a new immutable release is explicitly published.

## The boundary

The operator has three different kinds of truth. They are intentionally separate:

1. **Observations** are immutable facts about calls, states, interventions, and outcomes.
2. **Claims** are revisable interpretations of selected observations, with scope and falsifiers.
3. **Policy** is the explicitly promoted set of behavior-affecting parameters and claim dependencies.

Observations and claims may update automatically. Policy may not. A promoted policy is accepted only when semantic audits, claim dependencies, and the compiled-rule fingerprint agree.

## State files

All paths are relative to `state/` and can be redirected with `FARM_STATE_DIR` in tests.

- `observations.ndjson` — normalized run, sprint, intervention, and blind-window events.
- `claims.json` — current claim registry, atomically replaced.
- `claim_events.ndjson` — append-only claim transitions.
- `questions.ndjson` — one current row per question identity, atomically replaced.
- `question_events.ndjson` — append-only question transitions.
- `policy.json` — current explicitly promoted policy snapshot.
- `policy_events.ndjson` — append-only compile/promotion history.
- `provenance.ndjson` — pre-registered hypothesis, validation-result, and policy lineage.
- `champion.json` — accepted release and cumulative performance ratio.
- `efficacy_events.ndjson` — independent candidate acceptance/rejection outcomes.
- `audits.ndjson` — semantic and model-drift audit results.
- `experiments.ndjson` — bounded probe executions and outcomes.
- `segments/<ledger>/manifest.json` — ordered checksums for compressed immutable source segments.

`history.ndjson`, `tool_calls.ndjson`, `observations.ndjson`, and `intents.ndjson` remain logical source evidence. The supervisor may move old complete rows byte-for-byte into gzip segments while retaining a hot tail at the legacy path. No model summary replaces source rows, and transparent readers replay segments plus the tail.

## Lossless compaction

High-volume ledgers rotate after 64 MiB and retain 2,000 hot rows. Rotation is suspended while a release is provisional so rollback can never target an older reader before the segmented-ledger compatibility boundary is accepted. Writers and the compactor share a sidecar lock. Each closed segment records the uncompressed SHA-256, byte count, row count, first/last identity, and sequence in a manifest. A transaction record recovers an interrupted manifest/tail swap; unexpected bytes fail closed rather than being guessed at.

Compaction is valid only when replay is identical before and after rotation. `deploy/test_safety.py` asserts full-history equality, transparent tail reads, post-rotation appends, and checksum failure on tampering. Smaller registries are not compacted until every reader has migrated to the transparent interface.

## Observation schema

Every row carries:

- `schema_version`, `event_id`, `event`, and UTC `ts`
- actor context: `actor`, `run`, `sprint`, `step`, `worker`
- `policy_id` and `claim_registry_version`
- bounded event-specific `data`

MCP boundary spans inherit the same context. Worker threads explicitly bind it because Python context variables do not automatically cross threads.

## Claim schema

Every claim carries:

- stable `id`, `statement`, `category`, `status`
- `scope` and excluded regimes
- metric and estimator details
- structured `value`
- evidence references and immutable cohort fingerprints
- confidence score, level, and rationale
- first observed and last validated run
- refresh cadence and freshness state
- a concrete falsifier
- dependencies, supersession, and policy consumers

Supported statuses are `candidate`, `accepted`, `challenged`, `superseded`, and `retired`. Freshness is separate from status so an accepted but overdue finding cannot silently become false or disappear.

## Question schema

A question identity is the alert class plus its subject. Repeated alerts update the existing row instead of creating duplicates. Every row includes:

- stable `id`, `key`, class, subject, status, priority
- opening and last-seen run/timestamp, occurrence count
- hypothesis and the measurement that would settle it
- bounded coin/call/wall-time budget
- decision bundle and evidence references
- closing answer when `answered` or `abandoned`

Strategy alerts open questions. Only high-priority first occurrences page; repeats remain durable without repeated model spend.

## Policy schema

A snapshot contains:

- content-addressed `policy_id`
- compiled-rule fingerprint
- claim-registry semantic fingerprint and version
- behavior-affecting parameters
- parameter-to-claim dependency map
- invariants and objective
- compile audit and explicit promotion timestamp

Cycle and expansion record the same policy identity. A mismatch is visible and cannot silently rewrite behavior.

## Promotion rules

Promotion fails closed when:

- required claims are missing, challenged, retired, or superseded
- accepted contradictory claims coexist
- a policy parameter has no claim or safety-invariant owner
- the snapshot fingerprint does not match compiled rules
- estimator output and published claim status disagree
- a changed policy has no pre-registered hypothesis, null, falsifier, primary metric, or declared gain
- discovery and validation evidence overlap
- evidence is observational rather than an intervention, holdout, or direct mechanism
- the provenance graph contains a dependency cycle
- the recent policy sequence would oscillate A→B→A

Runtime remains deterministic: a missing or incompatible policy snapshot does not invent values. It uses the compiled rules and records the unversioned/mismatch condition for investigation.

## Research loop

Pure replay runs without MCP access and covers:

- stale strategy and idle-capital windows
- aging decision knobs and claims
- rival wake-up against each rival's own baseline
- output-model drift and regime changes
- counterfactual sweeps over growth threshold/window, threat share, and feed cooldown

Bounded probes are declared in a registry. Read-only probes may be scheduled under the existing farm lock. Mutating probes require explicit invocation and retain coin, call, and wall-time ceilings. The research agent pre-registers hypotheses by semantic fingerprint rather than model-written title. A hypothesis-linked probe cannot pass merely because its process exits zero: it must append a supported or falsified validation-result node with immutable evidence references. Policy promotion requires a matching durable supporting result. A failed hypothesis cannot be re-filed on unchanged evidence.

## Candidate efficacy

Release tests prove correctness; the canary and evaluator judge outcomes. Hard failures and losses beyond the 25% emergency floor revert immediately. At the complete candidate window, reliability releases must fit a 5% equivalence band and strategy releases must clear their pre-declared improvement with a 90% lower confidence bound. Accepted releases advance `champion.json`; projected cumulative performance below 95% of the anchor rejects the candidate, preventing a sequence of individually small regressions.

## Migration

1. Build and test registries from the full immutable history.
2. Bootstrap claims and compile a candidate policy without changing behavior.
3. Instrument cycle and expansion, preserving existing intent and tool-call logs.
4. Promote a compatible policy snapshot explicitly.
5. Publish through an immutable release only after parser, knowledge, safety, evidence, dashboard, topology, and trace gates pass.

Rollback is the previous `release` symlink target; epistemic files are additive and ignored by older releases.
