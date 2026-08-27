#!/usr/bin/env python3
"""Regression suite for compaction, provenance, efficacy, and anti-oscillation."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, List

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import (  # noqa: E402
    analysis, canary, compaction, control, evaluation, policy, probes,
    provenance, questions, workorders,
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
            first = provenance.register_hypothesis(spec, ["history#runs=1-20"], "pol-a", ["q-1"])
            duplicate = provenance.register_hypothesis(spec, ["history#runs=1-20"], "pol-a", ["q-1"])
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
            saved_registry, saved_command = probes._registry, probes._command
            script = Path(tmp) / "linked_probe.py"
            spec_probe = {
                "hypothesis_id": reopened["id"],
                "hypothesis": spec["hypothesis"],
                "evidence_class": "holdout",
                "command": ["unused"],
                "read_only": True,
                "autonomous": True,
                "budget": {"coins": 0, "calls": 1, "wall_seconds": 10},
                "stop_condition": "fixture",
                "evidence_destination": "state/provenance.ndjson",
            }
            try:
                probes._registry = lambda: {"linked": dict(spec_probe)}
                probes._command = lambda unused: [sys.executable, str(script)]
                script.write_text("print('no result')\n", encoding="utf-8")
                missing_result = probes.run_probe("linked", explicit=True, run=80)
                suite.check(missing_result["status"] == "evidence_missing",
                            "hypothesis-linked probe cannot pass without adjudicating evidence",
                            missing_result)
                script.write_text(
                    "import os, sys\n"
                    "sys.path.insert(0, %r)\n" % str(PROJECT) +
                    "from farm import provenance\n"
                    "provenance.record_result(os.environ['FARM_HYPOTHESIS_ID'], 'supported', "
                    "['experiment#cohort=81-90'], os.environ['FARM_EVIDENCE_CLASS'], {'effect': 0.04})\n",
                    encoding="utf-8",
                )
                opened = questions.open_or_update(
                    "model_drift", "MODEL DRIFT: linked fixture", item={"run": 89},
                    subject="linked fixture",
                )["question"]
                durable_result = probes.run_probe(
                    "linked", explicit=True, run=90, question_ids=[opened["id"]],
                )
                suite.check(durable_result["status"] == "passed",
                            "probe passes after writing a durable hypothesis result", durable_result)
                settled = next(row for row in questions.load_all() if row["id"] == opened["id"])
                suite.check(
                    settled["status"] == "answered"
                    and settled.get("probe_result_status") == "supported",
                    "a durable probe result closes its bound question", settled,
                )

                unrelated = Path(tmp) / "tool_calls.ndjson"
                compaction.append_json(unrelated, {"probe_id": "background", "event": "tool.call"})
                unlinked_spec = {
                    "hypothesis": "Pure replay settles a stale decision.",
                    "command": ["unused"], "read_only": True, "autonomous": True,
                    "budget": {"coins": 0, "calls": 0, "wall_seconds": 10},
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
                    replay["status"] == "passed" and replay["calls"] == 0,
                    "unrelated global telemetry cannot create a probe budget violation", replay,
                )
                stale_after = next(row for row in questions.load_all() if row["id"] == stale["id"])
                suite.check(stale_after["status"] == "answered",
                            "a successful replay closes an unlinked strategy question", stale_after)
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
                    [{"id": "q-fixture", "class": "strategy_stale", "subject": "farm"}], 101,
                )
            finally:
                probes._recent_events, probes.run_probe, probes._registry = (
                    saved_recent, saved_run_probe, saved_registry,
                )
            suite.check(called == ["scheduled"],
                        "a lock-contention skip does not consume probe cooldown", called)

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
            "farm/compaction.py", "farm/evaluation.py", "farm/provenance.py", "farm/policy.py"
        )), "model author cannot weaken its storage, efficacy, lineage, or promotion judges")
        supervisor_source = (PROJECT / "run.py").read_text(encoding="utf-8")
        suite.check("compaction_safe = False" in supervisor_source
                    and "if compaction_safe:" in supervisor_source,
                    "source ledgers cannot rotate while rollback may target an old reader")
        suite.check("publish and accept a compaction-capable release first" in supervisor_source
                    and "compaction.compatibility(state)" in supervisor_source,
                    "manual compaction enforces the accepted-reader compatibility watermark")

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
