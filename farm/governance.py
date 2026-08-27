"""Periodic deterministic review of whether autonomous operation is closing its loops.

This is deliberately not a model prompt. Every review is computed from durable local
state, has explicit pass/warn/fail contracts, records regressions and recoveries, and
uses only bounded remediations already owned by the safety kernel. The model remains
an exception-path author/research tool behind work orders, gates, and canaries.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import analysis, compaction, control, rules

SCHEMA_VERSION = 1
PASS = "pass"
WARN = "warn"
FAIL = "fail"
_SEVERITY = {PASS: 0, WARN: 1, FAIL: 2}


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else Path(__file__).resolve().parent.parent / "state"


def _ledger() -> Path:
    return Path(os.environ.get(
        "FARM_GOVERNANCE_LOG", str(_state_dir() / "governance_reviews.ndjson")
    ))


def _lock_path() -> Path:
    return Path(str(_ledger()) + ".lock")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_seconds(ts: Any) -> Optional[int]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def rows(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return analysis.read_ndjson(_ledger(), limit=limit)


def latest() -> Dict[str, Any]:
    history = rows(limit=1)
    return history[-1] if history else {}


def due(run: Optional[int], last: Optional[Dict[str, Any]] = None) -> bool:
    if not isinstance(run, int) or run <= 0:
        return False
    previous = last if last is not None else latest()
    last_run = previous.get("run")
    return not isinstance(last_run, int) or run - last_run >= rules.GOVERNANCE_REVIEW_RUNS


def _check(
    identity: str,
    status: str,
    summary: str,
    evidence: Optional[Dict[str, Any]] = None,
    owner: str = "supervisor",
) -> Dict[str, Any]:
    if status not in _SEVERITY:
        raise ValueError("invalid governance status: %s" % status)
    return {
        "id": identity,
        "status": status,
        "summary": str(summary)[:500],
        "evidence": evidence or {},
        "owner": owner,
    }


def collect_snapshot(run: int) -> Dict[str, Any]:
    """Read bounded local state only; no MCP or model calls."""
    from . import canary, claims, evaluation, policy, provenance, questions, scheduler, workorders

    state = _state_dir()
    history = analysis.history_rows(limit=rules.GOVERNANCE_REVIEW_RUNS + 1)
    services = []
    for spec in control.SERVICES:
        service = dict(spec)
        service.update(scheduler.status(spec["label"]))
        services.append(service)

    canary_store = str(state / "canary.json")
    dashboard_rows = analysis.read_ndjson(state / "dashboard_health.ndjson", limit=1)
    experiments = analysis.read_ndjson(state / "experiments.ndjson", limit=200)
    question_rows = questions.load_all()
    order_rows = list(workorders.current().values())
    live_release = control.project_root() / "release"
    live_revision = os.path.basename(os.path.realpath(str(live_release)))

    return {
        "run": run,
        "history": history,
        "services": services,
        "canary": canary.status(canary_store, str(state / "history.ndjson")),
        "compaction": compaction.state_status(state),
        "compaction_compatibility": compaction.compatibility(state),
        "live_revision": live_revision,
        "policy": policy.runtime_context(claims.load()),
        "dashboard": dashboard_rows[-1] if dashboard_rows else {},
        "questions": question_rows,
        "experiments": experiments,
        "orders": [dict(order, age_seconds=_age_seconds(order.get("ts"))) for order in order_rows],
        "efficacy": evaluation.status(canary_store),
        "lineage": provenance.status(),
    }


def assess(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pure contract evaluation, suitable for replay and release tests."""
    run = snapshot.get("run")
    checks: List[Dict[str, Any]] = []

    history = [row for row in snapshot.get("history") or [] if isinstance(row, dict)]
    first = history[0] if history else {}
    last = history[-1] if history else {}
    before_score, after_score = first.get("produce"), last.get("produce")
    if len(history) < 2:
        checks.append(_check(
            "execution.progress", FAIL, "fewer than two durable run rows are available",
            {"rows": len(history)}, "cycle",
        ))
    elif isinstance(before_score, (int, float)) and isinstance(after_score, (int, float)):
        gain = after_score - before_score
        checks.append(_check(
            "execution.progress", PASS if gain > 0 else FAIL,
            "lifetime produce advanced" if gain > 0 else "lifetime produce did not advance",
            {"rows": len(history), "gain": gain, "from_run": first.get("run"), "to_run": last.get("run")},
            "cycle",
        ))
    else:
        checks.append(_check(
            "execution.progress", WARN, "run history lacks comparable objective counters",
            {"rows": len(history)}, "cycle",
        ))

    rank = last.get("rank")
    checks.append(_check(
        "strategy.objective", PASS if rank == 1 else FAIL if isinstance(rank, int) else WARN,
        "objective position is rank 1" if rank == 1 else "objective position is not rank 1",
        {"rank": rank, "run": last.get("run")}, "research",
    ))

    services = snapshot.get("services") or []
    down = [row.get("key") for row in services if not row.get("loaded")]
    checks.append(_check(
        "runtime.services", PASS if services and not down else FAIL,
        "all required services are loaded" if services and not down else "required services are missing",
        {"expected": len(control.SERVICES), "observed": len(services), "down": down}, "supervisor",
    ))

    canary_state = snapshot.get("canary") or {}
    verdict = canary_state.get("verdict") or {}
    active = bool(canary_state.get("armed"))
    observed = int(verdict.get("runs_observed") or 0)
    overdue = active and observed > rules.EFFICACY_MIN_RUNS * 2
    checks.append(_check(
        "release.probation", FAIL if overdue else PASS,
        "release probation is bounded" if not overdue else "release probation exceeded its evidence window",
        {
            "active": active, "status": canary_state.get("status"),
            "revision": canary_state.get("revision"), "runs_observed": observed,
        },
        "supervisor",
    ))

    compaction_rows = snapshot.get("compaction") or []
    oversized = [
        row for row in compaction_rows
        if int(row.get("active_bytes") or 0) >= compaction.DEFAULT_MAX_BYTES
    ]
    compatibility = snapshot.get("compaction_compatibility") or {}
    compatible = compatibility.get("revision") == snapshot.get("live_revision")
    if not oversized:
        compaction_status = PASS
        compaction_summary = "hot evidence ledgers are bounded"
    elif active:
        compaction_status = WARN
        compaction_summary = "compaction debt is safely deferred during release probation"
    elif compatible:
        compaction_status = FAIL
        compaction_summary = "accepted reader has unresolved compaction debt"
    else:
        compaction_status = FAIL
        compaction_summary = "compaction compatibility boundary is not established"
    checks.append(_check(
        "evidence.compaction", compaction_status, compaction_summary,
        {
            "oversized": [row.get("ledger") for row in oversized],
            "largest_bytes": max((int(row.get("active_bytes") or 0) for row in compaction_rows), default=0),
            "compatibility_revision": compatibility.get("revision"),
            "live_revision": snapshot.get("live_revision"),
        },
        "supervisor",
    ))

    policy_state = snapshot.get("policy") or {}
    checks.append(_check(
        "knowledge.policy", PASS if policy_state.get("compatible") else FAIL,
        "runtime policy and claims are compatible" if policy_state.get("compatible")
        else "runtime policy or claim dependencies drifted",
        {"policy_id": policy_state.get("policy_id"), "errors": policy_state.get("errors") or []},
        "research",
    ))

    dashboard = snapshot.get("dashboard") or {}
    staleness = dashboard.get("staleness") or {}
    freshness_error = next(
        (value for key, value in staleness.items() if str(key).endswith("_error")), None
    )
    dashboard_ok = bool(dashboard.get("ok")) and not freshness_error
    checks.append(_check(
        "observability.dashboard", PASS if dashboard_ok else FAIL,
        "dashboard content and freshness are verified" if dashboard_ok
        else "dashboard health or freshness is not verified",
        {"ts": dashboard.get("ts"), "problems": dashboard.get("problems") or [], "freshness_error": freshness_error},
        "dashboard",
    ))

    question_rows = snapshot.get("questions") or []
    active_questions = [row for row in question_rows if row.get("status") in {"open", "probing"}]
    aged = [
        row for row in active_questions
        if isinstance(run, int) and isinstance(row.get("opened_run"), int)
        and run - int(row["opened_run"]) >= rules.GOVERNANCE_REVIEW_RUNS * 2
        and row.get("priority") in {"high", "critical"}
    ]
    recent_linked = [
        row for row in snapshot.get("experiments") or []
        if isinstance(run, int) and isinstance(row.get("run"), int)
        and run - int(row["run"]) <= rules.GOVERNANCE_REVIEW_RUNS * 2
        and row.get("status") == "passed" and row.get("question_ids")
    ]
    if aged and not recent_linked:
        learning_status = FAIL
        learning_summary = "high-priority questions are aging without linked probe results"
    elif aged:
        learning_status = WARN
        learning_summary = "high-priority questions remain open while probes are producing linked results"
    else:
        learning_status = PASS
        learning_summary = "question and probe flow has no aged high-priority backlog"
    checks.append(_check(
        "learning.question_flow", learning_status, learning_summary,
        {
            "open": len(active_questions), "probing": sum(row.get("status") == "probing" for row in active_questions),
            "aged_high_priority": [row.get("id") for row in aged[:12]],
            "recent_linked_probe_results": len(recent_linked),
        },
        "research",
    ))

    pending_repairs = [
        row for row in snapshot.get("orders") or []
        if row.get("status") in {"open", "claimed"}
        and row.get("severity") in {"breaking", "shape", "degraded"}
    ]
    stalled_repairs = [
        row for row in pending_repairs if int(row.get("age_seconds") or 0) > 7200
    ]
    checks.append(_check(
        "healing.repair_flow", FAIL if stalled_repairs else WARN if pending_repairs else PASS,
        "repair queue is clear" if not pending_repairs else
        "repair queue is moving" if not stalled_repairs else "repair queue has stalled",
        {
            "pending": [row.get("id") for row in pending_repairs[:12]],
            "stalled": [row.get("id") for row in stalled_repairs[:12]],
        },
        "author",
    ))

    efficacy = snapshot.get("efficacy") or {}
    champion = efficacy.get("champion") or {}
    lineage = snapshot.get("lineage") or {}
    strategy_orders = [
        row for row in snapshot.get("orders") or []
        if row.get("kind") == "strategy_hypothesis"
        and row.get("status") not in {"published", "abandoned", "superseded"}
    ]
    missing_lineage = [row.get("id") for row in strategy_orders if not row.get("provenance")]
    lineage_ok = bool(champion) and lineage.get("graph_valid") is True and not missing_lineage
    checks.append(_check(
        "safety.lineage", PASS if lineage_ok else FAIL,
        "champion and strategy lineage are established" if lineage_ok
        else "champion or strategy lineage is incomplete",
        {
            "champion": champion.get("revision"), "nodes": lineage.get("nodes"),
            "graph_valid": lineage.get("graph_valid"), "missing_orders": missing_lineage,
        },
        "supervisor",
    ))
    return checks


def _trend(checks: List[Dict[str, Any]], previous: Dict[str, Any]) -> Dict[str, List[str]]:
    before = {row.get("id"): row.get("status") for row in previous.get("checks") or []}
    regressions: List[str] = []
    recoveries: List[str] = []
    for row in checks:
        identity, status = row.get("id"), row.get("status")
        old = before.get(identity)
        if old in _SEVERITY and _SEVERITY.get(status, 0) > _SEVERITY[old]:
            regressions.append(str(identity))
        if old in _SEVERITY and _SEVERITY.get(status, 0) < _SEVERITY[old]:
            recoveries.append(str(identity))
    return {"regressions": regressions, "recoveries": recoveries}


def remediate(snapshot: Dict[str, Any], checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply only pre-declared, bounded recovery paths; never invent strategy."""
    from . import canary, questions, scheduler, workorders

    by_id = {row["id"]: row for row in checks}
    actions: List[Dict[str, Any]] = []

    for service in snapshot.get("services") or []:
        if service.get("loaded"):
            continue
        result = scheduler.ensure(str(service.get("label") or ""))
        actions.append({"action": "ensure_service", "target": service.get("key"), "result": result})

    released = workorders.release_stale(max_age_seconds=3600)
    if released:
        actions.append({"action": "release_stale_claims", "count": len(released)})

    storage = by_id.get("evidence.compaction") or {}
    canary_state = snapshot.get("canary") or {}
    compatibility = snapshot.get("compaction_compatibility") or {}
    if (storage.get("status") == FAIL and not canary_state.get("armed")
            and compatibility.get("revision") == snapshot.get("live_revision")):
        results = compaction.maintain(_state_dir())
        actions.append({
            "action": "retry_compaction",
            "compacted": [row.get("ledger") for row in results if row.get("compacted")],
        })

    learning = by_id.get("learning.question_flow") or {}
    if learning.get("status") == FAIL:
        opened = questions.open_or_update(
            "strategy_stale",
            "STRATEGY STALE: governance review found high-priority questions aging without linked probe results",
            item={"run": snapshot.get("run"), "ts": _utcnow()},
            subject="governance learning loop",
            decision_bundle=learning.get("evidence") or {},
            evidence_refs=["governance_reviews.ndjson#run=%s" % snapshot.get("run")],
        )
        actions.append({
            "action": "route_learning_review", "question_id": opened["question"].get("id"),
            "opened": bool(opened.get("opened") or opened.get("reopened")),
        })
    return actions


def run_review(
    run: int,
    force: bool = False,
    snapshot: Optional[Dict[str, Any]] = None,
    apply_remediation: bool = True,
) -> Dict[str, Any]:
    """Record one due review under a process lock, retrying missed boundaries."""
    path = _ledger()
    lock_path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous = latest()
        if not force and not due(run, previous):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return {
                "recorded": False, "run": run, "last_run": previous.get("run"),
                "next_run": (previous.get("run") or run) + rules.GOVERNANCE_REVIEW_RUNS,
            }
        current = snapshot if snapshot is not None else collect_snapshot(run)
        checks = assess(current)
        actions = remediate(current, checks) if apply_remediation else []
        trend = _trend(checks, previous)
        worst = max((_SEVERITY[row["status"]] for row in checks), default=0)
        status = next(name for name, value in _SEVERITY.items() if value == worst)
        record = {
            "schema_version": SCHEMA_VERSION,
            "ts": _utcnow(),
            "run": run,
            "cadence_runs": rules.GOVERNANCE_REVIEW_RUNS,
            "status": status,
            "checks": checks,
            "summary": {
                "pass": sum(row["status"] == PASS for row in checks),
                "warn": sum(row["status"] == WARN for row in checks),
                "fail": sum(row["status"] == FAIL for row in checks),
            },
            "regressions": trend["regressions"],
            "recoveries": trend["recoveries"],
            "actions": actions,
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, allow_nan=False, default=str) + "\n")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return dict(record, recorded=True)


def status(current_run: Optional[int] = None) -> Dict[str, Any]:
    record = latest()
    run = current_run
    if run is None:
        history = analysis.history_rows(limit=1)
        run = history[-1].get("run") if history else None
    last_run = record.get("run")
    return {
        "last": record,
        "last_run": last_run,
        "current_run": run,
        "cadence_runs": rules.GOVERNANCE_REVIEW_RUNS,
        "due": due(run, record),
        "next_run": (last_run + rules.GOVERNANCE_REVIEW_RUNS)
        if isinstance(last_run, int) else run,
    }
