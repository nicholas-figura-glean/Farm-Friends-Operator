"""Settled strategy rules, previously carried in the LLM prompt.

Everything here is pure arithmetic over parsed state: no I/O, no model.
Change a rule here and every future cycle follows it, at zero token cost.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

# --- market -----------------------------------------------------------------
ANIMAL_COST = {"chicken": 10, "pig": 20, "sheep": 20, "beehive": 25, "cow": 30}
ITEM_VALUE = {
    "egg": 2,
    "wheat": 2,
    "corn": 3,
    "wool": 4,
    "milk": 5,
    "honey": 6,
    "pumpkin": 6,
    "truffle": 8,
    "feed": 1,
    "coin": 1,
}
FEED_COST = 1

# --- engine choice (settled by large-sample measurement) --------------------
PRIMARY_KIND = "chicken"
MEASURED_UNITS_PER_COIN_TICK = {"chicken": 0.0895, "beehive": 0.0235, "pig": 0.0210}

# Adoption is restricted to chickens by comparative economics, not by the old
# per-farm-cap hypothesis. The run-50 mixed-species probe showed alternatives
# were negligible in observed collection, while the later full-history
# leaderboard fit showed total score output continued to scale with herd size.
# These are separate claims in state/claims.json: chicken is the best engine,
# and healthy marginal herd output remains positive. Neither implies that an
# animal added past 8,000 produces nothing.
ADOPTABLE_KINDS = ("chicken",)   # and only while growth_verdict() says it pays

# Crops were banned on prompt-era reasoning (worse units/coin than chickens).
# Also tested at run 50: one wheat, one corn and one pumpkin plot planted. 27
# minutes later all three still read "0% grown, about 15/20/30 min left" - the
# timers do not advance, so a plot is a coin sink that never yields produce.
# plant() does create unlimited extra plots, so this would have scaled badly.

# --- husbandry --------------------------------------------------------------
# REVERSED after measurement. The reasoning that produced the cooldown was that
# feed_animals('all') costs ~1.1-1.5 feed per fed animal regardless of hunger, so
# feeding less often had to be cheaper. It is cheaper, and it is much worse:
# production degrades sharply as time since the last bulk feed grows.
#
#   run  fed   u/chicken/min   revenue   feed cost   net per chicken
#   14   yes   0.294           15956     5171        1.96
#   15   no    0.177           10842     1404        1.52
#   16   no    0.056            3886     1216        0.39
#
# Feed is not a cost to minimise, it is the input that production scales with:
# skipping two feeds cut net revenue per chicken by 80%. So feed every run, with
# a trigger low enough that any hunger at all qualifies.
FEED_AT_HUNGER = 6           # effectively "feed whenever anyone is hungry"
FEED_URGENT_HUNGER = 60      # urgent bulk-feed trigger; never fan out by animal id
FEED_COOLDOWN_RUNS = 0       # no cooldown: see the table above
HUNGER_STOP = 70             # production stops entirely here
HUNGER_ALARM = 66            # only alarm when genuinely close to the stop

# RAISED 2 -> 50 after the run 291 starvation (see below). 2/animal was sized
# for the steady state only: at 11,869 animals it bought 23,753 feed, and the
# herd burns ~1,200 feed/minute, so the whole reserve was ~20 minutes of buffer.
# The loop is scheduled by launchd StartInterval, which does NOT fire while the
# Mac is asleep. A 19.3-hour sleep gap between run 290 and run 291 therefore
# starved the herd within the first ~20 minutes: hunger hit 100, crossed
# HUNGER_STOP, and production stopped for ~19 hours while 2.9M coins sat idle.
# Feed costs 1 coin, so the entire outage was avoidable for ~600k coins.
#
# 50/animal at the current herd is ~593k feed, ~8 hours of buffer, ~17% of the
# coin balance. FEED_BUFFER_MIN_MINUTES is the real intent; the per-animal
# number is the cheap approximation the plan arithmetic uses.
# 30/animal is ~290 minutes of runway at the measured burn, comfortably above
# FEED_BUFFER_MIN_MINUTES while costing half of what 50/animal locked up. Feed
# capital that is not needed for the buffer belongs in animals, which is what
# actually scores.
FEED_PER_ANIMAL_RESERVE = 30
FEED_BUFFER_MIN_MINUTES = 240   # alert if the reserve is worth less than this
FEED_BURN_PER_ANIMAL_MIN = 0.104  # measured: ~1,235 feed/min at 11,869 animals
# Daily risk events can charge vet/repair bills automatically. Keep liquid coins
# after every expansion so an event cannot turn an otherwise healthy farm cashless.
RISK_COIN_RESERVE = 5_000

# How far under the reserve target we tolerate before treating it as an incident.
#
# The target is recomputed from the FINAL animal count, but buy_feed was sized
# from an earlier count, and experiments/expand.py adopts throughout the run. So
# growing fast structurally leaves us a few hundred feed short of a target that
# moved underneath us. That is not a starvation signal, and the remedy for this
# alert class halves the adopt cap.
#
# Runs 337-347: short by 390 of 789,135 (0.05%) cut the adopt cap 400 -> 50 over
# four runs while the runway was a healthy 288 minutes the whole time. 15 of 20
# firings across 120 runs were false alarms.
#
# Sized to absorb one cycle's worth of concurrent adoption: ~1,100 animals at
# 30 feed each. The runway detector (FEED_BUFFER_MIN_MINUTES) remains the real
# guard, because it is denominated in the unit of the threat.
FEED_RESERVE_TOLERANCE_MIN = 40000
FEED_RESERVE_TOLERANCE_FRACTION = 0.02

# --- production measurement -------------------------------------------------
# Production is NOT a global 5-minute tick: the event feed shows per-animal
# timers, so units collected depends on how long it has been since the last
# collection. Everything is therefore measured per chicken per MINUTE.
# Observed healthy range across runs 2-6: 0.09-0.63 units/chicken/min.
# Lower bound raised to catch hunger-driven degradation: a fed herd runs
# 0.28-0.40, and 0.056 was the signature of two skipped feeds.
UNITS_PER_CHICKEN_MIN_BAND = (0.10, 1.00)
MIN_INTERVAL_FOR_RATE_CHECK = 4.0   # minutes; shorter samples are meaningless
ZERO_COLLECT_RUNS_TO_ALARM = 3      # consecutive empty collections before alarm

# --- what actually scores ---------------------------------------------------
# Units collected is NOT the score. Two measured facts make it a lagging and
# sometimes badly wrong proxy:
#   1. Lifetime produce accrues as animals produce, not when we collect it. Run 25
#      gained 41,207 produce while a collect call returned 572 units.
#   2. collect_produce answers "Nothing to collect right now ... or make sure your
#      animals are fed" while any hunger is present, and the produce then appears
#      in the barn after feed_animals. Runs 50 and 51 recorded collected={} and
#      then sold 11,597 and 7,934 eggs in the same run.
# So the authoritative measure of "are we still producing" is the leaderboard
# produce delta per minute, and that is what the detector below judges. It also
# catches the one failure that can actually cost the game: the herd reaching
# hunger 70, where production stops entirely while collection still looks normal.
# Floor is ~40% of the measured 1,550/min plateau at this herd size, but it must
# scale: an early 1,177-animal farm produced ~180/min and a fixed floor would
# have called that an incident every run. Measured healthy output is ~0.135
# produce/animal/min, so 0.05 is a ~third of that - low enough never to fire on
# ordinary variation (observed 927-2,075/min at 11.9k animals), high enough to
# catch production actually stopping.
PRODUCE_PER_MIN_FLOOR = 600.0
PRODUCE_FLOOR_PER_ANIMAL = 0.05
MIN_INTERVAL_FOR_PRODUCE_CHECK = 3.0

# A low measured rate has two very different causes and only one is an incident:
#   1. production degraded (hunger, a server change)  -> must alarm
#   2. there was simply nothing left to collect       -> must NOT alarm
# Case 2 dominated at herd scale and generated an alert nearly every run: runs
# 27-29 collected 62-760 units but ended with only 666-3303 units still ready,
# i.e. the herd was drained, not broken. Backlog is the discriminator, so the
# throughput detector consults it before crying wolf.
READY_BACKLOG_FLOOR = 2000          # absolute backlog considered "drained"
READY_BACKLOG_PER_ANIMAL = 0.5      # plus half a unit per animal
# The server now performs collection as one constant-time bulk operation. Drain
# the barn every run to minimize spoilage exposure; never loop or parallelize.
COLLECT_EVERY = 1
MAX_COLLECT_PASSES = 1

# --- expansion --------------------------------------------------------------
# Growth must leave enough of the 150s cycle budget for collection and feeding.
# At herd scale, a 2,000-call plan monopolizes the cycle, causes launchd skips,
# and turns a three-minute collection cadence into 15-30 minute gaps.
#
# This is the execution-budget ceiling only. The economic decision is separate
# and lives in growth_verdict(): stop only on fresh, regime-filtered evidence
# that marginal lifetime-score output is no longer positive.
MAX_ADOPTIONS_PER_RUN = 400
# Raised 25 -> 400 in the run 291 recovery. 25/run was sized when the herd was
# a few hundred animals; it is now the binding constraint on catching up.
# adopt_animal is one call per animal and the limiter allows ~4.5-5 calls/s, so
# a ~90s adoption window supports ~400. adopt_chickens() still stops on the
# wall-clock deadline, so this is a ceiling and never a mandate.
# launchd cadence is 300s. Phase timing showed ~65-75s of every run is
# inherent server work (collect ~30-40s and bulk feed ~35s at this herd size)
# that no amount of adoption parallelism can shrink. Keep the full cycle below
# the scheduler interval so skipped launches do not stretch feed/collection
# intervals and depress production.
# MEASURED at ~17,000 animals: collect_produce takes ~75s and feed_animals("all")
# ~83s. Those two calls alone are ~158s, so a 150s budget and a 170s watchdog
# could not fit one honest cycle - runs 304-305 died on "exceeded 170s hard
# timeout", which silently costs a feeding. Both calls scale with herd size, so
# the budget has to be sized from the server's real latency, not from a tidy 180s
# cadence. Cadence is now 300s with a 260s budget: still one feed every ~5
# minutes, which measured hunger 0 at 5.75-9.1 minute intervals.
CYCLE_BUDGET_SECONDS = 260
# The scheduler cadence, restated here so detectors can reason about gaps.
# launchd StartInterval does not fire while the machine is asleep, so a gap is
# silent: run 290 -> 291 was 19.3 hours and nothing complained until the herd
# had already starved past HUNGER_STOP and the rank was lost.
CYCLE_INTERVAL_SECONDS = 300
RUN_GAP_ALARM_MINUTES = 30
# Call-rate ceiling, established by measurement from two directions:
#  - throughput: 6 workers gave 0.68s mean adopt latency and 4.19 calls/s;
#    8 workers gave 1.64s and 4.67 calls/s. The server queues instead of
#    scaling, so asking for more than ~5/s buys nothing and only adds load.
#  - errors: runs at 6/s were clean, 8/s produced transport retries.
# Note this file has two writers - a human/agent editing it directly and the
# hourly supervisor automation reacting to alerts. releases/ is the audit trail.
MAX_CALLS_PER_SECOND = 5.0      # global, shared across all workers
SLOW_CALL_SECONDS = 1.2         # mean adopt SERVICE latency that triggers a courtesy ease-off

# Measured server service time for a single call with the limiter wide open and
# no concurrency: median 0.670s over 6 samples (2026-08-23, herd 61,586).
# SLOW_CALL_SECONDS must stay clear of this, or the courtesy ease-off fires during
# normal operation and throttles the only call that scores. It previously fired
# every run because the measurement included the rate-limiter wait (~workers/rate
# = 2.4s at 6 workers and 2.5/s) rather than the server's own latency.
MEASURED_SERVICE_SECONDS = 0.670
# Sized against the 300s cadence and the measured ~158s of unavoidable server
# work (collect ~75s + bulk feed ~83s), with headroom for reads and adoption.
CYCLE_HARD_TIMEOUT = 285        # self-kill before the next 300s run is due
# RAISED 0.5 -> 2.5. A 504 Gateway Timeout on this server is about call WEIGHT,
# not call rate: list_farm is ~1.6MB at 25,000 animals and feed_animals("all")
# touches every animal. Slowing the call rate cannot make a heavy call lighter,
# so throttling was treating the wrong variable - and it ratcheted:
# 5.00 -> 4.00 -> 3.20 -> 2.56 -> 2.05 -> 1.64 -> 1.31 -> 1.05/s over a few
# hours, which crippled adoption while John grew 45,179 -> 56,048 animals.
# 2.5/s is a floor that still lets the farm compete if healing misfires again.
MIN_CALLS_PER_SECOND = 2.5      # floor after repeated server errors
TRANSPORT_RETRY_ALARM = 5       # single retries are noise, not incidents
# The ratio clause used to apply at any call volume, so a single retry in a
# 35-call run (2.9%) looked like an incident and also convinced the cycle that
# the previous run was "unclean", which pinned the rate limiter at the 0.5/s
# floor for runs on end. A ratio is only meaningful once there are enough calls.
TRANSPORT_RATIO = 0.02
TRANSPORT_RATIO_MIN_CALLS = 100
ADOPT_WORKERS = 6               # more workers only queue; see the note above
ADOPT_PARALLEL_THRESHOLD = 20   # below this, serial is simpler and fast enough
VERIFY_EVERY = 6                # full re-read cadence when nothing looks off
JOURNAL_EVERY = 20              # journal entries are generated in Python

# --- trades -----------------------------------------------------------------
RIVALS = ("Guillermo G.", "John", "Neill", "Moe", "Aaron")
PREFERRED_TRADE_TARGETS = ("Guillermo G.", "Neill", "Aaron", "John", "Moe")
MAX_OPEN_OFFERS = 3
OFFER_FEED_QTY = 5
OFFER_COIN_WANT = 10
OFFER_MIN_AGE_MINUTES = 60  # never withdraw an unanswered offer younger than this
DECLINE_PAUSE_THRESHOLD = 2

# --- prohibitions -----------------------------------------------------------
FOOD_CROPS_BANNED = True
NEVER_SELL = ("feed",)
THREAT_SHARE = 0.50  # rival units/tick at or above this share of ours escalates

# --- epistemic self-audit ---------------------------------------------------
# These detectors ask whether a standing decision is still paying. They operate
# only on the immutable history ledger and open questions; none is reachable
# from a healing remedy.
AUDIT_WINDOW_RUNS = 30
AUDIT_HERD_SPREAD_FRACTION = 0.005
AUDIT_IDLE_SHARE = 0.50
AUDIT_MIN_IDLE_COINS = 500_000
AUDIT_IDLE_REVENUE_MULTIPLE = 20.0
AUDIT_KNOB_MAX_AGE_RUNS = 40
RIVAL_WAKE_RECENT_INTERVALS = 6
RIVAL_WAKE_BASE_ROWS = 12
RIVAL_WAKE_MIN_RATE = 0.5
RIVAL_WAKE_RATIO = 3.0
RIVAL_WAKE_FLAT_EPS = 0.05
GAP_RECON_MINUTES = 30
CLAIM_REFRESH_RUNS = 20
RESEARCH_AUDIT_RUNS = 10
# Broad autonomous operating review; aligned with the journal/claim evidence window.
GOVERNANCE_REVIEW_RUNS = JOURNAL_EVERY
PROBE_MIN_INTERVAL_RUNS = 20
# A repeated alert is not novel evidence immediately after its probe settled.
QUESTION_REOPEN_RUNS = 20

# --- self-healing -----------------------------------------------------------
# The supervisor remediates recurring alert classes in Python instead of waking
# a model. Every knob below can only make the loop MORE conservative or make it
# do bounded extra work, so a healing decision can never be catastrophic. Knobs
# relax one step per clean run so a transient incident does not throttle the
# farm forever.
HEAL_ENABLED = True
HEAL_MAX_ATTEMPTS = {
    # Transport healing is capped hard. Each 504 burst used to be allowed 4 fresh
    # throttles, and HEAL_ATTEMPT_RESET_RUNS cleared the counter after 4 quiet
    # runs, so a flaky server could ratchet the rate ceiling down indefinitely.
    # Throttling never fixed the 504s (they are heavy-call timeouts), so one
    # courtesy step per window is all it gets.
    "transport": 1,
    "backpressure": 2,
    "throughput": 3,
    "hunger": 3,
    "feed_reserve": 3,
    "zero_collect": 2,
    "adopt_failures": 2,
    "stale_loop": 5,
}
HEAL_ATTEMPT_RESET_RUNS = 4     # a class quiet this long starts over

# How long an alert stays actionable. An alert describes one instant; a remedy is
# applied to the farm as it is NOW, so evidence has to expire or the healer acts
# on conditions that no longer hold. Unbounded, the queue re-threw an alert from
# run 28 at a farm 350 runs later and throttled the call rate on it - twice,
# after both throttles had already been reset, because the justification never
# aged out. At a ~5 minute cadence this is roughly 24 runs of history.
ALERT_STALE_HOURS = 2.0
# An alert older than this describes a farm that no longer exists. Healing reads
# the LATEST row, so acting on a stale alert throttles current, healthy state:
# see the run 291 hunger alert that cut the adoption cap at run 295.
HEAL_ALERT_STALE_RUNS = 5
HEAL_SCHEDULER_MAX_REPAIRS_PER_HOUR = 4

# --- LLM cost model (estimates, for visibility only) ------------------------
# The whole point of the deterministic loop is that routine runs cost nothing.
# These figures price the exception path so the saving is measurable rather than
# asserted. Tokens are estimated from payload size; prices are per million.
CHARS_PER_TOKEN = 4.0
LLM_INPUT_COST_PER_MTOK = 3.00
LLM_OUTPUT_COST_PER_MTOK = 15.00
LLM_ESCALATION_CONTEXT_TOKENS = 9000   # system + runbook + tool defs per wake-up
LLM_ESCALATION_OUTPUT_TOKENS = 700     # typical investigation + reply

# -- headless authoring (farm/llm.py) ---------------------------------------
# Model preference chain for the author agent. The gateway roster changes without
# notice, so this is a chain and not a constant: pick_model() verifies each id
# against the live list and refuses to author with anything unlisted here.
# Dated stable ids come first; the named variants are ambiguous about capability.
LLM_AUTHOR_MODELS = (
    "gpt-5.5-2026-04-23",
    "gpt-5.4-2026-03-05",
    "gpt-5.6-terra",
    "gpt-5.1-2025-11-13",
    "gpt-5-2025-08-07",
)
# An author pass is expensive relative to a 0-token cycle, so it is rationed.
# These bound blast radius, not quality: a change that cannot be described in
# this many files is a change a human should be reading.
AUTHOR_MAX_FILES_PER_ORDER = 3
AUTHOR_MAX_ATTEMPTS_PER_ORDER = 2      # one retry with the gate failure fed back
AUTHOR_MIN_INTERVAL_RUNS = 5           # never author on consecutive cycles
# Eight passes is the normal exploration quota, not an operator dependency. Urgent
# repairs may draw from a bounded surge pool; aged backlog earns surge capacity in
# proportion to queue pressure. Cost, canary, interval, and gate limits remain hard.
AUTHOR_MAX_ORDERS_PER_DAY = 8
AUTHOR_MAX_SURGE_ORDERS_PER_DAY = 24   # absolute 24h blast-radius ceiling
AUTHOR_BACKLOG_SURGE_AGE_SECONDS = 4 * 60 * 60
AUTHOR_SURGE_PASSES_PER_QUEUED_ORDER = 2
AUTHOR_MAX_COST_USD_PER_DAY = 5.00     # hard spend ceiling for authoring

# -- canary (farm/canary.py) ------------------------------------------------
# Autonomous publishing is only safe if it is reversible without a human. A flip
# is therefore provisional: the new release must prove it did not slow the farm
# down before the canary is cleared.
#
# The tolerance is loose on purpose. produce_per_min is genuinely noisy -- it
# moves with herd size, wolf and sickness events, and server latency -- so a tight
# band would revert good releases on ordinary variance. The canary exists to
# catch a real break (a parser returning zero, a feed step failing), not to
# micro-optimise throughput.
CANARY_MIN_RUNS = 3                    # never judge a release on fewer runs
CANARY_MAX_RUNS = 10                   # decide by here, or clear it and move on
CANARY_REGRESSION_TOLERANCE = 0.25     # >25% slower than baseline is a revert
CANARY_BASELINE_RUNS = 6               # pre-flip safety window used for fast rollback
# A release that prevents completed runs cannot wait forever for run-count evidence.
CANARY_STALL_SECONDS = 30 * 60

# Safety and efficacy are deliberately different decisions. Reliability repairs
# need equivalence; strategy candidates must prove a pre-declared gain. The
# champion ledger carries a cumulative budget so repeated small losses cannot hide
# below the per-release emergency threshold.
EFFICACY_BASELINE_RUNS = 12
EFFICACY_MIN_RUNS = CANARY_MAX_RUNS
EFFICACY_CONFIDENCE_Z = 1.645           # two-sided 90% normal interval
STRATEGY_MIN_IMPROVEMENT = 0.01         # at least +1%, including the lower bound
RELIABILITY_EQUIVALENCE_TOLERANCE = 0.05
CUMULATIVE_REGRESSION_BUDGET = 0.05


def feed_reserve_target(animal_count: int, committed_feed: int) -> int:
    """Feed we want on hand: FEED_PER_ANIMAL_RESERVE each plus open offers."""
    return FEED_PER_ANIMAL_RESERVE * animal_count + committed_feed


def feed_buffer_minutes(feed: int, animal_count: int) -> float:
    """How long the feed on hand lasts at the measured burn rate.

    This is the number that actually mattered in the run 291 post-mortem: the
    old reserve looked healthy as an absolute count (23,753) while being only
    ~20 minutes of runway. A buffer expressed in minutes can be compared
    directly against how long the loop might be asleep.
    """
    if animal_count <= 0:
        return float("inf")
    burn = FEED_BURN_PER_ANIMAL_MIN * animal_count
    if burn <= 0:
        return float("inf")
    return feed / burn


# feed_animals and collect_produce are now constant-time bulk operations. Their
# failures are ordinary transport failures; there is no heavy-herd exemption.
HEAVY_HERD_TOOLS: Tuple[str, ...] = ()


def core_transport_errors(by_tool: Optional[Dict[str, int]], total: int = 0) -> int:
    """Transport retries that should participate in normal health handling."""
    if not by_tool:
        return int(total or 0)
    return sum(int(n or 0) for n in by_tool.values())


def transport_trouble(retries: int, calls: int) -> bool:
    """Whether transport retries are an incident rather than network noise.

    Shared by the detector and the cycle so that "clean enough to speed back up"
    and "quiet enough not to alarm" can never drift apart again.
    """
    retries = int(retries or 0)
    calls = int(calls or 0)
    if retries >= TRANSPORT_RETRY_ALARM:
        return True
    return calls >= TRANSPORT_RATIO_MIN_CALLS and retries > TRANSPORT_RATIO * calls


def backlog_drained(ready_units: int, animal_count: int) -> bool:
    """True when there was essentially nothing left to collect.

    A low units/chicken/min reading is only evidence of a production problem if
    produce is actually piling up. If the barn is drained, the loop is keeping
    up and the low reading is an artifact of a short or stretched interval.
    """
    allowance = max(READY_BACKLOG_FLOOR, READY_BACKLOG_PER_ANIMAL * (animal_count or 0))
    return (ready_units or 0) <= allowance


def produce_floor(animal_count: Optional[int]) -> float:
    """Minimum believable score rate for a farm of this size."""
    scaled = PRODUCE_FLOOR_PER_ANIMAL * float(animal_count or 0)
    return min(PRODUCE_PER_MIN_FLOOR, scaled)


def produce_rate_trouble(
    produce_delta: Optional[int],
    minutes: Optional[float],
    animal_count: Optional[int] = None,
) -> Optional[float]:
    """Score rate over a window, returned only when it is genuinely too low.

    None means "nothing to say": either the window is too short to judge, or the
    farm is producing normally. A float is the offending rate, and it means
    production itself has degraded - the only class of failure that can actually
    lose the game, because it is the one thing no amount of collecting fixes.
    """
    if produce_delta is None or minutes is None:
        return None
    if minutes < MIN_INTERVAL_FOR_PRODUCE_CHECK:
        return None
    rate = float(produce_delta) / float(minutes)
    return rate if rate < produce_floor(animal_count) else None


def adoptable(kind: str) -> bool:
    """Measured: adopting anything outside ADOPTABLE_KINDS buys feed cost only."""
    return kind in ADOPTABLE_KINDS


def _clamp(value, low, high, default, cast=float):
    try:
        value = cast(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def rate_ceiling(knobs: Dict[str, object]) -> float:
    """Healing may lower the call-rate ceiling, never raise it above measurement."""
    return _clamp(
        (knobs or {}).get("rate_ceiling", MAX_CALLS_PER_SECOND),
        MIN_CALLS_PER_SECOND,
        MAX_CALLS_PER_SECOND,
        MAX_CALLS_PER_SECOND,
    )


def adopt_cap(knobs: Dict[str, object]) -> int:
    return int(
        _clamp(
            (knobs or {}).get("adopt_cap", MAX_ADOPTIONS_PER_RUN),
            1,
            MAX_ADOPTIONS_PER_RUN,
            MAX_ADOPTIONS_PER_RUN,
            int,
        )
    )


def adopt_worker_count(knobs: Dict[str, object]) -> int:
    return int(
        _clamp((knobs or {}).get("adopt_workers", ADOPT_WORKERS), 1, ADOPT_WORKERS, ADOPT_WORKERS, int)
    )


def collect_passes(knobs: Dict[str, object]) -> int:
    return int(_clamp((knobs or {}).get("collect_passes", 1), 1, MAX_COLLECT_PASSES, 1, int))


def collect_fits_budget(animals: int) -> bool:
    """Bulk collection is constant-time and fits at every herd size."""
    return True


def should_collect(
    run_no: int,
    ready_units: int,
    animals: int,
    coins: Optional[int] = None,
    reserve_target: Optional[int] = None,
) -> bool:
    """Always drain once per run to reduce spoilage exposure."""
    return True


def escalation_cost(payload_tokens: int = 0) -> Tuple[int, int, float]:
    """Estimated (input, output, usd) for one LLM wake-up carrying payload."""
    tokens_in = int(LLM_ESCALATION_CONTEXT_TOKENS + max(0, payload_tokens))
    tokens_out = int(LLM_ESCALATION_OUTPUT_TOKENS)
    usd = (
        tokens_in / 1_000_000.0 * LLM_INPUT_COST_PER_MTOK
        + tokens_out / 1_000_000.0 * LLM_OUTPUT_COST_PER_MTOK
    )
    return tokens_in, tokens_out, round(usd, 6)


def should_feed(
    max_hunger: int,
    any_called_hungry: bool,
    runs_since_feed: int = 99,
    cooldown_runs: Optional[int] = None,
) -> bool:
    """Feed on urgency always, or routinely once the cooldown has elapsed.

    `cooldown_runs` exists for pure counterfactual replay. Runtime callers omit
    it and therefore consume the promoted compiled constant.
    """
    cooldown = FEED_COOLDOWN_RUNS if cooldown_runs is None else max(0, int(cooldown_runs))
    if max_hunger >= FEED_URGENT_HUNGER or any_called_hungry:
        return True
    return max_hunger >= FEED_AT_HUNGER and runs_since_feed >= cooldown


def sell_plan(inventory: Dict[str, int]) -> List[Tuple[str, int]]:
    """Sell every saleable good, most valuable first. Never sell feed."""
    plan = []
    for item, qty in inventory.items():
        if item in NEVER_SELL or qty <= 0 or item not in ITEM_VALUE:
            continue
        plan.append((item, qty))
    plan.sort(key=lambda p: ITEM_VALUE.get(p[0], 0) * p[1], reverse=True)
    return plan


def expansion_plan(
    coins: int,
    animal_count: int,
    feed: int,
    committed_feed: int,
    cap: int = None,
) -> Dict[str, int]:
    """Jointly solve feed top-up and chicken count.

    Each new chicken consumes animal cost plus its feed reserve. Expansion also
    preserves RISK_COIN_RESERVE for automatic vet and repair bills.
    `cap` lets the supervisor throttle growth without editing strategy.
    """
    cost = ANIMAL_COST[PRIMARY_KIND]
    spendable = max(0, int(coins) - RISK_COIN_RESERVE)
    limit = MAX_ADOPTIONS_PER_RUN if cap is None else max(0, min(int(cap), MAX_ADOPTIONS_PER_RUN))
    best = {"adopt": 0, "buy_feed": 0, "cash_reserve": RISK_COIN_RESERVE}
    for n in range(min(limit, spendable // cost), -1, -1):
        target = feed_reserve_target(animal_count + n, committed_feed)
        need_feed = max(0, target - feed)
        if need_feed * FEED_COST + n * cost <= spendable:
            best = {"adopt": n, "buy_feed": need_feed, "cash_reserve": RISK_COIN_RESERVE}
            break
    return best


# --- marginal growth gate ---------------------------------------------------
# The first version of this gate encoded a short, mixed-regime collection curve
# as a permanent per-farm plateau. That was false: the later regime-filtered
# leaderboard ledger shows an approximately linear relationship well beyond
# 8,000 animals, and restoring growth retook first place at run 416.
#
# The gate remains as a falsifiable safety brake for an actual server-mechanics
# change. It compares nearby herd cohorts, never ancient small-herd rows, and a
# nonzero maintenance floor keeps collecting evidence if it ever challenges
# growth. The authoritative claim lifecycle lives in farm/claims.py.
GROWTH_SAMPLE_MIN_MINUTES = 3.0   # shorter windows measure noise, not output
GROWTH_MIN_SAMPLES = 3            # per cohort, before any verdict is trusted
GROWTH_RECENT_BAND = 0.95         # >= 95% of current herd counts as "now"
# The comparison cohort is a WINDOW just below the current herd, not an open
# ended tail. Comparing against the whole history compares 11,400 animals with
# 420 and always concludes growth works; the question is whether the *marginal*
# few thousand animals paid, so the baseline has to be a herd of similar order.
GROWTH_SMALLER_HIGH = 0.90        # upper edge of the comparison window
GROWTH_SMALLER_LOW = 0.70         # lower edge of the comparison window
# LOWERED 0.10 -> 0.02 after the run 291-292 post-mortem found this single
# threshold had frozen the herd for 246 runs and cost first place.
#
# The comparison window is 70-90% of the current herd, so "now" is only an
# 11-43% larger herd. Demanding a 10% output gain from that is demanding roughly
# LINEAR scaling. Our own measured curve is sub-linear but strictly rising:
#
#   herd            produce/min     per animal/min
#    1,500- 2,999        642            0.285
#    3,000- 4,499        831            0.222
#    4,500- 5,999      1,014            0.193
#    6,000- 7,499      1,135            0.168
#    7,500- 8,999      1,450            0.176
#    9,000-10,499      1,520            0.156
#   10,500-11,999      1,629            0.145
#
# Total output never plateaued; only efficiency per animal fell. The old bar read
# that as a ceiling (1,645/min vs 1,553/min = +5.9% < 10%), set the adoption cap
# to MAINTENANCE_ADOPTIONS, and froze the herd at 11,869 for 246 runs while 3.2M
# coins piled up. Meanwhile John scaled to 42,859 animals and took first place at
# 2,391 produce/min against our 1,752 - with WORSE efficiency per animal
# (0.056 vs our 0.148). He simply never stopped growing.
#
# Coins are not the score; lifetime produce is. An idle coin is a wasted coin, so
# the only reason to stop adopting is output actually falling. 0.02 is a noise
# floor on a median of 3+ samples, not an economic hurdle: it rejects
# measurement jitter while still permitting the sub-linear growth above.
GROWTH_MIN_MARGINAL_GAIN = 0.02
GROWTH_RESUME_MARGIN = 0.15       # output this far above plateau => ceiling moved
GROWTH_MIN_HERD_TO_SATURATE = 2000  # never let early noise stop initial growth
# Never let the verdict latch growth off completely. With MAINTENANCE_ADOPTIONS
# at 0 the gate was a one-way ratchet: adoption stopped, the herd froze, and the
# only way back was more output from the very herd it had frozen - which cannot
# happen. A nonzero floor keeps the experiment alive, and it is close to free
# because expansion_plan reserves the feed budget before spending on animals.
MAINTENANCE_ADOPTIONS = 25


def median(values: List[float]) -> Optional[float]:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[len(values) // 2]


def clean_samples(
    raw: List[Tuple[int, float, int, int]]
) -> List[Tuple[int, float]]:
    """Filter (animals, minutes, produce_delta, max_hunger) into (animals, rate).

    Rejects windows too short to mean anything, windows with no production, and
    windows where the herd was starving (which measures feeding, not capacity).
    """
    out: List[Tuple[int, float]] = []
    for animals, minutes, produced, hunger in raw:
        if not animals or not minutes or minutes < GROWTH_SAMPLE_MIN_MINUTES:
            continue
        if produced is None or produced <= 0:
            continue
        if (hunger or 0) >= HUNGER_ALARM:
            continue
        out.append((int(animals), float(produced) / float(minutes)))
    return out


def growth_verdict(
    samples: List[Tuple[int, float]],
    current_animals: int,
    model: Optional[Dict[str, object]] = None,
    min_marginal_gain: Optional[float] = None,
    recent_band: Optional[float] = None,
    smaller_low: Optional[float] = None,
    smaller_high: Optional[float] = None,
) -> Dict[str, object]:
    """Does adding animals still add output? Pure function over measurements.

    Returns the verdict plus the plateau it is holding, so the caller can persist
    it. The plateau is recorded once, when saturation is first detected, and then
    left alone: continuously re-baselining it would make a genuine ceiling lift
    invisible.
    """
    model = model or {}
    was_saturated = bool(model.get("saturated"))
    plateau = model.get("plateau")
    plateau = float(plateau) if isinstance(plateau, (int, float)) else None
    marginal = GROWTH_MIN_MARGINAL_GAIN if min_marginal_gain is None else float(min_marginal_gain)
    recent_edge = GROWTH_RECENT_BAND if recent_band is None else float(recent_band)
    low_edge = GROWTH_SMALLER_LOW if smaller_low is None else float(smaller_low)
    high_edge = GROWTH_SMALLER_HIGH if smaller_high is None else float(smaller_high)

    recent = [rate for animals, rate in samples if animals >= current_animals * recent_edge]
    smaller = [
        rate
        for animals, rate in samples
        if current_animals * low_edge
        <= animals
        <= current_animals * high_edge
    ]
    recent_med = median(recent)
    smaller_med = median(smaller)

    def verdict(saturated, reason, keep_plateau):
        return {
            "saturated": bool(saturated),
            "reason": reason,
            "plateau": keep_plateau,
            "recent_units_per_min": round(recent_med, 1) if recent_med else None,
            "smaller_units_per_min": round(smaller_med, 1) if smaller_med else None,
            "recent_samples": len(recent),
            "smaller_samples": len(smaller),
            "herd": int(current_animals or 0),
        }

    # A lifted ceiling shows up as more output from the SAME herd. Check it before
    # anything else, because it is the one signal that should restart growth.
    if (
        was_saturated
        and plateau
        and recent_med
        and len(recent) >= GROWTH_MIN_SAMPLES
        and recent_med > plateau * (1 + GROWTH_RESUME_MARGIN)
    ):
        return verdict(
            False,
            "output %.0f/min is %.0f%% above the %.0f/min plateau - ceiling lifted, resuming growth"
            % (recent_med, 100.0 * (recent_med / plateau - 1.0), plateau),
            None,
        )

    if (current_animals or 0) < GROWTH_MIN_HERD_TO_SATURATE:
        return verdict(False, "herd below %d - growing" % GROWTH_MIN_HERD_TO_SATURATE, None)

    if (
        recent_med is None
        or smaller_med is None
        or len(recent) < GROWTH_MIN_SAMPLES
        or len(smaller) < GROWTH_MIN_SAMPLES
    ):
        # Hold the previous decision rather than flip-flopping on thin evidence.
        return verdict(
            was_saturated,
            "insufficient samples (%d recent, %d smaller) - holding previous verdict"
            % (len(recent), len(smaller)),
            plateau,
        )

    if recent_med <= smaller_med * (1 + marginal):
        return verdict(
            True,
            "output %.0f/min at %d animals vs %.0f/min at %d-%d - the marginal herd buys no output"
            % (
                recent_med,
                current_animals,
                smaller_med,
                int(current_animals * low_edge),
                int(current_animals * high_edge),
            ),
            plateau if was_saturated else recent_med,
        )

    return verdict(
        False,
        "output %.0f/min vs %.0f/min at %d-%d animals - still responding, growing"
        % (
            recent_med,
            smaller_med,
            int(current_animals * low_edge),
            int(current_animals * high_edge),
        ),
        None,
    )


def adoption_cap(verdict: Dict[str, object], knobs: Dict[str, object]) -> Tuple[int, str]:
    """Final adoptions allowed this run: the stricter of safety and economics."""
    safety = adopt_cap(knobs)
    # The maintenance cohort prevents an uncertain marginal model from latching
    # growth off forever. It must not run through a confirmed score-engine halt:
    # more obligations cannot restore production and can worsen server pressure.
    if verdict.get("production_stalled"):
        return 0, str(verdict.get("reason") or "production stalled")
    if verdict.get("saturated"):
        return MAINTENANCE_ADOPTIONS, str(verdict.get("reason") or "saturated")
    return safety, str(verdict.get("reason") or "growing")


def trade_value(item: str, qty: int) -> int:
    return ITEM_VALUE.get(item, 0) * qty


def should_accept(offer_item: str, offer_qty: int, want_item: str, want_qty: int) -> bool:
    """Accept only when what we receive is worth at least what we give."""
    return trade_value(offer_item, offer_qty) >= trade_value(want_item, want_qty)


def offer_targets(current_recipients: List[str], paused: List[str]) -> List[str]:
    """Which farmers to send a fresh 5-feed-for-10-coin offer to."""
    have = {r.strip().lower() for r in current_recipients}
    blocked = {p.strip().lower() for p in paused}
    slots = MAX_OPEN_OFFERS - len(have)
    if slots <= 0:
        return []
    out = []
    for name in PREFERRED_TRADE_TARGETS:
        if len(out) >= slots:
            break
        key = name.strip().lower()
        if key not in have and key not in blocked:
            out.append(name)
    return out


# --- epistemic audit (pure history replay) ---------------------------------

def _audit_rows(history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    rows = [
        row for row in (history or [])
        if isinstance(row, dict) and isinstance(row.get("run"), int) and not row.get("dry")
    ]
    rows.sort(key=lambda row: row["run"])
    # A caller may pass the current row both in history and separately. Keep the
    # last value for each run so every detector sees one chronological regime.
    return list({row["run"]: row for row in rows}.values())


def strategy_stale(history: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """A flat herd plus accumulating capital means a standing gate deserves review."""
    rows = _audit_rows(history)
    if len(rows) < AUDIT_WINDOW_RUNS:
        return None
    window = rows[-AUDIT_WINDOW_RUNS:]
    animals = [int(row.get("animals") or 0) for row in window]
    if not animals or min(animals) <= 0:
        return None
    spread = (max(animals) - min(animals)) / float(max(animals))
    first_coins = int(window[0].get("coins") or 0)
    last_coins = int(window[-1].get("coins") or 0)
    coin_gain = last_coins - first_coins
    revenue = sum(int(row.get("revenue") or 0) for row in window[1:])
    if (
        spread <= AUDIT_HERD_SPREAD_FRACTION
        and last_coins >= AUDIT_MIN_IDLE_COINS
        and revenue > 0
        and coin_gain > AUDIT_IDLE_SHARE * revenue
    ):
        return {
            "code": "strategy_stale",
            "run": window[-1]["run"],
            "window_from": window[0]["run"],
            "window_to": window[-1]["run"],
            "herd_min": min(animals),
            "herd_max": max(animals),
            "herd_spread": round(spread, 6),
            "coin_gain": coin_gain,
            "revenue": revenue,
            "idle_share": round(coin_gain / float(revenue), 3),
            "balance": last_coins,
            "alert": (
                "STRATEGY STALE: herd %d-%d across %d runs while coins grew %d "
                "(%.0f%% of %d revenue; balance %d)"
                % (
                    min(animals), max(animals), AUDIT_WINDOW_RUNS, coin_gain,
                    100.0 * coin_gain / revenue, revenue, last_coins,
                )
            ),
        }
    return None


def idle_capital(history: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Capital-side audit for a saturated/maintenance growth policy."""
    rows = _audit_rows(history)
    window = rows[-min(12, len(rows)):]
    if len(window) < 6:
        return None
    latest = window[-1]
    growth = latest.get("growth") or {}
    cap = growth.get("cap")
    if not isinstance(cap, (int, float)) or cap > MAINTENANCE_ADOPTIONS:
        return None
    revenues = [int(row.get("revenue") or 0) for row in window if int(row.get("revenue") or 0) > 0]
    typical = median(revenues)
    balance = int(latest.get("coins") or 0)
    if typical and balance >= AUDIT_MIN_IDLE_COINS and balance >= AUDIT_IDLE_REVENUE_MULTIPLE * typical:
        return {
            "code": "idle_capital",
            "run": latest["run"],
            "balance": balance,
            "typical_revenue": round(typical, 1),
            "multiple": round(balance / typical, 1),
            "adoption_cap": int(cap),
            "alert": (
                "IDLE CAPITAL: %d coins equal %.1fx a typical run while growth cap is %d"
                % (balance, balance / typical, int(cap))
            ),
        }
    return None


def knob_age(history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Decision signatures that have not changed for too many runs."""
    rows = _audit_rows(history)
    if len(rows) < 2:
        return []
    latest = rows[-1]
    current_run = latest["run"]
    findings: List[Dict[str, Any]] = []

    def changed_run(signature) -> int:
        current = signature(latest)
        for row in reversed(rows[:-1]):
            if signature(row) != current:
                return int(row["run"]) + 1
        return int(rows[0]["run"])

    growth = latest.get("growth") or {}
    if growth:
        start = changed_run(
            lambda row: (
                bool((row.get("growth") or {}).get("saturated")),
                (row.get("growth") or {}).get("cap"),
            )
        )
        validated = (latest.get("claim_validated_runs") or {}).get(
            "mechanic.output_linear_with_herd"
        )
        if isinstance(validated, int):
            start = max(start, validated)
        age = current_run - start
        if age >= AUDIT_KNOB_MAX_AGE_RUNS:
            findings.append({
                "code": "knob_age",
                "subject": "growth_gate",
                "run": current_run,
                "changed_run": start,
                "age_runs": age,
                "alert": "KNOB AGE: growth_gate unchanged for %d runs (since run %d)" % (age, start),
            })

    current_knobs = latest.get("knobs") or {}
    for name, value in sorted(current_knobs.items()):
        start = changed_run(lambda row, key=name: (row.get("knobs") or {}).get(key))
        age = current_run - start
        if age >= AUDIT_KNOB_MAX_AGE_RUNS:
            findings.append({
                "code": "knob_age",
                "subject": name,
                "run": current_run,
                "changed_run": start,
                "age_runs": age,
                "value": value,
                "alert": "KNOB AGE: %s=%s unchanged for %d runs (since run %d)" % (
                    name, value, age, start
                ),
            })
    return findings


def _rival_rate(rows: List[Dict[str, Any]], name: str) -> Optional[float]:
    if len(rows) < 2:
        return None
    from datetime import datetime
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        minutes = (
            datetime.strptime(str(rows[-1].get("ts")), fmt)
            - datetime.strptime(str(rows[0].get("ts")), fmt)
        ).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None
    before = (rows[0].get("rivals") or {}).get(name)
    after = (rows[-1].get("rivals") or {}).get(name)
    if minutes <= 0 or not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return None
    return max(0.0, (float(after) - float(before)) / minutes)


def _wake_state(rows: List[Dict[str, Any]], name: str) -> Optional[Dict[str, float]]:
    needed = RIVAL_WAKE_RECENT_INTERVALS + 1 + RIVAL_WAKE_BASE_ROWS
    if len(rows) < needed:
        return None
    recent_rows = rows[-(RIVAL_WAKE_RECENT_INTERVALS + 1):]
    base_rows = rows[-needed:-(RIVAL_WAKE_RECENT_INTERVALS + 1)]
    recent = _rival_rate(recent_rows, name)
    base = _rival_rate(base_rows, name)
    if recent is None or base is None:
        return None
    active = recent >= RIVAL_WAKE_MIN_RATE and (
        base <= RIVAL_WAKE_FLAT_EPS or recent >= RIVAL_WAKE_RATIO * base
    )
    return {"recent": recent, "base": base, "active": bool(active)}


def rival_wakes(history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Rising edges for rivals moving materially above their own baseline."""
    rows = _audit_rows(history)
    if not rows:
        return []
    names = sorted({name for row in rows for name in (row.get("rivals") or {})})
    findings: List[Dict[str, Any]] = []
    for name in names:
        state = _wake_state(rows, name)
        if not state or not state["active"]:
            continue
        previous = _wake_state(rows[:-1], name)
        if previous and previous["active"]:
            continue
        latest_score = (rows[-1].get("rivals") or {}).get(name)
        findings.append({
            "code": "rival_wake",
            "subject": name,
            "run": rows[-1]["run"],
            "recent_rate": round(state["recent"], 3),
            "base_rate": round(state["base"], 3),
            "lifetime_produce": latest_score,
            "alert": (
                "RIVAL WAKE: %s recent %.3f/min vs base %.3f/min over %d intervals "
                "(lifetime %s)"
                % (
                    name, state["recent"], state["base"],
                    RIVAL_WAKE_RECENT_INTERVALS, latest_score,
                )
            ),
        })
    return findings


def strategy_audit(history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for value in (strategy_stale(history), idle_capital(history)):
        if value:
            findings.append(value)
    findings.extend(knob_age(history))
    findings.extend(rival_wakes(history))
    return findings


# --- the objective ----------------------------------------------------------
# Everything above tunes a proxy: hunger, runway, throughput, call rate. None of
# it answers the only question that decides the game - are we going to pass the
# leader, and when?
#
# That absence is a real failure mode, not a cosmetic gap. The growth gate froze
# the herd for 246 runs while "healthy" on every proxy, and the healer ratcheted
# adoption down four times while the farm was in perfect condition. Both would
# have been caught immediately by a number that went the wrong way.
#
# The model is deliberately simple, because it is measured, not assumed:
#   - output is LINEAR in herd size (postmortem addendum: 0.175-0.187
#     produce/animal/min for both farms, no per-farm ceiling)
#   - so produce accumulates quadratically while the herd grows linearly
#
# For each side, with y = produce per animal per minute and g = animals per
# minute of adoption:
#
#   produce(t) = P + R*t + 0.5 * y * g * t**2
#
# We pass the leader at the first positive root of the difference. Behind on
# produce, we win only if our herd grows faster than theirs; if their rate is
# higher and they are growing at least as fast, there is no root and no plan.
WIN_ETA_ALARM_HOURS = 24.0   # a plausible ETA; beyond this we are not really racing
# A rival herd jump this large is a strategic event, not noise. Sized above the
# handful of animals a bankrupt rival adds by accident (John added 4 in 1.2h).
RIVAL_HERD_GROWTH_ALARM = 250
# The leader's produce is sampled in lumpy windows (John measured 884-8,321
# produce/min across runs 344-353 while his herd never moved), so the projection
# smooths it. 0.35 keeps ~3 runs of memory: responsive enough to catch a rival
# who genuinely wakes up, steady enough not to fire on one sample.
RIVAL_RATE_EWMA_ALPHA = 0.35


def win_projection(
    our_produce: int,
    our_rate: float,
    our_herd: int,
    our_growth_per_min: float,
    rival_produce: int,
    rival_rate: float,
    rival_herd: int,
    rival_growth_per_min: float = 0.0,
) -> Dict[str, Any]:
    """Minutes until we pass a rival on lifetime produce.

    Returns `eta_min=None` when the projection has no positive root, i.e. no
    amount of waiting wins and something has to change. `deficit_rate` is how
    much produce/min they are currently beating us by (negative = we are ahead
    on rate), which is the actionable half of the answer.
    """
    lead = (rival_produce or 0) - (our_produce or 0)
    our_yield = (our_rate / our_herd) if our_herd else 0.0
    rival_yield = (rival_rate / rival_herd) if rival_herd else 0.0

    # Quadratic: a*t^2 + b*t - lead = 0
    a = 0.5 * (our_yield * max(our_growth_per_min, 0.0)
               - rival_yield * max(rival_growth_per_min, 0.0))
    b = (our_rate or 0.0) - (rival_rate or 0.0)

    out = {
        "lead": lead,               # how far AHEAD the rival is (negative = we lead)
        "deficit_rate": -b,         # produce/min they are gaining on us
        "our_yield": round(our_yield, 4),
        "rival_yield": round(rival_yield, 4),
        "accel": round(a, 4),       # our quadratic edge from out-adopting them
        "eta_min": None,
        "ahead": lead < 0,
    }
    if lead < 0:
        out["eta_min"] = 0.0        # already ahead
        return out

    if abs(a) < 1e-9:
        # No growth edge: purely a race of constant rates.
        out["eta_min"] = (lead / b) if b > 0 else None
        return out

    disc = b * b + 4.0 * a * lead
    if disc < 0 or a <= 0 and b <= 0:
        return out                  # never, on current settings
    root = (-b + math.sqrt(disc)) / (2.0 * a)
    out["eta_min"] = root if root > 0 else None
    return out


def herd_to_out_rate(rival_rate: float, our_yield: float) -> int:
    """Herd size at which we merely MATCH the rival's current produce rate.

    Useful as a floor: below this, the gap is still widening no matter how long
    we run, so adoption throughput - not coins - is the thing to fix.
    """
    if our_yield <= 0:
        return 0
    return int(rival_rate / our_yield)


def affordable_adoptions(coins: int, herd: int, feed_on_hand: int = 0) -> int:
    """How many animals we can adopt and still keep the whole herd fed.

    Every adopted animal costs its price now, plus a feed reserve for itself:

        coins >= n * ANIMAL_COST + reserve_shortfall(herd + n)

    The subtlety is `reserve_shortfall`. The obvious version charges coins for
    the reserve of the ENTIRE herd:

        spendable = coins - FEED_PER_ANIMAL_RESERVE * herd

    which double-counts, because that feed is usually already sitting in the
    barn. At run 379 it reserved 1,905,660 coins to buy feed against a barn that
    already held 2,222,305 - 538 minutes of runway at the measured burn - and so
    reported that only 1,048 more animals were affordable while 1.9M coins sat
    idle. Coins and feed were both treated as if only coins existed.

    Feed already owned is feed we do not have to buy. So the floor is the
    SHORTFALL between the reserve the herd should have and the reserve it does
    have, and everything above that funds growth. New animals are still charged
    their full reserve up front, which keeps the conservative behaviour that
    matters: we never adopt an animal we cannot also feed.

    At run 379 this frees the balance to fund ~48,690 more animals (herd
    ~112,000), which experiments/endgame.py simulates as passing John in 6.4
    hours without ever starving. The old floor capped the herd at ~64,570, which
    the same simulation shows never wins at all.
    """
    cost = ANIMAL_COST[PRIMARY_KIND]
    per_animal = cost + FEED_PER_ANIMAL_RESERVE * FEED_COST
    shortfall = max(0, FEED_PER_ANIMAL_RESERVE * int(herd or 0) - int(feed_on_hand or 0))
    spendable = int(coins or 0) - shortfall * FEED_COST - RISK_COIN_RESERVE
    if spendable <= 0:
        return 0
    return int(spendable // per_animal)


# --- herd-scale feeding -----------------------------------------------------
# The former 120k hunger ceiling came from gateway-limited partial feeds. The
# server now applies feed_animals(all) as one constant-time bulk operation, so
# herd size itself no longer predicts incomplete coverage. Real hunger and feed
# runway remain live safety signals; there is no synthetic herd-size cap.
HUNGER_PER_ANIMAL_SLOPE = 0.0
HUNGER_HERD_INTERCEPT = 0.0


def projected_max_hunger(herd: int) -> float:
    """No herd-size projection: observe actual hunger after the bulk feed."""
    return 0.0


def hunger_safe_herd_ceiling(limit: Optional[int] = None) -> int:
    """Compatibility API for expansion: bulk feeding has no known herd cap."""
    return 10 ** 9
