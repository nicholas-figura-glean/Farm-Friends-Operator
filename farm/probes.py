"""Budgeted probe execution under the existing farm mutation lock."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import analysis, compaction, provenance, questions, rules

PROJECT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1


def _registry() -> Dict[str, Dict[str, Any]]:
    from experiments.registry import PROBES
    return {name: dict(value) for name, value in PROBES.items()}


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else PROJECT / "state"


def _ledger() -> Path:
    return Path(os.environ.get("FARM_EXPERIMENT_LOG", str(_state_dir() / "experiments.ndjson")))


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _append(row: Dict[str, Any]) -> None:
    path = _ledger()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(row, sort_keys=True, allow_nan=False, default=str) + "\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def list_probes() -> List[Dict[str, Any]]:
    return [dict({"id": name}, **value) for name, value in sorted(_registry().items())]


def _command(spec: Dict[str, Any]) -> List[str]:
    raw = list(spec.get("command") or [])
    if not raw:
        raise ValueError("probe has no command")
    script = PROJECT / raw[0]
    if not script.exists():
        raise ValueError("probe script missing: %s" % raw[0])
    return [sys.executable, str(script)] + raw[1:]


def _ensure_registration(spec: Dict[str, Any], question_ids: List[str], probe_id: str) -> None:
    identity = str(spec.get("hypothesis_id") or "")
    if not identity:
        return
    if any(
        row.get("event") == "hypothesis.registered" and row.get("node") == identity
        for row in provenance.events()
    ):
        return
    registration = provenance.register_hypothesis(
        spec,
        ["question:%s" % value for value in question_ids] or ["explicit-probe:%s" % probe_id],
        question_ids=question_ids,
    )
    if registration.get("id") != identity:
        raise provenance.ProvenanceError(
            "probe %s declares hypothesis %s but semantics resolve to %s"
            % (probe_id, identity, registration.get("id"))
        )


def _attributed_calls(execution_id: str) -> int:
    """Count only MCP telemetry emitted by this exact probe execution."""
    tool_log = _state_dir() / "tool_calls.ndjson"
    return sum(
        1 for row in compaction.read_rows(tool_log, limit=10_000)
        if row.get("probe_id") == execution_id
    )


def _finish_questions(
    question_ids: List[str],
    probe_id: str,
    run: Optional[int],
    result: Dict[str, Any],
) -> None:
    status = str(result.get("status") or "failed")
    hypothesis_id = str(result.get("hypothesis_id") or "")
    hypothesis_result = provenance.latest_result(hypothesis_id) if hypothesis_id else None
    evidence_ref = "probe:%s:%s" % (probe_id, result.get("started_ts") or result.get("ts"))
    refs = [evidence_ref]
    destination = result.get("evidence_destination")
    if destination:
        refs.append(str(destination))

    supported = {"supported", "accepted", "passed", "falsified", "rejected"}
    base_result_status = str((hypothesis_result or {}).get("status") or status)
    base_settled = status == "passed" and (not hypothesis_id or base_result_status in supported)
    question_map = {row.get("id"): row for row in questions.load_all()}
    policy_reconciliation: Optional[Dict[str, Any]] = None
    for question_id in question_ids:
        question = question_map.get(question_id) or {}
        result_status = base_result_status
        settled = base_settled
        # A policy audit exiting zero means only that it completed. It may not
        # close a policy-drift question until rebuilding the claims restores the
        # exact promoted policy fingerprint. This prevents a successful local
        # probe from hiding an unresolved runtime incompatibility.
        if question.get("class") == "policy_drift" and status == "passed":
            if policy_reconciliation is None:
                from . import claims, policy
                registry = claims.refresh()
                policy_reconciliation = policy.runtime_context(registry)
            settled = bool(policy_reconciliation.get("compatible"))
            result_status = "compatible" if settled else "incompatible"
        if settled:
            answer = "Probe %s completed with durable result %s." % (probe_id, result_status)
            questions.set_status(
                question_id, "answered", answer=answer, evidence_refs=refs,
                run=run, probe_id=probe_id, result_status=result_status,
            )
        else:
            reason = result.get("reason") or result_status
            if policy_reconciliation and not policy_reconciliation.get("compatible"):
                reason = "; ".join(policy_reconciliation.get("errors") or []) or reason
            answer = "Probe %s did not settle the question: %s." % (probe_id, reason)
            questions.set_status(
                question_id, "open", answer=answer, evidence_refs=refs,
                run=run, probe_id=probe_id, result_status=result_status,
            )


def run_probe(
    probe_id: str,
    explicit: bool = False,
    run: Optional[int] = None,
    question_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    registry = _registry()
    if probe_id not in registry:
        raise ValueError("unknown probe: %s" % probe_id)
    spec = registry[probe_id]
    if not explicit and (not spec.get("read_only") or not spec.get("autonomous")):
        raise ValueError("probe %s requires explicit invocation" % probe_id)
    bound_questions = sorted(set(str(value) for value in (question_ids or []) if value))
    _ensure_registration(spec, bound_questions, probe_id)

    lock_path = _state_dir() / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                result = {
                    "schema_version": SCHEMA_VERSION,
                    "ts": _utcnow(),
                    "run": run,
                    "probe_id": probe_id,
                    "status": "skipped",
                    "reason": "farm cycle holds the mutation lock",
                    "budget": spec.get("budget"),
                }
                _append(result)
                return result
            raise

        started = _utcnow()
        execution_id = "%s:%d:%d" % (probe_id, os.getpid(), time.time_ns())
        for question_id in bound_questions:
            questions.set_status(question_id, "probing", run=run, probe_id=probe_id)
        hypothesis_id = str(spec.get("hypothesis_id") or "")
        result_count_before = sum(
            1 for item in provenance.events()
            if item.get("event") == "hypothesis.result"
            and item.get("hypothesis_id") == hypothesis_id
        ) if hypothesis_id else 0
        budget = dict(spec.get("budget") or {})
        timeout = max(1, int(budget.get("wall_seconds") or 60))
        env = dict(os.environ)
        env["FARM_PROBE_ID"] = execution_id
        env["FARM_TOOL_CALL_LOG"] = str(_state_dir() / "tool_calls.ndjson")
        if hypothesis_id:
            env["FARM_HYPOTHESIS_ID"] = hypothesis_id
            env["FARM_EVIDENCE_CLASS"] = str(spec.get("evidence_class") or "holdout")
        try:
            completed = subprocess.run(
                _command(spec),
                cwd=str(PROJECT),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            status = "passed" if completed.returncode == 0 else "failed"
            output = (_text(completed.stdout) + _text(completed.stderr))[-8_000:]
            returncode = completed.returncode
            reason = None
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            output = (_text(exc.stdout) + _text(exc.stderr))[-8_000:]
            returncode = None
            reason = "wall-time budget exceeded"
        calls = _attributed_calls(execution_id)
        call_budget = max(0, int(budget.get("calls") or 0))
        if calls > call_budget:
            status = "budget_violation"
            reason = "probe made %d attributed MCP calls against budget %d" % (calls, call_budget)
        if status == "passed" and hypothesis_id:
            result_count_after = sum(
                1 for item in provenance.events()
                if item.get("event") == "hypothesis.result"
                and item.get("hypothesis_id") == hypothesis_id
            )
            if result_count_after <= result_count_before:
                status = "evidence_missing"
                reason = "hypothesis-linked probe did not record a validation result"
        result = {
            "schema_version": SCHEMA_VERSION,
            "ts": _utcnow(),
            "started_ts": started,
            "run": run,
            "probe_id": probe_id,
            "execution_id": execution_id,
            "question_ids": bound_questions,
            "status": status,
            "returncode": returncode,
            "reason": reason,
            "read_only": bool(spec.get("read_only")),
            "explicit": bool(explicit),
            "hypothesis": spec.get("hypothesis"),
            "hypothesis_id": hypothesis_id or None,
            "evidence_class": spec.get("evidence_class"),
            "budget": budget,
            "calls": calls,
            "stop_condition": spec.get("stop_condition"),
            "evidence_destination": spec.get("evidence_destination"),
            "output": output,
        }
        _append(result)
        _finish_questions(bound_questions, probe_id, run, result)
        return result
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_handle.close()


def _recent_events() -> List[Dict[str, Any]]:
    return analysis.read_ndjson(_ledger())


def maybe_run(open_questions: List[Dict[str, Any]], run: Optional[int]) -> Optional[Dict[str, Any]]:
    """Run at most one autonomous read-only probe.

    Remote probes retain the global cadence because calls and rival inspection
    are scarce. Pure local replays cost no calls and may settle a fail-closed
    novelty hold immediately; making them wait twenty runs would turn adaptation
    into an hour-long outage for no safety benefit.
    """
    if not isinstance(run, int) or not open_questions:
        return None
    events = _recent_events()
    last_run = max(
        [
            event.get("run") for event in events
            if isinstance(event.get("run"), int) and event.get("status") != "skipped"
        ],
        default=None,
    )
    throttled = bool(
        isinstance(last_run, int) and run - last_run < rules.PROBE_MIN_INTERVAL_RUNS
    )
    for probe_id, spec in sorted(_registry().items()):
        if not spec.get("read_only") or not spec.get("autonomous"):
            continue
        call_budget = int((spec.get("budget") or {}).get("calls") or 0)
        if throttled and call_budget > 0:
            continue
        allowed_classes = set(spec.get("question_classes") or [])
        subject_patterns = [str(value).lower() for value in spec.get("subject_patterns") or []]
        matching = []
        for question in open_questions:
            if question.get("class") not in allowed_classes:
                continue
            subject = "%s %s" % (question.get("subject") or "", question.get("key") or "")
            if subject_patterns and not any(pattern in subject.lower() for pattern in subject_patterns):
                continue
            matching.append(question)
        if matching:
            return run_probe(
                probe_id, explicit=False, run=run,
                question_ids=[str(question.get("id")) for question in matching if question.get("id")],
            )
    return None
