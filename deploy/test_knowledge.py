#!/usr/bin/env python3
"""Regression and failure-injection suite for the epistemic control plane."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import analysis, claims, cycle, heal, ledger, policy, probes, questions, research, rules  # noqa: E402


class Suite:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: List[str] = []

    def check(self, condition: bool, label: str, detail: Any = "") -> None:
        self.checks += 1
        passed = bool(condition)
        suffix = "  [%s]" % detail if detail and not passed else ""
        print("  %-4s %s%s" % ("ok" if passed else "FAIL", label, suffix))
        if not passed:
            self.failures.append(label + ((" [%s]" % detail) if detail else ""))

    def raises(self, exc_type, fn, label: str) -> None:
        raised = False
        try:
            fn()
        except exc_type:
            raised = True
        except Exception as exc:  # wrong failure is still a failure
            self.check(False, label, "%s: %s" % (exc.__class__.__name__, exc))
            return
        self.check(raised, label)


def read_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def first_finding(rows, code: str, subject: str = ""):
    for index in range(len(rows)):
        for finding in rules.strategy_audit(rows[: index + 1]):
            if finding.get("code") == code and (
                not subject or str(finding.get("subject", "")).lower() == subject.lower()
            ):
                return finding
    return None


def main() -> int:
    suite = Suite()
    source_history = PROJECT / "state" / "history.ndjson"
    live_rows = analysis.history_rows(path=source_history)
    suite.check(len(live_rows) > 400, "fixture contains more than the old hidden retention limit",
                len(live_rows))

    previous_env = dict(os.environ)
    saved_heal = (heal.STORE, heal.LEDGER)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            shutil.copy2(source_history, state / "history.ndjson")
            os.environ["FARM_STATE_DIR"] = str(state)
            os.environ["FARM_TOOL_CALL_LOG"] = str(state / "tool_calls.ndjson")
            heal.STORE = str(state / "heal.json")
            heal.LEDGER = str(state / "heal.ndjson")

            rows = analysis.history_rows()
            suite.check(len(rows) == len(live_rows), "full-history reader preserves every valid run")
            suite.check(min(row["animals"] for row in rows) < 1_000,
                        "early regime remains available after 400 runs")
            model = analysis.output_model(rows)
            suite.check(model["shape"] == "linear", "output estimator classifies the current game as linear",
                        model["shape"])
            suite.check((model["regression"].get("slope") or 0) > 0.05,
                        "healthy marginal output remains positive", model["regression"])
            # regression() publishes r at three decimals; equality is the
            # documented floor, not evidence of a sub-threshold association.
            suite.check((model["regression"].get("r") or 0) >= 0.7,
                        "healthy raw herd/output association remains positive", model["regression"])
            # Growth must still be paying. This is asserted on the scaling exponent,
            # not on the straight-line r of the bucket means, because that r cannot
            # distinguish the two failure directions and is not stable enough to gate
            # on. Both facts were measured on 2026-08-25:
            #
            #   * Wrong direction. On synthetic data with a true exponent of 0.70 --
            #     hard saturation, the exact condition that should stop adoption --
            #     straight-line r is 0.993. The old assertion passed most convincingly
            #     precisely when the farm had stopped paying for growth.
            #   * Unstable. On the live cohort, r of the bucket means was 0.925
            #     unweighted, 0.954 sample-weighted, 0.917 sqrt-weighted and 0.992 with
            #     the newest band dropped, and it moved 0.954 -> 0.943 on the arrival of
            #     one additional run. A threshold of 0.95 sat inside that spread, so the
            #     gate's verdict was decided by arbitrary choices rather than by the farm.
            #
            # The exponent has neither problem: it recovered 1.00/0.90/0.70/0.00 from
            # synthetic cohorts, and reads 1.159 on raw samples versus 1.129 on bucket
            # means, so bucket edges barely move it.
            suite.check(not model["saturating"],
                        "herd growth is still paying (scaling exponent >= 0.95)",
                        model["scaling"])
            suite.check((model["scaling"].get("exponent") or 0) >= 0.95,
                        "raw-sample scaling exponent is at least proportional",
                        model["scaling"])
            suite.check((model["scaling"].get("r") or 0) > 0.8,
                        "the power-law form fits the healthy cohort well",
                        model["scaling"])
            # r of the bucket means is still reported, but bounded loosely enough to
            # catch a structural break rather than ordinary bucket churn.
            suite.check((model["regression_bucketed"].get("r") or 0) > 0.85,
                        "bucket-smoothed herd/output association has not broken down",
                        model["regression_bucketed"])
            suite.check((model["regression_bucketed_weighted"].get("r") or 0) > 0.85,
                        "sample-weighted bucket fit agrees with the unweighted one",
                        model["regression_bucketed_weighted"])
            # The association is not causally identified; the model must say so, or the
            # dashboard will read a scaling law into a time series.
            suite.check(model["confound"]["identified"] is False,
                        "the herd/output association is recorded as unidentified")
            suite.check(len(model["cohort"]["sha256"]) == 64,
                        "evidence cohort has an immutable SHA-256 identity")

            # Claim lifecycle and semantic consistency.
            registry = claims.refresh(rows)
            suite.check(not claims.validate(registry), "fresh claim registry validates")
            suite.check((claims.get("mechanic.output_linear_with_herd", registry) or {}).get("status") == "accepted",
                        "linear claim is accepted")
            suite.check((claims.get("mechanic.per_farm_output_plateau", registry) or {}).get("status") == "superseded",
                        "false plateau is superseded")
            suite.check((claims.get("mechanic.crop_timers_stalled", registry) or {}).get("refresh", {}).get("state") == "overdue",
                        "old negative crop result becomes overdue instead of immortal")
            version = registry["registry_version"]
            same = claims.refresh(rows)
            suite.check(same["registry_version"] == version,
                        "identical evidence does not churn the registry version")
            claim_events = read_rows(state / "claim_events.ndjson")
            suite.check(len(claim_events) == len(registry["claims"]),
                        "first refresh writes one creation event per claim", len(claim_events))

            contradictory = copy.deepcopy(registry)
            for item in contradictory["claims"]:
                if item["id"] == "mechanic.per_farm_output_plateau":
                    item["status"] = "accepted"
                    item["superseded_by"] = None
            contradictory["semantic_fingerprint"] = claims.semantic_fingerprint(contradictory)
            contradictory["policy_fingerprint"] = claims.policy_fingerprint(contradictory)
            suite.check("contradictory output claims are both accepted" in claims.validate(contradictory),
                        "semantic validation rejects simultaneous linear and plateau truth")

            # Policy compilation, promotion, and tamper detection.
            candidate = policy.compile_snapshot(registry)
            suite.check(candidate["audit"]["ok"], "compatible candidate policy compiles", candidate["audit"])
            suite.check(set(candidate["parameters"]) == set(policy.OWNERS),
                        "every compiled parameter has an ownership declaration")
            promoted = policy.promote(candidate, registry)
            runtime = policy.runtime_context(registry)
            suite.check(runtime["compatible"] and runtime["policy_id"] == promoted["policy_id"],
                        "promoted policy matches compiled rules and claim decisions", runtime)

            tampered = copy.deepcopy(promoted)
            tampered["parameters"]["collect_every"] = int(tampered["parameters"]["collect_every"]) + 1
            (state / "policy.json").write_text(json.dumps(tampered), encoding="utf-8")
            runtime = policy.runtime_context(registry)
            suite.check(not runtime["compatible"], "policy content tampering is detected")
            suite.check("promoted policy content hash mismatch" in runtime["errors"],
                        "tamper failure names the content hash")
            policy.promote(candidate, registry)

            challenged = copy.deepcopy(registry)
            for item in challenged["claims"]:
                if item["id"] == "mechanic.output_linear_with_herd":
                    item["status"] = "challenged"
            challenged["semantic_fingerprint"] = claims.semantic_fingerprint(challenged)
            challenged["policy_fingerprint"] = claims.policy_fingerprint(challenged)
            rejected = policy.compile_snapshot(challenged)
            suite.check(not rejected["audit"]["ok"],
                        "policy compile fails when a required claim is challenged")
            suite.raises(ValueError, lambda: policy.promote(rejected, challenged),
                         "challenged evidence cannot be promoted")

            # Observation context, uniqueness, pairing, and bounded failure.
            with ledger.bind(actor="cycle", run=999, policy_id=promoted["policy_id"], step="plan"):
                first = ledger.record("test.observation", {"payload": "x" * 3_000}, strict=True)
                intervention_id = ledger.intervention("adopt", "planned", {"count": 3})
                ledger.intervention("adopt", "outcome", {"count": 3}, intervention_id=intervention_id)
            observations = read_rows(state / "observations.ndjson")
            suite.check(len({item["event_id"] for item in observations}) == len(observations),
                        "every observation row has a unique event id")
            suite.check(observations[0]["actor"] == "cycle" and observations[0]["run"] == 999,
                        "observation rows inherit actor and run context")
            suite.check(len(observations[0]["data"]["payload"]) == 2_000,
                        "observation payloads are bounded")
            paired = [item for item in observations if item["event"].startswith("intervention.")]
            suite.check(len(paired) == 2 and len({item["data"]["intervention_id"] for item in paired}) == 1,
                        "intervention phases pair by a stable intervention id")

            saved_intents, saved_raw = cycle.INTENTS, cycle.RAW_DIR
            cycle.INTENTS = str(state / "test-intents.ndjson")
            cycle.RAW_DIR = str(state / "raw" / "test")
            try:
                before = len(read_rows(state / "observations.ndjson"))
                cycle._intent("fixture_action", value=1)
                after_unscoped = len(read_rows(state / "observations.ndjson"))
                with ledger.bind(actor="selftest", run=1):
                    cycle._intent("fixture_action", value=2)
                after_scoped = len(read_rows(state / "observations.ndjson"))
            finally:
                cycle.INTENTS, cycle.RAW_DIR = saved_intents, saved_raw
            suite.check(after_unscoped == before,
                        "unscoped fixture intents cannot pollute the epistemic ledger")
            suite.check(after_scoped == before + 1,
                        "scoped execution intents still enter the epistemic ledger")
            invalid_id = observations[0]["event_id"]
            with ledger.bind(actor="migration"):
                ledger.record("observation.invalidated", {
                    "invalid_event_ids": [invalid_id],
                    "reason": "failure-injection test",
                }, strict=True)
            suite.check(
                invalid_id not in {row.get("event_id") for row in ledger.rows()}
                and invalid_id in {row.get("event_id") for row in ledger.rows(include_invalid=True)},
                "append-only invalidation removes bad observations from the valid view",
            )
            completion_row = {
                "run": 7, "rank": 1, "produce": 100, "animals": 10, "coins": 20,
                "feed": 300, "max_hunger": 6, "reserve_target": 300,
                "plan": {"adopt": 2, "buy_feed": 60},
                "plan_inputs": {"animals": 8, "coins": 100, "feed": 240, "committed_feed": 0},
                "adopted": 2, "adopt_requested": 2, "feed_bought": 60, "fed": True,
                "collect_passes": 1, "revenue": 0, "verified": True,
                "policy_id": promoted["policy_id"], "claim_registry_version": registry["registry_version"],
                "decision_trace": {"policy_id": promoted["policy_id"]}, "regimes": ["fed"],
            }
            ledger.record_cycle(completion_row, {"run": 6, "rank": 1, "produce": 90})
            completion = ledger.rows(include_invalid=True)[-1]
            suite.check(
                completion.get("actor") == "cycle"
                and completion.get("policy_id") == promoted["policy_id"]
                and completion.get("claim_registry_version") == registry["registry_version"],
                "cycle completion carries explicit actor, policy, and registry identity",
                completion,
            )

            name, remedy = heal.classify(
                "feed reserve still short after reconciliation: 3284805/3285915"
            )
            suite.check(name == "feed_reserve" and remedy is not None,
                        "reconciliation remainder is classified as operational feed reserve")
            cycle_source = (PROJECT / "farm" / "cycle.py").read_text(encoding="utf-8")
            expand_source = (PROJECT / "experiments" / "expand.py").read_text(encoding="utf-8")
            suite.check("remaining <= tolerance" in cycle_source,
                        "reconciliation source applies the same reserve tolerance as detection")
            suite.check('"expansion.failed"' in expand_source and '"hard_timeout"' in expand_source,
                        "expansion watchdog records a bounded failure outcome")

            old_log = os.environ["FARM_OBSERVATION_LOG"] if "FARM_OBSERVATION_LOG" in os.environ else None
            os.environ["FARM_OBSERVATION_LOG"] = str(state / "not-a-dir" / "events.ndjson")
            (state / "not-a-dir").write_text("x", encoding="utf-8")
            suite.check(bool(ledger.record("test.unwritable", {"ok": True})),
                        "best-effort ledger returns an id when storage is unavailable")
            if old_log is None:
                os.environ.pop("FARM_OBSERVATION_LOG", None)
            else:
                os.environ["FARM_OBSERVATION_LOG"] = old_log

            # Historical replay acceptance criteria.
            stale = first_finding(rows, "strategy_stale")
            suite.check(stale is not None and stale["run"] <= 80,
                        "strategy-stale replay fires by run 80", stale)
            suite.check(not any(
                finding.get("code") == "strategy_stale"
                for index in range(min(50, len(rows)))
                for finding in rules.strategy_audit(rows[: index + 1])
            ), "strategy-stale replay has zero fires in runs 1-50")
            john = first_finding(rows, "rival_wake", "John")
            suite.check(john is not None and john["run"] == 241,
                        "John rival wake first fires at run 241", john)
            suite.check(john is not None and abs(john["recent_rate"] - 0.5) < 0.01,
                        "John wake rate matches the measured replay", john)

            tool_log = state / "tool_calls.ndjson"
            before_size = tool_log.stat().st_size if tool_log.exists() else 0
            sweep = research.counterfactual_sweep(rows)
            after_size = tool_log.stat().st_size if tool_log.exists() else 0
            suite.check(sweep["mcp_calls"] == 0 and before_size == after_size,
                        "counterfactual sweep provably makes zero MCP calls")
            growth = next(item for item in sweep["dimensions"]
                          if item["parameter"] == "GROWTH_MIN_MARGINAL_GAIN")
            old_threshold = next(item for item in growth["alternatives"] if item["value"] == 0.10)
            suite.check(old_threshold["changed_runs"] > 100,
                        "old 10% threshold visibly changes the historical strategy", old_threshold)

            semantic = research.semantic_audit(rows, registry, promoted=policy.load())
            suite.check(semantic["ok"], "full semantic audit passes", semantic["errors"])
            suite.check(not any(
                name in heal.STRATEGY_CLASSES and remedy is not None
                for name, _, remedy in heal.CLASSES
            ), "no strategic class is reachable from a healing remedy")

            # Question identity, deduplication, close/reopen, and headless routing.
            q1 = questions.open_or_update(
                "rival_wake", "RIVAL WAKE: John recent 0.500/min vs base 0.000/min",
                row={"run": 241, "ts": "2026-08-22T00:00:00Z"},
            )
            q2 = questions.open_or_update(
                "rival_wake", "RIVAL WAKE: John recent 0.700/min vs base 0.100/min",
                row={"run": 242, "ts": "2026-08-22T00:05:00Z"},
            )
            suite.check(q1["question"]["id"] == q2["question"]["id"],
                        "same strategic condition keeps one stable question id")
            suite.check(len(questions.load_all()) == 1 and q2["question"]["occurrences"] == 2,
                        "re-alert updates one current question row")
            suite.check(not q1["page_on_open"] and not q2["page_on_open"],
                        "rival wake opens research without paging")
            questions.set_status(q1["question"]["id"], "answered", "rival resumed feeding", ["history#241"], 243)
            repeated = questions.open_or_update(
                "rival_wake", "RIVAL WAKE: John recent 0.800/min vs base 0.100/min",
                row={"run": 244, "ts": "2026-08-22T00:10:00Z"},
            )
            suite.check(not repeated["reopened"] and repeated["question"]["status"] == "answered",
                        "an immediate repeated alert does not erase a probe answer")
            reopened = questions.open_or_update(
                "rival_wake", "RIVAL WAKE: John recent 2.000/min vs base 0.000/min",
                row={"run": 268, "ts": "2026-08-22T02:00:00Z"},
            )
            suite.check(reopened["reopened"] and reopened["question"]["generation"] == 2,
                        "new evidence reopens an answered question as a new generation")
            suite.raises(ValueError, lambda: questions.set_status(q1["question"]["id"], "forgotten"),
                         "invalid question status is rejected")

            age1 = questions.open_or_update(
                "knob_age", "KNOB AGE: individual_feeds=100 unchanged for 40 runs (since run 435)",
                row={"run": 475, "ts": "2026-08-22T03:00:00Z"},
            )
            age2 = questions.open_or_update(
                "knob_age", "KNOB AGE: individual_feeds=100 unchanged for 41 runs (since run 435)",
                row={"run": 476, "ts": "2026-08-22T03:05:00Z"},
            )
            suite.check(age1["question"]["id"] == age2["question"]["id"],
                        "changing knob age text keeps one canonical identity")
            age3 = questions.open_or_update(
                "knob_age", "KNOB AGE: individual_feeds unchanged for 42 runs (since run 435)",
                row={"run": 477, "ts": "2026-08-22T03:10:00Z"},
            )
            suite.check(age2["question"]["id"] == age3["question"]["id"],
                        "knob identity ignores a value sometimes omitted from alert prose")
            current_path, _, _ = questions._paths()
            legacy = dict(age2["question"], id="q-legacy-changing-age",
                          key="knob_age:individual_feeds=100 unchanged for 42 runs (since run 435)",
                          subject="individual_feeds=100 unchanged for 42 runs (since run 435)",
                          occurrences=3)
            questions._write_current(current_path, questions.load_all() + [legacy])
            reconciled = questions.reconcile_duplicates(run=477)
            knob_rows = [q for q in questions.load_all() if q.get("class") == "knob_age"]
            suite.check(reconciled["removed"] == 1 and len(knob_rows) == 1,
                        "legacy changing-age identities reconcile into one durable row", reconciled)
            suite.check(knob_rows[0]["occurrences"] == 6,
                        "reconciliation preserves accumulated occurrences", knob_rows[0])

            # Healing's third disposition: durable agent-owned question, no page.
            shutil.rmtree(state)
            state.mkdir()
            shutil.copy2(source_history, state / "history.ndjson")
            heal.STORE = str(state / "heal.json")
            heal.LEDGER = str(state / "heal.ndjson")
            row = rows[290]
            result = heal.process(
                [{"run": 291, "ts": row.get("ts"), "alert": "RANK LOST: now #2"}],
                row,
                291,
            )
            suite.check(len(result["questions"]) == 1 and len(result["routed"]) == 1
                        and not result["escalated"],
                        "first critical strategy alert routes one question without paging")
            suite.check(not result["knobs"], "strategy question cannot change healing knobs")
            result2 = heal.process(
                [{"run": 292, "ts": row.get("ts"), "alert": "RANK LOST: now #2"}],
                dict(row, run=292),
                292,
            )
            suite.check(len(result2["questions"]) == 1 and len(result2["routed"]) == 1
                        and not result2["escalated"],
                        "repeated critical alert updates the same headless queue")
            suite.check(len(questions.load_all()) == 1 and questions.load_all()[0]["occurrences"] == 2,
                        "question disposition leaves one current ledger row")

            # Probe registry and budget enforcement.
            listed = {item["id"]: item for item in probes.list_probes()}
            suite.check(all((item.get("budget") or {}).get("wall_seconds") for item in listed.values()),
                        "every registered probe has a wall-time budget")
            suite.check(not listed["species_mix"]["autonomous"] and not listed["species_mix"]["read_only"],
                        "mutating species probe can never be autonomous")
            suite.raises(ValueError, lambda: probes.run_probe("species_mix", explicit=False, run=500),
                         "scheduler refuses a mutating probe")
            probe = probes.run_probe("counterfactual_sweep", explicit=False, run=500)
            suite.check(probe["status"] == "passed", "autonomous pure replay probe passes", probe)
            suite.check((probe.get("budget") or {}).get("calls") == 0,
                        "pure replay probe declares a zero-call budget")
            suite.check(not (state / "tool_calls.ndjson").exists(),
                        "pure replay probe leaves no MCP telemetry")

    finally:
        heal.STORE, heal.LEDGER = saved_heal
        os.environ.clear()
        os.environ.update(previous_env)

    print()
    if suite.failures:
        print("KNOWLEDGE TEST FAILED: %d of %d checks" % (len(suite.failures), suite.checks))
        for failure in suite.failures:
            print("  - " + failure)
        return 1
    print("KNOWLEDGE TEST PASSED: %d checks" % suite.checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
