"""Independent champion-versus-candidate efficacy adjudication.

The canary remains the fast catastrophic-regression brake. This module answers a
separate question at the end of the provisional window: did a strategy candidate
produce its pre-declared gain, or is a reliability release at least equivalent?
It also carries a cumulative regression budget across otherwise-small releases.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import rules

SCHEMA_VERSION = 1
IMPROVED = "improved"
EQUIVALENT = "equivalent"
REJECTED = "rejected"
INSUFFICIENT = "insufficient"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _paths(canary_store: str) -> Tuple[Path, Path, Path]:
    state = Path(canary_store).resolve().parent
    champion = state / "champion.json"
    events = state / "efficacy_events.ndjson"
    lock = state / ".efficacy.lock"
    return champion, events, lock


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("%s.tmp.%d" % (path, os.getpid()))
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def _append(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, allow_nan=False, default=str) + "\n")


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _per_animal(row: Dict[str, Any]) -> Optional[float]:
    rate = _number(row.get("produce_per_min"))
    herd = row.get("animals")
    if rate is not None and rate >= 0 and isinstance(herd, int) and herd > 0:
        return rate / float(herd)
    return None


def _rate(row: Dict[str, Any]) -> Optional[float]:
    rate = _number(row.get("produce_per_min"))
    return rate if rate is not None and rate >= 0 else None


def metric_samples(rows: Iterable[Dict[str, Any]], metric: str = "per_animal") -> List[float]:
    getter = _per_animal if metric == "per_animal" else _rate
    return [value for value in (getter(row) for row in rows) if value is not None]


def _contaminated(row: Dict[str, Any]) -> bool:
    counts = row.get("risk_event_counts") or {}
    if isinstance(counts, dict) and int(counts.get("aliens") or 0) > 0:
        return True
    rate = _number(row.get("produce_per_min"))
    return rate is not None and rate < 0


def baseline_samples(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    cohort = [row for row in rows if not _contaminated(row)][-rules.EFFICACY_BASELINE_RUNS :]
    per_animal = metric_samples(cohort, "per_animal")
    if len(per_animal) >= max(3, len(cohort) // 2):
        return {"metric": "per_animal", "samples": per_animal}
    return {"metric": "absolute", "samples": metric_samples(cohort, "absolute")}


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _variance(values: List[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return sum((value - mean) ** 2 for value in values) / float(len(values) - 1)


def _effect_interval(baseline: List[float], candidate: List[float]) -> Dict[str, Optional[float]]:
    base = _mean(baseline)
    observed = _mean(candidate)
    if base is None or observed is None or base <= 0:
        return {"baseline": base, "candidate": observed, "effect": None, "lower": None, "upper": None}
    variance = (_variance(baseline, base) / max(1, len(baseline))) + (
        _variance(candidate, observed) / max(1, len(candidate))
    )
    se = math.sqrt(max(0.0, variance))
    z = rules.EFFICACY_CONFIDENCE_Z
    difference = observed - base
    return {
        "baseline": base,
        "candidate": observed,
        "effect": difference / base,
        "lower": (difference - z * se) / base,
        "upper": (difference + z * se) / base,
    }


def champion(canary_store: str) -> Dict[str, Any]:
    path, _, _ = _paths(canary_store)
    return _read_json(path)


def ensure_champion(
    canary_store: str,
    revision: str,
    policy_id: str = "",
    run: Optional[int] = None,
) -> Dict[str, Any]:
    """Bootstrap the already-trusted rollback target exactly once.

    Before efficacy existed, the live release had no champion row even though it
    was the release every candidate would roll back to. Treating an implicit 1.0
    as a measured candidate caused the first reliability release to fail the
    cumulative budget instead of establishing a durable comparison anchor.
    """
    champion_path, events_path, lock_path = _paths(canary_store)
    if not revision:
        return {}
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = _read_json(champion_path)
        if current:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return current
        established = {
            "schema_version": SCHEMA_VERSION,
            "revision": revision,
            "policy_id": policy_id,
            "established_ts": _utcnow(),
            "established_run": run,
            "cumulative_ratio": 1.0,
            "history": [{
                "revision": revision,
                "policy_id": policy_id,
                "change_class": "bootstrap",
                "ts": _utcnow(),
                "cumulative_ratio": 1.0,
            }],
        }
        _atomic_json(champion_path, established)
        _append(events_path, {
            "schema_version": SCHEMA_VERSION,
            "event": "champion.bootstrapped",
            "ts": _utcnow(),
            "revision": revision,
            "policy_id": policy_id,
            "run": run,
            "reason": "trusted rollback target predates efficacy ledger",
        })
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return established


def judge(
    record: Dict[str, Any],
    usable_runs: List[Dict[str, Any]],
    canary_store: str,
) -> Dict[str, Any]:
    """Pure end-of-window efficacy verdict; no release or state mutation."""
    metric = str(record.get("efficacy_metric") or "per_animal")
    baseline = [
        float(value) for value in record.get("efficacy_baseline_samples") or []
        if _number(value) is not None
    ]
    candidate = metric_samples(usable_runs, metric)
    change_class = str(record.get("change_class") or "reliability")
    result: Dict[str, Any] = {
        "status": INSUFFICIENT,
        "accepted": False,
        "change_class": change_class,
        "metric": metric,
        "baseline_n": len(baseline),
        "candidate_n": len(candidate),
        "reason": "",
    }
    if len(candidate) < rules.EFFICACY_MIN_RUNS:
        result["reason"] = "%d/%d efficacy runs observed" % (len(candidate), rules.EFFICACY_MIN_RUNS)
        return result
    interval = _effect_interval(baseline, candidate)
    result.update({
        key: (round(value, 8) if isinstance(value, float) else value)
        for key, value in interval.items()
    })
    if interval["effect"] is None:
        if change_class == "strategy":
            result.update(status=REJECTED, reason="strategy candidate has no comparable baseline")
        else:
            result.update(status=EQUIVALENT, accepted=True,
                          reason="reliability release has no comparable efficacy baseline")
        return result

    prior = champion(canary_store)
    cumulative = float(prior.get("cumulative_ratio") or 1.0)
    projected = cumulative * (1.0 + float(interval["effect"]))
    result["prior_cumulative_ratio"] = round(cumulative, 6)
    result["projected_cumulative_ratio"] = round(projected, 6)

    if projected < 1.0 - rules.CUMULATIVE_REGRESSION_BUDGET:
        result.update(
            status=REJECTED,
            reason="cumulative release ratio %.3f exceeds the %.1f%% regression budget"
                   % (projected, 100.0 * rules.CUMULATIVE_REGRESSION_BUDGET),
        )
        return result

    lower = float(interval["lower"])
    effect = float(interval["effect"])
    if change_class == "strategy":
        expected = max(
            rules.STRATEGY_MIN_IMPROVEMENT,
            float(record.get("expected_improvement") or 0.0),
        )
        result["required_improvement"] = expected
        if effect >= expected and lower >= expected:
            result.update(
                status=IMPROVED,
                accepted=True,
                reason="strategy gain %.2f%% (lower bound %.2f%%) clears %.2f%%"
                       % (100.0 * effect, 100.0 * lower, 100.0 * expected),
            )
        else:
            result.update(
                status=REJECTED,
                reason="strategy gain %.2f%% (lower bound %.2f%%) does not prove %.2f%%"
                       % (100.0 * effect, 100.0 * lower, 100.0 * expected),
            )
        return result

    # Reliability changes are correctness repairs, not strategy claims. With only
    # one farm, the ten-run sequential rate is too noisy for a useful statistical
    # equivalence test (the interval routinely spans tens of percent). Use the
    # declared operational band for the point effect, retain the interval for
    # audit, and let the cumulative budget catch repeated small accepted losses.
    if effect < -rules.RELIABILITY_EQUIVALENCE_TOLERANCE:
        result.update(
            status=REJECTED,
            reason="reliability effect %.2f%% exceeds %.2f%% equivalence loss"
                   % (100.0 * effect, 100.0 * rules.RELIABILITY_EQUIVALENCE_TOLERANCE),
        )
        return result
    result.update(
        status=EQUIVALENT,
        accepted=True,
        reason="reliability effect %.2f%% is inside the %.2f%% operational band"
               % (100.0 * effect, 100.0 * rules.RELIABILITY_EQUIVALENCE_TOLERANCE),
    )
    return result


def record_resolution(
    record: Dict[str, Any],
    verdict: Dict[str, Any],
    canary_store: str,
) -> Dict[str, Any]:
    """Persist one efficacy/rejection event and advance champion only on acceptance."""
    champion_path, events_path, lock_path = _paths(canary_store)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = _read_json(champion_path)
        efficacy = dict(verdict.get("efficacy") or {})
        accepted = verdict.get("status") == "healthy" and (
            not efficacy or bool(efficacy.get("accepted"))
        )
        event = {
            "schema_version": SCHEMA_VERSION,
            "event": "candidate.accepted" if accepted else "candidate.rejected",
            "ts": _utcnow(),
            "revision": record.get("revision"),
            "previous": record.get("previous"),
            "policy_id": record.get("policy_id"),
            "hypothesis_id": record.get("hypothesis_id"),
            "change_class": record.get("change_class") or "reliability",
            "reason": verdict.get("reason"),
            "efficacy": efficacy,
        }
        _append(events_path, event)
        if accepted:
            projected = efficacy.get("projected_cumulative_ratio")
            cumulative = float(projected if projected is not None else current.get("cumulative_ratio") or 1.0)
            history = list(current.get("history") or [])[-19:]
            history.append({
                "revision": record.get("revision"),
                "policy_id": record.get("policy_id"),
                "hypothesis_id": record.get("hypothesis_id"),
                "change_class": record.get("change_class") or "reliability",
                "ts": _utcnow(),
                "cumulative_ratio": round(cumulative, 6),
            })
            updated = {
                "schema_version": SCHEMA_VERSION,
                "revision": record.get("revision"),
                "policy_id": record.get("policy_id"),
                "established_ts": _utcnow(),
                "established_run": verdict.get("last_run"),
                "cumulative_ratio": round(cumulative, 6),
                "history": history,
            }
            _atomic_json(champion_path, updated)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return event


def status(canary_store: str) -> Dict[str, Any]:
    champion_path, events_path, _ = _paths(canary_store)
    current = _read_json(champion_path)
    try:
        event_count = len([line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()])
    except (FileNotFoundError, OSError):
        event_count = 0
    return {"champion": current, "events": event_count}
