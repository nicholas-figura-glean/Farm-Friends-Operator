#!/usr/bin/env python3
"""The measured findings, as data rather than as prose.

This is the dashboard compatibility projection of the epistemic control plane.
Derived metrics come from the full immutable ledger and published conclusions
come from the versioned claim registry; hand-written sibling prose is never an
authority. Historical wrong turns remain visible only with status and scope.

Two kinds of evidence stay separate:

- **derived**: regime-filtered estimators recomputed from `state/history.ndjson`.
- **recorded**: bounded one-off probes with run, scope, freshness, and falsifier.

The cycle imports claims and policy only for identity and periodic sidecar
refresh. Evidence rendering cannot alter a decision or mutate the farm.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from . import analysis, claims, policy, questions, research, rules
except ImportError:  # direct `python3 farm/evidence.py`, useful while designing the tab
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from farm import analysis, claims, policy, questions, research, rules

PROJECT = Path(__file__).resolve().parent.parent
HISTORY = PROJECT / "state" / "history.ndjson"
TOKEN_LEDGER = PROJECT / "state" / "tokens.ndjson"
BEEHIVE_PROBE = PROJECT / "state" / "beehive_probe.json"

# Which produce each species is supposed to yield. Every mapped species now has
# nonzero observations; the table compares composition and recent exposure-
# normalized rates rather than treating low share as zero output.
KIND_PRODUCE = {
    "chicken": "egg",
    "pig": "truffle",
    "beehive": "honey",
    "sheep": "wool",
    "cow": "milk",
}

# Measured over 46 LLM-driven runs on 2026-08-20, before the loop was rewritten in
# Python. The range is real: cost per run depended on how much of `list_farm` the
# model re-read, which grew with the herd.
LLM_ERA = {
    "runs_measured": 46,
    "date": "2026-08-20",
    "input_tokens_low": 150_000,
    "input_tokens_high": 600_000,
    "tool_text_tokens": 62_000,
    "thinking_tokens": 59_000,
    "assistant_turns": 21,
    "cadence_seconds": 300,
    "note": "62k/run of raw tool text and 59k of thinking, re-sent across ~21 turns. "
            "list_farm alone grows ~65 bytes per animal and was read three or four "
            "times per run.",
}

# Public list pricing, the same constants farm/tokens.py bills the exception path
# with, so the counterfactual and the real ledger cannot disagree.
PRICE_PER_MTOK_IN = rules.LLM_INPUT_COST_PER_MTOK
PRICE_PER_MTOK_OUT = rules.LLM_OUTPUT_COST_PER_MTOK


def _history(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Full immutable evidence ledger by default; tails must be explicit."""
    return analysis.history_rows(limit=limit, path=HISTORY)


def _regression(points: List[tuple]) -> Dict[str, Optional[float]]:
    return analysis.linear_regression(points)


def _rate_samples(rows: List[Dict[str, Any]]) -> List[tuple]:
    """Legacy tuple projection of the shared regime-aware estimator."""
    return [
        (sample["herd"], sample["rate"])
        for sample in analysis.rate_samples(rows, healthy_only=True)
    ]


def ceiling(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Compatibility view of the authoritative herd/output claim.

    The key remains `ceiling` because the dashboard already consumes it, but the
    estimator is allowed to falsify that hypothesis. Its statement, status,
    scope, and numbers are generated from one shared model.
    """
    model = analysis.output_model(rows if rows is not None else _history())
    return {
        "buckets": model["buckets"],
        "samples": model["samples"],
        "regression": model["regression"],
        "regression_bucketed": model["regression_bucketed"],
        "regression_bucketed_weighted": model["regression_bucketed_weighted"],
        # The scaling exponent is the statistic that can see saturation; straight-line
        # r cannot (it reads 0.993 on a genuinely saturating cohort). Published so the
        # Findings tab shows the number the growth decision actually rests on.
        "scaling": model["scaling"],
        "scaling_bucketed": model["scaling_bucketed"],
        "saturating": model["saturating"],
        "confound": model["confound"],
        "regression_from": model["threshold"],
        "regression_below": model["regression_below"],
        # Kept for old renderers; the neutral name is authoritative.
        "plateau_median": model["median_above_threshold"],
        "median_above_threshold": model["median_above_threshold"],
        "herd_now": model["herd_now"],
        "output_now": model["output_now"],
        "shape": model["shape"],
        "confidence": model["confidence"],
        "cohort": model["cohort"],
        "claim_id": "mechanic.output_linear_with_herd",
        "claim": model["statement"],
    }


def species(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Collection composition, recent species rates, and the latest bounded probe."""
    model = analysis.species_model(
        rows if rows is not None else _history(),
        kind_produce=KIND_PRODUCE,
    )
    model["low_share_kinds"] = [
        item["kind"] for item in model["table"]
        if item["kind"] != "chicken" and float(item.get("share") or 0.0) < 0.01
    ]
    try:
        probe = json.loads(BEEHIVE_PROBE.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        probe = {}
    result = probe.get("result") if isinstance(probe.get("result"), dict) else {}
    model["probe"] = {
        "status": probe.get("status"),
        "started_ts": probe.get("started_ts"),
        "completed_ts": probe.get("completed_ts"),
        "batch": probe.get("adopted"),
        "beehives_before": probe.get("beehives_before"),
        "beehives_after": probe.get("beehives_after"),
        "windows": result.get("windows_observed"),
        "median_ratio": result.get("median_ratio"),
        "minimum_ratio": result.get("minimum_ratio"),
        "decision": result.get("decision"),
        "supported": result.get("supported"),
        "safety_failures": result.get("safety_failures") or [],
    }
    model["claim_id"] = "strategy.chicken_engine"
    model["claim"] = model.pop("statement")
    return model


def crops() -> Dict[str, Any]:
    """A recorded negative result: the timers never advance.

    Cannot be derived from history, because the finding is that nothing ever
    entered it. `plant()` will happily create unlimited plots, so a coin sink that
    never yields would have scaled badly.
    """
    return {
        "run": 50,
        "waited_minutes": 27,
        "plots": [
            {"crop": "wheat", "reading": "0% grown, about 15 min left"},
            {"crop": "corn", "reading": "0% grown, about 20 min left"},
            {"crop": "pumpkin", "reading": "0% grown, about 30 min left"},
        ],
        "claim_id": "mechanic.crop_timers_stalled",
        "claim": "In the run-50 server regime, all three plots remained at 0% after "
                 "27 minutes. The negative result is scoped and overdue for a bounded re-probe.",
    }


def collection() -> Dict[str, Any]:
    """Why 'units collected' is not the score, with the two runs that proved it."""
    return {
        "claim": "Produce accrues as animals produce, not when we collect - so the "
                 "authoritative signal is the leaderboard produce delta per minute.",
        "cases": [
            {
                "run": 25,
                "text": "gained 41,207 lifetime produce while a collect call returned 572 units",
            },
            {
                "run": 50,
                "text": "recorded collected={} and sold 11,597 eggs in the same run - "
                        "collect_produce refuses while any hunger is present, and the "
                        "produce appears in the barn during feed_animals instead",
            },
            {
                "run": 51,
                "text": "same shape again: collected={}, 7,934 eggs sold",
            },
        ],
        "claim_id": "mechanic.collection_not_score",
        "consequence": "Keep the herd producing and use collection only to bank enough "
                       "coins for feed and growth. Adoption raises score while the measured "
                       "healthy herd/output slope remains positive.",
    }


def detectors() -> Dict[str, Any]:
    """The two detectors that were firing almost every run for non-incidents.

    Worth its own panel because the fix removed most of the LLM spend, and because
    both were wrong in the same way: a ratio read without the context that makes it
    mean something.
    """
    return [
        {
            "name": "throughput",
            "was": "all-species collection units divided by chicken count, with either side of a fixed band treated as failure",
            "why_wrong": "mixed herds made the denominator invalid, and above-band output is positive evidence rather than an operational fault",
            "fix": "divide by all producing animals; suppress a low reading when the barn is drained or score is healthy, and route high output only to periodic model calibration",
            "evidence": "runs 27-29 collected 62-760 units but ended with only 666-3,303 "
                        "ready - the herd was drained, not broken",
        },
        {
            "name": "transport retries",
            "was": "any retry rate above a percentage threshold",
            "why_wrong": "one retry in a 35-call run is 2.9% and reads as an incident",
            "fix": "a call-volume floor, with rules.transport_trouble as the single "
                   "definition shared by the detector and the cycle's rate recovery",
            "evidence": "it also convinced the cycle the previous run was unclean, which "
                        "pinned the rate limiter at its 0.5/s floor for runs on end",
        },
        {
            "name": "production stall",
            "was": "one low produce/min window",
            "why_wrong": "leaderboard score now arrives in multi-run bursts; healthy history contains five adjacent zero windows over 26 minutes",
            "fix": "judge cumulative lifetime-score endpoints across a shared 30-35 minute wall-clock window, with hunger remaining immediate",
            "evidence": "runs 2310-2584 have no sub-floor 35-minute healthy window, while the real run-708 outage is detected after about 37 minutes",
        },
    ]


def timeline() -> List[Dict[str, Any]]:
    """Chronology preserves wrong turns, but labels their current status."""
    return [
        {"run": 2, "kind": "correction", "status": "accepted",
         "title": "Production is per-animal, not a global tick",
         "text": "980 units appeared 2.5 minutes after an empty collection."},
        {"run": 25, "kind": "correction", "status": "accepted",
         "title": "Collected units are not the score",
         "text": "41,207 lifetime produce arrived while collection returned 572 units."},
        {"run": 29, "kind": "detector", "status": "accepted",
         "title": "Collection throughput is not score throughput",
         "text": "Inventory collection is cadence- and transport-limited; the leaderboard is authoritative."},
        {"run": 46, "kind": "policy", "status": "superseded",
         "title": "A false plateau froze growth",
         "text": "A short, mixed-regime collection proxy was encoded as a permanent per-farm cap."},
        {"run": 50, "kind": "experiment", "status": "partially_supported",
         "title": "Chickens dominate the observed collection mix",
         "text": "Alternative species were negligible, but this did not prove a per-farm score ceiling."},
        {"run": 50, "kind": "experiment", "status": "overdue",
         "title": "Crop timers stalled in one server regime",
         "text": "Three crop types remained at 0% after 27 minutes; the negative result needs revalidation."},
        {"run": 291, "kind": "postmortem", "status": "accepted",
         "title": "A standing decision can fail while operations look healthy",
         "text": "The growth gate froze the herd for 246 runs and no detector questioned the strategy."},
        {"run": 315, "kind": "correction", "status": "accepted",
         "title": "Healthy output remained responsive to herd growth",
         "text": "The growth gate was reopened with a nonzero maintenance floor and lower marginal threshold."},
        {"run": 377, "kind": "correction", "status": "accepted",
         "title": "Full-history score data falsified the plateau",
         "text": "Output scaled approximately linearly through tens of thousands of animals."},
        {"run": 416, "kind": "result", "status": "accepted",
         "title": "The scaled strategy retook first place",
         "text": "The farm reached rank 1 with more than 100,000 animals after restoring growth."},
    ]


def _token_rows(limit: int = 5000) -> List[Dict[str, Any]]:
    """Read the full cost ledger with the same corrupt-tail tolerance as history."""
    try:
        lines = TOKEN_LEDGER.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def cost_history() -> Dict[str, Any]:
    """Actual Python-era ledger against the measured LLM-era counterfactual.

    The ledger began with deterministic execution, so it would be dishonest to
    draw the old model spend as actual ledger rows. Instead every Python run has
    two series:

    * actual: exactly what state/tokens.ndjson booked;
    * counterfactual: what that same number of cycles would have cost at the
      measured old-loop low/mid/high token load.

    This makes the visual claim strong without manufacturing historical precision
    that does not exist. If a future alert wakes a model, the green actual line
    will move automatically.
    """
    rows = _token_rows()
    per_run: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        run = row.get("run")
        if run is None:
            continue
        try:
            number = int(run)
        except (TypeError, ValueError):
            continue
        bucket = per_run.setdefault(
            number,
            {
                "run": number,
                "ts": row.get("ts"),
                "tokens": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
                "healed": 0,
                "escalations": 0,
                "kinds": [],
            },
        )
        bucket["ts"] = row.get("ts") or bucket["ts"]
        bucket["tokens"] += int(row.get("tokens") or 0)
        bucket["tokens_in"] += int(row.get("tokens_in") or 0)
        bucket["tokens_out"] += int(row.get("tokens_out") or 0)
        bucket["cost_usd"] += float(row.get("cost_usd") or 0.0)
        bucket["healed"] += int(row.get("healed") or 0)
        bucket["escalations"] += 1 if row.get("escalated") else 0
        if row.get("kind"):
            bucket["kinds"].append(str(row["kind"]))

    input_low = int(LLM_ERA["input_tokens_low"])
    input_high = int(LLM_ERA["input_tokens_high"])
    input_mid = round((input_low + input_high) / 2)
    output = int(LLM_ERA["thinking_tokens"])
    token_low, token_mid, token_high = input_low + output, input_mid + output, input_high + output
    cost_low = input_low / 1_000_000 * PRICE_PER_MTOK_IN + output / 1_000_000 * PRICE_PER_MTOK_OUT
    cost_mid = input_mid / 1_000_000 * PRICE_PER_MTOK_IN + output / 1_000_000 * PRICE_PER_MTOK_OUT
    cost_high = input_high / 1_000_000 * PRICE_PER_MTOK_IN + output / 1_000_000 * PRICE_PER_MTOK_OUT

    points: List[Dict[str, Any]] = []
    actual_tokens = 0
    actual_cost = 0.0
    healed = 0
    escalations = 0
    for index, run in enumerate(sorted(per_run), start=1):
        row = per_run[run]
        actual_tokens += int(row["tokens"])
        actual_cost += float(row["cost_usd"])
        healed += int(row["healed"])
        escalations += int(row["escalations"])
        points.append(
            {
                "run": run,
                "ts": row["ts"],
                "actual_tokens": int(row["tokens"]),
                "actual_cost": round(float(row["cost_usd"]), 6),
                "healed": int(row["healed"]),
                "escalations": int(row["escalations"]),
                "kinds": sorted(set(row["kinds"])),
                "cumulative_actual_tokens": actual_tokens,
                "cumulative_actual_cost": round(actual_cost, 6),
                "cumulative_healed": healed,
                "cumulative_escalations": escalations,
                "counterfactual_tokens_low": token_low * index,
                "counterfactual_tokens_mid": token_mid * index,
                "counterfactual_tokens_high": token_high * index,
                "counterfactual_cost_low": round(cost_low * index, 3),
                "counterfactual_cost_mid": round(cost_mid * index, 3),
                "counterfactual_cost_high": round(cost_high * index, 3),
            }
        )

    count = len(points)
    zero_runs = sum(1 for row in per_run.values() if not row["tokens"] and not row["cost_usd"])
    charged_runs = count - zero_runs
    month_runs = round(86_400 / int(LLM_ERA["cadence_seconds"]) * 30)
    midpoint_input_other = max(0, input_mid - int(LLM_ERA["tool_text_tokens"]))
    return {
        "points": points,
        "stats": {
            "ledger_runs": count,
            "first_run": points[0]["run"] if points else None,
            "last_run": points[-1]["run"] if points else None,
            "zero_runs": zero_runs,
            "charged_runs": charged_runs,
            "actual_tokens": actual_tokens,
            "actual_cost": round(actual_cost, 6),
            "healed": healed,
            "escalations": escalations,
            "healing_cost_avoided": round(healed * rules.escalation_cost(200)[2], 6),
            "counterfactual_tokens_mid": token_mid * count,
            "counterfactual_cost_low": round(cost_low * count, 2),
            "counterfactual_cost_mid": round(cost_mid * count, 2),
            "counterfactual_cost_high": round(cost_high * count, 2),
            "reduction_pct": round((1 - actual_cost / (cost_mid * count)) * 100, 3)
                             if count and cost_mid else None,
            "old_monthly_cost_low": round(cost_low * month_runs),
            "old_monthly_cost_mid": round(cost_mid * month_runs),
            "old_monthly_cost_high": round(cost_high * month_runs),
        },
        "per_run_assumption": {
            "input_tokens_low": input_low,
            "input_tokens_mid": input_mid,
            "input_tokens_high": input_high,
            "thinking_output_tokens": output,
            "tokens_low": token_low,
            "tokens_mid": token_mid,
            "tokens_high": token_high,
            "cost_low": round(cost_low, 3),
            "cost_mid": round(cost_mid, 3),
            "cost_high": round(cost_high, 3),
            "source": "46 measured LLM-driven runs on 2026-08-20",
        },
        "token_sources": [
            {"name": "repeated prompt + context", "tokens": midpoint_input_other,
             "note": "farm state and instructions re-sent across ~21 turns"},
            {"name": "raw tool text", "tokens": int(LLM_ERA["tool_text_tokens"]),
             "note": "including list_farm, which grows with every animal"},
            {"name": "thinking / output", "tokens": output,
             "note": "reasoning over thresholds now encoded directly in rules.py"},
        ],
        "changes": [
            {
                "era": "LLM-driven execution",
                "when": "2026-08-20 · 46 measured runs",
                "icon": "🧠",
                "kind": "before",
                "run": points[0]["run"] if points else None,
                "change": "A model re-read the farm, reasoned over the same thresholds, and drove every tool call every five minutes.",
                "impact": f"{input_low // 1000}k-{input_high // 1000}k billed input tokens per cycle; about ${cost_low:.2f}-${cost_high:.2f} including measured thinking/output.",
                "code": "prompt + repeated list_farm",
            },
            {
                "era": "Deterministic Python cycle",
                "when": "Python cutover · ledger begins at run 32",
                "icon": "🐍",
                "kind": "cutover",
                "run": 32,
                "change": "MCP transport, strict parsing, prices, reserves, budgets and the full action plan moved into small testable Python modules.",
                "impact": "Routine execution dropped to an explicit 0 tokens per cycle; cadence improved from 300s to 180s without increasing model spend.",
                "code": "mcp.py → parse.py → rules.py → cycle.py",
            },
            {
                "era": "Exception-only intelligence",
                "when": "watch.py + compact --alerts",
                "icon": "🔎",
                "kind": "python",
                "run": 32,
                "change": "The model stopped supervising normal runs. Deterministic detectors now decide whether anything deserves attention.",
                "impact": "The full farm state never enters a prompt during healthy operation; only a compact surviving alert can wake a model.",
                "code": "watch.py → journal.py → --alerts",
            },
            {
                "era": "Self-healing supervisor",
                "when": "first measured remedies · runs 32-33",
                "icon": "🛠️",
                "kind": "python",
                "run": 33,
                "change": "Transport, backpressure and feed-reserve failures gained bounded Python remedies before escalation.",
                "impact": f"{healed} alerts healed in the current ledger with {escalations} model wake-ups.",
                "code": "heal.py + 60s supervisor",
            },
            {
                "era": "False-plateau lesson",
                "when": "run 46 · superseded",
                "icon": "↩️",
                "kind": "before",
                "run": 46,
                "change": "An early mixed-regime sample incorrectly stopped adoption at a 10% marginal threshold.",
                "impact": "Later regime-filtered evidence disproved the plateau; the incident now documents why stale metrics cannot silently become permanent strategy.",
                "code": "POSTMORTEM-run377.md → growth.py",
            },
            {
                "era": "Dual-cap strategy",
                "when": "runs 1186-1404 · accepted canary",
                "icon": "🐝",
                "kind": "proof",
                "run": 1404,
                "change": "Animal and plot caps received separate denominators: chicken for below-cap growth, beehive for scarce-slot replacement, and food crops disabled for league score.",
                "impact": "29 capped holdout windows measured a 1.243x median beehive/chicken slot ratio; a 5,000-wheat intervention left exactly zero lifetime-score residual.",
                "code": "dual_cap_audit.py → strategy_policy.py → strategy.py",
            },
            {
                "era": "Auditable cost, not a claim",
                "when": f"runs {points[0]['run'] if points else '—'}-{points[-1]['run'] if points else '—'}",
                "icon": "🧾",
                "kind": "proof",
                "run": points[-1]["run"] if points else None,
                "change": "Every deterministic cycle and every remedy writes a ledger row, including zeros.",
                "impact": f"{zero_runs}/{count} Python-era runs at $0; any future escalation will move the actual line automatically.",
                "code": "tokens.py → state/tokens.ndjson",
            },
        ],
        "disclosure": "The green actual series is the ledger. The amber counterfactual is a range derived from "
                      "46 measured pre-Python runs, not reconstructed invoices. Thinking tokens are priced at "
                      "the configured output rate; billed input uses the measured 150k-600k range.",
    }


def cost_model(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Inputs for the counterfactual: what the old loop would cost at this cadence.

    The front end multiplies these out against a runs-per-day slider, because the
    interesting number is not one run's price - it is what a 5-minute cadence
    compounds to over a month, which is the whole reason the strategy moved into
    `rules.py`.
    """
    rows = rows if rows is not None else _history()
    return {
        "llm_era": LLM_ERA,
        "now": {
            "cadence_seconds": rules.CYCLE_INTERVAL_SECONDS,
            "input_tokens": 0,
            "runs_recorded": len(rows),
            "note": "Every cycle writes an explicit zero to state/tokens.ndjson. The only "
                    "rows with a cost are LLM wake-ups, booked when --alerts prints.",
        },
        "price_per_mtok_in": PRICE_PER_MTOK_IN,
        "price_per_mtok_out": PRICE_PER_MTOK_OUT,
        "claim": "Routine execution is deterministic and zero-token. Bounded research and "
                 "claim revision occur off the mutation path, with explicit policy promotion.",
    }


def report() -> Dict[str, Any]:
    """Everything, for one cached fetch by the dashboard.

    Served from its own endpoint rather than folded into /api/state: the state poll
    runs every 2 seconds and none of this changes at that rate.
    """
    rows = _history()
    persisted_registry = claims.load()
    registry = claims.refresh(rows, persist=False)
    semantic = research.semantic_audit(rows, registry=registry)
    sweep = research.counterfactual_view(rows)
    history_cost = cost_history()
    cost = cost_model(rows)
    cost["now"]["estimated_exception_cost"] = (history_cost.get("stats") or {}).get("actual_cost", 0.0)
    return {
        "ceiling": ceiling(rows),
        "species": species(rows),
        "crops": crops(),
        "collection": collection(),
        "detectors": detectors(),
        "timeline": timeline(),
        "cost": cost,
        "cost_history": history_cost,
        "claims": registry,
        "persisted_claims": persisted_registry,
        "questions": questions.summary(),
        "policy": {
            "runtime": policy.runtime_context(registry),
            "promoted": policy.load(),
        },
        "research": {
            "semantic_audit": semantic,
            "counterfactual": sweep,
            "model_drift": research.model_drift(rows),
        },
    }


if __name__ == "__main__":
    print(json.dumps(report(), indent=1, sort_keys=True))
