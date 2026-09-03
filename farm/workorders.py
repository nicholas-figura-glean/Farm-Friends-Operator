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

import fcntl
import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

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


@contextmanager
def _queue_lock(path: str) -> Iterator[None]:
    lock_path = path + ".lock"
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _append_unlocked(row: Dict[str, Any], path: str = QUEUE) -> Dict[str, Any]:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return row


def _append(row: Dict[str, Any], path: str = QUEUE) -> Dict[str, Any]:
    with _queue_lock(path):
        return _append_unlocked(row, path)


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

    with _queue_lock(path):
        existing = current(path).get(order_id)
        if (
            existing
            and existing.get("status") not in TERMINAL
            and existing.get("status") != FAILED
        ):
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
        return _append_unlocked(
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
                "intent": intent,
                "acceptance": list(acceptance or []),
                "files": list(files or []),
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
    """Atomically take ownership of one open order."""
    with _queue_lock(path):
        order = current(path).get(order_id)
        if (
            not order
            or order.get("status") not in {OPEN, FAILED}
            or (order.get("status") == FAILED and order.get("retryable") is not True)
            or int(order.get("attempts") or 0) >= MAX_ATTEMPTS
        ):
            return None
        return _append_unlocked(
            _event(
                order,
                CLAIMED,
                actor=actor,
                run=run,
                claim_token=secrets.token_hex(16),
                attempts=int(order.get("attempts") or 0) + 1,
            ),
            path,
        )


def lease_owned(order_id: str, claim_token: str, path: str = QUEUE) -> bool:
    order = current(path).get(order_id) or {}
    return bool(
        claim_token
        and order.get("status") == CLAIMED
        and order.get("claim_token") == claim_token
    )


def renew_claim(order_id: str, claim_token: str, path: str = QUEUE) -> Optional[Dict[str, Any]]:
    """Atomically renew a live author lease without incrementing attempts."""
    with _queue_lock(path):
        order = current(path).get(order_id)
        if (
            not order
            or order.get("status") != CLAIMED
            or order.get("claim_token") != claim_token
        ):
            return None
        return _append_unlocked(
            _event(order, CLAIMED, claim_token=claim_token), path
        )


def ensure_probe_path(order_id: str, path: str = QUEUE) -> Optional[Dict[str, Any]]:
    """Backfill the explicit new-file path required by bounded model creation."""
    with _queue_lock(path):
        order = current(path).get(order_id)
        if (
            not order
            or order.get("source") != "research_agent"
            or order.get("status") not in {OPEN, FAILED}
        ):
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
        return _append_unlocked(_event(order, str(order.get("status") or OPEN), files=files), path)


def attach_provenance(
    order_id: str,
    provenance: Dict[str, Any],
    path: str = QUEUE,
) -> Optional[Dict[str, Any]]:
    """Enrich a legacy live order without changing its queue status."""
    with _queue_lock(path):
        order = current(path).get(order_id)
        if (
            not order
            or order.get("status") not in {OPEN, FAILED}
            or bool(order.get("provenance"))
        ):
            return None
        return _append_unlocked(
            _event(order, str(order.get("status") or OPEN), provenance=dict(provenance)),
            path,
        )


def resolve(
    order_id: str,
    status: str,
    note: str = "",
    release: str = "",
    path: str = QUEUE,
    expected_status: Optional[Any] = None,
    expected_ts: Optional[str] = None,
    expected_claim_token: Optional[str] = None,
    **extra: Any,
) -> Optional[Dict[str, Any]]:
    with _queue_lock(path):
        order = current(path).get(order_id)
        if not order:
            return None
        if expected_status is not None:
            allowed = set(expected_status) if isinstance(expected_status, (set, list, tuple)) else {expected_status}
            if order.get("status") not in allowed:
                return None
        if expected_ts is not None and str(order.get("ts") or "") != str(expected_ts):
            return None
        if order.get("status") == CLAIMED:
            if not expected_claim_token or order.get("claim_token") != expected_claim_token:
                return None
        elif expected_claim_token is not None and order.get("claim_token") != expected_claim_token:
            return None
        return _append_unlocked(
            _event(order, status, note=note[:500], release=release, **extra), path
        )


def open_orders(path: str = QUEUE) -> List[Dict[str, Any]]:
    """Actionable orders, worst severity first, oldest first within a severity.

    A `claimed` order is included only if it looks abandoned; see `stale_claims`.
    """
    out = [
        order for order in current(path).values()
        if (
            order.get("status") == OPEN
            or (order.get("status") == FAILED and order.get("retryable") is True)
        )
        and int(order.get("attempts") or 0) < MAX_ATTEMPTS
    ]
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
    """Atomically return expired claims, including pre-token legacy rows."""
    released = []
    for stale in stale_claims(max_age_seconds, path):
        with _queue_lock(path):
            order = current(path).get(str(stale.get("id")))
            if (
                not order
                or order.get("status") != CLAIMED
                or str(order.get("ts") or "") != str(stale.get("ts") or "")
            ):
                continue
            status = ABANDONED if int(order.get("attempts") or 0) >= MAX_ATTEMPTS else OPEN
            note = (
                "claim expired; attempts exhausted"
                if status == ABANDONED
                else "claim expired; returned to queue"
            )
            released.append(_append_unlocked(
                _event(
                    order,
                    status,
                    note=note,
                    legacy_tokenless_claim=not bool(order.get("claim_token")),
                ),
                path,
            ))
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
