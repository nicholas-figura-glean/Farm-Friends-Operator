"""Normalized, append-only epistemic observations and execution context.

The existing history, intent, and tool-call logs remain intact. This ledger adds
stable actor/run/policy attribution and normalized intervention outcomes without
putting an analytics dependency on the mutation path. Every write is best effort:
observability may never break a farm cycle.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from . import compaction

SCHEMA_VERSION = 1
_CONTEXT: contextvars.ContextVar = contextvars.ContextVar("farm_epistemic_context", default={})
_COUNTER_LOCK = threading.Lock()
_COUNTER = 0


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parent.parent / "state"


def _path() -> Path:
    override = os.environ.get("FARM_OBSERVATION_LOG")
    return Path(override).resolve() if override else _state_dir() / "observations.ndjson"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str = "evt") -> str:
    global _COUNTER
    with _COUNTER_LOCK:
        _COUNTER += 1
        counter = _COUNTER
    return "%s-%d-%d-%s" % (prefix, os.getpid(), counter, uuid.uuid4().hex[:10])


def current() -> Dict[str, Any]:
    value = _CONTEXT.get()
    return dict(value) if isinstance(value, dict) else {}


@contextlib.contextmanager
def bind(**values: Any) -> Iterator[Dict[str, Any]]:
    merged = current()
    for key, value in values.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


def set_context(**values: Any):
    merged = current()
    for key, value in values.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return _CONTEXT.set(merged)


def reset_context(token) -> None:
    _CONTEXT.reset(token)


def _bounded(value: Any, depth: int = 0) -> Any:
    """Bound telemetry recursively while preserving useful structured values."""
    if depth > 5:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 2_000 else value[:1_999] + "…"
    if isinstance(value, dict):
        items = list(value.items())[:100]
        return {str(key)[:120]: _bounded(item, depth + 1) for key, item in items}
    if isinstance(value, (list, tuple, set)):
        return [_bounded(item, depth + 1) for item in list(value)[:100]]
    return _bounded(str(value), depth + 1)


def _append(row: Dict[str, Any], strict: bool = False) -> bool:
    return compaction.append_json(_path(), row, strict=strict)


def rows(include_invalid: bool = False) -> list:
    """Read observation rows, applying append-only invalidation markers."""
    parsed = compaction.read_rows(_path())
    if include_invalid:
        return parsed
    invalid = {
        event_id
        for row in parsed
        if row.get("event") == "observation.invalidated"
        for event_id in ((row.get("data") or {}).get("invalid_event_ids") or [])
    }
    return [row for row in parsed if row.get("event_id") not in invalid]


def record(
    event: str,
    data: Optional[Dict[str, Any]] = None,
    event_id: Optional[str] = None,
    strict: bool = False,
    **context: Any,
) -> str:
    merged = current()
    merged.update({key: value for key, value in context.items() if value is not None})
    identity = event_id or new_id(event.rsplit(".", 1)[-1])
    row: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": identity,
        "event": str(event),
        "ts": utcnow(),
        "monotonic_ns": time.monotonic_ns(),
        "data": _bounded(data or {}),
    }
    for key in (
        "actor", "run", "sprint", "step", "worker", "policy_id",
        "claim_registry_version", "release",
    ):
        value = merged.get(key)
        if value is not None:
            row[key] = _bounded(value)
    _append(row, strict=strict)
    return identity


def tool_context() -> Dict[str, Any]:
    """Small context projection safe to copy onto every MCP span."""
    value = current()
    return {
        key: value[key]
        for key in (
            "actor", "run", "sprint", "step", "worker", "policy_id",
            "claim_registry_version", "release",
        )
        if value.get(key) is not None
    }


def intervention(
    action: str,
    phase: str,
    detail: Optional[Dict[str, Any]] = None,
    intervention_id: Optional[str] = None,
    **context: Any,
) -> str:
    identity = intervention_id or new_id("int")
    payload = {"intervention_id": identity, "action": action, "phase": phase}
    payload.update(detail or {})
    record("intervention.%s" % phase, payload, **context)
    return identity


def record_cycle(
    row: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    anomalies: Optional[list] = None,
) -> str:
    plan = row.get("plan") or {}
    inputs = row.get("plan_inputs") or {}
    expected_animals = int(inputs.get("animals") or 0) + int(row.get("adopted") or 0)
    actual_animals = int(row.get("animals") or 0)
    expected_reserve = int(row.get("reserve_target") or 0)
    actual_feed = int(row.get("feed") or 0)
    produce_delta = None
    if previous and isinstance(previous.get("produce"), int) and isinstance(row.get("produce"), int):
        produce_delta = int(row["produce"]) - int(previous["produce"])
    data = {
        "pre": {
            "run": (previous or {}).get("run"),
            "rank": (previous or {}).get("rank"),
            "produce": (previous or {}).get("produce"),
            "animals": inputs.get("animals"),
            "coins": inputs.get("coins"),
            "feed": inputs.get("feed"),
        },
        "decision": row.get("decision_trace") or {
            "objective": "maximize league level first, then lifetime produce, subject to hunger, feed, and transport safety",
            "selected": plan,
            "growth": row.get("growth"),
        },
        "interventions": {
            "adopt_requested": row.get("adopt_requested"),
            "adopted": row.get("adopted"),
            "feed_bought": row.get("feed_bought"),
            "fed": row.get("fed"),
            "sold": row.get("revenue"),
            "collect_passes": row.get("collect_passes"),
        },
        "post": {
            "rank": row.get("rank"),
            "produce": row.get("produce"),
            "produce_delta": produce_delta,
            "produce_per_min": row.get("produce_per_min"),
            "animals": actual_animals,
            "coins": row.get("coins"),
            "feed": actual_feed,
            "max_hunger": row.get("max_hunger"),
        },
        "verification": {
            "verified": bool(row.get("verified")),
            "animals_expected": expected_animals,
            "animals_actual": actual_animals,
            "animal_residual": actual_animals - expected_animals,
            "feed_target": expected_reserve,
            "feed_actual": actual_feed,
            "feed_residual": actual_feed - expected_reserve,
            "adoption_residual": int(row.get("adopted") or 0) - int(plan.get("adopt") or 0),
        },
        "anomalies": list(anomalies or []),
        "regimes": row.get("regimes") or [],
        "duration_s": row.get("duration_s"),
    }
    return record(
        "cycle.completed",
        data,
        actor="cycle",
        run=row.get("run"),
        policy_id=row.get("policy_id"),
        claim_registry_version=row.get("claim_registry_version"),
    )


def blind_window(
    previous: Dict[str, Any],
    observed: Dict[str, Any],
    minutes: float,
    findings: Optional[list] = None,
) -> str:
    return record(
        "observation.blind_window",
        {
            "from_run": previous.get("run"),
            "from_ts": previous.get("ts"),
            "to_ts": observed.get("ts"),
            "minutes": round(minutes, 2),
            "pre_gap": {
                "rank": previous.get("rank"),
                "produce": previous.get("produce"),
                "rivals": previous.get("rivals"),
                "rival_herds": previous.get("rival_herds"),
            },
            "reentry": observed,
            "findings": list(findings or []),
        },
        run=observed.get("run"),
    )
