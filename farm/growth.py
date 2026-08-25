"""Runtime marginal-growth brake: is buying more herd still buying score?

rules.py owns the pure arithmetic; this module owns its recent state and file
reads. The verdict is persisted so thin evidence holds the prior decision rather
than flip-flopping, while a nonzero maintenance cohort keeps falsification alive.

The historical per-farm plateau that first motivated this gate was wrong and is
explicitly superseded in the claim registry. Full, regime-filtered leaderboard
evidence shows healthy output scaling through more than 100,000 animals. This
module remains only as a reversible safety brake for a future mechanics change;
it is not the authoritative knowledge store.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import rules

STORE = os.path.join("state", "growth.json")
HISTORY = os.path.join("state", "history.ndjson")
# Enough tail to keep the smaller-herd cohort in view long after growth stops,
# but bounded so a long-lived farm never reads a huge file every cycle.
TAIL_BYTES = 2_000_000
TAIL_ROWS = 400


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load() -> Dict[str, Any]:
    try:
        with open(STORE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(model: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        tmp = "%s.tmp.%d" % (STORE, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(model, fh, indent=1, sort_keys=True)
        os.replace(tmp, STORE)
    except OSError:
        pass


def _tail_rows() -> List[Dict[str, Any]]:
    """Last N history rows, read from the end so file size stays irrelevant."""
    try:
        size = os.path.getsize(HISTORY)
        with open(HISTORY, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                fh.readline()  # discard the partial line
            blob = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = []
    for line in blob.splitlines()[-TAIL_ROWS:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and not row.get("dry"):
            rows.append(row)
    return rows


def production_stall_windows(
    rows: Optional[List[Dict[str, Any]]] = None,
    minimum: int = 2,
) -> int:
    """Count consecutive healthy verified windows with no lifetime-score gain.

    Zero-output windows are intentionally excluded from the marginal regression,
    but they cannot be excluded from the safety decision: doing so turned a real
    production halt into "insufficient samples" and retained a 400-adoption cap.
    """
    history = rows if rows is not None else _tail_rows()
    streak = 0
    for before, after in reversed(list(zip(history, history[1:]))):
        produced_before, produced_after = before.get("produce"), after.get("produce")
        start, end = _parse_ts(before.get("ts")), _parse_ts(after.get("ts"))
        if produced_before is None or produced_after is None or not start or not end:
            break
        minutes = (end - start).total_seconds() / 60.0
        if int(produced_after) != int(produced_before):
            break
        if (
            int(after.get("max_hunger") or 0) >= rules.HUNGER_ALARM
            or minutes < rules.MIN_INTERVAL_FOR_PRODUCE_CHECK
        ):
            break
        if not after.get("verified"):
            # Reconciliation uncertainty is not evidence of recovery. Skip the
            # window, but keep walking across it while the score itself is flat.
            continue
        streak += 1
        if streak >= minimum:
            # Continue counting for an honest reason string and dashboard value.
            continue
    return streak


def production_stall_active(
    rows: Optional[List[Dict[str, Any]]] = None,
    model: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, int]:
    """Latch a confirmed halt until lifetime produce advances again."""
    history = rows if rows is not None else _tail_rows()
    streak = production_stall_windows(history)
    if streak >= 2:
        return True, streak
    previous = model or {}
    if not previous.get("production_stalled"):
        return False, streak
    latest_gain = None
    if len(history) >= 2:
        before, after = history[-2].get("produce"), history[-1].get("produce")
        if isinstance(before, int) and isinstance(after, int):
            latest_gain = after - before
    if latest_gain is not None and latest_gain > 0:
        return False, 0
    return True, max(streak, int(previous.get("production_stall_windows") or 2))


def samples(rows: Optional[List[Dict[str, Any]]] = None) -> List[Tuple[int, float]]:
    """(animals, units/min) pairs from consecutive runs.

    Lifetime produce from the leaderboard is the measurement, not units collected:
    it counts production even when the barn is not drained (run 25 gained 41,207
    produce while a single collect call returned 572 units).
    """
    rows = rows if rows is not None else _tail_rows()
    raw: List[Tuple[int, float, int, int]] = []
    for before, after in zip(rows, rows[1:]):
        produced_before, produced_after = before.get("produce"), after.get("produce")
        if produced_before is None or produced_after is None:
            continue
        start, end = _parse_ts(before.get("ts")), _parse_ts(after.get("ts"))
        if not start or not end:
            continue
        minutes = (end - start).total_seconds() / 60.0
        raw.append(
            (
                int(after.get("animals") or 0),
                minutes,
                int(produced_after) - int(produced_before),
                int(after.get("max_hunger") or 0),
            )
        )
    return rules.clean_samples(raw)


def decide(
    current_animals: int,
    knobs: Dict[str, Any],
    run: Optional[int] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Verdict + adoption cap for this run, persisting any change of mind."""
    model = load()
    rows = _tail_rows()
    stalled, stalled_windows = production_stall_active(rows, model)
    verdict = rules.growth_verdict(samples(rows), current_animals, model)
    if stalled:
        verdict["production_stalled"] = True
        verdict["production_stall_windows"] = stalled_windows
        verdict["saturated"] = True
        verdict["reason"] = (
            "lifetime produce unchanged for %d healthy verified windows - "
            "adoption paused until production resumes" % stalled_windows
        )
    else:
        verdict["production_stalled"] = False
        verdict["production_stall_windows"] = stalled_windows
    cap, reason = rules.adoption_cap(verdict, knobs)

    changed = bool(model.get("saturated")) != bool(verdict.get("saturated"))
    record = dict(verdict)
    record["updated_ts"] = _utcnow()
    record["run"] = run
    record["cap"] = cap
    if changed or not model:
        record["changed_ts"] = _utcnow()
        record["changed_run"] = run
    else:
        record["changed_ts"] = model.get("changed_ts")
        record["changed_run"] = model.get("changed_run")
    if persist:
        save(record)

    return {"cap": cap, "reason": reason, "verdict": record, "changed": changed and persist}


def status() -> Dict[str, Any]:
    """Current growth policy for reporting, without re-measuring."""
    model = load()
    if not model:
        return {"saturated": False, "reason": "no measurement yet", "cap": None}
    return model
