"""The work queue between detection and repair.

The contract watcher and the research agent both discover things that need a code
change. Neither is allowed to edit code: detection and mutation are separated so
that a noisy detector cannot rewrite the farm on its own, and so every change has
a durable, reviewable reason attached to it.

A work order is that reason. It records what was observed, which files and call
sites are implicated, what a correct fix looks like, and how the outcome will be
judged. The author agent consumes orders; it never invents them.

Lifecycle
---------
    open -> claimed -> (published | failed | abandoned)
                   \\-> superseded   (a newer order covers the same change)

The queue is an append-only NDJSON event log with one current row per order id,
matching the pattern already used by `state/questions.ndjson`. Append-only means
a crashed author agent cannot corrupt history, and the audit trail of what was
attempted survives even when a change is rolled back.

Ordering is by severity then age: a `breaking` order jumps the queue, because the
farm may already be failing while an `opportunity` order can wait for the next
pass.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

QUEUE = os.path.join("state", "workorders.ndjson")

OPEN = "open"
CLAIMED = "claimed"
PUBLISHED = "published"
FAILED = "failed"
ABANDONED = "abandoned"
SUPERSEDED = "superseded"

TERMINAL = (PUBLISHED, ABANDONED, SUPERSEDED)

# Worst first. Dashboard degradation is operational repair work and must not sit
# behind speculative opportunities merely because contract.SEVERITIES lacks it.
SEVERITY_ORDER = ("breaking", "degraded", "shape", "opportunity", "additive", "cosmetic")

# An order that has failed this many times stops being retried. Without this a
# change the model cannot fix becomes an infinite, billable loop.
MAX_ATTEMPTS = 3


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(row: Dict[str, Any], path: str = QUEUE) -> Dict[str, Any]:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def _rows(path: str = QUEUE) -> List[Dict[str, Any]]:
    try:
        lines = open(path, "r", encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("id"):
            out.append(value)
    return out


def current(path: str = QUEUE) -> Dict[str, Dict[str, Any]]:
    """Latest row per order id, retaining immutable first-submission time."""
    latest: Dict[str, Dict[str, Any]] = {}
    created: Dict[str, str] = {}
    for row in _rows(path):
        order_id = str(row["id"])
        first = created.setdefault(order_id, str(row.get("created_ts") or row.get("ts") or ""))
        value = dict(row)
        value["created_ts"] = str(row.get("created_ts") or first)
        latest[order_id] = value
    return latest


def submit(
    change: Dict[str, Any],
    source: str,
    intent: str,
    acceptance: Optional[List[str]] = None,
    files: Optional[List[str]] = None,
    path: str = QUEUE,
    provenance: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """File an order for a detected change, unless one is already live.

    Idempotent by change id: the watcher re-detects the same drift on every scan
    until it is fixed, and each of those scans must not enqueue another order.
    Returns None when the order already exists and is not terminal.
    """
    order_id = str(change.get("id") or "")
    if not order_id:
        return None

    existing = current(path).get(order_id)
    if existing and existing.get("status") not in TERMINAL:
        return None
    if existing and existing.get("status") == PUBLISHED:
        # Already fixed and shipped. If the watcher still sees it, the fix did
        # not take: reopen with the attempt count carried forward so MAX_ATTEMPTS
        # still applies and we cannot loop forever.
        pass

    attempts = int((existing or {}).get("attempts") or 0)
    if attempts >= MAX_ATTEMPTS:
        return None

    created_ts = _utcnow()
    return _append(
        {
            "id": order_id,
            "ts": created_ts,
            "created_ts": created_ts,
            "status": OPEN,
            "source": source,
            "severity": str(change.get("severity") or "additive"),
            "kind": str(change.get("kind") or "unknown"),
            "tool": str(change.get("tool") or ""),
            "summary": str(change.get("summary") or "")[:400],
            "we_use_it": bool(change.get("we_use_it")),
            "sites": list(change.get("sites") or []),
            "detail": change.get("detail") or {},
            # What the author agent is being asked to achieve, in prose. This is
            # the prompt's spine, so it is stored rather than regenerated.
            "intent": intent,
            # Objective, checkable conditions. The gate matrix is generic; these
            # are the order-specific ones.
            "acceptance": list(acceptance or []),
            "files": list(files or []),
            # Research-authored orders carry the pre-registered hypothesis and
            # cohort contract through authoring, release, canary, and promotion.
            "provenance": dict(provenance or {}),
            "attempts": attempts,
        },
        path,
    )


def _event(order: Dict[str, Any], status: str, **extra: Any) -> Dict[str, Any]:
    row = dict(order)
    row["status"] = status
    row["ts"] = _utcnow()
    row.update(extra)
    return row


def claim(order_id: str, actor: str, run: Optional[int] = None, path: str = QUEUE) -> Optional[Dict[str, Any]]:
    """Take ownership of an order and increment its attempt count."""
    order = current(path).get(order_id)
    if not order or order.get("status") in TERMINAL:
        return None
    return _append(
        _event(order, CLAIMED, actor=actor, run=run, attempts=int(order.get("attempts") or 0) + 1),
        path,
    )


def ensure_probe_path(order_id: str, path: str = QUEUE) -> Optional[Dict[str, Any]]:
    """Backfill the explicit new-file path required by bounded model creation."""
    order = current(path).get(order_id)
    if not order or order.get("source") != "research_agent":
        return None
    if order.get("kind") not in {"strategy_hypothesis", "unused_capability"}:
        return None
    stem = re.sub(r"^research-(?:hypothesis|capability)-", "", order_id)
    stem = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")[:48] or "generated"
    probe_path = "experiments/%s_probe.py" % stem
    files = list(order.get("files") or [])
    if probe_path in files:
        return None
    files = [probe_path] + files
    return _append(_event(order, str(order.get("status") or OPEN), files=files), path)


def attach_provenance(
    order_id: str,
    provenance: Dict[str, Any],
    path: str = QUEUE,
) -> Optional[Dict[str, Any]]:
    """Enrich a legacy live order without changing its queue status."""
    order = current(path).get(order_id)
    if not order:
        return None
    return _append(
        _event(order, str(order.get("status") or OPEN), provenance=dict(provenance)),
        path,
    )


def resolve(
    order_id: str,
    status: str,
    note: str = "",
    release: str = "",
    path: str = QUEUE,
    **extra: Any,
) -> Optional[Dict[str, Any]]:
    order = current(path).get(order_id)
    if not order:
        return None
    return _append(_event(order, status, note=note[:500], release=release, **extra), path)


def open_orders(path: str = QUEUE) -> List[Dict[str, Any]]:
    """Actionable orders, worst severity first, oldest first within a severity.

    A `claimed` order is included only if it looks abandoned; see `stale_claims`.
    """
    out = [o for o in current(path).values() if o.get("status") == OPEN]
    out.sort(key=lambda o: (_severity_rank(o), o.get("created_ts") or o.get("ts") or ""))
    return out


def _severity_rank(order: Dict[str, Any]) -> int:
    severity = str(order.get("severity") or "")
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def next_order(path: str = QUEUE) -> Optional[Dict[str, Any]]:
    orders = open_orders(path)
    return orders[0] if orders else None


def stale_claims(max_age_seconds: int = 3600, path: str = QUEUE) -> List[Dict[str, Any]]:
    """Orders claimed long ago and never resolved.

    The author agent can be killed mid-pass (launchd restart, machine sleep, a
    revoked token). Without this, such an order would stay `claimed` forever and
    silently block its own repair.
    """
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for order in current(path).values():
        if order.get("status") != CLAIMED:
            continue
        try:
            when = datetime.strptime(str(order.get("ts")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - when).total_seconds() > max_age_seconds:
            out.append(order)
    return out


def release_stale(max_age_seconds: int = 3600, path: str = QUEUE) -> List[Dict[str, Any]]:
    """Return abandoned claims to the queue so they can be retried."""
    released = []
    for order in stale_claims(max_age_seconds, path):
        if int(order.get("attempts") or 0) >= MAX_ATTEMPTS:
            released.append(_append(_event(order, ABANDONED, note="claim expired; attempts exhausted"), path))
        else:
            released.append(_append(_event(order, OPEN, note="claim expired; returned to queue"), path))
    return released


def summary(path: str = QUEUE) -> Dict[str, Any]:
    rows = current(path).values()
    counts: Dict[str, int] = {}
    for order in rows:
        status = str(order.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    pending = open_orders(path)
    return {
        "total": len(list(rows)),
        "by_status": counts,
        "open": len(pending),
        "breaking_open": sum(1 for o in pending if o.get("severity") == "breaking"),
        "next": pending[0]["id"] if pending else None,
    }
