#!/usr/bin/env python3
"""One-shot production recovery agent, scheduled every 30 minutes.

The watcher makes one leaderboard call while an outage is active. It proves
recovery only when this farm's lifetime-produce score exceeds the persisted
outage baseline. On proof it kickstarts the normal cycle, records recovery, and
becomes inert on later launchd invocations.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import ledger, parse, policy, scheduler  # noqa: E402
from farm.mcp import Client  # noqa: E402

STATE = PROJECT / "state"
STORE = STATE / "recovery_watch.json"
HISTORY = STATE / "history.ndjson"
LOCK = STATE / ".recovery-watch.lock"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def latest_history() -> Dict[str, Any]:
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("produce"), int):
            return row
    return {}


def recovery_decision(baseline: int, current: int) -> Tuple[bool, int]:
    """Recovery requires a strictly positive lifetime-score delta."""
    delta = int(current) - int(baseline)
    return delta > 0, delta


def kick_cycle() -> Dict[str, Any]:
    target = "%s/%s" % ("gui/%d" % os.getuid(), scheduler.CYCLE_LABEL)
    results = []
    for args in (["launchctl", "enable", target], ["launchctl", "kickstart", target]):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
            results.append({"command": args[1], "returncode": result.returncode})
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({"command": args[1], "error": str(exc)[:120]})
    return {"target": scheduler.CYCLE_LABEL, "results": results}


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("RECOVERY WATCH skipped: previous check still active")
        return 0

    stored = read_json(STORE)
    if stored.get("status") == "recovered":
        print("RECOVERY WATCH inert: recovered at %s" % stored.get("recovered_ts"))
        return 0

    latest = latest_history()
    baseline = stored.get("baseline_produce")
    if not isinstance(baseline, int):
        baseline = int(latest.get("produce") or 0)
    farmer = str(stored.get("farmer") or latest.get("leader") or "Nick")
    runtime = policy.runtime_context()
    ledger.set_context(
        actor="recovery_watch",
        run=latest.get("run"),
        policy_id=runtime.get("policy_id"),
        claim_registry_version=runtime.get("claim_registry_version"),
        step="check_production",
    )

    board = parse.parse_leaderboard(Client().call("leaderboard"))
    ours: Optional[Any] = next((row for row in board if row.name.lower() == farmer.lower()), None)
    if ours is None:
        raise RuntimeError("farmer %r missing from leaderboard" % farmer)
    recovered, delta = recovery_decision(baseline, ours.produce)

    record = dict(stored)
    record.update({
        "schema_version": 1,
        "farmer": farmer,
        "baseline_produce": baseline,
        "current_produce": ours.produce,
        "delta": delta,
        "checks": int(stored.get("checks") or 0) + 1,
        "last_checked_ts": utcnow(),
        "last_run_seen": latest.get("run"),
        "status": "recovered" if recovered else "waiting",
    })
    if recovered:
        record["recovered_ts"] = utcnow()
        record["resume"] = kick_cycle()
        print("RECOVERY WATCH confirmed +%d produce; normal cycle kickstarted" % delta)
        ledger.record("production.recovered", {"baseline": baseline, "current": ours.produce, "delta": delta})
    else:
        print("RECOVERY WATCH waiting: score=%d baseline=%d delta=%+d" % (ours.produce, baseline, delta))
        ledger.record("production.recovery_checked", {"baseline": baseline, "current": ours.produce, "delta": delta})
    write_json(STORE, record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
