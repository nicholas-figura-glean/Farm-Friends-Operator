"""Token and cost ledger for the exception path.

The deterministic loop is supposed to cost nothing, and this module is how that
claim gets audited instead of asserted. Routine runs record a zero-token row.
The only rows with a real cost are LLM wake-ups: the moment `--alerts` hands a
payload to a model. Tokens are estimated from payload size (chars/4) plus the
fixed context every wake-up carries; prices live in rules.py.

Nothing here talks to a model or a network. It is arithmetic over a log.
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import rules

LEDGER = os.path.join("state", "tokens.ndjson")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def estimate_tokens(text: str) -> int:
    """Cheap, stable token estimate. Deliberately not a tokenizer dependency."""
    if not text:
        return 0
    return int(math.ceil(len(text) / rules.CHARS_PER_TOKEN))


def cost(tokens_in: int, tokens_out: int) -> float:
    return round(
        (tokens_in or 0) / 1_000_000.0 * rules.LLM_INPUT_COST_PER_MTOK
        + (tokens_out or 0) / 1_000_000.0 * rules.LLM_OUTPUT_COST_PER_MTOK,
        6,
    )


def _append(row: Dict[str, Any]) -> Dict[str, Any]:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def record(
    kind: str,
    run: Optional[int],
    tokens_in: int = 0,
    tokens_out: int = 0,
    escalated: bool = False,
    healed: int = 0,
    note: str = "",
    reservation_id: str = "",
) -> Dict[str, Any]:
    tokens_in = int(tokens_in or 0)
    tokens_out = int(tokens_out or 0)
    return _append(
        {
            "ts": _utcnow(),
            "kind": kind,
            "run": run,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens": tokens_in + tokens_out,
            "cost_usd": cost(tokens_in, tokens_out),
            "escalated": bool(escalated),
            "healed": int(healed or 0),
            "note": note[:200],
            "reservation_id": str(reservation_id or "")[:160],
        }
    )


def record_cycle(run: Optional[int], note: str = "") -> Dict[str, Any]:
    """A completed deterministic run: zero tokens, logged so the zero is visible."""
    return record("cycle", run, note=note)


def record_heal(run: Optional[int], healed: int, note: str = "") -> Dict[str, Any]:
    """Alerts remediated in Python. Zero tokens, and it avoided a wake-up."""
    return record("heal", run, healed=healed, note=note)


def record_escalation(run: Optional[int], payload: str, note: str = "") -> Dict[str, Any]:
    """An LLM is about to read `payload`. This is the only row that costs money."""
    tokens_in, tokens_out, _ = rules.escalation_cost(estimate_tokens(payload))
    return record(
        "escalation",
        run,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        escalated=True,
        note=note,
    )


def avoided_cost(healed_count: int) -> float:
    """What the healed alerts would have cost had they woken a model."""
    usd = rules.escalation_cost(200)[2]
    return round(usd * max(0, int(healed_count or 0)), 6)


def tail(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read valid ledger rows.

    Lifetime totals must not silently become a tail as the ledger grows. Callers
    that only render recent detail pass an explicit limit; all-time summaries
    leave it unset.
    """
    try:
        lines = open(LEDGER).read().splitlines()
    except OSError:
        return []
    selected = lines[-limit:] if isinstance(limit, int) and limit > 0 else lines
    out = []
    for line in selected:
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


def by_run(limit: Optional[int] = None) -> Dict[int, Dict[str, Any]]:
    """Per-run totals, so a dashboard can join cost onto history rows."""
    totals: Dict[int, Dict[str, Any]] = {}
    for row in tail(limit):
        run = row.get("run")
        if run is None:
            continue
        bucket = totals.setdefault(
            int(run),
            {"tokens": 0, "cost_usd": 0.0, "escalations": 0, "healed": 0},
        )
        bucket["tokens"] += int(row.get("tokens") or 0)
        bucket["cost_usd"] = round(bucket["cost_usd"] + float(row.get("cost_usd") or 0.0), 6)
        bucket["escalations"] += 1 if row.get("escalated") else 0
        bucket["healed"] += int(row.get("healed") or 0)
    return totals


def summary(hours: int = 24, limit: Optional[int] = None) -> Dict[str, Any]:
    rows = tail(limit)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    window: List[Dict[str, Any]] = []
    for row in rows:
        try:
            stamp = datetime.strptime(row.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if stamp >= cutoff:
            window.append(row)

    healed_total = sum(int(r.get("healed") or 0) for r in rows)
    runs = sorted({int(r["run"]) for r in rows if r.get("run") is not None})
    # The dashboard joins only recent history rows. Keep that projection bounded
    # while lifetime totals above remain unbounded and authoritative.
    per_run = by_run(400)
    latest_run = runs[-1] if runs else None
    empty = {"tokens": 0, "cost_usd": 0.0, "escalations": 0, "healed": 0}
    return {
        "latest_run": latest_run,
        "latest": per_run.get(latest_run, empty),
        "window_hours": hours,
        "window_tokens": sum(int(r.get("tokens") or 0) for r in window),
        "window_cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in window), 6),
        "window_escalations": sum(1 for r in window if r.get("escalated")),
        "window_healed": sum(int(r.get("healed") or 0) for r in window),
        "total_tokens": sum(int(r.get("tokens") or 0) for r in rows),
        "total_cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in rows), 6),
        "total_escalations": sum(1 for r in rows if r.get("escalated")),
        "total_healed": healed_total,
        "avoided_cost_usd": avoided_cost(healed_total),
        "per_run": per_run,
        "cost_per_escalation_usd": rules.escalation_cost(200)[2],
    }
