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
   healed. It opens or updates one durable question. Only a high-priority first
   occurrence pages; repeated alerts add evidence without repeated model spend.
4. A class that keeps re-alerting despite its operational remedy escalates.
   Silent, endless self-healing that is not working is worse than a page.

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


def _raise_collect_passes(store: Dict[str, Any]) -> Optional[str]:
    current = rules.collect_passes(store.get("knobs") or {})
    if current >= rules.MAX_COLLECT_PASSES:
        return None
    _set(store, "collect_passes", current + 1)
    return "collect passes %d -> %d" % (current, current + 1)


# -- remedies ----------------------------------------------------------------
# Each takes (item, row, store) and returns a description of what it changed,
# or None when it has nothing left to try (which escalates).


def _heal_transport(item, row, store) -> Optional[str]:
    return _lower_rate(store, 0.8)


def _heal_backpressure(item, row, store) -> Optional[str]:
    actions = [a for a in (_lower_rate(store, 0.8), _lower_workers(store)) if a]
    return "; ".join(actions) if actions else None


def _heal_throughput(item, row, store) -> Optional[str]:
    """Low throughput that survived the detector's backlog test.

    Two fixable causes: produce accumulating faster than one collect call can
    drain, and a cycle so long that collection intervals stretch. Both have
    concrete levers.
    """
    actions = []
    animals = row.get("animals") or 0
    if not rules.backlog_drained(row.get("ready_units") or 0, animals):
        action = _raise_collect_passes(store)
        if action:
            actions.append(action)
    # Deliberately does NOT lower the adoption cap for a long run any more.
    # adopt_chickens() already stops on the wall-clock deadline, so adoption
    # cannot overrun the budget on its own - but a run that is long BECAUSE it
    # adopted would cut the cap, shrinking the next run's adoption, which is a
    # loop that ratchets growth to zero for the one reason that is not a fault.
    # The inherent cost is collect + bulk feed, which no adoption cap can shrink.
    return "; ".join(actions) if actions else None


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
    return _raise_collect_passes(store)


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
    ("tools_changed", r"tools/list changed", None),
    ("animals_fell", r"animal count fell", None),
    ("count_mismatch", r"animal count \d+ != expected", None),
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

    if "collect_passes" in current and quiet("throughput") and quiet("zero_collect"):
        value = rules.collect_passes(current)
        if value <= 2:
            current.pop("collect_passes", None)
            notes.append("collect passes restored to 1")
        else:
            current["collect_passes"] = value - 1
            notes.append("collect passes eased %d -> %d" % (value, value - 1))

    return notes


def process(
    pending: List[Dict[str, Any]],
    row: Optional[Dict[str, Any]],
    run: Optional[int] = None,
) -> Dict[str, Any]:
    """Heal what can be healed; report what genuinely needs a model."""
    store = load()
    row = row or {}
    run = run if run is not None else row.get("run")
    healed: List[Dict[str, str]] = []
    escalated: List[Dict[str, Any]] = []
    questioned: List[Dict[str, Any]] = []
    already = set(store.get("healed") or [])
    applied: Dict[str, str] = {}
    decision_bundle: Optional[Dict[str, Any]] = None

    if not rules.HEAL_ENABLED:
        return {
            "healed": [],
            "escalated": [
                {"alert": i.get("alert", ""), "class": "disabled", "reason": "healing off"}
                for i in pending
            ],
            "questions": [],
            "knobs": dict(store.get("knobs") or {}),
            "relaxed": [],
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
            result = questions.open_or_update(
                name,
                alert,
                row=row,
                item=item,
                decision_bundle=decision_bundle,
                evidence_refs=["history.ndjson#run=%s" % item.get("run")],
            )
            question = result["question"]
            questioned.append(
                {
                    "alert": alert,
                    "class": name,
                    "question_id": question.get("id"),
                    "opened": bool(result.get("opened") or result.get("reopened")),
                    "occurrences": question.get("occurrences"),
                    "priority": question.get("priority"),
                }
            )
            # This alert instance is disposed into a durable question. Mark the
            # run+alert key handled so each supervisor pass cannot count it again;
            # a future run has a new key and bumps the same question once.
            already.add(key)
            store.setdefault("healed", []).append(key)
            if result.get("page_on_open"):
                escalated.append(
                    {
                        "alert": alert,
                        "class": name,
                        "reason": "opened %s question %s" % (
                            question.get("priority"), question.get("id")
                        ),
                        "question_id": question.get("id"),
                        "decision_bundle": decision_bundle,
                    }
                )
            continue
        if remedy is None:
            escalated.append({"alert": alert, "class": name, "reason": "needs judgement"})
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
            escalated.append(
                {
                    "alert": alert,
                    "class": name,
                    "reason": "remedy ineffective after %d attempts" % (count - 1),
                }
            )
            continue
        action = remedy(item, row, store)
        if not action:
            escalated.append(
                {"alert": alert, "class": name, "reason": "no remedy left to apply"}
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

    relaxed = relax(store, run) if not escalated else []
    for note in relaxed:
        _log({"ts": _utcnow(), "run": run, "class": "relax", "action": note})

    save(store)
    return {
        "healed": healed,
        "escalated": escalated,
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
        "collect_passes": rules.collect_passes(current),
        "overrides": current,
    }
