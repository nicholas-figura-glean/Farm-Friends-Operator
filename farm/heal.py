"""Deterministic remediation: the supervisor's judgement, written down.

An alert used to mean "wake a model and let it decide". Most alerts, though,
recur with the same cause and the same fix, and paying tokens to rediscover that
fix every few minutes is waste. This module encodes the fixes.

Rules of the design:

1. Every remedy is bounded and conservative. Knobs may throttle growth or add a
   little extra work; none can spend coins, adopt, sell, trade, or gift. The
   worst a bad healing decision can do is slow the farm down.
2. Every knob relaxes one step per quiet pass, so a transient incident cannot
   throttle the farm permanently.
3. Anything strategic (rank, threats, stale decisions, rival wakes) is NOT
   healed. It opens or updates one durable question owned by the research and
   probe agents; repeated alerts add evidence without creating duplicate work.
4. Unknown alerts and remedies that exhaust their bounded attempts are routed to
   the same durable agent queue. They are never left waiting for operator input.
   The last verified policy remains active while the queue investigates.

State lives in state/heal.json, which only this module writes. The cycle reads
the knobs; it never writes them, so there is no race with the run loop's meta.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import questions, rules

STORE = os.path.join("state", "heal.json")
LEDGER = os.path.join("state", "heal.ndjson")
MAX_HEALED_KEYS = 500

DEFAULTS: Dict[str, Any] = {
    "version": 1,
    "knobs": {},
    "attempts": {},
    "healed": [],
    "scheduler": {},
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> Dict[str, Any]:
    try:
        with open(STORE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULTS))
    for key, value in DEFAULTS.items():
        data.setdefault(key, json.loads(json.dumps(value)))
    return data


def save(store: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    store["healed"] = list(store.get("healed") or [])[-MAX_HEALED_KEYS:]
    store["updated"] = _utcnow()
    tmp = STORE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(store, fh, indent=1, sort_keys=True)
    os.replace(tmp, STORE)


def knobs() -> Dict[str, Any]:
    """Read-only accessor for the run loop."""
    return dict(load().get("knobs") or {})


def healed_keys() -> set:
    """Alert keys already handled, for de-duplication.

    Defensive on purpose. This is a set() over whatever is in the state file, and
    a single unhashable element - one dict appended by hand - raised TypeError
    inside the supervisor, which failed the whole supervise pass and escalated to
    an LLM. A dedup helper must never be the reason we wake a model and spend
    money: skip what it cannot use and keep going.
    """
    out = set()
    for item in load().get("healed") or []:
        try:
            out.add(item)
        except TypeError:
            continue
    return out


def alert_key(item: Dict[str, Any]) -> str:
    return "%s:%s" % (item.get("run"), item.get("alert"))


def _log(entry: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


# -- knob helpers ------------------------------------------------------------


def _set(store: Dict[str, Any], name: str, value: Any) -> None:
    store.setdefault("knobs", {})[name] = value


def _lower_rate(store: Dict[str, Any], factor: float = 0.8) -> Optional[str]:
    current = rules.rate_ceiling(store.get("knobs") or {})
    target = max(rules.MIN_CALLS_PER_SECOND, round(current * factor, 2))
    if target >= current:
        return None  # already at the floor: nothing left to concede
    _set(store, "rate_ceiling", target)
    return "call-rate ceiling %.2f/s -> %.2f/s" % (current, target)


def _lower_workers(store: Dict[str, Any]) -> Optional[str]:
    current = rules.adopt_worker_count(store.get("knobs") or {})
    if current <= 1:
        return None
    _set(store, "adopt_workers", current - 1)
    return "adopt workers %d -> %d" % (current, current - 1)


def _lower_adopt_cap(store: Dict[str, Any], factor: float = 0.6) -> Optional[str]:
    current = rules.adopt_cap(store.get("knobs") or {})
    target = max(1, int(current * factor))
    if target >= current:
        return None
    _set(store, "adopt_cap", target)
    return "adopt cap %d -> %d/run" % (current, target)


# -- remedies ----------------------------------------------------------------
# Each takes (item, row, store) and returns a description of what it changed,
# or None when it has nothing left to try (which escalates).


def _heal_transport(item, row, store) -> Optional[str]:
    return _lower_rate(store, 0.8)


def _heal_backpressure(item, row, store) -> Optional[str]:
    actions = [a for a in (_lower_rate(store, 0.8), _lower_workers(store)) if a]
    return "; ".join(actions) if actions else None


def _heal_throughput(item, row, store) -> Optional[str]:
    """One bulk collection is invariant; throughput requires investigation."""
    # Deliberately do not invent a second collection pass or cut growth because
    # a long run adopted successfully. Both responses previously hid the actual
    # scoring signal and recreated the throttling loop from POSTMORTEM-run377.
    return None


def _heal_hunger(item, row, store) -> Optional[str]:
    """Hunger near the production stop. Never healed once it is AT the stop."""
    if (row.get("max_hunger") or 0) >= rules.HUNGER_STOP:
        return None
    # Feeding is one whole-herd operation now; reduce new obligations rather
    # than reintroducing a per-animal fan-out.
    return _lower_adopt_cap(store)


def _heal_feed_reserve(item, row, store) -> Optional[str]:
    """Feed under reserve means growth outran the feed budget: slow growth.

    Deliberately does NOT raise the reserve target, which would widen the very
    gap being alerted on.

    Second guard, added after runs 337-347: refuse to throttle at all while the
    runway is healthy. The detector now tolerates small shortfalls, but this
    remedy is the expensive one - it halves the adopt cap, which is the only
    thing that scores - so it re-checks the number that actually matters instead
    of trusting that an alert of this class implies danger. A shortfall against a
    30/animal target with 288 minutes of feed in the barn is an accounting
    artifact of concurrent adoption, not a starvation risk.
    """
    runway = rules.feed_buffer_minutes(row.get("feed") or 0, row.get("animals") or 0)
    if runway >= rules.FEED_BUFFER_MIN_MINUTES:
        return (
            "no action: runway %.0f min is at or above the %d min floor "
            "(shortfall is concurrent-adoption drift, not starvation)"
            % (runway, rules.FEED_BUFFER_MIN_MINUTES)
        )
    return _lower_adopt_cap(store, 0.5)


def _heal_zero_collect(item, row, store) -> Optional[str]:
    # Collection is exactly once per cycle. Repeating an ambiguous mutating call
    # can double-apply after a gateway timeout, so unresolved zeroes are routed.
    return None


def _heal_adopt_failures(item, row, store) -> Optional[str]:
    actions = [
        a
        for a in (_lower_rate(store, 0.7), _lower_workers(store), _lower_adopt_cap(store))
        if a
    ]
    return "; ".join(actions) if actions else None


def _heal_stale(item, row, store) -> Optional[str]:
    # The scheduler repair itself runs before healing (see farm/scheduler.py);
    # by the time this is classified the recovery cycle has already been fired.
    return "scheduler repair + recovery cycle"


Remedy = Callable[[Dict[str, Any], Dict[str, Any], Dict[str, Any]], Optional[str]]

# Ordered; first match wins. A remedy of None means "always escalate".
CLASSES: List[Tuple[str, str, Optional[Remedy]]] = [
    ("rank_lost", r"^RANK LOST", None),
    ("no_path_to_win", r"^NO PATH TO WIN", None),
    ("win_eta", r"^WIN ETA", None),
    ("threat", r"^THREAT:", None),
    ("overtaken", r"has passed us", None),
    ("rival_growing", r"^RIVAL GROWING:", None),
    ("rival_wake", r"^RIVAL WAKE:", None),
    ("strategy_stale", r"^STRATEGY STALE:", None),
    ("idle_capital", r"^IDLE CAPITAL:", None),
    ("knob_age", r"^KNOB AGE:", None),
    ("model_drift", r"^MODEL DRIFT:", None),
    ("policy_drift", r"^POLICY DRIFT:", None),
    ("activity_novelty_trade", r"^NOVEL ACTIVITY \[trade\]:", None),
    ("activity_novelty_rival", r"^NOVEL ACTIVITY \[rival\]:", None),
    ("activity_novelty_risk", r"^NOVEL ACTIVITY \[risk\]:", None),
    ("activity_novelty_tools", r"^NOVEL ACTIVITY \[tools\]:", None),
    ("tools_changed", r"tools/list changed", None),
    ("animals_fell", r"animal count fell", None),
    ("count_mismatch", r"animal count \d+ != expected", None),
    ("trade_policy_breach", r"^TRADE POLICY BREACH:", None),
    ("trades_in", r"incoming trade\(s\) pending review", None),
    ("stale_loop", r"primary loop stale", _heal_stale),
    ("hunger", r"^hunger \d+ at/above alarm", _heal_hunger),
    (
        "feed_reserve",
        r"^(?:feed \d+ below reserve target|feed reserve still short after reconciliation)",
        _heal_feed_reserve,
    ),
    ("zero_collect", r"no produce collected in \d+ consecutive runs", _heal_zero_collect),
    ("adopt_failures", r"^\d+ adopt call failures", _heal_adopt_failures),
    ("adopt_failures", r"^adopt stopped early", _heal_adopt_failures),
    ("transport", r"^\d+ transport retries across", _heal_transport),
    ("backpressure", r"call rate .* server pushing back", _heal_backpressure),
    ("throughput", r"^throughput .* outside band", _heal_throughput),
    ("feed_call_failed", r"^could not feed ", _heal_transport),
]

STRATEGY_CLASSES = {
    "rank_lost", "no_path_to_win", "win_eta", "threat", "overtaken",
    "rival_growing", "rival_wake", "strategy_stale", "idle_capital",
    "knob_age", "model_drift", "policy_drift", "tools_changed",
    "trade_policy_breach", "activity_novelty_trade",
    "activity_novelty_rival", "activity_novelty_risk",
    "activity_novelty_tools",
}


def classify(alert: str) -> Tuple[str, Optional[Remedy]]:
    for name, pattern, remedy in CLASSES:
        if re.search(pattern, alert or ""):
            return name, remedy
    return "unknown", None


def _attempts(store: Dict[str, Any], name: str, run: Optional[int]) -> int:
    book = store.setdefault("attempts", {})
    entry = book.setdefault(name, {"count": 0, "first_run": run, "last_run": run})
    last = entry.get("last_run")
    if (
        isinstance(run, int)
        and isinstance(last, int)
        and run - last > rules.HEAL_ATTEMPT_RESET_RUNS
    ):
        # The class went quiet long enough that the remedy evidently worked.
        entry["count"] = 0
        entry["first_run"] = run
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["last_run"] = run
    return entry["count"]


def relax(store: Dict[str, Any], run: Optional[int]) -> List[str]:
    """Walk knobs one step back toward default for classes that went quiet.

    Without this, one bad afternoon would leave the farm throttled forever.
    """
    notes: List[str] = []
    if not isinstance(run, int):
        return notes
    book = store.get("attempts") or {}
    current = store.setdefault("knobs", {})

    def quiet(name: str) -> bool:
        last = (book.get(name) or {}).get("last_run")
        return not isinstance(last, int) or run - last > rules.HEAL_ATTEMPT_RESET_RUNS

    if (
        "rate_ceiling" in current
        and quiet("transport")
        and quiet("backpressure")
        and quiet("adopt_failures")
    ):
        value = rules.rate_ceiling(current)
        # Recover at least as fast as _lower_rate throttles (0.8x per step).
        # Easing by 1.25x took as many quiet windows as there were bad ones, so a
        # single flaky hour left the farm slow for hours afterwards.
        target = min(rules.MAX_CALLS_PER_SECOND, round(value * 1.6, 2))
        if target >= rules.MAX_CALLS_PER_SECOND:
            current.pop("rate_ceiling", None)
            notes.append("rate ceiling restored to %.1f/s" % rules.MAX_CALLS_PER_SECOND)
        elif target > value:
            current["rate_ceiling"] = target
            notes.append("rate ceiling eased %.2f -> %.2f/s" % (value, target))

    if "adopt_workers" in current and quiet("backpressure") and quiet("adopt_failures"):
        value = rules.adopt_worker_count(current)
        if value + 1 >= rules.ADOPT_WORKERS:
            current.pop("adopt_workers", None)
            notes.append("adopt workers restored to %d" % rules.ADOPT_WORKERS)
        else:
            current["adopt_workers"] = value + 1
            notes.append("adopt workers eased %d -> %d" % (value, value + 1))

    if (
        "adopt_cap" in current
        and quiet("throughput")
        and quiet("feed_reserve")
        and quiet("hunger")
    ):
        value = rules.adopt_cap(current)
        target = min(rules.MAX_ADOPTIONS_PER_RUN, max(value + 1, int(value * 1.5)))
        if target >= rules.MAX_ADOPTIONS_PER_RUN:
            current.pop("adopt_cap", None)
            notes.append("adopt cap restored to %d" % rules.MAX_ADOPTIONS_PER_RUN)
        else:
            current["adopt_cap"] = target
            notes.append("adopt cap eased %d -> %d" % (value, target))

    # Purge pre-bulk-path knobs instead of displaying a control the cycle ignores.
    current.pop("collect_passes", None)
    current.pop("individual_feeds", None)
    return notes


def process(
    pending: List[Dict[str, Any]],
    row: Optional[Dict[str, Any]],
    run: Optional[int] = None,
) -> Dict[str, Any]:
    """Heal deterministically or route the condition to an autonomous owner."""
    store = load()
    row = row or {}
    run = run if run is not None else row.get("run")
    healed: List[Dict[str, str]] = []
    routed: List[Dict[str, Any]] = []
    questioned: List[Dict[str, Any]] = []
    already = set()
    for handled in store.get("healed") or []:
        try:
            already.add(handled)
        except TypeError:
            continue
    applied: Dict[str, str] = {}
    decision_bundle: Optional[Dict[str, Any]] = None

    def route(
        item: Dict[str, Any],
        alert_class: str,
        reason: str,
        bundle: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Transfer one alert to a durable agent-owned question.

        Routing is a terminal disposition for this alert instance, not a page.
        A later run can add evidence to the same stable question identity while
        probes, research, and authoring continue independently.
        """
        alert = str(item.get("alert") or "")
        result = questions.open_or_update(
            alert_class,
            alert,
            row=row,
            item=item,
            subject=item.get("subject"),
            decision_bundle=bundle,
            evidence_refs=["history.ndjson#run=%s" % item.get("run")],
        )
        question = result["question"]
        entry = {
            "alert": alert,
            "class": alert_class,
            "reason": reason,
            "question_id": question.get("id"),
            "opened": bool(result.get("opened") or result.get("reopened")),
            "occurrences": question.get("occurrences"),
            "priority": question.get("priority"),
        }
        if bundle is not None:
            entry["decision_bundle"] = bundle
        questioned.append(entry)
        routed.append(entry)
        key = alert_key(item)
        already.add(key)
        store.setdefault("healed", []).append(key)
        _log({
            "ts": _utcnow(), "run": run, "class": alert_class,
            "alert": alert, "action": "routed to autonomous question %s" % question.get("id"),
            "reason": reason,
        })

    if not rules.HEAL_ENABLED:
        for item in pending:
            key = alert_key(item)
            if key not in already:
                route(item, "healing_disabled", "deterministic healing disabled; queued for agent")
        save(store)
        return {
            "healed": [], "routed": routed, "escalated": [],
            "questions": questioned,
            "knobs": dict(store.get("knobs") or {}), "relaxed": [],
        }

    for item in pending:
        key = alert_key(item)
        if key in already:
            continue
        # Staleness guard. Alerts are a queue, and healing acts on the LATEST
        # row, so an old alert gets applied to a farm that has already moved on.
        # At run 295 a queued "hunger 100" alert from run 291 - the starvation
        # that had already been fixed - cut the adoption cap 25 -> 15 on a farm
        # sitting at hunger 0. The knob then throttled the recovery it was
        # supposedly protecting. Old alerts are history, not current conditions.
        item_run = item.get("run")
        if (
            isinstance(item_run, int)
            and isinstance(run, int)
            and run - item_run > rules.HEAL_ALERT_STALE_RUNS
        ):
            already.add(key)
            store.setdefault("healed", []).append(key)
            healed.append(
                {
                    "alert": item.get("alert", ""),
                    "class": "stale",
                    "action": "ignored: raised at run %s, now run %s" % (item_run, run),
                }
            )
            continue
        alert = item.get("alert", "")
        name, remedy = classify(alert)
        if remedy is None and name in STRATEGY_CLASSES:
            if decision_bundle is None:
                # Lazy import avoids a module cycle: semantic audits inspect this
                # class table to prove strategy cannot reach a remedy.
                from . import research
                decision_bundle = research.decision_bundle(row=row, include_sweep=True)
            route(item, name, "strategy evidence queued for research", decision_bundle)
            continue
        if remedy is None:
            route(
                item,
                "operational_%s" % name,
                "no deterministic remedy; queued for autonomous diagnosis",
            )
            continue
        # One remedy per class per pass. Several runs' worth of the same alert
        # describe ONE condition, and stepping the knob once per alert made the
        # healer over-correct badly (a 5.0/s ceiling fell to 2.05/s from four
        # queued copies of the same backpressure signal).
        if name in applied:
            healed.append(
                {"alert": alert, "class": name, "action": "covered by %s" % applied[name]}
            )
            already.add(key)
            store.setdefault("healed", []).append(key)
            continue
        limit = rules.HEAL_MAX_ATTEMPTS.get(name, 3)
        count = _attempts(store, name, run)
        if count > limit:
            route(
                item,
                "operational_%s" % name,
                "deterministic remedy ineffective after %d attempts; queued for alternate diagnosis"
                % (count - 1),
            )
            continue
        action = remedy(item, row, store)
        if not action:
            route(
                item,
                "operational_%s" % name,
                "deterministic remedy exhausted; queued for alternate diagnosis",
            )
            continue
        applied[name] = action
        healed.append({"alert": alert, "class": name, "action": action})
        already.add(key)
        store.setdefault("healed", []).append(key)
        _log(
            {
                "ts": _utcnow(),
                "run": run,
                "class": name,
                "alert": alert,
                "action": action,
                "attempt": count,
            }
        )

    relaxed = relax(store, run) if not routed else []
    for note in relaxed:
        _log({"ts": _utcnow(), "run": run, "class": "relax", "action": note})

    save(store)
    return {
        "healed": healed,
        "routed": routed,
        # Compatibility for older read-only consumers. Human escalation is retired.
        "escalated": [],
        "questions": questioned,
        "knobs": dict(store.get("knobs") or {}),
        "relaxed": relaxed,
    }


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    try:
        lines = open(LEDGER).read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def effective_knobs() -> Dict[str, Any]:
    """What the knobs actually resolve to after clamping, for reporting."""
    current = knobs()
    return {
        "rate_ceiling": rules.rate_ceiling(current),
        "adopt_cap": rules.adopt_cap(current),
        "adopt_workers": rules.adopt_worker_count(current),
        "overrides": current,
    }
