"""Declarative bounded-probe registry.

Mutating probes are never autonomous. The supervisor may schedule only entries
marked both read_only and autonomous, and every execution still runs under the
farm lock with a wall-time ceiling.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROBES = {
    "activity_replay": {
        "hypothesis": "New trade activity can be classified from resource flows, neutral alternatives, and subsequent counterparty growth before trade automation resumes.",
        "question_classes": ["activity_novelty_trade", "activity_novelty_rival"],
        "subject_patterns": [],
        "command": ["experiments/activity_probe.py"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 30},
        "tools": {},
        "outputs": ["activity_probe.json"],
        "stop_condition": "recent trade decisions are replayed or the evidence window is empty",
        "evidence_destination": "state/activity_probe.json",
    },
    "counterfactual_sweep": {
        "hypothesis": "A neighbouring decision constant changes historical outcomes.",
        "question_classes": ["strategy_stale", "idle_capital", "knob_age", "policy_drift"],
        "subject_patterns": ["farm", "growth", "policy", "output_linear", "governance"],
        "command": ["run.py", "--sweep"],
        "read_only": True,
        "autonomous": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 60},
        "tools": {},
        "outputs": ["counterfactual_sweep.json"],
        "stop_condition": "pure replay completes or wall-time expires",
        "evidence_destination": "state/counterfactual_sweep.json",
    },
    "endgame_replay": {
        "hypothesis": "A safe affordable herd target restores or preserves the objective path.",
        "question_classes": ["rank_lost", "no_path_to_win", "win_eta"],
        "command": ["experiments/endgame.py"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 30},
        "tools": {},
        "outputs": ["endgame_replay.json"],
        "stop_condition": "simulation table completes or wall-time expires",
        "evidence_destination": "state/endgame_replay.json",
    },
    "dual_cap_audit": {
        "hypothesis": "Animal and plot caps change the scarce-resource denominator: growth should optimize output per coin, while near-cap replacements optimize output per animal slot and plots are evaluated independently.",
        "question_classes": ["model_drift", "policy_drift", "strategy_stale", "knob_age", "idle_capital"],
        "subject_patterns": ["chicken", "beehive", "capacity", "cap", "semantic_contract"],
        "command": ["experiments/dual_cap_audit.py"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 60},
        "tools": {},
        "outputs": ["dual_cap_audit.json", "experiments.ndjson"],
        "stop_condition": "current capped mixed-species cohort is measured or explicitly reported insufficient",
        "evidence_destination": "state/dual_cap_audit.json and state/experiments.ndjson",
    },
    "crop_timer_revalidation": {
        "hypothesis": "Food crops now advance and harvest inside the server's declared 15/20/30-minute timers, falsifying the run-50 stalled-timer claim.",
        "question_classes": ["model_drift", "strategy_stale", "knob_age"],
        "subject_patterns": ["crop", "timer", "plot", "food_crops_banned"],
        "command": ["experiments/crop_timer_probe.py", "--start"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 17, "calls": 5, "wall_seconds": 2400},
        "tools": {
            "list_farm": {"max_calls": 2, "arguments": {}},
            "plant": {
                "max_calls": 3,
                "arguments": {
                    "kind": {"required": True, "enum": ["wheat", "corn", "pumpkin"]},
                },
            },
        },
        "outputs": ["dual_cap_probe.json", "experiments.ndjson"],
        "stop_condition": "one plot of each food crop reaches harvest or exceeds its declared timer plus six minutes",
        "evidence_destination": "state/dual_cap_probe.json and state/experiments.ndjson",
    },
    "crop_timer_analysis": {
        "hypothesis": "The current bounded crop cohort settles whether declared food-crop timers still hold.",
        "question_classes": ["knob_age", "model_drift", "strategy_stale"],
        "subject_patterns": ["mechanic.crop_timers_active", "mechanic.crop_timers_delayed"],
        "command": ["experiments/crop_timer_probe.py", "--analyze"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 60},
        "tools": {},
        "outputs": ["dual_cap_probe.json", "experiments.ndjson"],
        "stop_condition": "the three planted crops harvest or the observation remains explicitly incomplete",
        "evidence_destination": "state/dual_cap_probe.json and state/experiments.ndjson",
    },
    "crop_score_holdout": {
        "hypothesis": "A 5,000-plot wheat cohort contributes its harvested units to lifetime produce above deduplicated same-window animal production, justifying a bounded plot ramp.",
        "question_classes": ["model_drift", "strategy_stale", "idle_capital"],
        "subject_patterns": ["crop", "wheat", "plot", "food_crops_banned"],
        "command": ["experiments/crop_score_probe.py", "--start", "--qty", "5000"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 20000, "calls": 3, "wall_seconds": 1800},
        "tools": {
            "list_farm": {"max_calls": 2, "arguments": {}},
            "plant": {
                "max_calls": 1,
                "arguments": {
                    "kind": {"required": True, "equals": "wheat"},
                    "qty": {"required": True, "integer": True, "min": 1, "max": 5000},
                },
            },
        },
        "outputs": ["crop_score_probe.json", "experiments.ndjson"],
        "stop_condition": "5,000 wheat plots harvest or miss the timer; lifetime residual is separated from deduplicated animal production",
        "evidence_destination": "state/crop_score_probe.json and state/experiments.ndjson",
    },
    "crop_score_analysis": {
        "hypothesis": "The bounded wheat holdout settles whether harvested crops add lifetime score beyond same-window animal production.",
        "question_classes": ["knob_age", "model_drift", "strategy_stale", "idle_capital"],
        "subject_patterns": ["strategy.food_crop_score"],
        "command": ["experiments/crop_score_probe.py", "--analyze"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 60},
        "tools": {},
        "outputs": ["crop_score_probe.json", "experiments.ndjson"],
        "stop_condition": "the wheat harvest is attributed or the observation remains explicitly incomplete",
        "evidence_destination": "state/crop_score_probe.json and state/experiments.ndjson",
    },
    "species_mix": {
        "hypothesis": "An alternative species beats chickens on lifetime produce per total cost.",
        "question_classes": ["model_drift"],
        "command": ["experiments/species_probe.py"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 10_000, "calls": 500, "wall_seconds": 300},
        "tools": {
            "list_farm": {"max_calls": 2, "arguments": {}},
            "adopt_animal": {
                "max_calls": 400,
                "arguments": {
                    "kind": {"required": True, "enum": ["pig", "sheep", "cow", "beehive"]},
                },
            },
            "plant": {
                "max_calls": 3,
                "arguments": {
                    "kind": {"required": True, "enum": ["wheat", "corn", "pumpkin"]},
                },
            },
        },
        "outputs": ["probe.ndjson"],
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
        "tools": {
            "list_farm": {"max_calls": 2, "arguments": {}},
            "adopt_animal": {
                "max_calls": 1000,
                "arguments": {"kind": {"required": True, "equals": "beehive"}},
            },
        },
        "outputs": ["beehive_probe.json", "experiments.ndjson"],
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
        "tools": {
            "list_farm": {"max_calls": 3, "arguments": {}},
            "feed_animals": {
                "max_calls": 1,
                "arguments": {
                    "animal_id": {"required": True, "max_length": 128, "not_equals": "all"},
                },
            },
        },
        "outputs": ["probe.ndjson"],
        "stop_condition": "bounded cadence comparison completes or hunger reaches the alarm guard",
        "evidence_destination": "state/experiments.ndjson and safety.bulk_husbandry",
    },
    "visit_farm": {
        "hypothesis": "Calling visit_farm may change lifetime produce or reveal no-op observability.",
        "question_classes": ["unused_capability", "opportunity", "model_drift"],
        "command": ["experiments/registry.py", "--visit-farm-probe", "farmer"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 30},
        "tools": {},
        "outputs": ["experiments.ndjson"],
        "stop_condition": "disabled until visit_farm is routed through the protected broker",
        "evidence_destination": "state/experiments.ndjson",
    },
    "propose_trade_message": {
        "hypothesis": "Supplying propose_trade.message improves immediate produce per coin versus the current no-message baseline.",
        "question_classes": ["opportunity", "model_drift"],
        "command": ["experiments/registry.py", "--propose-trade-message-probe"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 30},
        "tools": {},
        "outputs": ["experiments.ndjson"],
        "stop_condition": "disabled until trade liability is represented by a protected cost model",
        "evidence_destination": "state/experiments.ndjson",
    },
    "peek_top_rival": {
        "hypothesis_id": "hyp-acb4268935bb27c3",
        "hypothesis": "The current best rival's farm state cannot plausibly generate more than 25% of our recent per-cycle gain before the next cycle, so threat allocation should remain unchanged.",
        "null_hypothesis": "The proposed mechanism produces no measurable improvement in projected_rival_next_cycle_gain.",
        "falsifier": "visit_farm shows the best rival has a pending harvest, herd, or resource stockpile consistent with a next-cycle gain above 176383 produce.",
        "primary_metric": "projected_rival_next_cycle_gain",
        "evidence_class": "direct_mechanism",
        "question_classes": [
            "rival_wake", "threat", "rival_growing", "rank_lost", "overtaken",
        ],
        "subject_patterns": [],
        "command": ["experiments/registry.py", "--peek-top-rival"],
        "read_only": True,
        "autonomous": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 30},
        "tools": {},
        "outputs": ["peek_top_rival_probe.json", "experiments.ndjson", "provenance.ndjson"],
        "stop_condition": "disabled until rival reads are routed through the protected broker",
        "evidence_destination": "state/peek_top_rival_probe.json and state/experiments.ndjson",
    },
    "verify_rival_dormancy": {
        "hypothesis_id": "hyp-1b6a1b1123efffe4",
        "hypothesis": "best_rival_gain=0 reflects true rival inactivity, so no defensive parameter change is currently warranted.",
        "null_hypothesis": "The probe produces no measurable improvement in max_top5_rival_gain_per_min.",
        "falsifier": "Any top-5 rival shows positive net gain across two consecutive scoreboard samples within one cycle.",
        "primary_metric": "max_top5_rival_gain_per_min",
        "evidence_class": "causal_validation",
        "question_classes": ["opportunity", "strategy_hypothesis", "threat", "rival_wake"],
        "subject_patterns": ["scoreboard", "rival", "dormancy", "top-5"],
        "command": ["experiments/verify_rival_dormancy_probe.py"],
        "read_only": True,
        "autonomous": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 285},
        "tools": {},
        "outputs": ["verify_rival_dormancy_probe.json", "experiments.ndjson", "provenance.ndjson"],
        "stop_condition": "disabled until scoreboard reads are routed through the protected broker",
        "evidence_destination": "state/verify_rival_dormancy_probe.json and state/experiments.ndjson",
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


def _read_coin_snapshot():
    import json
    from pathlib import Path

    def find_coin_value(value, prefix=""):
        if not isinstance(value, dict):
            return None
        for key, child in value.items():
            child_key = str(key)
            child_path = f"{prefix}.{child_key}" if prefix else child_key
            if child_key.lower() in ("coins", "coin", "balance", "money") and isinstance(child, (int, float)):
                return {"key": child_path, "value": child}
        for key, child in value.items():
            found = find_coin_value(child, f"{prefix}.{key}" if prefix else str(key))
            if found is not None:
                return found
        return None

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
        found = find_coin_value(data)
        if found is not None:
            found["path"] = str(path)
            return found
    return None


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


def _run_propose_trade_message_probe(argv):
    import os
    import shlex
    import subprocess
    import time

    budget = PROBES["propose_trade_message"]["budget"]
    started = time.monotonic()
    message = (argv[0] if argv else os.environ.get("PROPOSE_TRADE_PROBE_MESSAGE", "bounded propose_trade message probe"))[:120]
    before_produce = _read_lifetime_produce_snapshot()
    before_coins = _read_coin_snapshot()
    status = "not_configured"
    call = {"attempted": False, "message": message, "message_argument": "message"}

    command = os.environ.get("PROPOSE_TRADE_COMMAND")
    if command:
        # The live cycle remains on the proven no-message path; this explicit
        # probe appends only the new optional argument to a caller-supplied,
        # bounded propose_trade command.
        args = shlex.split(command)
        if "{message}" in args:
            args = [message if arg == "{message}" else arg for arg in args]
        else:
            args += ["--message", message]
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

    after_produce = _read_lifetime_produce_snapshot()
    after_coins = _read_coin_snapshot()
    before_produce_value = before_produce["value"] if before_produce else None
    after_produce_value = after_produce["value"] if after_produce else None
    before_coin_value = before_coins["value"] if before_coins else None
    after_coin_value = after_coins["value"] if after_coins else None
    produce_delta = (
        after_produce_value - before_produce_value
        if before_produce_value is not None and after_produce_value is not None
        else None
    )
    coin_spend = before_coin_value - after_coin_value if before_coin_value is not None and after_coin_value is not None else None
    produce_per_coin = produce_delta / coin_spend if produce_delta is not None and coin_spend and coin_spend > 0 else None
    _append_visit_farm_probe_outcome(
        {
            "kind": "propose_trade_message_probe",
            "capability": "propose_trade",
            "argument": "message",
            "status": status,
            "budget": budget,
            "read_only": PROBES["propose_trade_message"]["read_only"],
            "autonomous": PROBES["propose_trade_message"]["autonomous"],
            "before_lifetime_produce": before_produce,
            "after_lifetime_produce": after_produce,
            "lifetime_produce_delta": produce_delta,
            "before_coins": before_coins,
            "after_coins": after_coins,
            "coin_spend": coin_spend,
            "produce_per_coin": produce_per_coin,
            "baseline": "compare against historical/current no-message propose_trade outcomes; live cycle calls are unchanged",
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
    if sys.argv[1:2] == ["--propose-trade-message-probe"]:
        raise SystemExit(_run_propose_trade_message_probe(sys.argv[2:]))
    if sys.argv[1:2] == ["--peek-top-rival"]:
        raise SystemExit(_run_peek_top_rival_probe(sys.argv[2:]))
