#!/usr/bin/env python3
"""Read-only strategy audit for separate animal and plot capacity constraints.

The pre-cap policy optimized output per purchase coin. Once the herd is at a hard
league cap, the scarce input is an animal slot, so mature same-window output per
animal becomes the replacement metric. Plot capacity is independent and is
reported separately; this audit never plants, adopts, or changes policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from farm import compaction, parse  # noqa: E402

STATE = Path(os.environ.get("FARM_STATE_DIR", str(ROOT / "state"))).resolve()
HISTORY = STATE / "history.ndjson"
RAW_FARM = STATE / "raw" / "latest" / "list_farm_final.txt"
OUT = STATE / "dual_cap_audit.json"
CROP_SCORE = STATE / "crop_score_probe.json"
EXPERIMENTS = STATE / "experiments.ndjson"
MIN_WINDOWS = 5
MIN_SLOT_RATIO = 1.10
CAP_FRACTION = 0.90
MIN_FLOWERS = 8
# Wildflowers take ten minutes to bloom. At the normal five-minute cycle cadence,
# three consecutive observations (the planting row plus two later rows) prove that
# the whole-farm honey bonus was active for the measured collection interval.
MIN_FLOWER_QUALIFY_ROWS = 3


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cap_regime_start(rows: List[Dict[str, Any]]) -> Optional[int]:
    previous: Optional[Dict[str, Any]] = None
    for row in rows:
        if previous:
            before = int(previous.get("animals") or 0)
            after = int(row.get("animals") or 0)
            if before >= 50_000 and 0 < after <= before * 0.25:
                return int(row.get("run") or 0)
        previous = row
    return None


def _ratio_rows(
    rows: List[Dict[str, Any]],
    fallback_capacity: Optional[int],
    regime_start: Optional[int],
) -> List[Dict[str, Any]]:
    """Return bonus-qualified, pre-mutation species-rate observations.

    A cycle collects before it adopts and records the final herd afterward. Using
    that final herd as the collection denominator made newly adopted beehives look
    unproductive. The smaller of the previous and final species counts excludes
    same-cycle additions while still accounting for losses.

    Plot telemetry records planted flowers, not bloom state. Requiring three
    consecutive rows at the eight-flower floor covers the declared ten-minute
    bloom delay and prevents no-bonus intervals from falsifying a bonus-scoped
    claim. Legacy rows without plot telemetry are intentionally not guessed into
    the current-regime cohort.
    """
    samples: List[Dict[str, Any]] = []
    previous: Optional[Dict[str, Any]] = None
    flower_streak = 0
    for row in rows:
        plot_counts = row.get("plot_counts")
        flowers = (
            int(plot_counts.get("wildflowers") or 0)
            if isinstance(plot_counts, dict) else None
        )
        flower_streak = flower_streak + 1 if flowers is not None and flowers >= MIN_FLOWERS else 0

        run = int(row.get("run") or 0)
        capacity = int(row.get("animal_capacity") or 0)
        capacity_source = "history"
        if not capacity and fallback_capacity and regime_start and run >= regime_start:
            capacity = int(fallback_capacity)
            capacity_source = "inferred_from_current_contract_after_regime_boundary"
        animals = int(row.get("animals") or 0)
        by_kind = row.get("by_kind") or {}
        collected = row.get("collected") or {}
        reported_bees = int(by_kind.get("beehive") or 0)
        reported_chickens = int(by_kind.get("chicken") or 0)
        bees = reported_bees
        chickens = reported_chickens
        exposure_source = "current_state"
        previous_by_kind = (previous or {}).get("by_kind") or {}
        if previous_by_kind:
            prior_bees = int(previous_by_kind.get("beehive") or reported_bees)
            prior_chickens = int(previous_by_kind.get("chicken") or reported_chickens)
            bees = min(reported_bees, prior_bees)
            chickens = min(reported_chickens, prior_chickens)
            exposure_source = "minimum_of_previous_and_final_state"

        honey = int(collected.get("honey") or 0)
        eggs = int(collected.get("egg") or 0)
        interval = float(row.get("interval_min") or 0.0)
        eligible = bool(
            capacity
            and animals / float(capacity) >= CAP_FRACTION
            and flower_streak >= MIN_FLOWER_QUALIFY_ROWS
            and bees and chickens and honey and eggs and interval > 0
        )
        if eligible:
            bee_rate = honey / float(bees) / interval
            chicken_rate = eggs / float(chickens) / interval
            if chicken_rate > 0:
                samples.append({
                    "run": run,
                    "league": row.get("league"),
                    "capacity": capacity,
                    "capacity_source": capacity_source,
                    "animals": animals,
                    "beehives": bees,
                    "chickens": chickens,
                    "reported_beehives": reported_bees,
                    "reported_chickens": reported_chickens,
                    "exposure_source": exposure_source,
                    "wildflowers": flowers,
                    "flower_qualification_rows": flower_streak,
                    "honey": honey,
                    "eggs": eggs,
                    "interval_min": interval,
                    "beehive_per_animal_min": bee_rate,
                    "chicken_per_animal_min": chicken_rate,
                    "ratio": bee_rate / chicken_rate,
                })
        previous = row
    return samples


def _cohort_hash(samples: List[Dict[str, Any]]) -> str:
    material = json.dumps(samples, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def analyze(
    rows: Optional[List[Dict[str, Any]]] = None,
    farm: Optional[parse.Farm] = None,
) -> Dict[str, Any]:
    history = list(rows) if rows is not None else compaction.read_rows(HISTORY, limit=2_000)
    current = farm
    if current is None:
        try:
            current = parse.parse_farm(RAW_FARM.read_text(encoding="utf-8"))
        except (OSError, parse.ParseDrift):
            current = None
    regime_start = _cap_regime_start(history)
    samples = _ratio_rows(
        history,
        current.capacity if current else None,
        regime_start,
    )
    ratios = [float(item["ratio"]) for item in samples]
    median_ratio = statistics.median(ratios) if ratios else None
    minimum_ratio = min(ratios) if ratios else None
    capped = bool(
        current and current.capacity
        and current.animal_count / float(current.capacity) >= 0.99
    )
    flowers = int((current.counts_by_crop if current else {}).get("wildflowers") or 0)
    try:
        crop_state = json.loads(CROP_SCORE.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        crop_state = {}
    crop_result = crop_state.get("result") if isinstance(crop_state.get("result"), dict) else {}
    crop_score_supported = bool(crop_result.get("supported"))
    crop_policy = (
        "bounded_score_candidate" if crop_score_supported
        else "disabled_for_league_score" if crop_result.get("status") == "complete"
        else "await_current_timer_probe"
    )
    slot_supported = bool(
        len(samples) >= MIN_WINDOWS
        and median_ratio is not None and median_ratio >= MIN_SLOT_RATIO
        and minimum_ratio is not None and minimum_ratio >= 1.0
        and flowers >= MIN_FLOWERS
    )
    recommendation = "beehive" if slot_supported else "chicken"
    return {
        "schema_version": 1,
        "experiment": "dual_cap_strategy_audit",
        "evaluated_ts": utcnow(),
        "read_only": True,
        "animal_regime": {
            "current_animals": current.animal_count if current else None,
            "capacity": current.capacity if current else None,
            "capacity_fraction": (
                current.animal_count / float(current.capacity)
                if current and current.capacity else None
            ),
            "at_cap": capped,
            "regime_started_run": regime_start,
            "regime_boundary": "first >=50k to <=25% herd collapse; current capacity applied only afterward",
            "growth_metric": "output per purchase coin",
            "scarce_slot_metric": "steady-state collected units per animal-minute",
            "growth_kind": "chicken",
            "replacement_kind": recommendation,
            "replacement_threshold": CAP_FRACTION,
            "minimum_slot_ratio": MIN_SLOT_RATIO,
            "windows": len(samples),
            "runs": [item["run"] for item in samples],
            "median_beehive_vs_chicken": median_ratio,
            "minimum_beehive_vs_chicken": minimum_ratio,
            "supported": slot_supported,
        },
        "plot_regime": {
            "current_plots": current.plot_count if current else None,
            "capacity": current.plot_capacity if current else None,
            "capacity_fraction": (
                current.plot_count / float(current.plot_capacity)
                if current and current.plot_capacity else None
            ),
            "counts": current.counts_by_crop if current else {},
            "minimum_wildflowers": MIN_FLOWERS,
            "minimum_flower_qualification_rows": MIN_FLOWER_QUALIFY_ROWS,
            "flower_bonus_satisfied": flowers >= MIN_FLOWERS,
            "food_crop_policy": crop_policy,
            "crop_score_residual": crop_result.get("crop_score_residual"),
            "crop_score_supported": crop_score_supported,
        },
        "cohort": {
            "name": "capped-mixed-species-same-window",
            "sha256": _cohort_hash(samples),
            "samples": samples,
        },
        "decision": {
            "growth_kind": "chicken",
            "capped_replacement_kind": recommendation,
            "capped_replacement_supported": slot_supported,
            "food_crop_kind": None,
        },
        "falsifier": (
            "Five current healthy, bloom-qualified capped mixed-species windows put mature "
            "beehive/chicken per-slot ratio below 1.10 or any qualified window below 1.0."
        ),
    }


def write_result(result: Dict[str, Any]) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fingerprint = str((result.get("cohort") or {}).get("sha256") or "")
    prior = None
    try:
        for line in reversed(EXPERIMENTS.read_text(encoding="utf-8").splitlines()):
            row = json.loads(line)
            if row.get("event") == "dual_cap_strategy_audit.completed":
                prior = str((row.get("cohort") or {}).get("sha256") or "")
                break
    except (OSError, TypeError, ValueError):
        pass
    if fingerprint and fingerprint != prior:
        with EXPERIMENTS.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(result, event="dual_cap_strategy_audit.completed"), sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    result = analyze()
    if not args.no_write:
        write_result(result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (result.get("animal_regime") or {}).get("supported") else 2


if __name__ == "__main__":
    raise SystemExit(main())
