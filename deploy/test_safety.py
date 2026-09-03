#!/usr/bin/env python3
"""Regression suite for compaction, provenance, efficacy, and anti-oscillation."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, List

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import (  # noqa: E402
    analysis, canary, claims, compaction, control, evaluation, policy, probes,
    provenance, questions, sandbox, workorders,
)


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

    def raises(self, exc: type, fn: Callable[[], Any], label: str) -> None:
        try:
            fn()
        except exc:
            self.check(True, label)
        except Exception as error:  # wrong failure is still failure
            self.check(False, label, "%s: %s" % (error.__class__.__name__, error))
        else:
            self.check(False, label, "did not raise")


def section(title: str) -> None:
    print("\n== %s" % title)


def runs(start: int, count: int, per_animal: float) -> List[dict]:
    return [
        {
            "run": start + index,
            "produce_per_min": per_animal * 100.0,
            "animals": 100,
            "collected": 10,
        }
        for index in range(count)
    ]


def write_rows(path: Path, rows: List[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def main() -> int:
    suite = Suite()
    previous_env = dict(os.environ)
    try:
        section("lossless checksummed ledger compaction")
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            ledger = state / "history.ndjson"
            expected = [{"run": index, "ts": "2026-08-26T00:%02d:00Z" % index}
                        for index in range(1, 21)]
            for row in expected:
                compaction.append_json(ledger, row)
            before = compaction.read_rows(ledger)
            result = compaction.compact_ledger(ledger, max_bytes=1, hot_rows=5)
            after = compaction.read_rows(ledger)
            suite.check(result.get("compacted") and result.get("rows_moved") == 15,
                        "oversized ledger rotates all but the bounded hot tail", result)
            suite.check(before == expected == after,
                        "logical replay is identical before and after compaction")
            suite.check(len(ledger.read_text(encoding="utf-8").splitlines()) == 5,
                        "legacy active path retains only the hot rows")
            suite.check(compaction.read_rows(ledger, limit=3) == expected[-3:],
                        "bounded tail reads cross the segment boundary transparently")
            saved_recover = compaction.recover
            try:
                compaction.recover = lambda unused: (_ for _ in ()).throw(
                    AssertionError("normal reads took the exclusive recovery path"))
                no_recovery_tail = compaction.read_rows(ledger, limit=3)
            finally:
                compaction.recover = saved_recover
            suite.check(no_recovery_tail == expected[-3:],
                        "normal reads avoid the exclusive recovery lock when no transaction exists")

            # A dashboard tail read must not parse a 204MB active ledger from byte zero.
            # Include a row larger than the read block and malformed trailing lines so
            # the reverse scanner proves both boundary reconstruction and valid-row limits.
            large_active = state / "large.ndjson"
            large_rows = [{"run": 1}, {"run": 2, "blob": "x" * 70000}, {"run": 3}]
            large_active.write_bytes(
                b"".join((json.dumps(row) + "\n").encode("utf-8") for row in large_rows)
                + b"not-json\n[]\n"
            )
            saved_parse = compaction._parse
            tail_error = None
            try:
                compaction._parse = lambda payload: (_ for _ in ()).throw(
                    AssertionError("bounded active tail used the full-history parser"))
                bounded_tail = compaction.read_rows(large_active, limit=2)
            except Exception as exc:  # noqa: BLE001 - reported as a suite failure
                bounded_tail, tail_error = [], str(exc)
            finally:
                compaction._parse = saved_parse
            suite.check(bounded_tail == large_rows[-2:],
                        "bounded active reads scan backward across blocks", tail_error or bounded_tail)
            suite.check(analysis.read_ndjson(ledger) == expected,
                        "analysis uses the transparent full-history reader")
            compaction.append_json(ledger, {"run": 21, "ts": "2026-08-26T00:21:00Z"})
            suite.check([row["run"] for row in compaction.read_rows(ledger)][-2:] == [20, 21],
                        "appends continue on the hot ledger after rotation")
            status = compaction.status(ledger)
            suite.check(status["segments"] == 1 and status["archived_rows"] == 15,
                        "manifest reports immutable archived rows", status)
            marked = compaction.mark_compatible(state, "rev-compatible")
            suite.check(
                marked["revision"] == "rev-compatible"
                and compaction.compatibility(state)["reader"] == "segmented-ndjson-v1",
                "accepted readers establish an explicit compaction watermark",
            )

            manifest = json.loads((state / "segments" / "history" / "manifest.json").read_text())
            segment = state / "segments" / "history" / manifest["segments"][0]["file"]
            payload = bytearray(segment.read_bytes())
            payload[-1] ^= 0xFF
            segment.write_bytes(bytes(payload))
            suite.raises(compaction.CompactionError, lambda: compaction.read_rows(ledger),
                         "tampered archive fails closed instead of changing an estimator")

        section("pre-registration and acyclic evidence lineage")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FARM_STATE_DIR"] = tmp
            spec = {
                "hypothesis": "A candidate feed policy improves produce per animal.",
                "null_hypothesis": "The candidate has no produce-per-animal benefit.",
                "falsifier": "A held-out cohort is no better than champion.",
                "primary_metric": "produce per animal per minute",
                "expected_improvement": 0.02,
            }
            first = provenance.register_hypothesis(spec, ["history#runs=1-20"], "pol-a")
            duplicate = provenance.register_hypothesis(spec, ["history#runs=1-20"], "pol-a")
            suite.check(first.get("accepted") and first.get("id", "").startswith("hyp-"),
                        "hypothesis is pre-registered by semantic identity", first)
            suite.check(not duplicate.get("accepted") and duplicate.get("duplicate"),
                        "title-independent duplicate registration is suppressed", duplicate)
            provenance.record_result(
                first["id"], "falsified", ["experiment#cohort=21-30"], "holdout",
                {"effect": -0.01},
            )
            blocked = provenance.register_hypothesis(spec, ["history#runs=1-20"], "pol-a")
            reopened = provenance.register_hypothesis(spec, ["history#runs=31-50"], "pol-a")
            suite.check(not blocked.get("accepted") and "novel" in blocked.get("reason", ""),
                        "failed hypothesis cannot loop on unchanged evidence", blocked)
            suite.check(reopened.get("accepted"),
                        "genuinely new discovery evidence can reopen a failed hypothesis", reopened)
            suite.check(provenance.status()["graph_valid"], "persisted provenance graph is acyclic")
            cycle_rows = [
                {"event": "x", "node": "a", "parents": ["b"]},
                {"event": "x", "node": "b", "parents": ["a"]},
            ]
            suite.raises(provenance.ProvenanceError,
                         lambda: provenance.validate_graph(cycle_rows),
                         "explicit circular lineage is rejected")

            provenance.record_result(
                reopened["id"], "supported", ["experiment#cohort=51-70"], "holdout",
                {"effect": 0.03, "lower_bound": 0.02},
            )
            contract = {
                "hypothesis_id": reopened["id"],
                "null_hypothesis": spec["null_hypothesis"],
                "falsifier": spec["falsifier"],
                "primary_metric": spec["primary_metric"],
                "evidence_class": "holdout",
                "expected_improvement": 0.02,
                "discovery_evidence": ["history#runs=31-50"],
                "validation_evidence": ["experiment#cohort=51-70"],
            }
            errors = provenance.validate_promotion_contract(contract, "pol-b", "pol-a", [])
            suite.check(not errors, "disjoint pre-registered holdout result can promote", errors)
            unsupported = dict(contract, validation_evidence=["experiment#cohort=71-90"])
            suite.check(any("supporting" in item for item in
                            provenance.validate_promotion_contract(unsupported, "pol-b", "pol-a", [])),
                        "a contract cannot invent validation evidence without a result node")
            observational = dict(contract, evidence_class="observational")
            suite.check(any("observational" in item for item in
                            provenance.validate_promotion_contract(observational, "pol-b", "pol-a", [])),
                        "correlation may propose a probe but cannot promote policy")
            overlapping = dict(contract, validation_evidence=["history#runs=31-50"])
            suite.check(any("overlap" in item for item in
                            provenance.validate_promotion_contract(overlapping, "pol-b", "pol-a", [])),
                        "discovery rows cannot validate their own hypothesis")

            generation_question = questions.open_or_update(
                "model_drift", "MODEL DRIFT: generation-fenced promotion",
                item={"run": 60}, subject="generation-fence",
            )["question"]
            generation_spec = dict(
                spec,
                hypothesis="A generation-fenced candidate improves output.",
            )
            generation_registration = provenance.register_hypothesis(
                generation_spec,
                ["history#runs=51-60"],
                "pol-a",
                [generation_question["id"]],
                {generation_question["id"]: generation_question["generation"]},
                {generation_question["id"]: generation_question["last_seen_run"]},
            )
            generation_validation = ["experiment#cohort=61-70"]
            provenance.record_result(
                generation_registration["id"], "supported", generation_validation,
                "holdout", {"effect": 0.03},
            )
            generation_contract = {
                "hypothesis_id": generation_registration["id"],
                "null_hypothesis": generation_spec["null_hypothesis"],
                "falsifier": generation_spec["falsifier"],
                "primary_metric": generation_spec["primary_metric"],
                "evidence_class": "holdout",
                "expected_improvement": generation_spec["expected_improvement"],
                "discovery_evidence": ["history#runs=51-60"],
                "validation_evidence": generation_validation,
                "question_generations": {generation_question["id"]: 1},
            }
            suite.check(
                not provenance.validate_promotion_contract(
                    generation_contract, "pol-generation", "pol-a", []
                ),
                "current generation-bound result may authorize its matching contract",
            )
            questions.set_status(
                generation_question["id"], "answered", "generation one result",
                ["experiment#cohort=61-70"], 60,
                expected_generation=1, expected_status="open", evidence_cutoff_run=60,
            )
            questions.open_or_update(
                "model_drift", "MODEL DRIFT: generation-fenced promotion recurred",
                item={"run": 61}, subject="generation-fence",
            )
            suite.check(
                any("generation advanced" in error for error in
                    provenance.validate_promotion_contract(
                        generation_contract, "pol-generation", "pol-a", []
                    )),
                "a reopened question invalidates its older policy-promotion result",
            )
            policy_events = [
                {"event": "promoted", "policy_id": "pol-a"},
                {"event": "promoted", "policy_id": "pol-b"},
            ]
            suite.check(provenance.policy_oscillation("pol-a", policy_events) is not None,
                        "A to B to A policy oscillation pauses promotion")

            def candidate(value: int) -> dict:
                item = {
                    "schema_version": 1,
                    "status": "candidate",
                    "objective": {"metric": "fixture"},
                    "parameters": {"fixture": value},
                    "owners": {"fixture": {"claims": [], "invariants": ["bounded"]}},
                    "invariants": {"bounded": "fixture"},
                    "rules_fingerprint": "rules-%s" % value,
                    "claim_policy_fingerprint": "claims",
                    "required_claims": [],
                    "audit": {"ok": True, "errors": [], "warnings": []},
                }
                item["policy_id"] = policy._policy_id(item)
                return item

            policy_a, policy_b = candidate(1), candidate(2)
            policy.promote(policy_a)
            suite.raises(ValueError, lambda: policy.promote(policy_b),
                         "changed policy cannot bypass the promotion contract")
            promoted_b = policy.promote(policy_b, promotion_contract=contract)
            suite.check(promoted_b["policy_id"] == policy_b["policy_id"],
                        "policy promotion accepts the validated holdout contract")
            suite.raises(ValueError,
                         lambda: policy.promote(policy_a, promotion_contract=contract),
                         "integrated policy gate rejects A to B to A oscillation")

            peek_spec = probes._registry()["peek_top_rival"]
            suite.check(
                provenance.hypothesis_id(peek_spec) == peek_spec.get("hypothesis_id")
                and "threat" in peek_spec.get("question_classes", []),
                "the rival probe carries matching lineage and routes live threat questions",
                peek_spec,
            )
            history_path = Path(tmp) / "history.ndjson"
            history_path.write_text(
                json.dumps({"run": 1, "rivals": {}, "rival_herds": {}, "rival_coins": {}}) + "\n",
                encoding="utf-8",
            )
            forged_activity = {
                "schema_version": 1, "settled": True, "runs": [1, 999999],
                "trade_decision_runs": [999999], "rival_change_runs": [],
                "decisions_observed": 1, "material_rival_changes": [],
            }
            suite.raises(
                sandbox.ResultValidationError,
                lambda: probes._trusted_adjudication(
                    "activity_replay", Path(tmp), [{
                        "path": "activity_probe.json", "kind": "json", "value": forged_activity,
                    }],
                ),
                "worker-supplied status and future run ids cannot forge adjudication",
            )
            trade_question = questions.open_or_update(
                "activity_novelty_trade", "NOVEL ACTIVITY [trade]: fixture",
                item={"run": 5}, subject="trade-fixture",
            )["question"]
            rival_question = questions.open_or_update(
                "activity_novelty_rival", "NOVEL ACTIVITY [rival]: Bob changed",
                item={"run": 5}, subject="bob",
            )["question"]
            unrelated_rival = questions.open_or_update(
                "activity_novelty_rival", "NOVEL ACTIVITY [rival]: Alice changed",
                item={"run": 5}, subject="alice",
            )["question"]
            for question in (trade_question, rival_question, unrelated_rival):
                questions.set_status(
                    question["id"], "probing", run=5, probe_id="activity_replay",
                    expected_generation=question["generation"], expected_status="open",
                )
            probes._finish_questions(
                [trade_question["id"], rival_question["id"], unrelated_rival["id"]], "activity_replay", 5,
                {
                    "status": "passed", "ts": "2026-08-27T00:00:00Z",
                    "adjudication": {
                        "settled": True, "status": "supported",
                        "question_classes": ["activity_novelty_trade", "activity_novelty_rival"],
                        "coverage": {
                            "activity_novelty_trade": {
                                "settled": False, "status": "inconclusive",
                                "evidence_cutoff_run": None,
                            },
                            "activity_novelty_rival": {
                                "settled": True, "status": "supported",
                                "evidence_cutoff_run": 5, "subjects": ["bob"],
                            },
                        },
                    },
                    "question_bindings": {
                        trade_question["id"]: {"generation": trade_question["generation"]},
                        rival_question["id"]: {"generation": rival_question["generation"]},
                        unrelated_rival["id"]: {"generation": unrelated_rival["generation"]},
                    },
                },
            )
            scoped_questions = {row["id"]: row for row in questions.load_all()}
            suite.check(
                scoped_questions[trade_question["id"]]["status"] == "open"
                and scoped_questions[rival_question["id"]]["status"] == "answered"
                and scoped_questions[unrelated_rival["id"]]["status"] == "open",
                "activity evidence closes only its matching class and subject",
                scoped_questions,
            )
            saved_registry, saved_command = probes._registry, probes._command
            script = Path(tmp) / "linked_probe.py"
            opened = questions.open_or_update(
                "model_drift", "MODEL DRIFT: linked fixture", item={"run": 79},
                subject="linked fixture",
            )["question"]
            linked_hypothesis = dict(
                spec,
                hypothesis="A separately registered fixture measurement improves output.",
            )
            linked_id = provenance.hypothesis_id(linked_hypothesis)
            spec_probe = {
                "hypothesis_id": linked_id,
                "hypothesis": linked_hypothesis["hypothesis"],
                "null_hypothesis": linked_hypothesis["null_hypothesis"],
                "falsifier": linked_hypothesis["falsifier"],
                "primary_metric": linked_hypothesis["primary_metric"],
                "evidence_class": "holdout",
                "command": ["unused"],
                "read_only": True,
                "autonomous": False,
                "budget": {"coins": 0, "calls": 0, "wall_seconds": 10},
                "tools": {},
                "outputs": ["fixture_result.json", "provenance.ndjson"],
                "stop_condition": "fixture",
                "evidence_destination": "state/fixture_result.json and state/provenance.ndjson",
            }
            try:
                probes._registry = lambda: {"linked": dict(spec_probe)}
                probes._command = lambda unused: [sys.executable, str(script)]
                script.write_text("print('no result')\n", encoding="utf-8")
                missing_result = probes.run_probe(
                    "linked", explicit=True, run=80, question_ids=[opened["id"]]
                )
                suite.check(missing_result["status"] == "evidence_missing",
                            "hypothesis-linked probe cannot pass without adjudicating evidence",
                            missing_result)
                script.write_text(
                    "import os, sys\n"
                    "from pathlib import Path\n"
                    "sys.path.insert(0, %r)\n" % str(PROJECT) +
                    "from farm import provenance\n"
                    "result_path=Path(os.environ['FARM_STATE_DIR'])/'fixture_result.json'\n"
                    "result_path.write_text('{\"effect\":0.04}\\n')\n"
                    "provenance.record_result(os.environ['FARM_HYPOTHESIS_ID'], 'supported', "
                    "['worker-supplied-ref-is-not-authoritative'], os.environ['FARM_EVIDENCE_CLASS'], {'effect': 0.04})\n",
                    encoding="utf-8",
                )
                durable_result = probes.run_probe(
                    "linked", explicit=True, run=90, question_ids=[opened["id"]],
                )
                suite.check(
                    durable_result["status"] == "awaiting_adjudication",
                    "worker-proposed causal status cannot become a trusted result",
                    durable_result,
                )
                candidate_result = durable_result.get("candidate_adjudication") or {}
                durable_hash = hashlib.sha256(
                    (Path(tmp) / "fixture_result.json").read_bytes()
                ).hexdigest()
                suite.check(
                    candidate_result.get("validation_evidence")
                    == ["state/fixture_result.json#sha256=" + durable_hash]
                    and candidate_result.get("trusted") is False
                    and provenance.latest_result(linked_id) is None,
                    "parent hashes measurement bytes without granting promotion authority",
                    candidate_result,
                )
                settled = next(row for row in questions.load_all() if row["id"] == opened["id"])
                suite.check(
                    settled["status"] == "open"
                    and settled.get("probe_result_status") == "awaiting_adjudication",
                    "unadjudicated hypothesis evidence leaves its question open", settled,
                )

                unrelated = Path(tmp) / "tool_calls.ndjson"
                compaction.append_json(unrelated, {"probe_id": "background", "event": "tool.call"})
                unlinked_spec = {
                    "hypothesis": "Pure replay settles a stale decision.",
                    "command": ["unused"], "read_only": True, "autonomous": False,
                    "budget": {"coins": 0, "calls": 0, "wall_seconds": 10},
                    "tools": {}, "outputs": [],
                    "stop_condition": "fixture", "evidence_destination": "state/audits.ndjson",
                }
                probes._registry = lambda: {"unlinked": dict(unlinked_spec)}
                script.write_text("print('replay complete')\n", encoding="utf-8")
                stale = questions.open_or_update(
                    "strategy_stale", "STRATEGY STALE: fixture", item={"run": 91},
                    subject="farm",
                )["question"]
                replay = probes.run_probe(
                    "unlinked", explicit=True, run=92, question_ids=[stale["id"]],
                )
                suite.check(
                    replay["status"] == "evidence_missing" and replay["calls"] == 0,
                    "unrelated global telemetry cannot create a probe budget violation", replay,
                )
                stale_after = next(row for row in questions.load_all() if row["id"] == stale["id"])
                suite.check(
                    stale_after["status"] == "open"
                    and stale_after.get("probe_result_status") == "evidence_missing",
                    "process exit without admitted adjudication cannot close a question",
                    stale_after,
                )

                policy_question = questions.open_or_update(
                    "policy_drift", "POLICY DRIFT: fixture", item={"run": 93},
                    subject="semantic_contract",
                )["question"]
                saved_refresh, saved_runtime = claims.refresh, policy.runtime_context
                try:
                    claims.refresh = lambda: {"registry_version": 2, "claims": []}
                    policy.runtime_context = lambda registry=None: {
                        "compatible": False, "errors": ["fixture remains incompatible"],
                    }
                    probes._finish_questions(
                        [policy_question["id"]], "unlinked", 94,
                        {"status": "passed", "ts": "2026-08-27T00:00:00Z"},
                    )
                    unresolved = next(
                        row for row in questions.load_all() if row["id"] == policy_question["id"]
                    )
                    suite.check(
                        unresolved["status"] == "open"
                        and unresolved.get("probe_result_status") is None,
                        "an unbound completion cannot alter unresolved policy drift",
                        unresolved,
                    )

                    policy.runtime_context = lambda registry=None: {
                        "compatible": True, "errors": [], "policy_id": "pol-restored",
                    }
                    probes._finish_questions(
                        [policy_question["id"]], "unlinked", 95,
                        {"status": "passed", "ts": "2026-08-27T00:01:00Z"},
                    )
                    resolved = next(
                        row for row in questions.load_all() if row["id"] == policy_question["id"]
                    )
                    suite.check(
                        resolved["status"] == "open"
                        and resolved.get("probe_result_status") is None,
                        "policy compatibility alone cannot replace admitted deciding evidence",
                        resolved,
                    )
                finally:
                    claims.refresh, policy.runtime_context = saved_refresh, saved_runtime
            finally:
                probes._registry, probes._command = saved_registry, saved_command

            saved_recent, saved_run_probe, saved_registry = (
                probes._recent_events, probes.run_probe, probes._registry,
            )
            called: List[str] = []
            try:
                probes._recent_events = lambda: [{"run": 100, "status": "skipped"}]
                probes._registry = lambda: {
                    "scheduled": {
                        "read_only": True, "autonomous": True,
                        "question_classes": ["strategy_stale"],
                        "subject_patterns": ["farm"],
                    }
                }
                probes.run_probe = lambda probe_id, **kwargs: called.append(probe_id) or {"status": "passed"}
                probes.maybe_run(
                    [{"id": "q-fixture", "class": "strategy_stale", "subject": "farm",
                      "status": "open"}], 101,
                )
            finally:
                probes._recent_events, probes.run_probe, probes._registry = (
                    saved_recent, saved_run_probe, saved_registry,
                )
            suite.check(called == ["scheduled"],
                        "a lock-contention skip does not consume probe cooldown", called)

            saved_recent, saved_run_probe, saved_registry = (
                probes._recent_events, probes.run_probe, probes._registry,
            )
            identity = probes._executor_identity()
            called = []
            try:
                probes._recent_events = lambda: [{
                    "run": 101, "probe_id": "alpha", "status": "failed",
                    "executor_identity": identity, "budget": {"calls": 0},
                }]
                probes._registry = lambda: {
                    name: {
                        "read_only": True, "autonomous": True,
                        "question_classes": ["strategy_stale"],
                        "subject_patterns": ["farm"], "budget": {"calls": 0},
                    }
                    for name in ("alpha", "beta")
                }
                probes.run_probe = lambda probe_id, **kwargs: called.append(probe_id) or {"status": "passed"}
                probes.maybe_run(
                    [{"id": "q-fixture", "class": "strategy_stale", "subject": "farm",
                      "status": "open", "priority": "high", "generation_opened_run": 1}],
                    101,
                )
            finally:
                probes._recent_events, probes.run_probe, probes._registry = (
                    saved_recent, saved_run_probe, saved_registry,
                )
            suite.check(
                called == ["beta"],
                "a same-run failed probe backs off while another eligible probe progresses",
                called,
            )

            saved_recent, saved_run_probe, saved_registry = (
                probes._recent_events, probes.run_probe, probes._registry,
            )
            called = []
            remote_spec = {
                "read_only": True, "autonomous": True,
                "question_classes": ["strategy_stale"],
                "subject_patterns": ["farm"], "budget": {"calls": 1},
            }
            try:
                probes._recent_events = lambda: [{
                    "run": 100, "probe_id": "local", "status": "failed",
                    "executor_identity": identity, "budget": {"calls": 0},
                }]
                probes._registry = lambda: {"remote": remote_spec}
                probes.run_probe = lambda probe_id, **kwargs: called.append(probe_id) or {"status": "passed"}
                probes.maybe_run(
                    [{"id": "q-fixture", "class": "strategy_stale", "subject": "farm",
                      "status": "open"}], 101,
                )
                probes._recent_events = lambda: [{
                    "run": 100, "probe_id": "prior-remote", "status": "failed",
                    "executor_identity": identity, "budget": {"calls": 1},
                }]
                probes.maybe_run(
                    [{"id": "q-fixture", "class": "strategy_stale", "subject": "farm",
                      "status": "open"}], 101,
                )
            finally:
                probes._recent_events, probes.run_probe, probes._registry = (
                    saved_recent, saved_run_probe, saved_registry,
                )
            suite.check(
                called == ["remote"],
                "zero-call failures do not consume remote cadence but remote attempts do",
                called,
            )

            from experiments import crop_timer_probe
            crop_root = Path(tmp) / "crop-zero-yield"
            crop_root.mkdir()
            saved_crop_paths = (
                crop_timer_probe.PROBE, crop_timer_probe.TOOL_CALLS,
                crop_timer_probe.EXPERIMENTS,
            )
            try:
                crop_timer_probe.PROBE = crop_root / "dual_cap_probe.json"
                crop_timer_probe.TOOL_CALLS = crop_root / "tool_calls.ndjson"
                crop_timer_probe.EXPERIMENTS = crop_root / "experiments.ndjson"
                crop_timer_probe.EXPERIMENTS.touch()
                crop_timer_probe.PROBE.write_text(json.dumps({
                    "started_ts": "2026-08-27T00:00:00Z", "baseline_run": 1,
                    "budget": {"calls": 0, "coins": 0},
                    "after": {"plot_counts": {"wheat": 1, "corn": 1, "pumpkin": 1}},
                }))
                crop_timer_probe.TOOL_CALLS.write_text("".join(
                    json.dumps({
                        "event": "end", "tool": "harvest", "run": index + 2,
                        "ts": "2026-08-27T00:%02d:00Z" % minute,
                        "result": "Harvested plot 0 %s" % crop,
                    }) + "\n"
                    for index, (crop, minute) in enumerate(
                        (("wheat", 15), ("corn", 20), ("pumpkin", 30))
                    )
                ))
                zero_yield = crop_timer_probe.analyze()
            finally:
                (crop_timer_probe.PROBE, crop_timer_probe.TOOL_CALLS,
                 crop_timer_probe.EXPERIMENTS) = saved_crop_paths
            suite.check(
                zero_yield.get("status") == "complete"
                and zero_yield.get("all_timers_supported") is False,
                "zero-yield harvests cannot support the crop timer claim",
                zero_yield,
            )
            from experiments import endgame
            capped_race = endgame.analyze({
                "run": 10, "rank": 2, "leader": "Neill", "league": "Gold I",
                "animals": 100, "animal_capacity": 100, "produce": 1_000,
                "coins": 1_000_000, "feed": 1_000_000,
                "rivals": {"John": 10, "Neill": 1_000_000},
                "rival_herds": {"John": 1, "Neill": 100_000},
                "rival_leagues": {"John": "Gold I", "Neill": "Gold I"},
                "projection": {"our_growth_per_min": 0.0, "rival_growth_per_min": 1.0},
            })
            suite.check(
                capped_race.get("rival") == "Neill"
                and max(item["target"] for item in capped_race.get("options") or []) == 100
                and capped_race.get("safe_path") is False,
                "endgame replay targets the actual leader and cannot simulate through capacity",
                capped_race,
            )
            missing_rival = endgame.analyze({
                "run": 11, "rank": 2, "leader": "Missing", "animals": 100,
                "animal_capacity": 100, "produce": 1_000, "coins": 1_000_000,
                "feed": 1_000_000, "rivals": {}, "rival_herds": {}, "projection": {},
            })
            suite.check(
                missing_rival.get("objective_rival_observed") is False
                and missing_rival.get("safe_path") is False,
                "missing objective-rival evidence leaves endgame adjudication inconclusive",
                missing_rival,
            )

            queue = str(Path(tmp) / "workorders.ndjson")
            workorders.submit(
                {
                    "id": "legacy-strategy", "kind": "strategy_hypothesis",
                    "severity": "opportunity", "summary": "Legacy hypothesis",
                    "detail": {
                        "hypothesis": "A fixture intervention improves output.",
                        "falsifier": "The fixture result is flat.",
                        "metric": "fixture output",
                    },
                },
                source="research_agent", intent="Build a bounded fixture probe.", path=queue,
            )
            migration = provenance.reconcile_workorders(queue)
            migrated_order = workorders.current(queue)["legacy-strategy"]
            suite.check(
                migration["migrated"] == 1
                and migration["probe_paths_enriched"] == 1
                and (migrated_order.get("provenance") or {}).get("hypothesis_id")
                and "experiments/legacy_strategy_probe.py" in (migrated_order.get("files") or []),
                "legacy strategy work orders receive lineage and an explicit probe path", migrated_order,
            )

        section("champion versus candidate efficacy")
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "canary.json")
            baseline = [1.0] * 12
            base_record = {
                "revision": "rev-b", "previous": "rev-a", "change_class": "strategy",
                "expected_improvement": 0.01, "efficacy_metric": "per_animal",
                "efficacy_baseline_samples": baseline,
            }
            improved = evaluation.judge(base_record, runs(13, 10, 1.05), store)
            flat = evaluation.judge(base_record, runs(13, 10, 1.0), store)
            reliable = evaluation.judge(dict(base_record, change_class="reliability"),
                                        runs(13, 10, 0.98), store)
            suite.check(improved["accepted"] and improved["status"] == evaluation.IMPROVED,
                        "strategy candidate must demonstrate its declared gain", improved)
            suite.check(not flat["accepted"] and flat["status"] == evaluation.REJECTED,
                        "strategy candidate with no gain is rejected", flat)
            suite.check(reliable["accepted"] and reliable["status"] == evaluation.EQUIVALENT,
                        "reliability release can pass a tight equivalence gate", reliable)
            duplicate_window = runs(23, 10, 1.0)
            for row in duplicate_window:
                row["interval_min"] = 5.0
            duplicate_window[-1].update(produce_per_min=0.0, interval_min=0.2, collected=0)
            weighted_reliable = evaluation.judge(
                dict(base_record, change_class="reliability", baseline_per_animal=1.0),
                duplicate_window,
                store,
            )
            suite.check(
                weighted_reliable["accepted"]
                and weighted_reliable.get("weighting") == "interval_min"
                and weighted_reliable.get("candidate", 0) > 0.99
                and (weighted_reliable.get("unweighted_interval") or {}).get("candidate") == 0.9,
                "a seconds-long duplicate zero cannot fail reliability equivalence",
                weighted_reliable,
            )
            noisy_candidate = runs(33, 10, 0.0)
            for index, row in enumerate(noisy_candidate):
                row["produce_per_min"] = 44.0 if index % 2 else 0.0
                row["interval_min"] = 5.0
            noisy_record = dict(
                base_record,
                change_class="reliability",
                baseline_per_animal=0.235,
                efficacy_baseline_samples=[0.0, 0.4] * 6,
            )
            noisy = evaluation.judge(noisy_record, noisy_candidate, store)
            suite.check(
                not noisy["accepted"] and noisy["status"] == evaluation.INCONCLUSIVE,
                "borderline burst-phase reliability miss stays live without champion promotion",
                noisy,
            )
            clear_loss = evaluation.judge(
                dict(base_record, change_class="reliability", baseline_per_animal=1.0),
                runs(43, 10, 0.70), store,
            )
            suite.check(
                clear_loss["status"] == evaluation.REJECTED,
                "a statistically clear reliability loss still rejects",
                clear_loss,
            )

            bootstrap = evaluation.ensure_champion(store, "rev-a", policy_id="pol-a", run=12)
            repeated = evaluation.ensure_champion(store, "rev-other", policy_id="pol-b", run=13)
            suite.check(
                bootstrap["revision"] == repeated["revision"] == "rev-a"
                and bootstrap["cumulative_ratio"] == 1.0,
                "the trusted rollback target bootstraps the champion exactly once", repeated,
            )
            champion_path = Path(tmp) / "champion.json"
            champion_path.write_text(json.dumps({"cumulative_ratio": 0.96}), encoding="utf-8")
            budget = evaluation.judge(dict(base_record, change_class="reliability"),
                                      runs(13, 10, 0.98), store)
            suite.check(not budget["accepted"] and "cumulative" in budget["reason"],
                        "small per-release losses cannot exceed the cumulative budget", budget)

        section("strategy canary and trusted boundary")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.ndjson"
            store = root / "canary.json"
            audit = root / "canary.ndjson"
            base = runs(1, 12, 1.0)
            write_rows(history, base)
            canary.arm("rev-b", "rev-a", change_class="strategy",
                       hypothesis_id="hyp-test", expected_improvement=0.01,
                       store=str(store), history=str(audit), run_history=str(history))
            write_rows(history, base + runs(13, 10, 1.0))
            no_gain = canary.evaluate(str(store), str(history))
            suite.check(no_gain["status"] == canary.REGRESSED
                        and "does not prove" in no_gain["reason"],
                        "safe but ineffective strategy release is reverted", no_gain)
            canary.resolve(no_gain, project=tmp, store=str(store), history=str(audit))

            write_rows(history, base)
            canary.arm("rev-c", "rev-a", change_class="strategy",
                       hypothesis_id="hyp-test", expected_improvement=0.01,
                       store=str(store), history=str(audit), run_history=str(history))
            write_rows(history, base + runs(13, 10, 1.05))
            gain = canary.evaluate(str(store), str(history))
            suite.check(gain["status"] == canary.HEALTHY
                        and (gain.get("efficacy") or {}).get("accepted"),
                        "strategy release promotes only after independent efficacy", gain)
            canary.resolve(gain, project=tmp, store=str(store), history=str(audit))
            suite.check(
                compaction.compatibility(root).get("revision") == "rev-c",
                "a healthy canary establishes the accepted reader watermark",
                compaction.compatibility(root),
            )

        suite.check(control.is_protected("farm/cycle.py"),
                    "model author cannot rewrite the live strategy cycle")
        suite.check(all(control.is_protected(path) for path in (
            "farm/compaction.py", "farm/evaluation.py", "farm/governance.py",
            "farm/provenance.py", "farm/policy.py"
        )), "model author cannot weaken its storage, efficacy, lineage, or promotion judges")
        supervisor_source = (PROJECT / "run.py").read_text(encoding="utf-8")
        suite.check("compaction_safe = False" in supervisor_source
                    and "if compaction_safe:" in supervisor_source,
                    "source ledgers cannot rotate while rollback may target an old reader")
        suite.check("publish and accept a compaction-capable release first" in supervisor_source
                    and "compaction.compatibility(state)" in supervisor_source,
                    "manual compaction enforces the accepted-reader compatibility watermark")
        suite.check(
            supervisor_source.count('control.project_root() / "release"') >= 2
            and 'state.parent / "release"' not in supervisor_source,
            "working-tree and deployed compaction resolve the canonical release pointer",
        )

    finally:
        os.environ.clear()
        os.environ.update(previous_env)

    print()
    if suite.failures:
        print("SAFETY TEST FAILED: %d of %d checks" % (len(suite.failures), suite.checks))
        for failure in suite.failures:
            print("  - " + failure)
        return 1
    print("SAFETY TEST PASSED: %d checks" % suite.checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
