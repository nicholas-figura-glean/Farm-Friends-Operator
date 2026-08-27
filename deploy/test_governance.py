#!/usr/bin/env python3
"""Deterministic tests for the periodic autonomous systems review."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import control, governance, questions, rules  # noqa: E402


class Suite:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: List[str] = []

    def check(self, condition: bool, label: str, detail: Any = "") -> None:
        self.checks += 1
        passed = bool(condition)
        print("  %-4s %s%s" % ("ok" if passed else "FAIL", label,
                               (" [%s]" % detail) if detail and not passed else ""))
        if not passed:
            self.failures.append(label + ((" [%s]" % detail) if detail else ""))


def healthy_snapshot(run: int = 120) -> Dict[str, Any]:
    history = [
        {"run": value, "produce": 1_000_000 + value * 10_000, "rank": 1}
        for value in range(run - rules.GOVERNANCE_REVIEW_RUNS, run + 1)
    ]
    return {
        "run": run,
        "history": history,
        "services": [dict(spec, loaded=True) for spec in control.SERVICES],
        "canary": {"armed": False, "status": "healthy", "revision": "rev-live"},
        "compaction": [
            {"ledger": "tool_calls.ndjson", "active_bytes": 1024, "segments": 1},
        ],
        "compaction_compatibility": {"revision": "rev-live"},
        "live_revision": "rev-live",
        "policy": {"compatible": True, "policy_id": "pol-fixture", "errors": []},
        "dashboard": {
            "ok": True, "ts": "2026-08-27T00:00:00Z",
            "problems": [], "staleness": {"cycle_age": 60},
        },
        "questions": [],
        "experiments": [],
        "orders": [],
        "efficacy": {"champion": {"revision": "rev-live", "cumulative_ratio": 1.0}},
        "lineage": {"graph_valid": True, "nodes": 1},
    }


def broken_snapshot(run: int = 120) -> Dict[str, Any]:
    value = healthy_snapshot(run)
    value["history"] = [
        {"run": run - 20, "produce": 2_000_000, "rank": 1},
        {"run": run, "produce": 2_000_000, "rank": 2},
    ]
    value["services"][0]["loaded"] = False
    value["canary"] = {
        "armed": True, "status": "watching", "revision": "rev-new",
        "verdict": {"runs_observed": rules.EFFICACY_MIN_RUNS * 2 + 1},
    }
    value["compaction"] = [{
        "ledger": "tool_calls.ndjson", "active_bytes": governance.compaction.DEFAULT_MAX_BYTES * 3,
        "segments": 0,
    }]
    value["compaction_compatibility"] = {}
    value["policy"] = {"compatible": False, "policy_id": "pol-bad", "errors": ["fixture drift"]}
    value["dashboard"] = {
        "ok": True, "ts": "2026-08-27T00:00:00Z", "problems": [],
        "staleness": {"cycle_age_error": "fixture freshness failure"},
    }
    value["questions"] = [{
        "id": "q-aged", "status": "open", "priority": "high", "opened_run": run - 80,
    }]
    value["orders"] = [{
        "id": "repair-aged", "status": "open", "severity": "breaking",
        "age_seconds": 10_000, "kind": "repair", "provenance": {},
    }, {
        "id": "strategy-unlinked", "status": "open", "severity": "opportunity",
        "age_seconds": 10, "kind": "strategy_hypothesis", "provenance": {},
    }]
    value["efficacy"] = {"champion": {}}
    value["lineage"] = {"graph_valid": False, "nodes": 0}
    return value


def main() -> int:
    suite = Suite()

    print("== review contract")
    healthy = governance.assess(healthy_snapshot())
    suite.check(len(healthy) == 10, "review covers ten autonomous operating contracts", healthy)
    suite.check(all(row["status"] == governance.PASS for row in healthy),
                "a healthy system passes every contract", healthy)
    suite.check({row["owner"] for row in healthy} >= {
        "cycle", "supervisor", "research", "dashboard", "author"
    }, "every loop has explicit review ownership")

    broken = governance.assess(broken_snapshot())
    failed = {row["id"] for row in broken if row["status"] == governance.FAIL}
    for expected in (
        "execution.progress", "strategy.objective", "runtime.services",
        "release.probation", "knowledge.policy", "observability.dashboard",
        "learning.question_flow", "healing.repair_flow", "safety.lineage",
    ):
        suite.check(expected in failed, "broken fixture fails %s" % expected, sorted(failed))
    storage = next(row for row in broken if row["id"] == "evidence.compaction")
    suite.check(storage["status"] == governance.WARN,
                "compaction debt is deferred, not acted on, during probation", storage)

    print("== run cadence, persistence, and trend")
    previous_env = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FARM_STATE_DIR"] = tmp
            first = governance.run_review(
                100, snapshot=healthy_snapshot(100), apply_remediation=False,
            )
            early = governance.run_review(
                119, snapshot=healthy_snapshot(119), apply_remediation=False,
            )
            due = governance.run_review(
                120, snapshot=broken_snapshot(120), apply_remediation=False,
            )
            recovered = governance.run_review(
                140, snapshot=healthy_snapshot(140), apply_remediation=False,
            )
            suite.check(first.get("recorded") and first["run"] == 100,
                        "first review records immediately")
            suite.check(not early.get("recorded") and early.get("next_run") == 120,
                        "review does not run before the 20-run boundary", early)
            suite.check(due.get("recorded") and due["status"] == governance.FAIL,
                        "missed modulo boundaries retry at the next due run", due)
            suite.check("runtime.services" in due.get("regressions", []),
                        "review records newly regressed contracts", due.get("regressions"))
            suite.check("runtime.services" in recovered.get("recoveries", []),
                        "later reviews record autonomous recovery", recovered.get("recoveries"))
            persisted = governance.rows()
            suite.check([row["run"] for row in persisted] == [100, 120, 140],
                        "only due reviews are durable", [row.get("run") for row in persisted])

            review_path = Path(tmp) / "governance_reviews.ndjson"
            suite.check(review_path.is_file() and len(review_path.read_text().splitlines()) == 3,
                        "reviews are append-only NDJSON")

            remediation_fixture = broken_snapshot(160)
            remediation_fixture["services"] = [dict(spec, loaded=True) for spec in control.SERVICES]
            routed = governance.run_review(
                160, force=True, snapshot=remediation_fixture, apply_remediation=True,
            )
            actions = {row.get("action") for row in routed.get("actions") or []}
            suite.check("route_learning_review" in actions,
                        "a stalled learning loop opens a bounded strategy question", routed.get("actions"))
            opened = [row for row in questions.open_questions()
                      if row.get("subject") == "governance learning loop"]
            suite.check(len(opened) == 1 and opened[0]["class"] == "strategy_stale",
                        "governance remediation enters the existing probe lifecycle", opened)
    finally:
        os.environ.clear()
        os.environ.update(previous_env)

    print("== trust boundary and CLI integration")
    suite.check(rules.GOVERNANCE_REVIEW_RUNS == rules.JOURNAL_EVERY == 20,
                "review aligns with the journal and claim evidence window")
    suite.check(control.is_protected("farm/governance.py"),
                "the model author cannot rewrite its own reviewer")
    release_source = (PROJECT / "deploy" / "release.sh").read_text(encoding="utf-8")
    author_source = (PROJECT / "experiments" / "author_agent.py").read_text(encoding="utf-8")
    run_source = (PROJECT / "run.py").read_text(encoding="utf-8")
    suite.check("deploy/test_governance.py" in release_source,
                "governance tests gate manual releases")
    suite.check("deploy/test_governance.py" in author_source,
                "governance tests gate autonomous releases")
    suite.check("governance.run_review" in run_source and "--governance-status" in run_source,
                "the supervisor and CLI expose the periodic review")
    governance_source = (PROJECT / "farm" / "governance.py").read_text(encoding="utf-8")
    suite.check('control.project_root() / "release"' in governance_source,
                "deployed governance resolves the canonical release pointer")

    print()
    if suite.failures:
        print("GOVERNANCE TEST FAILED: %d of %d checks" % (len(suite.failures), suite.checks))
        for failure in suite.failures:
            print("  - " + failure)
        return 1
    print("GOVERNANCE TEST PASSED: %d checks" % suite.checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
