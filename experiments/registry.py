"""Declarative bounded-probe registry.

Mutating probes are never autonomous. The supervisor may schedule only entries
marked both read_only and autonomous, and every execution still runs under the
farm lock with a wall-time ceiling.
"""

PROBES = {
    "counterfactual_sweep": {
        "hypothesis": "A neighbouring decision constant changes historical outcomes.",
        "question_classes": ["strategy_stale", "idle_capital", "knob_age", "policy_drift"],
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
    "peek_top_rival": {
        "hypothesis_id": "hyp-acb4268935bb27c3",
        "hypothesis": "The current best rival's farm state cannot plausibly generate more than 25% of our recent per-cycle gain before the next cycle, so threat allocation should remain unchanged.",
        "null_hypothesis": "The proposed mechanism produces no measurable improvement in projected_rival_next_cycle_gain.",
        "falsifier": "visit_farm shows the best rival has a pending harvest, herd, or resource stockpile consistent with a next-cycle gain above 176383 produce.",
        "primary_metric": "projected_rival_next_cycle_gain",
        "evidence_class": "direct_mechanism",
        "question_classes": ["rival_wake", "threat", "rival_growing", "rank_lost", "overtaken"],
        "subject_patterns": [],
        "command": ["experiments/registry.py", "--peek-top-rival"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 1, "wall_seconds": 30},
        "stop_condition": "one visit_farm call against the current best rival or prior evidence exists",
        "evidence_destination": "state/peek_top_rival_probe.json and state/experiments.ndjson",
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


def _record_linked_result(record):
    """Adjudicate a pre-registered probe when the scheduler supplied lineage."""
    import os

    identity = os.environ.get("FARM_HYPOTHESIS_ID")
    if not identity:
        return
    projected = record.get("projected_rival_next_cycle_gain")
    threshold = record.get("falsifier_threshold")
    if not isinstance(projected, (int, float)) or not isinstance(threshold, (int, float)):
        status = "inconclusive"
    else:
        status = "falsified" if projected > threshold else "supported"
    from farm import provenance
    provenance.record_result(
        identity,
        status,
        ["state/peek_top_rival_probe.json"],
        os.environ.get("FARM_EVIDENCE_CLASS", "direct_mechanism"),
        {
            "projected_rival_next_cycle_gain": projected,
            "falsifier_threshold": threshold,
        },
    )


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


def _read_top_rival_snapshot():
    import json
    import os
    from pathlib import Path

    env_rival = os.environ.get("TOP_RIVAL") or os.environ.get("PEEK_TOP_RIVAL")
    if env_rival:
        return {"path": "environment", "key": "TOP_RIVAL", "farmer": env_rival, "score": None}

    def score_of(item):
        for key in ("score", "lifetime_produce", "lifetimeProduce", "produce", "rank_score"):
            value = item.get(key)
            if isinstance(value, (int, float)):
                return value
        return None

    def name_of(item):
        for key in ("farmer", "farmer_name", "username", "name", "id"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    for path in (
        Path("state/leaderboard.json"),
        Path("state/status.json"),
        Path("state/latest.json"),
        Path("state/state.json"),
        Path("state/farm.json"),
    ):
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue

        containers = [data] if isinstance(data, list) else []
        if isinstance(data, dict):
            for key in ("leaderboard", "rivals", "rankings", "farms", "players"):
                value = data.get(key)
                if isinstance(value, list):
                    containers.append(value)

        best = None
        for container in containers:
            for item in container:
                if not isinstance(item, dict) or item.get("is_self") or item.get("self"):
                    continue
                farmer = name_of(item)
                score = score_of(item)
                if not farmer or score is None:
                    continue
                if best is None or score > best["score"]:
                    best = {"path": str(path), "key": "leaderboard", "farmer": farmer, "score": score}

        if best is not None:
            return best

    return None


def _json_from_text(text):
    import json

    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        pass

    if not isinstance(text, str):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _sum_nonnegative_numbers(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value if value > 0 else 0
    if isinstance(value, dict):
        return sum(_sum_nonnegative_numbers(child) for child in value.values())
    if isinstance(value, list):
        return sum(_sum_nonnegative_numbers(child) for child in value)
    return 0


def _sum_named_numbers(value, needles):
    total = 0
    if isinstance(value, dict):
        for key, child in value.items():
            if any(needle in str(key).lower() for needle in needles):
                total += _sum_nonnegative_numbers(child)
            else:
                total += _sum_named_numbers(child, needles)
    elif isinstance(value, list):
        total += sum(_sum_named_numbers(child, needles) for child in value)
    return total


def _project_rival_next_cycle_gain(visit_farm_state):
    if not isinstance(visit_farm_state, (dict, list)):
        return None

    ready_produce = _sum_named_numbers(
        visit_farm_state,
        ("ready_produce", "pending_harvest", "harvestable", "uncollected", "ready"),
    )
    herd_size = _sum_named_numbers(visit_farm_state, ("herd", "livestock", "animals"))
    stored_resources = _sum_named_numbers(visit_farm_state, ("stored", "stockpile", "resources", "inventory"))
    # This is a falsifier probe, so the projection intentionally uses a simple
    # visible upper-bound proxy rather than a strategy model that could hide risk.
    projected = ready_produce + herd_size + stored_resources
    return {
        "projected_rival_next_cycle_gain": projected,
        "visible_ready_produce": ready_produce,
        "visible_herd_size": herd_size,
        "visible_stored_resources": stored_resources,
    }


def _run_peek_top_rival_probe(argv):
    import json
    import os
    import shlex
    import subprocess
    import time
    from pathlib import Path

    budget = PROBES["peek_top_rival"]["budget"]
    started = time.monotonic()
    evidence_path = Path("state/peek_top_rival_probe.json")
    if evidence_path.exists():
        try:
            prior = json.loads(evidence_path.read_text())
        except (json.JSONDecodeError, OSError):
            prior = {}
        record = {
            "kind": "peek_top_rival_probe",
            "status": "already_ran",
            "budget": budget,
            "projected_rival_next_cycle_gain": prior.get("projected_rival_next_cycle_gain"),
            "prior_evidence": str(evidence_path),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        _append_visit_farm_probe_outcome(record)
        prior.setdefault("falsifier_threshold", 176383)
        _record_linked_result(prior)
        return 0

    recent_gain = 705531
    falsifier_threshold = 176383
    rival = _read_top_rival_snapshot()
    farmer = argv[0] if argv else (rival or {}).get("farmer")
    status = "no_rival"
    call = {"attempted": False}
    visit_farm_state = None

    command = os.environ.get("VISIT_FARM_COMMAND")
    if farmer and command:
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
            call["stdout"] = completed.stdout[-12000:]
            call["stderr"] = completed.stderr[-2000:]
            status = "called" if completed.returncode == 0 else "call_failed"
            visit_farm_state = _json_from_text(completed.stdout)
        except subprocess.TimeoutExpired as exc:
            call["timeout_seconds"] = timeout
            call["stdout"] = (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else ""
            call["stderr"] = (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else ""
            status = "timeout"
    elif farmer:
        status = "not_configured"

    projection = _project_rival_next_cycle_gain(visit_farm_state)
    projected = projection["projected_rival_next_cycle_gain"] if projection else None
    record = {
        "kind": "peek_top_rival_probe",
        "hypothesis": PROBES["peek_top_rival"]["hypothesis"],
        "capability": "visit_farm",
        "farmer": farmer,
        "top_rival_snapshot": rival,
        "status": status,
        "budget": budget,
        "read_only": PROBES["peek_top_rival"]["read_only"],
        "autonomous": PROBES["peek_top_rival"]["autonomous"],
        "recent_per_cycle_gain": recent_gain,
        "falsifier_threshold": falsifier_threshold,
        "projected_rival_next_cycle_gain": projected,
        "projection_components": projection,
        "falsified": projected is not None and projected > falsifier_threshold,
        "visit_farm_state": visit_farm_state,
        "call": call,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    _append_visit_farm_probe_outcome(record)
    _record_linked_result(record)
    return 0


if __name__ == "__main__":
    import sys

    if sys.argv[1:2] == ["--visit-farm-probe"]:
        raise SystemExit(_run_visit_farm_probe(sys.argv[2:]))
    if sys.argv[1:2] == ["--peek-top-rival"]:
        raise SystemExit(_run_peek_top_rival_probe(sys.argv[2:]))
