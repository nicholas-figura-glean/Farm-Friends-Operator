"""Pure replay, semantic audits, model drift, and strategy decision bundles."""

from __future__ import annotations

import fcntl
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import analysis, claims, policy, questions, rules

SCHEMA_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else Path(__file__).resolve().parent.parent / "state"


def _audit_path() -> Path:
    return Path(os.environ.get("FARM_AUDIT_LOG", str(_state_dir() / "audits.ndjson")))


def _append_audit(row: Dict[str, Any]) -> None:
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(row, sort_keys=True, allow_nan=False, default=str) + "\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ranges(values: Iterable[int]) -> List[Dict[str, int]]:
    ordered = sorted(set(int(value) for value in values))
    if not ordered:
        return []
    out: List[Dict[str, int]] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        out.append({"from": start, "to": previous, "runs": previous - start + 1})
        start = previous = value
    out.append({"from": start, "to": previous, "runs": previous - start + 1})
    return out


def _growth_decisions(
    rows: Sequence[Dict[str, Any]],
    marginal: float,
    low: float = rules.GROWTH_SMALLER_LOW,
    high: float = rules.GROWTH_SMALLER_HIGH,
) -> Dict[int, bool]:
    samples = analysis.rate_samples(rows, healthy_only=True)
    model: Dict[str, Any] = {}
    decisions: Dict[int, bool] = {}
    for row in rows:
        run = row.get("run")
        herd = row.get("animals")
        if not isinstance(run, int) or not isinstance(herd, int):
            continue
        available = [
            (int(sample["herd"]), float(sample["rate"]))
            for sample in samples
            if isinstance(sample.get("run"), int) and sample["run"] <= run
        ]
        verdict = rules.growth_verdict(
            available,
            herd,
            model,
            min_marginal_gain=marginal,
            smaller_low=low,
            smaller_high=high,
        )
        decisions[run] = bool(verdict.get("saturated"))
        model = dict(verdict)
    return decisions


def _threat_events(rows: Sequence[Dict[str, Any]], share: float) -> List[int]:
    events: List[int] = []
    prior: Dict[str, Tuple[float, float]] = {}
    previous: Optional[Dict[str, Any]] = None
    for row in rows:
        if previous is None:
            previous = row
            continue
        ours = None
        if isinstance(row.get("produce"), int) and isinstance(previous.get("produce"), int):
            ours = row["produce"] - previous["produce"]
        for name, value in (row.get("rivals") or {}).items():
            before = (previous.get("rivals") or {}).get(name)
            if not isinstance(value, (int, float)) or not isinstance(before, (int, float)):
                continue
            gained = float(value) - float(before)
            old = prior.get(name)
            if ours and ours > 0 and old and old[1] > 0:
                if gained >= share * ours and old[0] >= share * old[1]:
                    events.append(int(row.get("run") or 0))
            if ours is not None:
                prior[name] = (gained, float(ours))
        previous = row
    return [run for run in events if run > 0]


def _feed_decisions(rows: Sequence[Dict[str, Any]], cooldown: int) -> Dict[int, bool]:
    decisions: Dict[int, bool] = {}
    last_feed: Optional[int] = None
    for row in rows:
        run = row.get("run")
        if not isinstance(run, int):
            continue
        since = run - last_feed if last_feed is not None else 99
        decision = rules.should_feed(
            int(row.get("max_hunger") or 0),
            False,
            since,
            cooldown_runs=cooldown,
        )
        decisions[run] = decision
        # Replay observed state, not the counterfactual action, so each variant
        # compares the decision at the same historical observation.
        if row.get("fed"):
            last_feed = run
    return decisions


def counterfactual_sweep(
    rows: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Perturb decision constants over immutable history. No MCP import or call."""
    history = list(rows) if rows is not None else analysis.history_rows()
    live_growth = _growth_decisions(history, rules.GROWTH_MIN_MARGINAL_GAIN)
    dimensions: List[Dict[str, Any]] = []

    growth_alternatives = []
    for value in (0.0, 0.02, 0.05, 0.10):
        decisions = _growth_decisions(history, value)
        changed = [run for run, answer in decisions.items() if live_growth.get(run) != answer]
        growth_alternatives.append({
            "value": value,
            "changed_runs": len(changed),
            "ranges": _ranges(changed),
            "first_changed_run": min(changed) if changed else None,
            "last_changed_run": max(changed) if changed else None,
        })
    dimensions.append({
        "parameter": "GROWTH_MIN_MARGINAL_GAIN",
        "live": rules.GROWTH_MIN_MARGINAL_GAIN,
        "alternatives": growth_alternatives,
    })

    window_alternatives = []
    for low, high in ((0.60, 0.85), (0.70, 0.90), (0.80, 0.95)):
        decisions = _growth_decisions(history, rules.GROWTH_MIN_MARGINAL_GAIN, low, high)
        changed = [run for run, answer in decisions.items() if live_growth.get(run) != answer]
        window_alternatives.append({
            "value": {"low": low, "high": high},
            "changed_runs": len(changed),
            "ranges": _ranges(changed),
            "first_changed_run": min(changed) if changed else None,
            "last_changed_run": max(changed) if changed else None,
        })
    dimensions.append({
        "parameter": "GROWTH_COMPARISON_WINDOW",
        "live": {"low": rules.GROWTH_SMALLER_LOW, "high": rules.GROWTH_SMALLER_HIGH},
        "alternatives": window_alternatives,
    })

    live_threat = set(_threat_events(history, rules.THREAT_SHARE))
    threat_alternatives = []
    for value in (0.25, 0.50, 0.75):
        events = set(_threat_events(history, value))
        changed = sorted(events.symmetric_difference(live_threat))
        threat_alternatives.append({
            "value": value,
            "events": len(events),
            "changed_runs": len(changed),
            "ranges": _ranges(changed),
        })
    dimensions.append({
        "parameter": "THREAT_SHARE",
        "live": rules.THREAT_SHARE,
        "alternatives": threat_alternatives,
    })

    live_feed = _feed_decisions(history, rules.FEED_COOLDOWN_RUNS)
    feed_alternatives = []
    for value in (0, 1, 2):
        decisions = _feed_decisions(history, value)
        changed = [run for run, answer in decisions.items() if live_feed.get(run) != answer]
        feed_alternatives.append({
            "value": value,
            "changed_runs": len(changed),
            "ranges": _ranges(changed),
        })
    dimensions.append({
        "parameter": "FEED_COOLDOWN_RUNS",
        "live": rules.FEED_COOLDOWN_RUNS,
        "alternatives": feed_alternatives,
    })

    sensitive = [
        dimension["parameter"]
        for dimension in dimensions
        if any(item.get("changed_runs") for item in dimension["alternatives"]
               if item.get("value") != dimension.get("live"))
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_ts": _utcnow(),
        "rows_replayed": len(history),
        "run_from": history[0].get("run") if history else None,
        "run_to": history[-1].get("run") if history else None,
        "mcp_calls": 0,
        "dimensions": dimensions,
        "sensitive_parameters": sensitive,
    }


def model_drift(rows: Optional[Sequence[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    history = list(rows) if rows is not None else analysis.history_rows()
    samples = analysis.rate_samples(history, healthy_only=True)
    findings: List[Dict[str, Any]] = []
    if len(samples) >= 40:
        prior = samples[-40:-20]
        recent = samples[-20:]
        prior_yield = analysis.median(sample["rate"] / sample["herd"] for sample in prior)
        recent_yield = analysis.median(sample["rate"] / sample["herd"] for sample in recent)
        if prior_yield and recent_yield:
            ratio = recent_yield / prior_yield
            if ratio < 0.65 or ratio > 1.35:
                findings.append({
                    "code": "model_drift",
                    "subject": "output_yield",
                    "run": history[-1].get("run") if history else None,
                    "prior": round(prior_yield, 5),
                    "recent": round(recent_yield, 5),
                    "ratio": round(ratio, 3),
                    "alert": (
                        "MODEL DRIFT: output_yield recent %.5f vs prior %.5f "
                        "(%+.0f%% across two 20-sample cohorts)"
                        % (recent_yield, prior_yield, 100.0 * (ratio - 1.0))
                    ),
                })

    return findings


def semantic_audit(
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    registry: Optional[Dict[str, Any]] = None,
    promoted: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    history = list(rows) if rows is not None else analysis.history_rows()
    registry = registry or claims.load() or claims.build(history)
    promoted = promoted if promoted is not None else policy.load()
    errors = list(claims.validate(registry))
    warnings: List[str] = []
    mapping = claims.claim_map(registry)
    output = analysis.output_model(history)
    linear = mapping.get("mechanic.output_linear_with_herd") or {}
    plateau = mapping.get("mechanic.per_farm_output_plateau") or {}

    expected_linear = output.get("shape") == "linear"
    if expected_linear and linear.get("status") != "accepted":
        errors.append("output estimator is linear but linear claim is not accepted")
    if expected_linear and plateau.get("status") != "superseded":
        errors.append("output estimator is linear but plateau claim is not superseded")
    if output.get("shape") == "plateau" and plateau.get("status") != "accepted":
        errors.append("output estimator is plateau but plateau claim is not accepted")

    candidate = policy.compile_snapshot(registry)
    errors.extend(candidate.get("audit", {}).get("errors") or [])
    warnings.extend(candidate.get("audit", {}).get("warnings") or [])
    if promoted:
        runtime = policy.runtime_context(registry)
        if not runtime.get("compatible"):
            warnings.extend(runtime.get("errors") or [])
    else:
        warnings.append("no promoted policy snapshot")

    # The strategy classes are structurally separated from remedies. Import
    # lazily so heal can use research decision bundles without an import cycle.
    try:
        from . import heal
        strategy_classes = {
            "rank_lost", "threat", "overtaken", "strategy_stale", "idle_capital",
            "knob_age", "rival_wake", "rival_growing", "no_path_to_win", "win_eta",
            "hunger_wall", "model_drift", "policy_drift", "tools_changed",
        }
        remedied = [name for name, _, remedy in heal.CLASSES if name in strategy_classes and remedy is not None]
        if remedied:
            errors.append("strategy classes reachable from remedies: %s" % ",".join(sorted(set(remedied))))
    except Exception as exc:  # pragma: no cover - surfaced as an audit error in production
        errors.append("could not inspect healing reachability: %s" % str(exc)[:100])

    for claim in claims.overdue(registry):
        warnings.append("accepted claim overdue: %s" % claim.get("id"))

    return {
        "schema_version": SCHEMA_VERSION,
        "ts": _utcnow(),
        "run": history[-1].get("run") if history else None,
        "ok": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "stats": {
            "history_rows": len(history),
            "output_shape": output.get("shape"),
            "output_slope": (output.get("regression") or {}).get("slope"),
            "output_r": (output.get("regression") or {}).get("r"),
            "output_n": (output.get("regression") or {}).get("n"),
            "claims": len(registry.get("claims") or []),
            "open_questions": len(questions.open_questions()),
        },
    }


def decision_bundle(
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    row: Optional[Dict[str, Any]] = None,
    include_sweep: bool = True,
) -> Dict[str, Any]:
    history = list(rows) if rows is not None else analysis.history_rows()
    latest = row or (history[-1] if history else {})
    recent = history[-18:]
    rivals = sorted({name for item in recent for name in (item.get("rivals") or {})})
    trajectories = []
    for name in rivals:
        points = [
            {
                "run": item.get("run"),
                "produce": (item.get("rivals") or {}).get(name),
                "herd": (item.get("rival_herds") or {}).get(name),
                "coins": (item.get("rival_coins") or {}).get(name),
            }
            for item in recent
            if (item.get("rivals") or {}).get(name) is not None
        ]
        trajectories.append({"name": name, "points": points})
    bundle = {
        "run": latest.get("run"),
        "standing": {
            "rank": latest.get("rank"),
            "produce": latest.get("produce"),
            "animals": latest.get("animals"),
            "coins": latest.get("coins"),
            "feed": latest.get("feed"),
            "feed_runway_min": round(rules.feed_buffer_minutes(
                int(latest.get("feed") or 0), int(latest.get("animals") or 0)
            ), 1),
            "projection": latest.get("projection"),
        },
        "growth": latest.get("growth"),
        "knobs": latest.get("knobs"),
        "policy": policy.runtime_context(),
        "open_questions": [
            {
                "id": item.get("id"),
                "class": item.get("class"),
                "subject": item.get("subject"),
                "status": item.get("status"),
                "priority": item.get("priority"),
                "occurrences": item.get("occurrences"),
                "last_seen_run": item.get("last_seen_run"),
            }
            for item in questions.open_questions()
        ],
        "rival_trajectories": trajectories,
    }
    if include_sweep:
        sweep = counterfactual_sweep(history)
        bundle["counterfactual"] = {
            "rows_replayed": sweep["rows_replayed"],
            "mcp_calls": sweep["mcp_calls"],
            "sensitive_parameters": sweep["sensitive_parameters"],
            "dimensions": sweep["dimensions"],
        }
    return bundle


def run_audit(
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    persist: bool = True,
    open_findings: bool = True,
) -> Dict[str, Any]:
    history = list(rows) if rows is not None else analysis.history_rows()
    registry = claims.load() or claims.build(history)
    # A refresh is itself a re-litigation. Historical rows created before the
    # epistemic layer cannot carry that fact, so project current claim validation
    # onto the audit view without rewriting the immutable history ledger.
    audit_history = list(history)
    if audit_history:
        latest_with_claims = dict(audit_history[-1])
        latest_with_claims["claim_validated_runs"] = {
            claim_id: claim.get("last_validated_run")
            for claim_id, claim in claims.claim_map(registry).items()
            if isinstance(claim.get("last_validated_run"), int)
        }
        audit_history[-1] = latest_with_claims
    latest = audit_history[-1] if audit_history else {}
    findings = rules.strategy_audit(audit_history)
    findings.extend(model_drift(audit_history))
    for claim in claims.overdue(registry):
        findings.append({
            "code": "knob_age",
            "subject": claim.get("id"),
            "run": latest.get("run"),
            "alert": "KNOB AGE: %s evidence is overdue (%s runs old)" % (
                claim.get("id"), (claim.get("refresh") or {}).get("age_runs")
            ),
        })
    audit = semantic_audit(audit_history, registry)
    if not audit["ok"]:
        findings.append({
            "code": "policy_drift",
            "subject": "semantic_contract",
            "run": latest.get("run"),
            "alert": "POLICY DRIFT: semantic audit failed: %s" % "; ".join(audit["errors"][:3]),
        })

    opened = []
    if open_findings:
        bundle = None
        for finding in findings:
            if bundle is None:
                bundle = decision_bundle(audit_history, latest, include_sweep=True)
            result = questions.open_or_update(
                finding["code"],
                finding["alert"],
                row=latest,
                subject=finding.get("subject"),
                decision_bundle=bundle,
                evidence_refs=["history.ndjson#run=%s" % latest.get("run")],
            )
            opened.append({
                "id": result["question"]["id"],
                "opened": result["opened"],
                "page_on_open": result["page_on_open"],
            })

    result = {
        "schema_version": SCHEMA_VERSION,
        "ts": _utcnow(),
        "run": latest.get("run"),
        "findings": findings,
        "questions": opened,
        "semantic": audit,
    }
    if persist:
        _append_audit(result)
    return result
