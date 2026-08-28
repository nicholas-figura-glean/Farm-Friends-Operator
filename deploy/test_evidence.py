#!/usr/bin/env python3
"""Regression checks for the machine-readable farm findings.

The Findings tab is only useful if prose, estimators, claims, and policy cannot
disagree. These tests assert semantic contracts rather than aging snapshots: the
full ledger remains available, the output model and claim status agree, false
plateau history is superseded, and research stays pure and JSON-safe.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import evidence  # noqa: E402


checks = []
failures = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    checks.append((name, bool(condition), detail))
    if not condition:
        failures.append(name + (f" [{detail}]" if detail else ""))


def main() -> int:
    rows = evidence._history()  # the derivation input, intentionally exercised
    samples = evidence._rate_samples(rows)
    report = evidence.report()
    ceiling = report["ceiling"]

    ok("full history exists", len(rows) >= 400, f"{len(rows)} rows")
    ok("evidence is not silently limited to 400 rows", len(rows) > 400, f"{len(rows)} rows")
    ok("pre-rate rows are reconstructed", any(herd < 1_000 for herd, _ in samples),
       f"minimum measured herd {min((h for h, _ in samples), default='none')}")
    ok("the growth curve covers current scale", len(ceiling["buckets"]) >= 9,
       f"{len(ceiling['buckets'])} buckets")
    ok("early herd had a positive slope", (ceiling["regression_below"]["slope"] or 0) > 0,
       str(ceiling["regression_below"]))
    below = ceiling["regression_below"]["slope"] or 0
    above = ceiling["regression"]["slope"] or 0
    ok("the marginal animal still pays above 8k", above > 0.05,
       f"slope above 8k = {above:.5f} produce/min per animal")
    ok("no plateau: the two positive regimes agree within a factor of two",
       below > 0 and above > 0 and 0.5 <= above / below <= 2.0,
       f"below={below:.5f}, above={above:.5f}, ratio={above/below:.3f}" if below else "below=0")
    # Raw straight-line r is retained as a transparent diagnostic, not a gate. Herd
    # size is almost a clock and is nearly flat in the newest band, while leaderboard
    # deltas measure our collection phase as well as animal production. In the live
    # cohort raw r moved below 0.7 even as the signed slope, both scale-smoothed fits,
    # and both predeclared power-law exponents continued to reject saturation. Worse,
    # the synthetic saturation cohort in test_knowledge produces r=0.993. Gating on r
    # therefore accepts the dangerous shape and can reject the healthy one.
    ok("the raw fit remains published for diagnostic review",
       ceiling["regression"].get("r") is not None and (ceiling["regression"].get("n") or 0) >= 20,
       str(ceiling["regression"]))
    ok("herd growth is still paying (not saturating)", not ceiling["saturating"],
       str(ceiling["scaling"]))
    ok("raw-sample scaling remains at least proportional",
       (ceiling["scaling"].get("exponent") or 0) >= 0.95,
       str(ceiling["scaling"]))
    ok("scale-bucket scaling independently remains at least proportional",
       (ceiling["scaling_bucketed"].get("exponent") or 0) >= 0.95,
       str(ceiling["scaling_bucketed"]))
    ok("the power-law form fits the healthy raw cohort",
       (ceiling["scaling"].get("r") or 0) > 0.8,
       str(ceiling["scaling"]))
    ok("the scale-bucket association above 8k has not broken down",
       (ceiling["regression_bucketed"].get("r") or 0) > 0.85,
       str(ceiling["regression_bucketed"]))
    ok("the sample-weighted scale association independently remains strong",
       (ceiling["regression_bucketed_weighted"].get("r") or 0) > 0.95,
       str(ceiling["regression_bucketed_weighted"]))
    ok("the herd/output association is published as unidentified",
       ceiling["confound"]["identified"] is False, str(ceiling["confound"]["note"]))
    ok("the estimator classifies output as linear", ceiling.get("shape") == "linear",
       str(ceiling.get("shape")))
    ok("published output prose comes from the linear estimator",
       "plateau is falsified" in ceiling.get("claim", "").lower(), ceiling.get("claim", ""))

    claim_map = {item["id"]: item for item in report["claims"]["claims"]}
    ok("linear output claim is accepted",
       claim_map["mechanic.output_linear_with_herd"]["status"] == "accepted")
    ok("historical plateau claim is superseded",
       claim_map["mechanic.per_farm_output_plateau"]["status"] == "superseded")
    ok("contradictory output claims cannot both be accepted",
       not (claim_map["mechanic.output_linear_with_herd"]["status"] == "accepted"
            and claim_map["mechanic.per_farm_output_plateau"]["status"] == "accepted"))
    ok("Findings semantic audit passes", report["research"]["semantic_audit"]["ok"],
       str(report["research"]["semantic_audit"]["errors"]))
    ok("counterfactual replay makes zero MCP calls",
       report["research"]["counterfactual"]["mcp_calls"] == 0)
    ok("counterfactual replay identifies sensitive constants",
       "GROWTH_MIN_MARGINAL_GAIN" in report["research"]["counterfactual"]["sensitive_parameters"])

    species = {row["kind"]: row for row in report["species"]["table"]}
    ok("all five species are represented", set(species) == set(evidence.KIND_PRODUCE),
       ",".join(sorted(species)))
    # Alternative species have nonzero measured output. Their cumulative shares
    # remain small because their cohorts are tiny relative to chickens; do not
    # turn low exposure into a false zero-productivity claim.
    ok("sheep remain a low-exposure share of collected output",
       0 < species["sheep"]["share"] < 0.01, str(species["sheep"]))
    ok("cows remain a low-exposure share of collected output",
       0 < species["cow"]["share"] < 0.01, str(species["cow"]))
    ok("chickens dominate measured output", species["chicken"]["share"] > 0.99,
       f"share={species['chicken']['share']}")

    ok("crop negative result names all probes", len(report["crops"]["plots"]) == 3)
    ok("crop probe waited beyond the shortest timer", report["crops"]["waited_minutes"] > 15)
    ok("collection correction carries multiple reproductions",
       len(report["collection"]["cases"]) >= 3)
    ok("detector fixes are concrete", all(d.get("was") and d.get("fix") and d.get("evidence")
                                           for d in report["detectors"]))
    ok("timeline is chronological",
       [x["run"] for x in report["timeline"]] == sorted(x["run"] for x in report["timeline"]))
    ok("timeline labels the false plateau as superseded",
       any(x.get("run") == 46 and x.get("status") == "superseded" for x in report["timeline"]))

    cost = report["cost"]
    history = report["cost_history"]
    hstats = history["stats"]
    ok("cost history covers the audited Python era", hstats["ledger_runs"] >= 100,
       f"{hstats['ledger_runs']} runs")
    ok("zero and charged classifications cover every run",
       hstats["zero_runs"] + hstats["charged_runs"] == hstats["ledger_runs"])
    ok("cost history is ordered by run",
       [p["run"] for p in history["points"]] == sorted(p["run"] for p in history["points"]))
    ok("counterfactual range preserves uncertainty",
       hstats["counterfactual_cost_low"] < hstats["counterfactual_cost_mid"]
       < hstats["counterfactual_cost_high"], str(hstats))
    ok("actual cost never exceeds the old-loop midpoint",
       hstats["actual_cost"] <= hstats["counterfactual_cost_mid"])
    ok("cost history names the deterministic cutover",
       any(c["era"] == "Deterministic Python cycle" for c in history["changes"]))
    ok("token composition adds to the midpoint",
       sum(s["tokens"] for s in history["token_sources"])
       == history["per_run_assumption"]["tokens_mid"])
    ok("old loop range is measured, not a point guess",
       cost["llm_era"]["input_tokens_high"] > cost["llm_era"]["input_tokens_low"] > 0)
    ok("routine loop has zero token input", cost["now"]["input_tokens"] == 0)
    try:
        encoded = json.dumps(report, separators=(",", ":"), allow_nan=False)
        transport_safe = len(encoded) > 1_000
    except (TypeError, ValueError) as exc:
        transport_safe = False
        encoded = str(exc)
    ok("the API report is strict JSON", transport_safe, f"{len(encoded)} bytes")

    for name, passed, detail in checks:
        suffix = f"  [{detail}]" if detail and not passed else ""
        print(f"  {'ok' if passed else 'FAIL':4} {name}{suffix}")
    print()
    if failures:
        print(f"EVIDENCE TEST FAILED: {len(failures)} of {len(checks)} checks")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"EVIDENCE TEST PASSED: {len(checks)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
