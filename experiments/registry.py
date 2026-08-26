"""Declarative bounded-probe registry.

Mutating probes are never autonomous. The supervisor may schedule only entries
marked both read_only and autonomous, and every execution still runs under the
farm lock with a wall-time ceiling.
"""

PROBES = {
    "counterfactual_sweep": {
        "hypothesis": "A neighbouring decision constant changes historical outcomes.",
        "question_classes": ["strategy_stale", "knob_age", "policy_drift"],
        "subject_patterns": ["farm", "growth", "policy", "output_linear"],
        "command": ["run.py", "--sweep"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 60},
        "stop_condition": "pure replay completes or wall-time expires",
        "evidence_destination": "state/audits.ndjson",
    },
    "endgame_replay": {
        "hypothesis": "A safe affordable herd target restores or preserves the objective path.",
        "question_classes": ["rank_lost", "no_path_to_win", "win_eta"],
        "command": ["experiments/endgame.py"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 30},
        "stop_condition": "simulation table completes or wall-time expires",
        "evidence_destination": "state/experiments.ndjson",
    },
    "species_mix": {
        "hypothesis": "An alternative species beats chickens on lifetime produce per total cost.",
        "question_classes": ["model_drift"],
        "command": ["experiments/species_probe.py"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 10_000, "calls": 500, "wall_seconds": 300},
        "stop_condition": "one bounded mixed-species batch and its observation window",
        "evidence_destination": "state/experiments.ndjson and claim strategy.chicken_engine",
        "status": "historically answered; explicit re-probe only",
    },
    "beehive_scale": {
        "hypothesis": "A beehive beats a chicken on lifetime-produce proxy per latency-limited adoption call at current scale.",
        "question_classes": ["model_drift", "strategy_stale"],
        "command": ["experiments/beehive_probe.py", "--execute"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 80_000, "calls": 1_010, "wall_seconds": 2_400},
        "stop_condition": "1,000 bounded adoptions and five healthy verified observation windows",
        "evidence_destination": "state/beehive_probe.json and state/experiments.ndjson",
        "status": "completed at baseline run 639; chicken retained because promotion gate failed",
    },
    "feed_economics": {
        "hypothesis": "Current feeding cadence maximizes net score production without crossing hunger risk.",
        "question_classes": ["model_drift", "hunger_wall"],
        "command": ["experiments/feed_economics.py"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 50_000, "calls": 30, "wall_seconds": 600},
        "stop_condition": "bounded cadence comparison completes or hunger reaches the alarm guard",
        "evidence_destination": "state/experiments.ndjson and safety.bulk_husbandry",
    },
    "visit_farm": {
        "hypothesis": "Calling visit_farm may change lifetime produce or reveal no-op observability.",
        "question_classes": ["unused_capability", "opportunity", "model_drift"],
        "command": ["experiments/registry.py", "--visit-farm-probe", "farmer"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 0, "calls": 1, "wall_seconds": 30},
        "stop_condition": "one explicitly invoked visit_farm call and immediate lifetime-produce comparison",
        "evidence_destination": "state/experiments.ndjson",
    },
}


def _read_lifetime_produce_snapshot():
    import json
    from pathlib import Path

    for path in (
        Path("state/farm.json"),
        Path("state/status.json"),
        Path("state/latest.json"),
        Path("state/state.json"),
    ):
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue

        for key in ("lifetime_produce", "lifetimeProduce", "lifetimeProduceCount"):
            value = data.get(key)
            if isinstance(value, (int, float)):
                return {"path": str(path), "key": key, "value": value}

        farm = data.get("farm")
        if isinstance(farm, dict):
            for key in ("lifetime_produce", "lifetimeProduce", "lifetimeProduceCount"):
                value = farm.get(key)
                if isinstance(value, (int, float)):
                    return {"path": str(path), "key": "farm." + key, "value": value}

    return None


def _append_visit_farm_probe_outcome(record):
    import json
    from pathlib import Path

    path = Path("state/experiments.ndjson")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _run_visit_farm_probe(argv):
    import os
    import shlex
    import subprocess
    import sys
    import time

    budget = PROBES["visit_farm"]["budget"]
    started = time.monotonic()
    farmer = argv[0] if argv else "farmer"
    before = _read_lifetime_produce_snapshot()
    status = "not_configured"
    call = {"attempted": False}

    command = os.environ.get("VISIT_FARM_COMMAND")
    if command:
        # Explicit command configuration keeps this mutating/unknown probe out of
        # autonomous scheduling while still allowing a bounded one-call measurement.
        args = shlex.split(command) + [farmer]
        timeout = max(1, min(budget["wall_seconds"], budget["wall_seconds"] - int(time.monotonic() - started)))
        call["attempted"] = True
        call["command"] = command
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            call["returncode"] = completed.returncode
            call["stdout"] = completed.stdout[-2000:]
            call["stderr"] = completed.stderr[-2000:]
            status = "called" if completed.returncode == 0 else "call_failed"
        except subprocess.TimeoutExpired as exc:
            call["timeout_seconds"] = timeout
            call["stdout"] = (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else ""
            call["stderr"] = (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else ""
            status = "timeout"

    after = _read_lifetime_produce_snapshot()
    before_value = before["value"] if before else None
    after_value = after["value"] if after else None
    delta = after_value - before_value if before_value is not None and after_value is not None else None
    _append_visit_farm_probe_outcome(
        {
            "kind": "visit_farm_probe",
            "capability": "visit_farm",
            "farmer": farmer,
            "status": status,
            "budget": budget,
            "read_only": PROBES["visit_farm"]["read_only"],
            "autonomous": PROBES["visit_farm"]["autonomous"],
            "before_lifetime_produce": before,
            "after_lifetime_produce": after,
            "lifetime_produce_delta": delta,
            "call": call,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    )
    return 0


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["--visit-farm-probe"]:
        raise SystemExit(_run_visit_farm_probe(sys.argv[2:]))
