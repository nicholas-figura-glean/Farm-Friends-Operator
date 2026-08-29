"""Versioned, machine-readable claims about how the game works.

A claim is not a comment and not a policy constant. It is a scoped interpretation
of evidence with confidence, freshness, a falsifier, and a supersession path.
This registry is the sole source for Findings conclusions and policy dependency
checks.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import analysis, rules

SCHEMA_VERSION = 1
VALID_STATUSES = {"candidate", "accepted", "challenged", "superseded", "retired"}


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else Path(__file__).resolve().parent.parent / "state"


def _paths() -> Tuple[Path, Path, Path]:
    current = Path(os.environ.get("FARM_CLAIMS_FILE", str(_state_dir() / "claims.json")))
    events = Path(os.environ.get("FARM_CLAIM_EVENTS_FILE", str(_state_dir() / "claim_events.ndjson")))
    lock = Path(str(current) + ".lock")
    return current, events, lock


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _confidence(score: float, rationale: str) -> Dict[str, Any]:
    bounded = max(0.0, min(1.0, float(score)))
    if bounded >= 0.85:
        level = "high"
    elif bounded >= 0.6:
        level = "medium"
    else:
        level = "low"
    return {"score": round(bounded, 3), "level": level, "rationale": rationale}


def _freshness(last_validated: Optional[int], current_run: Optional[int], max_age: int) -> Dict[str, Any]:
    age = None
    if isinstance(last_validated, int) and isinstance(current_run, int):
        age = max(0, current_run - last_validated)
    overdue = age is not None and age > max_age
    return {
        "max_age_runs": max_age,
        "age_runs": age,
        "state": "overdue" if overdue else "current" if age is not None else "unknown",
    }


def _claim(
    claim_id: str,
    statement: str,
    category: str,
    status: str,
    scope: Dict[str, Any],
    metric: str,
    estimator: Dict[str, Any],
    value: Dict[str, Any],
    evidence_refs: List[str],
    confidence: Dict[str, Any],
    first_observed_run: Optional[int],
    last_validated_run: Optional[int],
    max_age_runs: int,
    current_run: Optional[int],
    falsifier: str,
    consumers: List[str],
    decision: Dict[str, Any],
    supersedes: Optional[List[str]] = None,
    superseded_by: Optional[str] = None,
    dependencies: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError("invalid claim status: %s" % status)
    return {
        "id": claim_id,
        "statement": statement,
        "category": category,
        "status": status,
        "scope": scope,
        "metric": metric,
        "estimator": estimator,
        "value": value,
        "evidence_refs": evidence_refs,
        "confidence": confidence,
        "first_observed_run": first_observed_run,
        "last_validated_run": last_validated_run,
        "refresh": _freshness(last_validated_run, current_run, max_age_runs),
        "falsifier": falsifier,
        "consumers": consumers,
        "decision": decision,
        "supersedes": list(supersedes or []),
        "superseded_by": superseded_by,
        "dependencies": list(dependencies or []),
    }


def build(rows: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    history = list(rows) if rows is not None else analysis.history_rows()
    current_run = max([row.get("run") for row in history if isinstance(row.get("run"), int)], default=None)
    output = analysis.output_model(history)
    species = analysis.species_model(history)
    output_shape = output.get("shape")
    linear_status = "accepted" if output_shape == "linear" else "challenged"
    plateau_status = "superseded" if output_shape == "linear" else (
        "accepted" if output_shape == "plateau" else "challenged"
    )
    output_run = output.get("sample_run_to")
    slope = (output.get("regression") or {}).get("slope")
    correlation = (output.get("regression") or {}).get("r")
    sample_count = int((output.get("regression") or {}).get("n") or 0)

    chicken = next((item for item in species["table"] if item["kind"] == "chicken"), {})
    try:
        probe_state = json.loads((_state_dir() / "beehive_probe.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        probe_state = {}
    probe_result = probe_state.get("result") if isinstance(probe_state.get("result"), dict) else {}
    measured_coin_rates = [
        float(item.get("recent_units_per_purchase_coin_min"))
        for item in species["table"]
        if isinstance(item.get("recent_units_per_purchase_coin_min"), (int, float))
    ]
    chicken_coin_rate = chicken.get("recent_units_per_purchase_coin_min")
    chicken_supported = bool(
        isinstance(chicken_coin_rate, (int, float))
        and measured_coin_rates
        and float(chicken_coin_rate) >= max(measured_coin_rates)
        and probe_result.get("decision") != "promote_beehive"
    )
    claims: List[Dict[str, Any]] = [
        _claim(
            "objective.league_first",
            "The leaderboard is ordered lexicographically by league level first and lifetime produce second; an available prestige is the mandatory progression action because it preserves lifetime produce and raises animal capacity.",
            "objective",
            "accepted",
            {"server_regime": "league leaderboard", "ordering": ["league_level", "lifetime_produce"]},
            "league level, then lifetime produce",
            {"kind": "server_contract_plus_direct_state", "tool": "prestige"},
            {"primary": "league_level", "secondary": "lifetime_produce", "prestige_preserves_lifetime": True, "prestige_raises_capacity": True},
            [
                "state/contract.json#tools.prestige.description_sha=16241be9cffd",
                "state/raw/latest/leaderboard.txt#by-league-then-lifetime-produce",
                "state/raw/latest/list_farm_state.txt#prestige-available",
            ],
            _confidence(0.99, "The MCP contract states the ordering and effects explicitly, and both current state surfaces independently expose them."),
            current_run,
            current_run,
            100,
            current_run,
            "A captured leaderboard ranks a lower league above a higher league, or a verified prestige reduces lifetime produce or fails to raise capacity.",
            ["policy.objective", "cycle.mechanics", "canary.progression", "evidence.progression"],
            {"objective_order": ["league_level", "lifetime_produce"], "prestige_when_available": True},
        ),
        _claim(
            "objective.lifetime_produce",
            "Within a league, lifetime produce is the authoritative secondary score; collection is an inventory-banking action, not the scoring event.",
            "objective",
            "accepted",
            {"all_runs": True, "excludes": []},
            "leaderboard lifetime-produce delta",
            {"kind": "direct_observation", "cases": [25, 50, 51]},
            {"score_field": "produce", "collection_field": "units_collected"},
            ["history.ndjson#run=25", "history.ndjson#run=50", "history.ndjson#run=51"],
            _confidence(0.99, "Repeated score increases without matching collection and inventory appearing during feed."),
            25,
            current_run,
            100,
            current_run,
            "A controlled interval in which lifetime produce changes only at collection time and never while uncollected.",
            ["watch.production", "policy.objective", "evidence.collection", "endgame.forecast"],
            {"score_metric": "lifetime_produce", "priority": "secondary_within_league"},
            dependencies=["objective.league_first"],
        ),
        _claim(
            "mechanic.output_linear_with_herd",
            output["statement"],
            "mechanic",
            linear_status,
            {
                "hunger_lt": rules.HUNGER_ALARM,
                "gap_minutes_lte": 30,
                "herd_gte": output["threshold"],
                "score_metric": "lifetime_produce",
                "excluded_regimes": ["starved", "hunger_risk", "blind_gap"],
            },
            "healthy lifetime-produce/min versus herd size",
            {"kind": "least_squares", "threshold": output["threshold"], "cohort": output["cohort"]},
            {"shape": output_shape, "slope": slope, "r": correlation, "n": sample_count},
            ["history.ndjson#runs=%s-%s" % (output.get("sample_run_from"), output_run),
             "cohort:sha256:%s" % output["cohort"]["sha256"]],
            _confidence(float(output.get("confidence") or 0.0), "Regime-filtered full-history fit, refreshed from immutable score deltas."),
            2,
            output_run,
            20,
            current_run,
            "A sufficiently powered healthy cohort above 8,000 animals with sustained near-zero or negative marginal slope.",
            ["growth.verdict", "policy.adoption", "watch.win_projection", "evidence.output"],
            {"output_shape": output_shape, "marginal_positive": bool(slope is not None and slope > 0.05)},
            supersedes=["mechanic.per_farm_output_plateau"] if output_shape == "linear" else [],
        ),
        _claim(
            "mechanic.per_farm_output_plateau",
            (
                "The historical per-farm plateau conclusion is falsified by the regime-filtered full ledger."
                if output_shape == "linear"
                else output["statement"]
            ),
            "mechanic",
            plateau_status,
            {"historical_runs": "2-56", "known_confounders": ["collection proxy", "partial feeding", "mixed regimes"]},
            "marginal lifetime-produce/min above 8,000 animals",
            {"kind": "historical_hypothesis_retest", "replacement_estimator": "mechanic.output_linear_with_herd"},
            {"shape": "plateau", "current_output_shape": output_shape, "current_slope": slope, "current_r": correlation},
            ["POSTMORTEM-run291.md", "POSTMORTEM-run377.md", "cohort:sha256:%s" % output["cohort"]["sha256"]],
            _confidence(0.98 if output_shape == "linear" else 0.55, "The later full-history score fit directly tests and currently rejects the plateau."),
            46,
            output_run,
            20,
            current_run,
            "A fresh healthy cohort with slope statistically and practically indistinguishable from zero.",
            [],
            {"output_shape": "superseded" if output_shape == "linear" else output_shape},
            superseded_by="mechanic.output_linear_with_herd" if output_shape == "linear" else None,
        ),
        _claim(
            "mechanic.collection_not_score",
            "Produce accrues to lifetime score independently of collection; collection converts accrued produce into saleable inventory and coins.",
            "mechanic",
            "accepted",
            {"observed_runs": [25, 50, 51], "score_metric": "lifetime_produce"},
            "score delta compared with collection result",
            {"kind": "case_reproduction", "minimum_cases": 3},
            {"reproductions": 3},
            ["history.ndjson#run=25", "history.ndjson#run=50", "history.ndjson#run=51"],
            _confidence(0.97, "Three independent reproductions with score/inventory divergence."),
            25,
            current_run,
            100,
            current_run,
            "Repeated controlled intervals where score remains flat until collection and then jumps by the collected quantity.",
            ["rules.should_collect", "watch.production", "evidence.collection"],
            {"collection_creates_score": False},
            dependencies=["objective.league_first", "objective.lifetime_produce"],
        ),
        _claim(
            "strategy.chicken_engine",
            "Chicken remains the promoted adoption policy: it has the strongest recent output per purchase coin, and the run-639 bounded 1,000-beehive scale probe failed its predeclared immediate-output gate despite later steady-state ratios near 1.25x.",
            "strategy",
            "accepted" if chicken_supported else "challenged",
            {"metric": species["scope"], "does_not_claim": "per-farm output cap"},
            "recent same-window output per animal and purchase coin, plus bounded scale-probe decision",
            {"kind": "recent_species_rates_and_intervention", "cohort": species["cohort"]},
            {"chicken_share": chicken.get("share"), "table": species["table"], "beehive_probe": probe_result},
            ["cohort:sha256:%s" % species["cohort"]["sha256"], "experiments/species_probe.py#run=50", "experiments/beehive_probe.py#baseline_run=639"],
            _confidence(0.9, "Chicken leads on capital efficiency; the scaled beehive cohort did not clear the conservative promotion gate because of multi-window warm-up."),
            1,
            current_run,
            100,
            current_run,
            "A bounded alternative-species probe clears its predeclared output-per-adoption gate without warm-up, feed, or transport regressions.",
            ["policy.primary_kind", "rules.adoptable", "evidence.species"],
            {"primary_kind": "chicken"},
            dependencies=["objective.league_first", "objective.lifetime_produce"],
        ),
        _claim(
            "mechanic.crop_timers_stalled",
            "In the run-50 probe, wheat, corn, and pumpkin remained at 0% after 27 minutes; the result is scoped to that server regime and is overdue for revalidation.",
            "mechanic",
            "accepted",
            {"run": 50, "server_regime": "2026-08-20", "crops": ["wheat", "corn", "pumpkin"]},
            "reported crop growth after elapsed wall time",
            {"kind": "bounded_negative_probe", "elapsed_minutes": 27},
            {"advanced": False, "plots": 3},
            ["experiments/species_probe.py#crop-probe", "history.ndjson#run=50"],
            _confidence(0.78, "Direct negative probe, but only one historical server regime."),
            50,
            50,
            200,
            current_run,
            "Any planted food crop advances above 0% or becomes harvestable under a current bounded re-probe.",
            ["policy.food_crops_banned", "evidence.crops"],
            {"food_crops_banned": True},
        ),
        _claim(
            "safety.bulk_husbandry",
            "Whole-herd feeding is constant-time; safety is governed by observed hunger and feed runway, not a synthetic herd-size ceiling.",
            "safety",
            "accepted",
            {"bulk_feed_path": "feed_animals(all)", "herd_scale": "all"},
            "actual post-feed hunger and feed runway",
            {"kind": "server_capability_contract", "announced": "2026-08-24"},
            {"herd_ceiling": None, "observe_hunger": True, "preserve_feed_runway": True},
            ["Farm Friends update 2026-08-24", "farm/cycle.py#feed_if_needed"],
            _confidence(0.95, "Explicit constant-time whole-herd operation; live hunger and runway remain direct falsifiers."),
            current_run,
            current_run,
            40,
            current_run,
            "Bulk feed latency or post-feed hunger begins increasing systematically with herd size.",
            ["policy.feed_reserve", "watch.hunger", "experiments.expand"],
            {"hunger_ceiling_kind": "none", "feed_scope": "all"},
            supersedes=["safety.hunger_wall_projection"],
        ),
        _claim(
            "transport.bulk_operations_constant_time",
            "feed_animals(all) and collect_produce each execute as one constant-time bulk operation at every herd size.",
            "transport",
            "accepted",
            {"tools": ["feed_animals", "collect_produce"], "herd_scale": "all"},
            "one call per operation with herd-size-independent server execution",
            {"kind": "server_capability_contract", "announced": "2026-08-24"},
            {"feed_calls_per_cycle": 1, "collect_calls_per_cycle": 1, "per_id_fanout": False},
            ["Farm Friends update 2026-08-24", "farm/cycle.py#bulk-operations"],
            _confidence(0.95, "Explicit server capability update; runtime spans will continuously falsify latency or fan-out regressions."),
            current_run,
            current_run,
            40,
            current_run,
            "Either operation regains herd-size latency growth, times out systematically, or emits more than one logical call per cycle.",
            ["rules.should_collect", "rules.core_transport_errors", "cycle.collect", "cycle.feed_if_needed"],
            {"collect_every": 1, "feed_scope": "all", "heavy_transport_exemption": False},
            supersedes=["transport.bulk_504_applies_server_side"],
        ),
    ]

    registry = {
        "schema_version": SCHEMA_VERSION,
        "registry_version": 1,
        "updated_ts": _utcnow(),
        "current_run": current_run,
        "claims": sorted(claims, key=lambda claim: claim["id"]),
    }
    registry["semantic_fingerprint"] = semantic_fingerprint(registry)
    registry["policy_fingerprint"] = policy_fingerprint(registry)
    return registry


def _semantic_claim(claim: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: claim.get(key)
        for key in (
            "id", "statement", "category", "status", "scope", "metric", "estimator",
            "value", "evidence_refs", "confidence", "falsifier", "consumers", "decision",
            "supersedes", "superseded_by", "dependencies",
        )
    }


def semantic_fingerprint(registry: Dict[str, Any]) -> str:
    payload = [_semantic_claim(claim) for claim in registry.get("claims") or []]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def policy_fingerprint(registry: Dict[str, Any]) -> str:
    payload = [
        {
            "id": claim.get("id"),
            "status": claim.get("status"),
            "decision": claim.get("decision"),
            "superseded_by": claim.get("superseded_by"),
        }
        for claim in registry.get("claims") or []
        if claim.get("consumers")
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def load() -> Dict[str, Any]:
    current, _, _ = _paths()
    try:
        value = json.loads(current.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("%s.tmp.%d" % (path, os.getpid()))
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def _append_event(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(value, sort_keys=True, allow_nan=False, default=str) + "\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def refresh(
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    candidate = build(rows)
    existing = load()
    old_version = int(existing.get("registry_version") or 0)
    changed = candidate["semantic_fingerprint"] != existing.get("semantic_fingerprint")
    candidate["registry_version"] = old_version + 1 if changed else max(1, old_version)
    if not persist:
        return candidate

    current, events, lock = _paths()
    current.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        existing = load()
        old_map = {claim.get("id"): claim for claim in existing.get("claims") or []}
        if changed or not existing:
            _atomic_write(current, candidate)
            for claim in candidate["claims"]:
                prior = old_map.get(claim["id"])
                if prior is None:
                    event = "created"
                elif (prior.get("status"), prior.get("decision")) != (claim.get("status"), claim.get("decision")):
                    event = "transitioned"
                else:
                    event = "revalidated"
                _append_event(
                    events,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "event": event,
                        "ts": _utcnow(),
                        "run": candidate.get("current_run"),
                        "claim_id": claim["id"],
                        "from_status": (prior or {}).get("status"),
                        "to_status": claim.get("status"),
                        "registry_version": candidate["registry_version"],
                        "evidence_refs": claim.get("evidence_refs"),
                    },
                )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return candidate


def claim_map(registry: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    value = registry or load()
    return {claim["id"]: claim for claim in value.get("claims") or [] if claim.get("id")}


def get(claim_id: str, registry: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    return claim_map(registry).get(claim_id)


def validate(registry: Optional[Dict[str, Any]] = None) -> List[str]:
    value = registry or load()
    errors: List[str] = []
    if value.get("schema_version") != SCHEMA_VERSION:
        errors.append("claim registry schema mismatch")
    seen = set()
    mapping = claim_map(value)
    for claim in value.get("claims") or []:
        claim_id = claim.get("id")
        if not claim_id or claim_id in seen:
            errors.append("duplicate or missing claim id: %s" % claim_id)
            continue
        seen.add(claim_id)
        if claim.get("status") not in VALID_STATUSES:
            errors.append("%s has invalid status %s" % (claim_id, claim.get("status")))
        if not claim.get("falsifier"):
            errors.append("%s has no falsifier" % claim_id)
        if claim.get("status") == "superseded" and not claim.get("superseded_by"):
            errors.append("%s is superseded without a replacement" % claim_id)
        replacement = claim.get("superseded_by")
        if replacement and replacement not in mapping:
            errors.append("%s replacement %s is missing" % (claim_id, replacement))
        for dependency in claim.get("dependencies") or []:
            if dependency not in mapping:
                errors.append("%s dependency %s is missing" % (claim_id, dependency))
    linear = mapping.get("mechanic.output_linear_with_herd") or {}
    plateau = mapping.get("mechanic.per_farm_output_plateau") or {}
    if linear.get("status") == "accepted" and plateau.get("status") == "accepted":
        errors.append("contradictory output claims are both accepted")
    if value and value.get("semantic_fingerprint") != semantic_fingerprint(value):
        errors.append("claim registry semantic fingerprint mismatch")
    if value and value.get("policy_fingerprint") != policy_fingerprint(value):
        errors.append("claim registry policy fingerprint mismatch")
    return errors


def overdue(registry: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    value = registry or load()
    return [
        claim for claim in value.get("claims") or []
        if (claim.get("refresh") or {}).get("state") == "overdue"
        and claim.get("status") == "accepted"
    ]
