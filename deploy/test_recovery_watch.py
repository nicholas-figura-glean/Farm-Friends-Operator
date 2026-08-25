#!/usr/bin/env python3
"""Headless regression checks for the 30-minute recovery watcher."""

from __future__ import annotations

import json
import plistlib
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from experiments import recovery_watch as watch  # noqa: E402

checks = 0
failures = []


def check(value, label, detail=""):
    global checks
    checks += 1
    if value:
        print("  ok  ", label)
    else:
        print("  FAIL", label, detail)
        failures.append(label)


check(watch.recovery_decision(100, 100) == (False, 0), "flat score does not declare recovery")
check(watch.recovery_decision(100, 99) == (False, -1), "score loss does not declare recovery")
check(watch.recovery_decision(100, 101) == (True, 1), "positive lifetime-score delta declares recovery")

plist_path = PROJECT / "deploy" / "com.nickfigura.farmfriends.recovery.plist"
plist = plistlib.loads(plist_path.read_bytes())
check(plist["StartInterval"] == 1800, "launchd cadence is exactly 30 minutes")
check(plist["RunAtLoad"] is True, "watcher performs an immediate baseline check")

saved = {
    "STATE": watch.STATE, "STORE": watch.STORE, "HISTORY": watch.HISTORY, "LOCK": watch.LOCK,
    "Client": watch.Client, "kick_cycle": watch.kick_cycle,
    "runtime_context": watch.policy.runtime_context, "set_context": watch.ledger.set_context,
    "record": watch.ledger.record,
}
try:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        watch.STATE = root
        watch.STORE = root / "recovery_watch.json"
        watch.HISTORY = root / "history.ndjson"
        watch.LOCK = root / ".lock"
        watch.HISTORY.write_text(json.dumps({
            "run": 1, "produce": 100, "leader": "Nick", "ts": "2026-08-25T00:00:00Z"
        }) + "\n", encoding="utf-8")
        current = {"score": 100, "calls": 0, "kicks": 0}

        class FakeClient:
            def call(self, tool):
                current["calls"] += 1
                return "Farm Friends leaderboard (by lifetime produce)\n1. Nick: %d produce, 10 animals, 20 coins" % current["score"]

        watch.Client = FakeClient
        watch.kick_cycle = lambda: current.__setitem__("kicks", current["kicks"] + 1) or {"target": "cycle"}
        watch.policy.runtime_context = lambda: {"policy_id": "test", "claim_registry_version": 1}
        watch.ledger.set_context = lambda **kwargs: None
        watch.ledger.record = lambda *args, **kwargs: None

        check(watch.main() == 0, "initial scheduled check succeeds")
        first = json.loads(watch.STORE.read_text())
        check(first["status"] == "waiting" and first["baseline_produce"] == 100,
              "initial check persists the outage baseline", str(first))
        check(current["kicks"] == 0, "flat score does not kickstart the farm")

        current["score"] = 105
        check(watch.main() == 0, "recovery check succeeds")
        recovered = json.loads(watch.STORE.read_text())
        check(recovered["status"] == "recovered" and recovered["delta"] == 5,
              "positive delta is persisted as recovery", str(recovered))
        check(current["kicks"] == 1, "recovery kickstarts the normal cycle exactly once")

        calls = current["calls"]
        check(watch.main() == 0 and current["calls"] == calls,
              "watcher becomes inert after recovery")
finally:
    watch.STATE = saved["STATE"]
    watch.STORE = saved["STORE"]
    watch.HISTORY = saved["HISTORY"]
    watch.LOCK = saved["LOCK"]
    watch.Client = saved["Client"]
    watch.kick_cycle = saved["kick_cycle"]
    watch.policy.runtime_context = saved["runtime_context"]
    watch.ledger.set_context = saved["set_context"]
    watch.ledger.record = saved["record"]

if failures:
    print("\nRECOVERY WATCH TEST FAILED: %d of %d" % (len(failures), checks))
    raise SystemExit(1)
print("\nRECOVERY WATCH TEST PASSED: %d checks" % checks)
