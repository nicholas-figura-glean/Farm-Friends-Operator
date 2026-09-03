#!/usr/bin/env python3
"""Regression gate for dual-cap strategy learning and execution."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, List

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "experiments"))

from farm import canary, claims, mechanics, parse, policy, rules, strategy  # noqa: E402
import crop_score_probe  # noqa: E402
import dual_cap_audit  # noqa: E402
import registry  # noqa: E402


class Suite:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: List[str] = []

    def check(self, condition: Any, label: str, detail: Any = "") -> None:
        self.checks += 1
        ok = bool(condition)
        print("  %-4s %s%s" % ("ok" if ok else "FAIL", label,
                                " [%s]" % detail if detail and not ok else ""))
        if not ok:
            self.failures.append(label + (" [%s]" % detail if detail else ""))


def animal() -> parse.Animal:
    return parse.Animal(1, "Henrietta", "chicken", "delighted", 0, 100)


def main() -> int:
    suite = Suite()

    print("== protected field parsing")
    text = (
        "🌾 Nick's Farm  🪙 100000 coins  💠 Platinum I (level 12)\n"
        "   Lifetime produce 1000 · animals 16874/16875 · plots 5067/33750 · next level at 2000 produce\n\n"
        "Animals (16874 total — summarising by kind):\n  🐔 chicken: 16874\n"
        "  A few of them up close:\n"
        "    🐔 Henrietta the chicken (#1) is delighted. hunger 0/100, happiness 100/100\n"
        "Fields (5067 plots — summarising by kind):\n"
        "  🌽 corn: 1 planted, 0 ready to harvest\n"
        "  🎃 pumpkin: 1 planted, 1 ready to harvest\n"
        "  🌾 wheat: 5001 planted, 5000 ready to harvest\n"
        "  🌼 wildflowers: 64 planted, 64 in bloom\n"
        "Barn inventory: 🌱 feed x100000\n"
    )
    farm = parse.parse_farm(text)
    suite.check(farm.counts_by_crop == {"corn": 1, "pumpkin": 1, "wheat": 5001, "wildflowers": 64},
                "summary parser retains exact crop counts", farm.counts_by_crop)
    suite.check(farm.food_crop_count == 5003, "food crop count excludes permanent flowers", farm.food_crop_count)
    by_crop = {item.crop: item for item in farm.plots}
    suite.check(not by_crop["corn"].harvestable,
                "zero ready is not mistaken for harvestable", by_crop["corn"])
    suite.check(by_crop["pumpkin"].harvestable and by_crop["wheat"].harvestable,
                "positive ready counts are harvestable")

    print("\n== literal strategy policy")
    loaded = strategy.load()
    suite.check(not loaded["errors"], "shipped strategy policy validates", loaded["errors"])
    suite.check(strategy.animal_kind(1_000, 16_875, {"wildflowers": 64}, loaded) == "chicken",
                "capital-constrained growth remains chicken")
    suite.check(strategy.animal_kind(16_874, 16_875, {"wildflowers": 64}, loaded) == "beehive",
                "near-cap natural-loss replacement becomes beehive")
    suite.check(strategy.animal_kind(16_874, 16_875, {"wildflowers": 2}, loaded) == "chicken",
                "beehive promotion fails closed without the measured flower bonus")
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "executed"
        malicious = Path(tmp) / "strategy.py"
        malicious.write_text(
            "open(%r, 'w').write('bad')\nSTRATEGY_POLICY = {}\n" % str(marker), encoding="utf-8"
        )
        rejected = strategy.load(malicious, {"tools": {}}, verify_lineage=False)
        suite.check(rejected["errors"] and not marker.exists(),
                    "editable strategy data cannot execute Python", rejected["errors"])

    print("\n== cap-regime audit")
    rows = [
        {"run": 1, "animals": 100_000, "collected": {}, "by_kind": {},
         "plot_counts": {"wildflowers": 8}},
        {"run": 2, "animals": 10_000, "collected": {}, "by_kind": {},
         "plot_counts": {"wildflowers": 8}},
    ]
    for run in range(3, 8):
        rows.append({
            "run": run,
            "animals": 9_500,
            "interval_min": 5.0,
            "plot_counts": {"wildflowers": 8},
            "by_kind": {"beehive": 100, "chicken": 9_400},
            "collected": {"honey": 125, "egg": 9_400},
        })
    fixture_farm = parse.Farm(
        coins=100_000, animals=[animal()], animal_total=9_500,
        animal_counts={"chicken": 9_400, "beehive": 100},
        plots=[parse.Plot(0, "wildflowers", "8 planted, 8 in bloom", count=8)],
        plot_total=8, capacity=10_000, plot_capacity=20_000,
    )
    audit = dual_cap_audit.analyze(rows, fixture_farm)
    suite.check(audit["animal_regime"]["regime_started_run"] == 2,
                "audit explicitly detects the herd-collapse regime boundary", audit["animal_regime"])
    suite.check(audit["animal_regime"]["supported"]
                and audit["decision"]["capped_replacement_kind"] == "beehive",
                "five capped same-window samples support slot-efficient replacement", audit["animal_regime"])

    conditioned = list(rows)
    conditioned.extend([
        {"run": 8, "animals": 9_500, "interval_min": 5.0,
         "plot_counts": {}, "by_kind": {"beehive": 100, "chicken": 9_400},
         "collected": {"honey": 50, "egg": 9_400}},
        {"run": 9, "animals": 9_500, "interval_min": 5.0,
         "plot_counts": {"wildflowers": 8}, "by_kind": {"beehive": 100, "chicken": 9_400},
         "collected": {"honey": 50, "egg": 9_400}},
        {"run": 10, "animals": 9_500, "interval_min": 5.0,
         "plot_counts": {"wildflowers": 8}, "by_kind": {"beehive": 100, "chicken": 9_400},
         "collected": {"honey": 50, "egg": 9_400}},
        {"run": 11, "animals": 9_500, "interval_min": 5.0,
         "plot_counts": {"wildflowers": 8}, "by_kind": {"beehive": 100, "chicken": 9_400},
         "collected": {"honey": 125, "egg": 9_400}},
        {"run": 12, "animals": 9_500, "interval_min": 5.0,
         "plot_counts": {"wildflowers": 8}, "by_kind": {"beehive": 200, "chicken": 9_300},
         "collected": {"honey": 125, "egg": 9_300}},
    ])
    conditioned_audit = dual_cap_audit.analyze(conditioned, fixture_farm)
    conditioned_samples = {
        item["run"]: item for item in conditioned_audit["cohort"]["samples"]
    }
    suite.check(not {8, 9, 10}.intersection(conditioned_samples)
                and {11, 12}.issubset(conditioned_samples),
                "pre-bloom flower rows cannot falsify the bonus-scoped cohort",
                sorted(conditioned_samples))
    suite.check(conditioned_samples[12]["beehives"] == 100
                and conditioned_samples[12]["reported_beehives"] == 200
                and conditioned_samples[12]["ratio"] > 1.1,
                "collection denominators exclude same-cycle species additions",
                conditioned_samples[12])

    print("\n== runtime planning")
    near_cap = rules.expansion_plan(
        100_000, 16_870, 1_000_000, 0, cap=400,
        animal_capacity=16_875, crop_counts={"wildflowers": 64},
    )
    growth = rules.expansion_plan(
        100_000, 1_000, 1_000_000, 0, cap=400,
        animal_capacity=16_875, crop_counts={"wildflowers": 64},
    )
    suite.check(near_cap["kind"] == "beehive" and near_cap["adopt"] == 5,
                "near-cap plan prices and selects beehive replacements", near_cap)
    suite.check(growth["kind"] == "chicken" and growth["adopt"] == 400,
                "below-cap plan retains fast chicken growth", growth)

    barn_fire = parse.Farm(
        coins=2_000_000, animals=[animal()], animal_total=7_000,
        animal_counts={"chicken": 7_000}, capacity=10_000,
        plots=[parse.Plot(0, "wildflowers", "8 planted, 8 in bloom", count=8)],
        plot_total=8, plot_capacity=20_000, inventory={"feed": 1_000_000},
        crisis=parse.Crisis("barn_fire", "BARN FIRE", "12:00", "resolve_crisis", 0.45),
    )
    mechanic_state = mechanics.load_policies()
    decision = mechanics.next_decision(
        barn_fire, mechanics.active_tools(mechanic_state), loaded=mechanic_state
    )["decision"]
    suite.check(decision and decision["tool"] == "resolve_crisis",
                "barn fire is treated as direct animal destruction, not inventory-only", decision)

    print("\n== claims and probes")
    registry_claims = claims.build()
    by_id = {item["id"]: item for item in registry_claims["claims"]}
    suite.check(by_id["strategy.chicken_engine"]["status"] == "accepted"
                and by_id["strategy.chicken_engine"]["decision"] == {"growth_kind": "chicken"},
                "chicken claim is scoped to capital-efficient growth", by_id["strategy.chicken_engine"])
    capped_claim = by_id["strategy.capped_slot_efficiency"]
    capped_samples = (capped_claim.get("estimator") or {}).get("cohort", {}).get("samples") or []
    suite.check(capped_claim["status"] == "accepted"
                and capped_samples
                and capped_claim["last_validated_run"] == capped_samples[-1]["run"]
                and capped_claim["value"]["minimum_beehive_vs_chicken"] >= 1.0
                and capped_claim["scope"]["minimum_flower_qualification_rows"] == 3,
                "capped replacement claim uses the current bloom-qualified exposure cohort",
                {"last_validated_run": capped_claim.get("last_validated_run"),
                 "samples": len(capped_samples), "minimum": capped_claim.get("value", {}).get("minimum_beehive_vs_chicken")})
    suite.check(by_id["mechanic.crop_timers_stalled"]["status"] == "superseded"
                and by_id["mechanic.crop_timers_active"]["status"] == "superseded"
                and by_id["mechanic.crop_timers_delayed"]["status"] == "accepted",
                "current delayed-positive timer intervention supersedes both stale and exact-timer claims")
    crop_value = by_id["strategy.food_crop_score"]["value"]
    suite.check(by_id["strategy.food_crop_score"]["status"] == "accepted"
                and crop_value["crop_score_residual"] < crop_value["minimum_supported_residual"]
                and by_id["strategy.food_crop_score"]["decision"]["food_crop_kind"] is None,
                "scaled wheat holdout disables crops for the league-score objective",
                by_id["strategy.food_crop_score"])
    suite.check("dual_cap_audit" in registry.PROBES
                and registry.PROBES["dual_cap_audit"]["read_only"]
                and registry.PROBES["dual_cap_audit"]["autonomous"],
                "dual-cap audit is registered for autonomous read-only refresh")
    dual_cap_route = registry.PROBES["dual_cap_audit"]
    suite.check("policy_drift" in dual_cap_route["question_classes"]
                and "semantic_contract" in dual_cap_route["subject_patterns"]
                and not {"crop", "plot"}.intersection(dual_cap_route["subject_patterns"]),
                "dual-cap reconciliation cannot falsely settle generic crop freshness",
                dual_cap_route)
    suite.check("crop_timer_revalidation" in registry.PROBES
                and not registry.PROBES["crop_timer_revalidation"]["autonomous"],
                "mutating crop timer probe remains explicit and bounded")
    suite.check("crop_score_holdout" in registry.PROBES,
                "scaled crop score attribution has its own bounded probe")
    with tempfile.TemporaryDirectory() as tmp:
        old_paths = (crop_score_probe.PROBE, crop_score_probe.TOOL_CALLS, crop_score_probe.EXPERIMENTS)
        try:
            crop_score_probe.PROBE = Path(tmp) / "probe.json"
            crop_score_probe.TOOL_CALLS = Path(tmp) / "calls.ndjson"
            crop_score_probe.EXPERIMENTS = Path(tmp) / "experiments.ndjson"
            crop_score_probe.EXPERIMENTS.write_text("", encoding="utf-8")
            crop_score_probe.PROBE.write_text(json.dumps({
                "started_ts": "2026-08-29T16:00:00Z", "baseline_run": 1,
                "planned": {"expected_yield": 15, "before": {"lifetime_produce": 100}},
                "falsifier": "fixture",
            }), encoding="utf-8")
            fixture_calls = [
                {"ts": "2026-08-29T16:10:00Z", "event": "end", "tool": "collect_produce",
                 "run": 2, "result": "Collected:\nTotal: 🥚 40 egg"},
                {"ts": "2026-08-29T16:10:01Z", "event": "end", "tool": "list_farm",
                 "run": 2, "result": "Lifetime produce 140 · animals 10/10 · plots 5/20"},
                {"ts": "2026-08-29T16:10:02Z", "event": "end", "tool": "harvest",
                 "run": 2, "result": "🚜 Harvested 🌾 15 wheat. Your fields are clear."},
            ]
            crop_score_probe.TOOL_CALLS.write_text(
                "".join(json.dumps(row) + "\n" for row in fixture_calls), encoding="utf-8"
            )
            crop_result = crop_score_probe.analyze()
        finally:
            crop_score_probe.PROBE, crop_score_probe.TOOL_CALLS, crop_score_probe.EXPERIMENTS = old_paths
        suite.check(crop_result["crop_score_residual"] == 0 and not crop_result["supported"],
                    "crop holdout subtracts authoritative animal collection instead of claiming it as crop score",
                    crop_result)
    compiled = policy.compile_snapshot(registry_claims)
    suite.check(compiled["audit"]["ok"], "dual-cap policy compiles with claim ownership", compiled["audit"])
    suite.check(compiled["parameters"]["capped_replacement_kind"] == "beehive",
                "compiled policy exposes the active capped replacement")

    print("\n== strategy canary")
    with tempfile.TemporaryDirectory() as tmp:
        history = Path(tmp) / "history.ndjson"
        store = Path(tmp) / "canary.json"
        audit_path = Path(tmp) / "canary.ndjson"
        baseline = [
            {"run": run, "animals": 16_875, "animal_capacity": 16_875,
             "by_kind": {"chicken": 16_875}, "produce_per_min": 2_700.0,
             "interval_min": 5.0, "collected": 13_500}
            for run in range(1, 13)
        ]
        history.write_text("".join(json.dumps(row) + "\n" for row in baseline), encoding="utf-8")
        armed = canary.arm(
            "rev-slots", "rev-old", change_class="strategy",
            expected_improvement=0.10, strategy_intent="capped_replacement",
            store=str(store), history=str(audit_path), run_history=str(history),
        )
        candidate = [
            {"run": 13, "animals": 16_875, "animal_capacity": 16_875,
             "by_kind": {"chicken": 16_874, "beehive": 1},
             "produce_per_min": 2_700.0, "interval_min": 5.0, "collected": 13_500,
             "strategy_policy_fingerprint": armed["strategy_policy_fingerprint"],
             "strategy_policy_errors": []},
            {"run": 14, "animals": 16_875, "animal_capacity": 16_875,
             "by_kind": {"chicken": 16_873, "beehive": 2},
             "produce_per_min": 2_701.0, "interval_min": 5.0, "collected": 13_505,
             "strategy_policy_fingerprint": armed["strategy_policy_fingerprint"],
             "strategy_policy_errors": []},
            {"run": 15, "animals": 16_875, "animal_capacity": 16_875,
             "by_kind": {"chicken": 16_873, "beehive": 2},
             "produce_per_min": 2_701.0, "interval_min": 5.0, "collected": 13_505,
             "strategy_policy_fingerprint": armed["strategy_policy_fingerprint"],
             "strategy_policy_errors": []},
        ]
        history.write_text(
            "".join(json.dumps(row) + "\n" for row in baseline + candidate), encoding="utf-8"
        )
        verdict = canary.evaluate(str(store), str(history))
        suite.check(verdict["status"] == canary.HEALTHY
                    and (verdict.get("efficacy") or {}).get("metric") == "capped_animal_slot_output",
                    "live beehive vacancy replacement verifies the supported strategy directly", verdict)

    print()
    if suite.failures:
        print("STRATEGY TEST FAILED: %d of %d checks" % (len(suite.failures), suite.checks))
        for failure in suite.failures:
            print("  - " + failure)
        return 1
    print("STRATEGY TEST PASSED: %d checks" % suite.checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
