"""Compile, audit, promote, and identify executable farm policy.

The Python rules remain the deterministic implementation. A promoted policy is a
content-addressed contract proving which parameters are compiled, which claims
justify them, and which safety invariants own the rest. Runtime only reports a
promoted identity when that contract exactly matches the executing rules and the
claim decisions it depends on.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import claims, mechanics, provenance, rules

SCHEMA_VERSION = 1

INVARIANTS: Dict[str, str] = {
    "deterministic_runtime": "Routine actions are selected by pure Python; no model is on the cycle path.",
    "feed_before_score": "No growth action may knowingly cross the hunger stop or consume required feed runway.",
    "bounded_mutation": "Every mutation has a call, coin, count, or wall-time bound.",
    "strict_parsing": "Unknown server wording fails closed before dependent mutation.",
    "objective_guard": "A remedy that slows the scoring action must re-check the objective-denominated safety signal.",
    "question_not_remedy": "Strategy uncertainty opens a question and is never auto-healed by throttling growth.",
    "explicit_promotion": "Evidence changes cannot change behavior until a compatible policy or validated strategy release is promoted.",
}

# Every parameter has a claim and/or invariant owner. This is itself validated.
OWNERS: Dict[str, Dict[str, List[str]]] = {
    "primary_kind": {"claims": ["strategy.chicken_engine"], "invariants": ["bounded_mutation"]},
    "food_crops_banned": {"claims": ["mechanic.crop_timers_stalled"], "invariants": ["bounded_mutation"]},
    "growth_min_marginal_gain": {"claims": ["mechanic.output_linear_with_herd"], "invariants": ["explicit_promotion"]},
    "growth_recent_band": {"claims": ["mechanic.output_linear_with_herd"], "invariants": ["explicit_promotion"]},
    "growth_smaller_low": {"claims": ["mechanic.output_linear_with_herd"], "invariants": ["explicit_promotion"]},
    "growth_smaller_high": {"claims": ["mechanic.output_linear_with_herd"], "invariants": ["explicit_promotion"]},
    "maintenance_adoptions": {"claims": ["mechanic.output_linear_with_herd"], "invariants": ["bounded_mutation"]},
    "collect_every": {"claims": ["mechanic.collection_not_score"], "invariants": ["bounded_mutation"]},
    "feed_at_hunger": {"claims": ["objective.lifetime_produce"], "invariants": ["feed_before_score"]},
    "feed_cooldown_runs": {"claims": ["objective.lifetime_produce"], "invariants": ["feed_before_score"]},
    "feed_reserve_per_animal": {"claims": ["safety.bulk_husbandry"], "invariants": ["feed_before_score"]},
    "feed_buffer_min_minutes": {"claims": ["safety.bulk_husbandry"], "invariants": ["feed_before_score"]},
    "hunger_alarm": {"claims": ["safety.bulk_husbandry"], "invariants": ["feed_before_score"]},
    "hunger_stop": {"claims": ["safety.bulk_husbandry"], "invariants": ["feed_before_score"]},
    "max_adoptions_per_run": {"claims": ["mechanic.output_linear_with_herd"], "invariants": ["bounded_mutation"]},
    "max_calls_per_second": {"claims": ["transport.bulk_operations_constant_time"], "invariants": ["bounded_mutation"]},
    "adopt_workers": {"claims": ["transport.bulk_operations_constant_time"], "invariants": ["bounded_mutation"]},
    "cycle_budget_seconds": {"claims": ["transport.bulk_operations_constant_time"], "invariants": ["bounded_mutation"]},
    "risk_coin_reserve": {"claims": ["transport.bulk_operations_constant_time"], "invariants": ["bounded_mutation"]},
    "cycle_hard_timeout": {"claims": [], "invariants": ["bounded_mutation"]},
    "threat_share": {"claims": ["objective.lifetime_produce"], "invariants": ["question_not_remedy"]},
    "audit_window_runs": {"claims": ["mechanic.output_linear_with_herd"], "invariants": ["question_not_remedy"]},
    "rival_wake_min_rate": {"claims": ["objective.lifetime_produce"], "invariants": ["question_not_remedy"]},
    "capability_policy_schema": {"claims": ["objective.league_first"], "invariants": ["bounded_mutation", "strict_parsing", "explicit_promotion"]},
}

REQUIRED_CLAIMS = {
    "objective.league_first",
    "objective.lifetime_produce",
    "mechanic.output_linear_with_herd",
    "mechanic.collection_not_score",
    "strategy.chicken_engine",
    "safety.bulk_husbandry",
    "transport.bulk_operations_constant_time",
}


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else Path(__file__).resolve().parent.parent / "state"


def _paths() -> Tuple[Path, Path, Path]:
    current = Path(os.environ.get("FARM_POLICY_FILE", str(_state_dir() / "policy.json")))
    events = Path(os.environ.get("FARM_POLICY_EVENTS_FILE", str(_state_dir() / "policy_events.ndjson")))
    lock = Path(str(current) + ".lock")
    return current, events, lock


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parameters() -> Dict[str, Any]:
    return {
        "primary_kind": rules.PRIMARY_KIND,
        "food_crops_banned": rules.FOOD_CROPS_BANNED,
        "growth_min_marginal_gain": rules.GROWTH_MIN_MARGINAL_GAIN,
        "growth_recent_band": rules.GROWTH_RECENT_BAND,
        "growth_smaller_low": rules.GROWTH_SMALLER_LOW,
        "growth_smaller_high": rules.GROWTH_SMALLER_HIGH,
        "maintenance_adoptions": rules.MAINTENANCE_ADOPTIONS,
        "collect_every": rules.COLLECT_EVERY,
        "feed_at_hunger": rules.FEED_AT_HUNGER,
        "feed_cooldown_runs": rules.FEED_COOLDOWN_RUNS,
        "feed_reserve_per_animal": rules.FEED_PER_ANIMAL_RESERVE,
        "feed_buffer_min_minutes": rules.FEED_BUFFER_MIN_MINUTES,
        "hunger_alarm": rules.HUNGER_ALARM,
        "hunger_stop": rules.HUNGER_STOP,
        "max_adoptions_per_run": rules.MAX_ADOPTIONS_PER_RUN,
        "max_calls_per_second": rules.MAX_CALLS_PER_SECOND,
        "adopt_workers": rules.ADOPT_WORKERS,
        "cycle_budget_seconds": rules.CYCLE_BUDGET_SECONDS,
        "cycle_hard_timeout": rules.CYCLE_HARD_TIMEOUT,
        "risk_coin_reserve": rules.RISK_COIN_RESERVE,
        "threat_share": rules.THREAT_SHARE,
        "audit_window_runs": getattr(rules, "AUDIT_WINDOW_RUNS", 30),
        "rival_wake_min_rate": getattr(rules, "RIVAL_WAKE_MIN_RATE", 0.5),
        "capability_policy_schema": mechanics.POLICY_SCHEMA_VERSION,
    }


def rules_fingerprint(values: Optional[Dict[str, Any]] = None) -> str:
    encoded = json.dumps(values or parameters(), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _content(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: snapshot.get(key)
        for key in (
            "schema_version", "objective", "parameters", "owners", "invariants",
            "rules_fingerprint", "claim_policy_fingerprint", "required_claims",
        )
    }


def _policy_id(snapshot: Dict[str, Any]) -> str:
    encoded = json.dumps(_content(snapshot), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "pol-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def compile_snapshot(registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    registry = registry or claims.load() or claims.build()
    mapping = claims.claim_map(registry)
    values = parameters()
    errors = list(claims.validate(registry))
    warnings: List[str] = []

    for required in sorted(REQUIRED_CLAIMS):
        claim = mapping.get(required)
        if claim is None:
            errors.append("required claim missing: %s" % required)
        elif claim.get("status") != "accepted":
            errors.append("required claim %s is %s" % (required, claim.get("status")))
        elif (claim.get("refresh") or {}).get("state") == "overdue":
            warnings.append("required claim overdue: %s" % required)

    for name in values:
        owner = OWNERS.get(name)
        if not owner:
            errors.append("policy parameter has no owner: %s" % name)
            continue
        if not owner.get("claims") and not owner.get("invariants"):
            errors.append("policy parameter has empty ownership: %s" % name)
        for claim_id in owner.get("claims") or []:
            if claim_id not in mapping:
                errors.append("%s owner claim missing: %s" % (name, claim_id))
        for invariant in owner.get("invariants") or []:
            if invariant not in INVARIANTS:
                errors.append("%s owner invariant missing: %s" % (name, invariant))

    snapshot: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "candidate",
        "created_ts": _utcnow(),
        "promoted_ts": None,
        "objective": {
            "metric": "league level, then leaderboard lifetime produce",
            "ordering": "lexicographic",
            "direction": "maximize",
            "constraints": ["hunger", "feed runway", "transport budget", "bounded mutation"],
        },
        "parameters": values,
        "owners": OWNERS,
        "invariants": INVARIANTS,
        "rules_fingerprint": rules_fingerprint(values),
        "claim_registry_version": registry.get("registry_version"),
        "claim_semantic_fingerprint": registry.get("semantic_fingerprint"),
        "claim_policy_fingerprint": registry.get("policy_fingerprint") or claims.policy_fingerprint(registry),
        "required_claims": sorted(REQUIRED_CLAIMS),
        "audit": {"ok": not errors, "errors": sorted(set(errors)), "warnings": sorted(set(warnings))},
    }
    snapshot["policy_id"] = _policy_id(snapshot)
    return snapshot


def load() -> Dict[str, Any]:
    current, _, _ = _paths()
    try:
        value = json.loads(current.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, snapshot: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("%s.tmp.%d" % (path, os.getpid()))
    tmp.write_text(json.dumps(snapshot, indent=1, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def _event(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _events(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def promote(
    snapshot: Optional[Dict[str, Any]] = None,
    registry: Optional[Dict[str, Any]] = None,
    promotion_contract: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    candidate = dict(snapshot or compile_snapshot(registry))
    if not (candidate.get("audit") or {}).get("ok"):
        raise ValueError("policy promotion rejected: %s" % "; ".join((candidate.get("audit") or {}).get("errors") or []))
    if candidate.get("policy_id") != _policy_id(candidate):
        raise ValueError("policy content hash mismatch")
    current, events, lock = _paths()
    current.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        previous = load()
        lineage_errors = provenance.validate_promotion_contract(
            promotion_contract,
            str(candidate.get("policy_id") or ""),
            previous.get("policy_id"),
            _events(events),
        )
        if lineage_errors:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            raise ValueError("policy promotion rejected: %s" % "; ".join(lineage_errors))
        # Durable lineage is written before behavior changes. If that append fails,
        # promotion fails closed with the previous policy still current. A later
        # snapshot/event write failure may leave an unused authorization record, but
        # can never leave behavior promoted without its evidence graph.
        provenance.record_policy_promotion(
            str(candidate["policy_id"]), previous.get("policy_id"), promotion_contract
        )
        candidate["status"] = "promoted"
        candidate["promoted_ts"] = _utcnow()
        if promotion_contract:
            candidate["promotion_contract"] = dict(promotion_contract)
        _write(current, candidate)
        _event(
            events,
            {
                "schema_version": SCHEMA_VERSION,
                "event": "promoted",
                "ts": candidate["promoted_ts"],
                "policy_id": candidate["policy_id"],
                "previous_policy_id": previous.get("policy_id"),
                "rules_fingerprint": candidate["rules_fingerprint"],
                "claim_policy_fingerprint": candidate["claim_policy_fingerprint"],
                "hypothesis_id": (promotion_contract or {}).get("hypothesis_id"),
                "validation_evidence": (promotion_contract or {}).get("validation_evidence") or [],
            },
        )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return candidate


def runtime_context(registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    registry = registry or claims.load()
    promoted = load()
    compiled_fingerprint = rules_fingerprint()
    current_claim_fingerprint = (
        registry.get("policy_fingerprint") if registry else None
    )
    errors: List[str] = []
    if not registry:
        errors.append("no claim registry")
    if not promoted:
        errors.append("no promoted policy snapshot")
    else:
        if promoted.get("status") != "promoted":
            errors.append("policy snapshot is not promoted")
        if promoted.get("rules_fingerprint") != compiled_fingerprint:
            errors.append("compiled rules differ from promoted policy")
        if current_claim_fingerprint and promoted.get("claim_policy_fingerprint") != current_claim_fingerprint:
            errors.append("claim decisions differ from promoted policy")
        if promoted.get("policy_id") != _policy_id(promoted):
            errors.append("promoted policy content hash mismatch")
    compatible = not errors
    return {
        "policy_id": promoted.get("policy_id") if compatible else "compiled-" + compiled_fingerprint,
        "promoted_policy_id": promoted.get("policy_id"),
        "compatible": compatible,
        "errors": errors,
        "rules_fingerprint": compiled_fingerprint,
        "claim_registry_version": registry.get("registry_version") if registry else None,
        "claim_policy_fingerprint": current_claim_fingerprint,
        "claim_validated_runs": {
            claim_id: claim.get("last_validated_run")
            for claim_id, claim in claims.claim_map(registry).items()
            if isinstance(claim.get("last_validated_run"), int)
        } if registry else {},
        "parameters": parameters(),
    }


def decision_trace(
    selected: Dict[str, Any],
    inputs: Dict[str, Any],
    growth: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    runtime = context or runtime_context()
    adopt = int(selected.get("adopt") or 0)
    alternatives = {
        "no_growth": {"adopt": 0, "buy_feed": max(0, rules.feed_reserve_target(
            int(inputs.get("animals") or 0), int(inputs.get("committed_feed") or 0)
        ) - int(inputs.get("feed") or 0))},
        "maintenance": {"adopt": min(rules.MAINTENANCE_ADOPTIONS, adopt)},
        "selected": dict(selected),
    }
    return {
        "policy_id": runtime.get("policy_id"),
        "policy_compatible": runtime.get("compatible"),
        "objective": "maximize league level first, then lifetime produce, subject to feed, hunger, transport, and mutation bounds",
        "inputs": dict(inputs),
        "selected": dict(selected),
        "alternatives": alternatives,
        "growth_evidence": growth or {},
        "claim_ids": [
            "objective.league_first",
            "objective.lifetime_produce",
            "mechanic.output_linear_with_herd",
            "mechanic.collection_not_score",
            "strategy.chicken_engine",
            "safety.bulk_husbandry",
        ],
    }
