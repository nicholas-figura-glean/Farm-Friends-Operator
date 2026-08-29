#!/usr/bin/env python3
"""Bounded current-regime falsifier for food-crop timers and per-plot yield.

``--start`` plants exactly one wheat, corn, and pumpkin (17 coins, three calls)
under the cycle lock, then exits. ``--analyze`` reads immutable tool telemetry and
records observed harvest time/yield without another mutation. It does not promote
large-scale planting; crop contribution to lifetime score requires a later scaled
holdout once timers are established.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from farm import ledger, parse, policy, rules  # noqa: E402
from farm.mcp import Client  # noqa: E402

STATE = ROOT / "state"
PROBE = STATE / "dual_cap_probe.json"
EXPERIMENTS = STATE / "experiments.ndjson"
TOOL_CALLS = STATE / "tool_calls.ndjson"
LOCK = STATE / ".lock"
CROPS = {"wheat": {"cost": 4, "minutes": 15}, "corn": {"cost": 5, "minutes": 20}, "pumpkin": {"cost": 8, "minutes": 30}}
HARVEST_RE = re.compile(r"Harvested\s+\S+\s+(?P<qty>\d+)\s+(?P<crop>wheat|corn|pumpkin)", re.IGNORECASE)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _append(row: Dict[str, Any]) -> None:
    with EXPERIMENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _latest_run() -> int:
    best = 0
    for line in (STATE / "history.ndjson").read_text(errors="replace").splitlines():
        try:
            best = max(best, int(json.loads(line).get("run") or 0))
        except (TypeError, ValueError):
            continue
    return best


def start() -> Dict[str, Any]:
    handle = LOCK.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        runtime = policy.runtime_context()
        ledger.set_context(actor="probe", step="crop_timer", probe="crop-timer", policy_id=runtime.get("policy_id"))
        client = Client()
        before = parse.parse_farm(client.call("list_farm"))
        if before.crisis:
            raise RuntimeError("active crisis %s" % before.crisis.kind)
        if before.plot_capacity is None or before.plot_capacity - before.plot_count < len(CROPS):
            raise RuntimeError("fewer than three plot slots remain")
        cost = sum(item["cost"] for item in CROPS.values())
        if before.coins - cost < rules.RISK_COIN_RESERVE:
            raise RuntimeError("crop probe would cross the risk coin reserve")
        intervention = ledger.intervention(
            "crop_timer_probe", "planned",
            {"crops": list(CROPS), "budget": {"coins": cost, "calls": 3, "plots": 3}},
        )
        responses = {
            crop: client.call("plant", kind=crop, _transport_retries=1)
            for crop in CROPS
        }
        after = parse.parse_farm(client.call("list_farm"))
        row = {
            "schema_version": 1,
            "experiment": "dual_cap_crop_timer",
            "status": "observing",
            "started_ts": utcnow(),
            "baseline_run": _latest_run(),
            "budget": {"coins": cost, "calls": 3, "plots": 3, "wall_seconds": 2400},
            "before": {"coins": before.coins, "plots": before.plot_count, "lifetime_produce": before.lifetime_produce},
            "after": {"coins": after.coins, "plots": after.plot_count, "lifetime_produce": after.lifetime_produce},
            "responses": responses,
            "intervention_id": intervention,
            "falsifier": "any crop advances and harvests inside its current declared timer window",
        }
        PROBE.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _append(dict(row, event="dual_cap_crop_timer.started"))
        ledger.intervention("crop_timer_probe", "outcome", row, intervention_id=intervention)
        return row
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def analyze() -> Dict[str, Any]:
    state = json.loads(PROBE.read_text(encoding="utf-8"))
    started = parse_ts(state.get("started_ts"))
    observations: Dict[str, Dict[str, Any]] = {}
    noops: List[Dict[str, Any]] = []
    for line in TOOL_CALLS.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        ts = parse_ts(row.get("ts"))
        if not started or not ts or ts < started or row.get("event") != "end" or row.get("tool") != "harvest":
            continue
        result = str(row.get("result") or "")
        matches = list(HARVEST_RE.finditer(result))
        if not matches:
            noops.append({"ts": row.get("ts"), "run": row.get("run"), "result": result[:200]})
            continue
        for match in matches:
            crop = match.group("crop").lower()
            observations.setdefault(crop, {
                "crop": crop,
                "harvested_ts": row.get("ts"),
                "run": row.get("run"),
                "yield": int(match.group("qty")),
                "elapsed_minutes": round((ts - started).total_seconds() / 60.0, 3),
                "declared_minutes": CROPS[crop]["minutes"],
                "plant_cost": CROPS[crop]["cost"],
                "sale_value": rules.ITEM_VALUE[crop],
            })
    for item in observations.values():
        item["units_per_plot_minute"] = item["yield"] / float(item["elapsed_minutes"] or 1)
        item["sale_revenue"] = item["yield"] * item["sale_value"]
        item["net_coins"] = item["sale_revenue"] - item["plant_cost"]
        item["timer_within_tolerance"] = item["elapsed_minutes"] <= item["declared_minutes"] + 6
    complete = set(observations) == set(CROPS)
    timer_supported = complete and all(item["timer_within_tolerance"] for item in observations.values())
    best = max(
        observations.values(),
        key=lambda item: item["units_per_plot_minute"],
        default=None,
    )
    result = {
        "schema_version": 1,
        "experiment": "dual_cap_crop_timer",
        "status": "complete" if complete else "observing",
        "evaluated_ts": utcnow(),
        "started_ts": state.get("started_ts"),
        "baseline_run": state.get("baseline_run"),
        "budget": state.get("budget"),
        "observations": observations,
        "noop_harvests": noops,
        "all_timers_supported": timer_supported,
        "old_stalled_timer_claim_falsified": bool(observations),
        "best_observed_crop": best.get("crop") if best else None,
        "best_units_per_plot_minute": best.get("units_per_plot_minute") if best else None,
        "score_attribution": "requires scaled alternating holdout; three plots are below animal-score noise",
        "next_probe": "bounded crop-score holdout" if complete else "wait for declared timers",
        "policy_changed": False,
    }
    state.update({"status": result["status"], "result": result})
    PROBE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if complete:
        _append(dict(result, event="dual_cap_crop_timer.completed"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--analyze", action="store_true")
    args = parser.parse_args()
    if args.start:
        result = start()
    else:
        result = analyze()
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("status") == "observing":
        return 3
    return 0 if result.get("all_timers_supported") or args.start else 2


if __name__ == "__main__":
    raise SystemExit(main())
