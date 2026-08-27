"""Versioned evidence lineage and anti-circular promotion gates.

The graph is append-only. Nodes are immutable identities (hypotheses, validation
results, policies, and releases); edges always point from prior evidence to the new
node. Policy promotion validates that graph, keeps discovery and validation cohorts
disjoint, rejects observational evidence as causal proof, and pauses A->B->A policy
oscillation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

SCHEMA_VERSION = 1
VALID_EVIDENCE_CLASSES = {"intervention", "holdout", "direct_mechanism"}
TERMINAL_FAILURES = {"rejected", "reverted", "falsified", "failed"}


class ProvenanceError(ValueError):
    """A decision would violate evidence lineage or anti-cycle rules."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else Path(__file__).resolve().parent.parent / "state"


def _path() -> Path:
    return Path(os.environ.get("FARM_PROVENANCE_LOG", str(_state_dir() / "provenance.ndjson")))


def _lock_path() -> Path:
    return Path(str(_path()) + ".lock")


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2_000]
    if isinstance(value, dict):
        return {str(key)[:100]: _bounded(item, depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set)):
        return [_bounded(item, depth + 1) for item in list(value)[:100]]
    return str(value)[:2_000]


def _append(row: Dict[str, Any]) -> Dict[str, Any]:
    path = _path()
    lock_path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, allow_nan=False, default=str) + "\n")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return row


def events() -> List[Dict[str, Any]]:
    try:
        lines = _path().read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("event"):
            out.append(value)
    return out


def _normalize(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^a-z0-9._%+\-/ ]+", "", text)
    return text.strip()


def semantic_fingerprint(spec: Dict[str, Any]) -> str:
    """Stable hypothesis identity derived from substance, not a model-written title."""
    content = {
        "hypothesis": _normalize(spec.get("hypothesis")),
        "null_hypothesis": _normalize(spec.get("null_hypothesis")),
        "primary_metric": _normalize(spec.get("primary_metric") or spec.get("metric")),
        "falsifier": _normalize(spec.get("falsifier")),
    }
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def hypothesis_id(spec: Dict[str, Any]) -> str:
    return "hyp-" + semantic_fingerprint(spec)[:16]


def _latest_by_node(rows: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in list(rows) if rows is not None else events():
        node = str(row.get("node") or "")
        if node:
            latest[node] = row
    return latest


def register_hypothesis(
    spec: Dict[str, Any],
    discovery_evidence: Iterable[str],
    context_policy_id: Optional[str] = None,
    question_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Pre-register a hypothesis before a probe or validation cohort is observed."""
    value = dict(spec)
    value.setdefault(
        "null_hypothesis",
        "The proposed mechanism produces no measurable improvement in %s."
        % (value.get("primary_metric") or value.get("metric") or "the primary outcome"),
    )
    required = {
        "hypothesis": value.get("hypothesis"),
        "null_hypothesis": value.get("null_hypothesis"),
        "falsifier": value.get("falsifier"),
        "primary_metric": value.get("primary_metric") or value.get("metric"),
    }
    missing = sorted(key for key, item in required.items() if not str(item or "").strip())
    evidence = sorted(set(str(item) for item in discovery_evidence if str(item).strip()))
    if missing:
        raise ProvenanceError("hypothesis registration missing: %s" % ", ".join(missing))
    if not evidence:
        raise ProvenanceError("hypothesis registration requires discovery evidence")

    value["primary_metric"] = required["primary_metric"]
    identity = hypothesis_id(value)
    prior = [row for row in events() if row.get("node") == identity]
    latest = prior[-1] if prior else None
    if latest:
        prior_evidence = set(latest.get("discovery_evidence") or [])
        status = str(latest.get("status") or "registered")
        if status in TERMINAL_FAILURES and set(evidence).issubset(prior_evidence):
            return {
                "accepted": False,
                "duplicate": True,
                "id": identity,
                "reason": "failed hypothesis requires novel discovery evidence",
                "prior_status": status,
            }
        if status not in TERMINAL_FAILURES:
            return {
                "accepted": False,
                "duplicate": True,
                "id": identity,
                "reason": "hypothesis is already registered",
                "prior_status": status,
            }

    row = {
        "schema_version": SCHEMA_VERSION,
        "event": "hypothesis.registered",
        "ts": _utcnow(),
        "node": identity,
        "node_type": "hypothesis",
        "status": "registered",
        "semantic_fingerprint": semantic_fingerprint(value),
        "hypothesis": _bounded(value.get("hypothesis")),
        "null_hypothesis": _bounded(value.get("null_hypothesis")),
        "falsifier": _bounded(value.get("falsifier")),
        "primary_metric": _bounded(value.get("primary_metric")),
        "expected_improvement": value.get("expected_improvement"),
        "discovery_evidence": evidence,
        "question_ids": sorted(set(str(item) for item in (question_ids or []) if str(item).strip())),
        # Context is intentionally not a parent edge. A hypothesis may be discovered
        # while policy A is live without claiming that policy A proves the hypothesis.
        "context_policy_id": context_policy_id,
        "parents": ["evidence:" + item for item in evidence],
    }
    _append(row)
    validate_graph()
    return {"accepted": True, "duplicate": False, "id": identity, "record": row}


def record_result(
    identity: str,
    status: str,
    validation_evidence: Iterable[str],
    evidence_class: str,
    effect: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if evidence_class not in VALID_EVIDENCE_CLASSES:
        raise ProvenanceError("validation evidence class is not causal: %s" % evidence_class)
    refs = sorted(set(str(item) for item in validation_evidence if str(item).strip()))
    if not refs:
        raise ProvenanceError("validation result requires evidence references")
    if identity not in _latest_by_node():
        raise ProvenanceError("unknown hypothesis: %s" % identity)
    node = "result-" + hashlib.sha256(
        (identity + "\n" + "\n".join(refs)).encode("utf-8")
    ).hexdigest()[:16]
    row = {
        "schema_version": SCHEMA_VERSION,
        "event": "hypothesis.result",
        "ts": _utcnow(),
        "node": node,
        "node_type": "validation_result",
        "hypothesis_id": identity,
        "status": status,
        "evidence_class": evidence_class,
        "validation_evidence": refs,
        "effect": _bounded(effect or {}),
        "parents": [identity] + ["evidence:" + item for item in refs],
    }
    _append(row)
    # Update the hypothesis' latest status without mutating its registration.
    _append({
        "schema_version": SCHEMA_VERSION,
        "event": "hypothesis.status",
        "ts": _utcnow(),
        "node": identity,
        "node_type": "hypothesis",
        "status": status,
        "result_node": node,
        "discovery_evidence": next(
            (item.get("discovery_evidence") or [] for item in reversed(events())
             if item.get("node") == identity and item.get("event") == "hypothesis.registered"),
            [],
        ),
        # Status is an annotation on the immutable hypothesis node. The result
        # already depends on the hypothesis; adding the reverse edge here would
        # manufacture a cycle where none exists in the decision lineage.
        "parents": [],
    })
    validate_graph()
    return row


def latest_result(identity: str) -> Optional[Dict[str, Any]]:
    """Return the newest durable validation result for one hypothesis."""
    matches = [
        row for row in events()
        if row.get("event") == "hypothesis.result" and row.get("hypothesis_id") == identity
    ]
    return matches[-1] if matches else None


def graph(rows: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Set[str]]:
    adjacency: Dict[str, Set[str]] = {}
    for row in list(rows) if rows is not None else events():
        node = str(row.get("node") or "")
        if not node:
            continue
        adjacency.setdefault(node, set())
        for parent in row.get("parents") or []:
            parent = str(parent)
            if not parent.startswith("evidence:"):
                adjacency.setdefault(parent, set()).add(node)
    return adjacency


def validate_graph(rows: Optional[Sequence[Dict[str, Any]]] = None) -> None:
    adjacency = graph(rows)
    visiting: Set[str] = set()
    visited: Set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ProvenanceError("provenance cycle detected at %s" % node)
        if node in visited:
            return
        visiting.add(node)
        for child in adjacency.get(node, set()):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(adjacency):
        visit(node)


def _policy_ids(policy_events: Sequence[Dict[str, Any]]) -> List[str]:
    identities: List[str] = []
    for row in policy_events:
        if row.get("event") != "promoted" or not row.get("policy_id"):
            continue
        identity = str(row["policy_id"])
        if not identities or identities[-1] != identity:
            identities.append(identity)
    return identities


def policy_oscillation(candidate_policy_id: str, policy_events: Sequence[Dict[str, Any]]) -> Optional[str]:
    identities = _policy_ids(policy_events)
    if not identities or candidate_policy_id == identities[-1]:
        return None
    recent = identities[-6:]
    if candidate_policy_id in recent:
        chain = recent + [candidate_policy_id]
        return "policy oscillation paused: %s" % " -> ".join(chain)
    return None


def validate_promotion_contract(
    contract: Optional[Dict[str, Any]],
    candidate_policy_id: str,
    previous_policy_id: Optional[str],
    policy_events: Sequence[Dict[str, Any]],
) -> List[str]:
    """Return deterministic promotion errors; an unchanged policy needs no new trial."""
    if not previous_policy_id or candidate_policy_id == previous_policy_id:
        return []
    errors: List[str] = []
    oscillation = policy_oscillation(candidate_policy_id, policy_events)
    if oscillation:
        errors.append(oscillation)
    value = contract or {}
    required = (
        "hypothesis_id", "null_hypothesis", "falsifier", "primary_metric",
        "evidence_class", "expected_improvement", "discovery_evidence", "validation_evidence",
    )
    for key in required:
        item = value.get(key)
        if item is None or item == "" or item == []:
            errors.append("promotion contract missing: %s" % key)
    evidence_class = value.get("evidence_class")
    if evidence_class and evidence_class not in VALID_EVIDENCE_CLASSES:
        errors.append("observational evidence may open a probe but cannot promote policy")
    discovery = set(str(item) for item in value.get("discovery_evidence") or [])
    validation = set(str(item) for item in value.get("validation_evidence") or [])
    overlap = sorted(discovery.intersection(validation))
    if overlap:
        errors.append("discovery and validation evidence overlap: %s" % ", ".join(overlap[:3]))
    try:
        if value.get("expected_improvement") is not None and float(value["expected_improvement"]) <= 0:
            errors.append("expected improvement must be positive and predeclared")
    except (TypeError, ValueError):
        errors.append("expected improvement must be numeric")
    identity = str(value.get("hypothesis_id") or "")
    registered = [
        row for row in events()
        if row.get("node") == identity and row.get("event") == "hypothesis.registered"
    ]
    if identity and not registered:
        errors.append("promotion hypothesis was not pre-registered: %s" % identity)
    elif registered:
        registered_discovery = set(registered[-1].get("discovery_evidence") or [])
        if discovery != registered_discovery:
            errors.append("promotion discovery cohort differs from pre-registration")
        supporting_results = [
            row for row in events()
            if row.get("event") == "hypothesis.result"
            and row.get("hypothesis_id") == identity
            and row.get("status") in {"supported", "accepted", "passed"}
            and row.get("evidence_class") == evidence_class
            and set(str(item) for item in row.get("validation_evidence") or []) == validation
        ]
        if not supporting_results:
            errors.append("promotion has no durable supporting validation result")
    validate_graph()
    return sorted(set(errors))


def record_policy_promotion(
    candidate_policy_id: str,
    previous_policy_id: Optional[str],
    contract: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    value = contract or {}
    validation = sorted(set(str(item) for item in value.get("validation_evidence") or []))
    row = {
        "schema_version": SCHEMA_VERSION,
        "event": "policy.promoted",
        "ts": _utcnow(),
        "node": candidate_policy_id,
        "node_type": "policy",
        "previous_policy_id": previous_policy_id,
        "hypothesis_id": value.get("hypothesis_id"),
        "evidence_class": value.get("evidence_class"),
        "validation_evidence": validation,
        "parents": ([str(value["hypothesis_id"])] if value.get("hypothesis_id") else [])
        + ["evidence:" + item for item in validation],
    }
    # Check the proposed edge set before making it durable. A rejected cycle must
    # not poison the append-only graph it was supposed to protect.
    validate_graph(events() + [row])
    _append(row)
    return row


def reconcile_workorders(path: Optional[str] = None) -> Dict[str, Any]:
    """Attach lineage to strategy proposals created before provenance existed."""
    from . import workorders

    queue = path or workorders.QUEUE
    migrated = 0
    enriched = 0
    errors: List[str] = []
    orders = list(workorders.current(queue).values())
    for order in orders:
        try:
            if workorders.ensure_probe_path(str(order.get("id")), queue):
                enriched += 1
        except Exception as exc:  # noqa: BLE001
            errors.append("%s path: %s" % (order.get("id"), str(exc)[:140]))
    for order in orders:
        if order.get("kind") != "strategy_hypothesis" or order.get("provenance"):
            continue
        if order.get("status") in workorders.TERMINAL:
            continue
        detail = order.get("detail") or {}
        if not isinstance(detail, dict):
            detail = {}
        metric = str(detail.get("metric") or "declared primary outcome")
        spec = {
            "hypothesis": detail.get("hypothesis") or order.get("summary"),
            "null_hypothesis": detail.get("null_hypothesis")
            or "The proposed mechanism produces no measurable improvement in %s." % metric,
            "falsifier": detail.get("falsifier")
            or "The bounded probe fails the work order acceptance criteria.",
            "primary_metric": metric,
            "expected_improvement": detail.get("expected_improvement"),
        }
        discovery = ["workorder:%s" % order.get("id")]
        try:
            registered = register_hypothesis(spec, discovery)
            identity = str(registered.get("id") or hypothesis_id(spec))
            contract = dict(spec)
            contract.update({
                "hypothesis_id": identity,
                "discovery_evidence": discovery,
                "question_ids": [],
                "migration": "pre-lineage work order",
            })
            workorders.attach_provenance(str(order.get("id")), contract, queue)
            migrated += 1
        except Exception as exc:  # noqa: BLE001 - one legacy row must not block others
            errors.append("%s: %s" % (order.get("id"), str(exc)[:160]))
    return {"migrated": migrated, "probe_paths_enriched": enriched, "errors": errors}


def status() -> Dict[str, Any]:
    rows = events()
    latest = _latest_by_node(rows)
    hypotheses = [row for row in latest.values() if row.get("node_type") == "hypothesis"]
    validate_graph(rows)
    return {
        "events": len(rows),
        "nodes": len(latest),
        "hypotheses": len(hypotheses),
        "failed_hypotheses": sum(1 for row in hypotheses if row.get("status") in TERMINAL_FAILURES),
        "graph_valid": True,
    }
