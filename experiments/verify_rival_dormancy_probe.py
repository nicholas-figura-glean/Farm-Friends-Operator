#!/usr/bin/env python3
"""Bounded read-only probe for rival scoreboard dormancy."""

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

HYPOTHESIS_ID = "hyp-1b6a1b1123efffe4"
EVIDENCE_CLASS = "causal_validation"
EVIDENCE_PATH = Path("state/verify_rival_dormancy_probe.json")
EXPERIMENTS_PATH = Path("state/experiments.ndjson")
BUDGET = {"coins": 0, "calls": 2, "wall_seconds": 285}


def _json_from_text(text):
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
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _call_get_scoreboard(timeout):
    command = os.environ.get("GET_SCOREBOARD_COMMAND") or os.environ.get("SCOREBOARD_COMMAND")
    if command:
        args = shlex.split(command)
        started = time.monotonic()
        try:
            completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            return None, {
                "capability": "get_scoreboard",
                "attempted": True,
                "provider": "command",
                "timeout_seconds": timeout,
                "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        return _json_from_text(completed.stdout), {
            "capability": "get_scoreboard",
            "attempted": True,
            "provider": "command",
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-2000:],
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }

    for module_name in ("farm.mcp", "farm.client", "farm.api"):
        try:
            module = __import__(module_name, fromlist=["get_scoreboard"])
            getter = getattr(module, "get_scoreboard", None)
            if callable(getter):
                return getter(), {"capability": "get_scoreboard", "attempted": True, "provider": module_name}
        except Exception as exc:
            last_error = repr(exc)
            continue
    return None, {"capability": "get_scoreboard", "attempted": False, "provider": None, "error": locals().get("last_error")}


def _entries(scoreboard):
    if isinstance(scoreboard, list):
        return [item for item in scoreboard if isinstance(item, dict)]
    if not isinstance(scoreboard, dict):
        return []
    for key in ("scoreboard", "leaderboard", "rankings", "rivals", "players", "farms"):
        value = scoreboard.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for value in scoreboard.values():
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    return []


def _is_self(item):
    if item.get("is_self") or item.get("self"):
        return True
    me = os.environ.get("FARMER_NAME") or os.environ.get("PLAYER_NAME") or os.environ.get("USERNAME")
    return bool(me and _identity(item) == me)


def _identity(item):
    for key in ("farmer", "farmer_name", "username", "name", "id", "player_id"):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None


def _number(value):
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _sum_named(value, needles):
    total = 0.0
    if isinstance(value, dict):
        for key, child in value.items():
            if any(needle in str(key).lower() for needle in needles):
                total += _sum_all_numbers(child)
            else:
                total += _sum_named(child, needles)
    elif isinstance(value, list):
        for child in value:
            total += _sum_named(child, needles)
    return total


def _sum_all_numbers(value):
    if isinstance(value, dict):
        return sum(_sum_all_numbers(child) for child in value.values())
    if isinstance(value, list):
        return sum(_sum_all_numbers(child) for child in value)
    return _number(value)


def _first_number(item, keys):
    for key in keys:
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _aggregate(item, position):
    return {
        "identity": _identity(item) or f"rank-{position}",
        "rank": _first_number(item, ("rank", "position", "place")) or position,
        "animals": _sum_named(item, ("animal", "animals", "herd", "livestock")),
        "coins": _first_number(item, ("coins", "coin", "balance", "money")) or _sum_named(item, ("coins", "coin", "balance", "money")),
        "produce": _first_number(item, ("produce", "lifetime_produce", "lifetimeProduce", "score", "rank_score")) or _sum_named(item, ("produce", "score")),
    }


def _top5_rivals(scoreboard):
    rivals = []
    for position, item in enumerate(_entries(scoreboard), start=1):
        if not _is_self(item):
            rivals.append(_aggregate(item, position))
    rivals.sort(key=lambda item: (item["rank"], item["identity"]))
    return rivals[:5]


def _sample(label, timeout):
    scoreboard, call = _call_get_scoreboard(timeout)
    return {
        "label": label,
        "monotonic_seconds": time.monotonic(),
        "wall_time": time.time(),
        "top5_rival_aggregates": _top5_rivals(scoreboard),
        "call": call,
    }


def _metric(start_sample, end_sample):
    start_by_id = {item["identity"]: item for item in start_sample["top5_rival_aggregates"]}
    elapsed_minutes = max((end_sample["monotonic_seconds"] - start_sample["monotonic_seconds"]) / 60.0, 1.0 / 60.0)
    gains = []
    for end_item in end_sample["top5_rival_aggregates"]:
        start_item = start_by_id.get(end_item["identity"])
        if not start_item:
            continue
        net_gain = sum(end_item[key] - start_item[key] for key in ("animals", "coins", "produce"))
        gains.append(
            {
                "identity": end_item["identity"],
                "net_gain": net_gain,
                "gain_per_min": net_gain / elapsed_minutes,
                "start": start_item,
                "end": end_item,
            }
        )
    max_gain = max((item["gain_per_min"] for item in gains), default=0.0)
    return max_gain, gains, elapsed_minutes


def _append_record(record):
    EXPERIMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EXPERIMENTS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _record_result(record):
    from farm import provenance

    provenance.record_result(
        HYPOTHESIS_ID,
        "falsified" if record["falsified"] else "supported",
        [str(EVIDENCE_PATH)],
        EVIDENCE_CLASS,
        {
            "max_top5_rival_gain_per_min": record["max_top5_rival_gain_per_min"],
            "falsified": record["falsified"],
            "validation_evidence": [str(EVIDENCE_PATH)],
        },
    )


def main():
    started = time.monotonic()
    first_timeout = max(1, min(20, BUDGET["wall_seconds"]))
    start_sample = _sample("cycle_start", first_timeout)
    elapsed = time.monotonic() - started
    default_gap = max(0.0, min(240.0, BUDGET["wall_seconds"] - elapsed - 20.0))
    gap = float(os.environ.get("RIVAL_DORMANCY_SAMPLE_GAP_SECONDS", default_gap))
    if gap > 0:
        time.sleep(min(gap, max(0.0, BUDGET["wall_seconds"] - (time.monotonic() - started) - 5.0)))
    second_timeout = max(1, min(20, int(BUDGET["wall_seconds"] - (time.monotonic() - started)) or 1))
    end_sample = _sample("cycle_end", second_timeout)
    max_gain, rival_gains, elapsed_minutes = _metric(start_sample, end_sample)
    record = {
        "kind": "verify_rival_dormancy_probe",
        "hypothesis_id": HYPOTHESIS_ID,
        "hypothesis": "best_rival_gain=0 reflects true rival inactivity, so no defensive parameter change is currently warranted.",
        "falsifier": "Any top-5 rival shows positive net gain across two consecutive scoreboard samples within one cycle.",
        "primary_metric": "max_top5_rival_gain_per_min",
        "max_top5_rival_gain_per_min": max_gain,
        "falsified": max_gain > 0,
        "status": "falsified" if max_gain > 0 else "supported",
        "evidence_class": EVIDENCE_CLASS,
        "budget": BUDGET,
        "samples": [start_sample, end_sample],
        "rival_gains": rival_gains,
        "elapsed_minutes_between_samples": elapsed_minutes,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    _append_record(record)
    _record_result(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

if False:
    pass