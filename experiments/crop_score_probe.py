#!/usr/bin/env python3
"""Bounded wheat score holdout after the current timer probe succeeds.

Plants a predeclared 5,000-plot wheat cohort (20,000 coins, one non-retried bulk
call), then attributes lifetime-produce growth against deduplicated animal
production events. Crop strategy is not changed by this script.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from farm import ledger, parse, policy, rules  # noqa: E402
from farm.mcp import Client  # noqa: E402

STATE = Path(os.environ.get("FARM_STATE_DIR", str(ROOT / "state"))).resolve()
PROBE = STATE / "crop_score_probe.json"
TOOL_CALLS = STATE / "tool_calls.ndjson"
EXPERIMENTS = STATE / "experiments.ndjson"
LOCK = STATE / ".lock"
DEFAULT_QTY = 5_000
MAX_QTY = 5_000
CROP = "wheat"
YIELD_PER_PLOT = 3
COST_PER_PLOT = 4
TIMER_MINUTES = 15
HARVEST_RE = re.compile(r"Harvested\s+\S+\s+(?P<qty>\d+)\s+wheat", re.IGNORECASE)
LIFETIME_RE = re.compile(r"Lifetime produce (?P<value>\d+)")
PRODUCTION_RE = re.compile(
    r"^(?P<hour>\d{2}:\d{2}) UTC\s+.*?Your animals produced\s+.*?"
    r"(?P<qty>\d+)\s+(?P<item>egg|honey|milk|truffle|wool)\.?$",
    re.IGNORECASE,
)
COLLECT_RE = re.compile(
    r"(?P<qty>\d+)\s+(?P<item>egg|honey|milk|truffle|wool)",
    re.IGNORECASE,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def latest_run() -> int:
    best = 0
    for line in (STATE / "history.ndjson").read_text(errors="replace").splitlines():
        try:
            best = max(best, int(json.loads(line).get("run") or 0))
        except (TypeError, ValueError):
            pass
    return best


def append_experiment(row: Dict[str, Any]) -> None:
    with EXPERIMENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def start(qty: int) -> Dict[str, Any]:
    if qty < 100 or qty > MAX_QTY:
        raise ValueError("qty must be between 100 and %d" % MAX_QTY)
    handle = LOCK.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        runtime = policy.runtime_context()
        ledger.set_context(actor="probe", step="crop_score_holdout", probe="crop-score-wheat", policy_id=runtime.get("policy_id"))
        client = Client()
        before = parse.parse_farm(client.call("list_farm"))
        if before.crisis or before.food_crop_count:
            raise RuntimeError("probe requires crisis-free farm with no food crops")
        if not before.plot_capacity or before.plot_capacity - before.plot_count < qty:
            raise RuntimeError("insufficient plot capacity")
        cost = qty * COST_PER_PLOT
        if before.coins - cost < rules.RISK_COIN_RESERVE:
            raise RuntimeError("probe would cross risk coin reserve")
        intervention = ledger.intervention(
            "crop_score_holdout", "planned",
            {"crop": CROP, "qty": qty, "cost": cost, "expected_yield": qty * YIELD_PER_PLOT},
        )
        started = utcnow()
        response = client.call("plant", kind=CROP, qty=qty, _transport_retries=1)
        after = parse.parse_farm(client.call("list_farm"))
        state = {
            "schema_version": 1,
            "experiment": "crop_score_holdout",
            "status": "observing",
            "started_ts": started,
            "baseline_run": latest_run(),
            "intervention_id": intervention,
            "planned": {
                "crop": CROP,
                "qty": qty,
                "cost": cost,
                "expected_yield": qty * YIELD_PER_PLOT,
                "declared_minutes": TIMER_MINUTES,
                "budget": {"calls": 3, "coins": cost, "plots": qty, "wall_seconds": 1800},
                "before": {
                    "coins": before.coins,
                    "lifetime_produce": before.lifetime_produce,
                    "plots": before.plot_count,
                    "animals": before.animal_count,
                },
            },
            "plant_response": response,
            "after_plant": {
                "coins": after.coins,
                "lifetime_produce": after.lifetime_produce,
                "plots": after.plot_count,
                "plot_counts": after.counts_by_crop,
            },
            "falsifier": "harvested wheat creates no lifetime-produce residual above same-window animal production",
        }
        PROBE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        append_experiment(dict(state, event="crop_score_holdout.started"))
        ledger.intervention("crop_score_holdout", "outcome", state, intervention_id=intervention)
        return state
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _event_datetime(day: datetime, hour: str) -> datetime:
    h, minute = [int(value) for value in hour.split(":")]
    return day.replace(hour=h, minute=minute, second=0, microsecond=0)


def analyze() -> Dict[str, Any]:
    state = json.loads(PROBE.read_text(encoding="utf-8"))
    prior_result = state.get("result") if isinstance(state.get("result"), dict) else None
    started = parse_ts(state.get("started_ts"))
    if not started:
        raise RuntimeError("probe has no valid start timestamp")
    harvest: Optional[Dict[str, Any]] = None
    lifetime_samples: List[Tuple[datetime, int, str]] = []
    animal_events: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    collection_calls: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []
    for line in TOOL_CALLS.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        ts = parse_ts(row.get("ts"))
        if not ts or ts < started or row.get("event") != "end":
            continue
        rows.append(row)
        result = str(row.get("result") or "")
        if row.get("tool") == "harvest":
            match = HARVEST_RE.search(result)
            if match and harvest is None:
                harvest = {
                    "ts": row.get("ts"), "time": ts, "run": row.get("run"),
                    "yield": int(match.group("qty")), "result": result[:300],
                    "elapsed_minutes": (ts - started).total_seconds() / 60.0,
                }
        if row.get("tool") == "list_farm":
            match = LIFETIME_RE.search(result)
            if match:
                lifetime_samples.append((ts, int(match.group("value")), str(row.get("step") or "")))
        if row.get("tool") == "collect_produce":
            items = [
                {"item": match.group("item").lower(), "qty": int(match.group("qty"))}
                for match in COLLECT_RE.finditer(result)
            ]
            collection_calls.append({
                "ts": row.get("ts"), "time": ts, "run": row.get("run"),
                "items": items, "units": sum(item["qty"] for item in items),
                "result": result[:300],
            })
        if row.get("tool") == "farm_events":
            for event_line in result.splitlines():
                match = PRODUCTION_RE.match(event_line.strip())
                if not match:
                    continue
                event_time = _event_datetime(started, match.group("hour"))
                if event_time < started - timedelta(minutes=1):
                    continue
                key = (match.group("hour"), match.group("item").lower(), int(match.group("qty")))
                animal_events[key] = {
                    "ts": event_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "item": match.group("item").lower(),
                    "qty": int(match.group("qty")),
                }
    baseline = int(((state.get("planned") or {}).get("before") or {}).get("lifetime_produce") or 0)
    expected = int((state.get("planned") or {}).get("expected_yield") or 0)
    if not harvest:
        result = dict(state, status="observing", evaluated_ts=utcnow(), reason="harvest not observed yet")
        return result
    cutoff = harvest["time"] + timedelta(minutes=6)
    relevant_lifetime = [value for ts, value, _ in lifetime_samples if ts <= cutoff]
    latest_lifetime = max(relevant_lifetime, default=baseline)
    relevant_events = [
        event for event in animal_events.values()
        if parse_ts(event["ts"]) and parse_ts(event["ts"]) <= cutoff
    ]
    relevant_collections = [
        {key: value for key, value in call.items() if key != "time"}
        for call in collection_calls if call["time"] <= cutoff
    ]
    collected_animal_units = sum(int(call["units"]) for call in relevant_collections)
    # Collection responses are authoritative quantities for this bounded window.
    # Event text is only a fallback because farm_events is truncated and often
    # omits the underlying production lines.
    event_animal_units = sum(int(event["qty"]) for event in relevant_events)
    animal_units = collected_animal_units if relevant_collections else event_animal_units
    attribution_source = "collect_produce" if relevant_collections else "deduplicated_farm_events"
    lifetime_delta = latest_lifetime - baseline
    residual = lifetime_delta - animal_units
    supported = bool(
        harvest["yield"] >= expected
        and harvest["elapsed_minutes"] <= TIMER_MINUTES + 6
        and residual >= expected * 0.75
    )
    result = {
        "schema_version": 1,
        "experiment": "crop_score_holdout",
        "status": "complete",
        "evaluated_ts": utcnow(),
        "started_ts": state.get("started_ts"),
        "baseline_run": state.get("baseline_run"),
        "planned": state.get("planned"),
        "harvest": {key: value for key, value in harvest.items() if key != "time"},
        "baseline_lifetime_produce": baseline,
        "latest_lifetime_produce": latest_lifetime,
        "lifetime_delta": lifetime_delta,
        "animal_production_events": sorted(relevant_events, key=lambda item: item["ts"]),
        "animal_collection_calls": relevant_collections,
        "animal_production_units": animal_units,
        "animal_attribution_source": attribution_source,
        "crop_score_residual": residual,
        "minimum_supported_residual": expected * 0.75,
        "supported": supported,
        "decision": "enable_bounded_wheat_ramp" if supported else "do_not_promote_crops",
        "policy_changed": False,
        "falsifier": state.get("falsifier"),
    }
    if prior_result and prior_result.get("crop_score_residual") != result.get("crop_score_residual"):
        result["supersedes_evaluation"] = prior_result.get("evaluated_ts")
        event = "crop_score_holdout.corrected"
    else:
        event = "crop_score_holdout.completed"
    state.update({"status": "complete", "result": result, "completed_ts": utcnow()})
    PROBE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    append_experiment(dict(result, event=event))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--qty", type=int, default=DEFAULT_QTY)
    args = parser.parse_args()
    result = start(args.qty) if args.start else analyze()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    if result.get("status") == "observing":
        return 3
    return 0 if result.get("supported") or args.start else 2


if __name__ == "__main__":
    raise SystemExit(main())
