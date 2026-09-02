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

from farm import claims, control, gates, governance, policy, questions, rules  # noqa: E402


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
        "release_gate_health": {"status": "pass", "revision": "rev-live", "failed": []},
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
    value["release_gate_health"] = {
        "status": "fail", "revision": "rev-other", "failed": ["knowledge"],
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
    suite.check(len(healthy) == 11, "review covers eleven autonomous operating contracts", healthy)
    suite.check(all(row["status"] == governance.PASS for row in healthy),
                "a healthy system passes every contract", healthy)
    suite.check({row["owner"] for row in healthy} >= {
        "cycle", "supervisor", "research", "dashboard", "author"
    }, "every loop has explicit review ownership")

    broken = governance.assess(broken_snapshot())
    failed = {row["id"] for row in broken if row["status"] == governance.FAIL}
    for expected in (
        "execution.progress", "strategy.objective", "runtime.services",
        "release.probation", "release.gate_health", "knowledge.policy", "observability.dashboard",
        "learning.question_flow", "healing.repair_flow", "safety.lineage",
    ):
        suite.check(expected in failed, "broken fixture fails %s" % expected, sorted(failed))
    storage = next(row for row in broken if row["id"] == "evidence.compaction")
    suite.check(storage["status"] == governance.WARN,
                "compaction debt is deferred, not acted on, during probation", storage)
    aged_canary = healthy_snapshot()
    aged_canary["canary"] = {
        "armed": True, "status": "watching", "revision": "rev-stuck",
        "armed_ts": "2020-01-01T00:00:00Z", "verdict": {"runs_observed": 0},
    }
    probation = next(row for row in governance.assess(aged_canary)
                     if row["id"] == "release.probation")
    suite.check(probation["status"] == governance.FAIL,
                "wall-clock age catches a canary with no completed runs", probation)

    print("== release gate certification")
    certified = {
        "schema_version": gates.SCHEMA_VERSION,
        "run": 120,
        "revision": "rev-live",
        "matrix_fingerprint": gates.fingerprint(),
        "observed": gates.names(),
        "complete": True,
        "passed": True,
        "failed": [],
    }
    suite.check(gates.assess("rev-live", 120, certified)["status"] == governance.PASS,
                "complete current matrix certification passes")
    suite.check(gates.assess("rev-live", 120, {})["status"] == governance.FAIL,
                "missing historical certification fails closed")
    suite.check(gates.assess("rev-other", 120, certified)["status"] == governance.FAIL,
                "certification for another revision fails closed")
    changed_matrix = dict(certified, matrix_fingerprint="old-matrix")
    suite.check(gates.assess("rev-live", 120, changed_matrix)["status"] == governance.FAIL,
                "a matrix change invalidates prior certification")
    previous_env = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory() as gate_tmp:
            os.environ["FARM_STATE_DIR"] = gate_tmp
            outcome = {
                "passed": True,
                "results": [{"gate": name, "ok": True} for name in gates.names()],
                "failed": [],
            }
            gates.record(outcome, "rev-a", 100, "pol-a")
            gates.record(outcome, "rev-b", 120, "pol-b")
            suite.check(gates.assess("rev-a", 125)["status"] == governance.PASS,
                        "rollback restores the archived certification for its revision")
            inherited_rows = [
                {"gate": name, "ok": None, "status": "inherited"}
                if name in {"knowledge", "evidence"}
                else {"gate": name, "ok": True, "status": "passed"}
                for name in gates.names()
            ]
            inherited = gates.record(
                {"passed": True, "results": inherited_rows, "failed": []},
                "rev-compat", 120, "pol-b", inherited_from="rev-a",
                waived=["knowledge", "evidence"], inherited_run=100,
            )
            suite.check(inherited["passed"]
                        and gates.assess("rev-compat", 120)["status"] == governance.PASS,
                        "compatibility certification marks inherited gates explicitly", inherited)
            suite.check(gates.assess("rev-compat", 141)["status"] == governance.WARN,
                        "compatibility release cannot refresh inherited evidence age")
            fabricated = gates.record(
                outcome, "rev-fabricated", 120, "pol-b", inherited_from="rev-a",
                waived=["knowledge", "evidence"], inherited_run=100,
            )
            suite.check(not fabricated["passed"],
                        "waived gates cannot be fabricated as fresh passing rows", fabricated)
    finally:
        os.environ.clear()
        os.environ.update(previous_env)

    print("== learning-flow boundaries")
    managed = {
        "id": "q-managed", "status": "open", "priority": "high",
        "owner": "research", "next_step": "run bounded fixture probe",
        "generation_opened_run": 81, "next_step_due_run": 121,
    }
    age_39 = questions.health(120, rows=[managed], event_rows=[])
    suite.check(age_39["status"] != governance.FAIL,
                "high-priority question remains inside the SLO at age 39", age_39)
    age_40 = questions.health(121, rows=[managed], event_rows=[])
    suite.check(age_40["status"] == governance.FAIL
                and age_40["overdue_high_priority"] == ["q-managed"],
                "high-priority question fails exactly at age 40", age_40)
    missing = questions.health(120, rows=[{
        "id": "q-missing", "status": "open", "priority": "critical",
    }], event_rows=[])
    suite.check(missing["status"] == governance.FAIL
                and missing["high_missing_metadata"] == ["q-missing"],
                "unknown age or ownership fails high-priority hygiene", missing)
    probing = questions.health(120, rows=[
        dict(managed, id="q-one", status="probing"),
        dict(managed, id="q-two", status="probing"),
    ], event_rows=[])
    suite.check(probing["status"] == governance.FAIL and probing["probing"] == 2,
                "probing WIP cannot exceed the single mutation boundary", probing)
    updates_only = questions.health(120, rows=[], event_rows=[
        {"event": "updated", "run": 119, "question_id": "q-managed"},
    ])
    suite.check(updates_only["current_flow"]["arrivals"] == 0
                and updates_only["current_flow"]["closures"] == 0,
                "question updates do not masquerade as arrival or closure flow", updates_only)

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
            suite.check("prioritize_learning_backlog" in actions,
                        "a stalled learning loop prioritizes existing WIP", routed.get("actions"))
            suite.check("route_policy_review" in actions,
                        "unresolved policy drift enters the bounded research lifecycle", routed.get("actions"))
            opened = [row for row in questions.open_questions()
                      if row.get("subject") == "governance learning loop"]
            suite.check(not opened,
                        "governance does not worsen WIP with an umbrella question", opened)
            policy_questions = [row for row in questions.open_questions()
                                if row.get("subject") == "semantic_contract"]
            suite.check(len(policy_questions) == 1
                        and policy_questions[0]["class"] == "policy_drift",
                        "policy incompatibility stays visible as a critical question", policy_questions)

            scope = governance._policy_repair_scope(
                {"claims": [
                    {"id": "strategy.chicken_engine", "status": "challenged"},
                    {"id": "strategy.capped_slot_efficiency", "status": "challenged"},
                ]},
                {"required_claims": [
                    "strategy.chicken_engine", "strategy.capped_slot_efficiency",
                ]},
            )
            suite.check(scope["files"] == ["experiments/dual_cap_audit.py"],
                        "known dual-cap claim drift routes only to its editable evidence producer", scope)

            stale_snapshot = healthy_snapshot(180)
            stale_snapshot["policy"] = {
                "compatible": False, "policy_id": "compiled-fixture",
                "errors": ["claim decisions differ from promoted policy"],
            }
            stale_checks = governance.assess(stale_snapshot)
            saved_refresh, saved_runtime = claims.refresh, policy.runtime_context
            try:
                claims.refresh = lambda: {"registry_version": 9, "claims": []}
                policy.runtime_context = lambda registry=None: {
                    "compatible": True, "policy_id": "pol-restored",
                    "claim_registry_version": 9, "errors": [],
                }
                reconciliation = governance.remediate(stale_snapshot, stale_checks)
            finally:
                claims.refresh, policy.runtime_context = saved_refresh, saved_runtime
            knowledge = next(row for row in stale_checks if row["id"] == "knowledge.policy")
            suite.check(knowledge["status"] == governance.PASS
                        and any(row.get("action") == "refresh_claim_registry"
                                for row in reconciliation),
                        "governance immediately clears drift caused only by stale persisted claims",
                        {"check": knowledge, "actions": reconciliation})
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
    suite.check("gates.commands()" in author_source
                and any("deploy/test_governance.py" in command for _, command in gates.MATRIX),
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
