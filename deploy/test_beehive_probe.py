#!/usr/bin/env python3
"""Pure regression checks for the bounded beehive promotion gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from experiments import beehive_probe as probe  # noqa: E402

checks = 0
failures = []


def check(condition, name, detail=""):
    global checks
    checks += 1
    if condition:
        print("  ok  ", name)
    else:
        print("  FAIL", name, detail)
        failures.append(name + (": " + detail if detail else ""))


def row(run, ratio, *, hunger=0, errors=0, runway_feed=3_200_000):
    bees = 1_100
    chickens = 100_000
    eggs = 100_000
    honey = int(round(ratio * bees))
    return {
        "run": run,
        "verified": True,
        "interval_min": 5.0,
        "by_kind": {"beehive": bees, "chicken": chickens},
        "collected": {"honey": honey, "egg": eggs},
        "animals": bees + chickens,
        "feed": runway_feed,
        "max_hunger": hunger,
        "transport_errors_core": errors,
    }


state = {"baseline_run": 10, "beehives_before": 100, "beehives_after": 1_100}
original = probe.history_rows
try:
    probe.history_rows = lambda: [row(run, value) for run, value in enumerate([1.2] * 5, 11)]
    result = probe.analyze(state)
    check(result["supported"], "five uniformly strong windows promote beehives", json.dumps(result))
    check(result["decision"] == "promote_beehive", "supported result names the promotion decision")

    probe.history_rows = lambda: [
        row(run, value) for run, value in enumerate([0.23, 0.73, 1.24, 1.25, 1.25], 11)
    ]
    result = probe.analyze(state)
    check(not result["supported"], "warm-up underperformance fails the conservative gate")
    check(result["decision"] == "retain_chicken", "failed gate explicitly retains chicken")
    check(result["median_ratio"] > 1.1 and result["minimum_ratio"] < 1.0,
          "minimum-window guard can reject a favorable median", json.dumps(result))

    probe.history_rows = lambda: [row(run, 1.2, errors=1 if run == 13 else 0) for run in range(11, 16)]
    result = probe.analyze(state)
    check(not result["supported"] and result["safety_failures"],
          "transport regression blocks promotion", json.dumps(result))
finally:
    probe.history_rows = original

if failures:
    print("\nBEEHIVE PROBE TEST FAILED: %d of %d checks" % (len(failures), checks))
    for failure in failures:
        print("  -", failure)
    raise SystemExit(1)
print("\nBEEHIVE PROBE TEST PASSED: %d checks" % checks)
