#!/usr/bin/env python3
"""Bounded headless test of beehives versus chickens at the current scale.

The production policy remains chicken-only while this probe runs. The probe adds
one bounded beehive cohort, then compares item-specific collection in the same
healthy windows. Because one successful adoption consumes one latency-limited
call for either species and feed burn is per animal, the promotion metric is
units per animal-minute (equivalently units per successful adoption call), not
units per purchase coin.

Promotion gate (all required):
  * at least five post-intervention healthy verified windows;
  * median beehive/chicken per-animal output ratio >= 1.10;
  * every observed ratio >= 1.00;
  * no core transport error, hunger alarm, or feed-runway breach.

If the gate is not met, the result is ``retain_chicken``. This script never
changes policy; promotion is a separate reviewed code change.
"""

import argparse
import fcntl
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from farm import ledger, mcp, parse, policy, rules  # noqa: E402
from farm.mcp import McpError, ToolError  # noqa: E402

STATE = Path(os.environ.get("FARM_STATE_DIR", str(ROOT / "state"))).resolve()
HISTORY = STATE / "history.ndjson"
EXPERIMENTS = STATE / "experiments.ndjson"
PROBE_STATE = STATE / "beehive_probe.json"
CYCLE_LOCK = STATE / ".lock"
PROBE_LOCK = STATE / ".beehive_probe.lock"

KIND = "beehive"
ITEM = "honey"
CONTROL_KIND = "chicken"
CONTROL_ITEM = "egg"
DEFAULT_BATCH = 1_000
MAX_BATCH = 2_000
CHUNK = 150
WORKERS = 8
RATE = 3.0
MIN_WINDOWS = 5
PROMOTION_RATIO = 1.10
HUNGER_LIMIT = rules.HUNGER_ALARM


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_experiment(row: Dict[str, Any]) -> None:
    EXPERIMENTS.parent.mkdir(parents=True, exist_ok=True)
    with open(EXPERIMENTS, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def history_rows() -> List[Dict[str, Any]]:
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def latest_run() -> int:
    return max(
        (int(row["run"]) for row in history_rows() if isinstance(row.get("run"), int)),
        default=0,
    )


def acquire(path: Path, timeout: float) -> Any:
    deadline = time.time() + timeout
    handle = open(path, "w", encoding="utf-8")
    while time.time() < deadline:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            time.sleep(2)
    handle.close()
    raise RuntimeError("could not acquire %s within %.0fs" % (path.name, timeout))


def release(handle: Any) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def adopt_chunk(endpoint: str, count: int) -> Tuple[int, List[str]]:
    done = 0
    failures: List[str] = []
    lock = threading.Lock()
    parent_context = ledger.current()

    def one(_: int) -> None:
        nonlocal done
        ledger.set_context(**dict(parent_context, worker=threading.current_thread().name))
        client = mcp.Client(endpoint)
        try:
            client.call("adopt_animal", kind=KIND)
            with lock:
                done += 1
        except (ToolError, McpError) as exc:
            with lock:
                if len(failures) < 10:
                    failures.append(str(exc)[:180])

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(one, range(count)))
    return done, failures


def feed_runway(row: Dict[str, Any]) -> float:
    animals = int(row.get("animals") or 0)
    feed = int(row.get("feed") or 0)
    return rules.feed_buffer_minutes(feed, animals)


def window_ratio(row: Dict[str, Any]) -> Optional[float]:
    counts = row.get("by_kind") or {}
    collected = row.get("collected") or {}
    bees = int(counts.get(KIND) or 0)
    chickens = int(counts.get(CONTROL_KIND) or 0)
    honey = int(collected.get(ITEM) or 0)
    eggs = int(collected.get(CONTROL_ITEM) or 0)
    if bees <= 0 or chickens <= 0 or honey <= 0 or eggs <= 0:
        return None
    return (honey / float(bees)) / (eggs / float(chickens))


def healthy_post_windows(baseline_run: int, min_bees: int) -> List[Dict[str, Any]]:
    rows = []
    for row in history_rows():
        if int(row.get("run") or 0) <= baseline_run:
            continue
        if not row.get("verified") or float(row.get("interval_min") or 0) < 4.0:
            continue
        if int((row.get("by_kind") or {}).get(KIND) or 0) < min_bees:
            continue
        ratio = window_ratio(row)
        if ratio is None:
            continue
        enriched = dict(row)
        enriched["beehive_chicken_ratio"] = ratio
        enriched["feed_runway_min"] = feed_runway(row)
        rows.append(enriched)
    return rows


def analyze(state: Dict[str, Any], min_windows: int = MIN_WINDOWS) -> Dict[str, Any]:
    baseline_run = int(state["baseline_run"])
    beehives_after = int(state["beehives_after"])
    # Daily risk events can remove an animal after a clean intervention. Allow
    # at most 1% cohort attrition so a later healthy window is not discarded,
    # while still requiring essentially the full experimental exposure.
    min_bees = max(
        int(state.get("beehives_before") or 0),
        int(beehives_after * 0.99),
    )
    rows = healthy_post_windows(baseline_run, min_bees)
    ratios = [float(row["beehive_chicken_ratio"]) for row in rows]
    safety_failures = []
    for row in rows:
        run = row.get("run")
        if int(row.get("transport_errors_core") or 0) > 0:
            safety_failures.append("run %s core transport errors" % run)
        if int(row.get("max_hunger") or 0) >= HUNGER_LIMIT:
            safety_failures.append("run %s hunger %s" % (run, row.get("max_hunger")))
        if float(row["feed_runway_min"]) < rules.FEED_BUFFER_MIN_MINUTES:
            safety_failures.append(
                "run %s feed runway %.1f" % (run, row["feed_runway_min"])
            )
    enough = len(rows) >= min_windows
    median_ratio = statistics.median(ratios) if ratios else None
    minimum_ratio = min(ratios) if ratios else None
    supported = bool(
        enough
        and median_ratio is not None
        and median_ratio >= PROMOTION_RATIO
        and minimum_ratio is not None
        and minimum_ratio >= 1.0
        and not safety_failures
    )
    decision = "promote_beehive" if supported else "retain_chicken"
    return {
        "experiment": "beehive_scale_probe",
        "evaluated_ts": utcnow(),
        "baseline_run": baseline_run,
        "beehives_before": state.get("beehives_before"),
        "beehives_after": beehives_after,
        "minimum_beehive_exposure": min_bees,
        "windows_required": min_windows,
        "windows_observed": len(rows),
        "runs": [row.get("run") for row in rows],
        "ratios": [round(value, 6) for value in ratios],
        "median_ratio": round(median_ratio, 6) if median_ratio is not None else None,
        "minimum_ratio": round(minimum_ratio, 6) if minimum_ratio is not None else None,
        "promotion_ratio": PROMOTION_RATIO,
        "safety_failures": safety_failures,
        "supported": supported,
        "decision": decision,
        "policy_changed": False,
    }


def execute(batch: int, wait_windows: int, poll_seconds: int) -> Dict[str, Any]:
    if batch <= 0 or batch > MAX_BATCH:
        raise ValueError("batch must be between 1 and %d" % MAX_BATCH)
    runtime = policy.runtime_context()
    primary = str((runtime.get("parameters") or {}).get("primary_kind") or rules.PRIMARY_KIND)
    if primary != CONTROL_KIND:
        raise RuntimeError("probe requires chicken policy baseline, found %s" % primary)

    probe_handle = acquire(PROBE_LOCK, 5)
    intervention_id = None
    try:
        os.environ["FARM_PROBE_ID"] = "beehive-scale-%s" % utcnow().replace(":", "")
        ledger.set_context(actor="probe", step="beehive_scale", policy_id=runtime.get("policy_id"))
        mcp.LIMITER.set_rate(min(RATE, float(rules.MAX_CALLS_PER_SECOND)))
        client = mcp.Client()
        baseline_run = latest_run()
        cycle_handle = acquire(CYCLE_LOCK, 360)
        try:
            before = parse.parse_farm(client.call("list_farm"))
        finally:
            release(cycle_handle)
        bees_before = int(before.counts_by_kind.get(KIND, 0))
        required = batch * (rules.ANIMAL_COST[KIND] + rules.FEED_PER_ANIMAL_RESERVE)
        spendable = before.coins - rules.RISK_COIN_RESERVE
        if spendable < required:
            raise RuntimeError("insufficient safe budget: need %d, have %d" % (required, spendable))

        state = {
            "experiment": "beehive_scale_probe",
            "status": "adopting",
            "started_ts": utcnow(),
            "baseline_run": baseline_run,
            "policy_before": primary,
            "policy_id": runtime.get("policy_id"),
            "batch_planned": batch,
            "beehives_before": bees_before,
            "animals_before": before.animal_count,
            "coins_before": before.coins,
            "feed_before": before.feed,
        }
        PROBE_STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        append_experiment(dict(state, event="beehive_probe.planned"))
        intervention_id = ledger.intervention("beehive_scale_probe", "planned", state)

        adopted = 0
        failures: List[str] = []
        started = time.time()
        while adopted < batch:
            want = min(CHUNK, batch - adopted)
            cycle_handle = acquire(CYCLE_LOCK, 360)
            try:
                done, failed = adopt_chunk(client.endpoint, want)
            finally:
                release(cycle_handle)
            adopted += done
            failures.extend(failed)
            print("beehive probe: adopted %d/%d (chunk failures %d)" % (adopted, batch, len(failed)), flush=True)
            if failed or done != want:
                break
            time.sleep(2)

        cycle_handle = acquire(CYCLE_LOCK, 360)
        try:
            after = parse.parse_farm(client.call("list_farm"))
        finally:
            release(cycle_handle)
        bees_after = int(after.counts_by_kind.get(KIND, 0))
        state.update(
            {
                "status": "observing",
                "adopted": adopted,
                "failures": failures,
                "beehives_after": bees_after,
                "animals_after": after.animal_count,
                "coins_after": after.coins,
                "feed_after": after.feed,
                "feed_runway_after_min": round(
                    rules.feed_buffer_minutes(after.feed, after.animal_count), 2
                ),
                "adoption_seconds": round(time.time() - started, 2),
            }
        )
        PROBE_STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        append_experiment(dict(state, event="beehive_probe.adopted"))
        ledger.intervention(
            "beehive_scale_probe", "outcome", state, intervention_id=intervention_id
        )
        if adopted != batch or bees_after - bees_before != adopted:
            state["status"] = "failed"
            state["decision"] = "retain_chicken"
            state["reason"] = "adoption batch incomplete or count residual"
            PROBE_STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            append_experiment(dict(state, event="beehive_probe.failed"))
            return state

        while True:
            result = analyze(state, min_windows=wait_windows)
            print(json.dumps(result, sort_keys=True), flush=True)
            if result["windows_observed"] >= wait_windows:
                state.update({"status": "complete", "result": result, "completed_ts": utcnow()})
                PROBE_STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                append_experiment(dict(result, event="beehive_probe.completed"))
                return result
            time.sleep(max(10, poll_seconds))
    finally:
        release(probe_handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="perform the bounded live adoption")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--wait-windows", type=int, default=MIN_WINDOWS)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--analyze", action="store_true", help="analyze the recorded intervention only")
    parser.add_argument("--finalize", action="store_true", help="persist the current analysis as the final decision")
    args = parser.parse_args()

    if args.analyze or args.finalize:
        state = json.loads(PROBE_STATE.read_text(encoding="utf-8"))
        result = analyze(state, min_windows=args.wait_windows)
        if args.finalize:
            state.update({"status": "complete", "result": result, "completed_ts": utcnow()})
            PROBE_STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            append_experiment(dict(result, event="beehive_probe.completed"))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["supported"] else 2
    if not args.execute:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "batch": args.batch,
                    "max_batch": MAX_BATCH,
                    "windows_required": args.wait_windows,
                    "promotion_ratio": PROMOTION_RATIO,
                    "current_policy": rules.PRIMARY_KIND,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    result = execute(args.batch, args.wait_windows, args.poll_seconds)
    return 0 if result.get("supported") else 2


if __name__ == "__main__":
    raise SystemExit(main())
