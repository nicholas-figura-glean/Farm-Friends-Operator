"""Budgeted probe execution under the existing farm mutation lock."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import analysis, compaction, mcp, probe_guard, provenance, questions, rules, sandbox

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


def _tail_file(path: Path, limit: int = 4_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max(1, int(limit))))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


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
    relative = str(raw[0]).replace("\\", "/")
    if relative.startswith("/") or ".." in relative.split("/"):
        raise ValueError("probe command escapes the project: %s" % relative)
    candidate = PROJECT / relative
    if candidate.is_symlink():
        raise ValueError("probe script is symlinked: %s" % relative)
    script = candidate.resolve()
    try:
        script.relative_to(PROJECT.resolve())
    except ValueError as exc:
        raise ValueError("probe command escapes the project: %s" % relative) from exc
    if not script.is_file():
        raise ValueError("probe script missing or unsafe: %s" % relative)
    return [sys.executable, str(script)] + [str(value) for value in raw[1:]]


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
    """Legacy diagnostic count; authoritative usage comes from the parent grant."""
    tool_log = _state_dir() / "tool_calls.ndjson"
    return sum(
        1 for row in compaction.read_rows(tool_log, limit=10_000)
        if row.get("probe_id") == execution_id and row.get("event") == "start"
        and row.get("tool") != "tools/list"
    )


def _outputs(spec: Dict[str, Any]) -> List[str]:
    names = sorted(set(str(value) for value in (spec.get("outputs") or []) if value))
    for name in names:
        path = Path(name)
        if path.is_absolute() or len(path.parts) != 1 or ".." in path.parts:
            raise ValueError("probe output escapes projected state: %s" % name)
        if path.suffix not in {".json", ".ndjson"}:
            raise ValueError("unsupported probe output type: %s" % name)
    return names


def _validated_provenance_result(
    projection: Dict[str, Any],
    hypothesis_id: str,
    spec: Dict[str, Any],
    declared_outputs: List[str],
) -> Optional[Dict[str, Any]]:
    """Validate a worker claim and derive evidence identity from admitted bytes."""
    if not hypothesis_id:
        return None
    root = Path(projection["root"])
    path = root / "provenance.ndjson"
    baseline = (projection.get("baselines") or {}).get("provenance.ndjson") or {}
    size = int(baseline.get("size") or 0)
    if not path.is_file() or path.is_symlink():
        raise sandbox.ResultValidationError("probe provenance result is missing or unsafe")
    data = path.read_bytes()
    if (len(data) < size
            or hashlib.sha256(data[:size]).hexdigest() != baseline.get("sha256")):
        raise sandbox.ResultValidationError("probe rewrote projected provenance")
    rows = []
    for line in data[size:].decode("utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict) and value.get("event") == "hypothesis.result":
                rows.append(value)
    matching = [row for row in rows if row.get("hypothesis_id") == hypothesis_id]
    if len(matching) > 1:
        raise sandbox.ResultValidationError("probe emitted multiple hypothesis results")
    if not matching:
        return None
    row = matching[0]
    status = str(row.get("status") or "")
    if status not in {"supported", "falsified", "rejected", "inconclusive"}:
        raise sandbox.ResultValidationError("probe emitted an invalid result status")
    evidence_class = str(spec.get("evidence_class") or "")
    if not evidence_class or row.get("evidence_class") != evidence_class:
        raise sandbox.ResultValidationError("probe cannot choose its evidence class")
    effect = row.get("effect") if isinstance(row.get("effect"), dict) else {}
    try:
        encoded_effect = json.dumps(effect, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise sandbox.ResultValidationError("probe effect is not strict JSON") from exc
    if len(encoded_effect) > 20_000:
        raise sandbox.ResultValidationError("probe effect exceeds its size limit")
    refs: List[str] = []
    for name in declared_outputs:
        # JSON results are canonicalized by admit_outputs before persistence. Hash
        # that exact representation rather than worker formatting; NDJSON ledgers
        # are supporting audit streams, not the content-addressed result object.
        if not name.endswith(".json") or name == "provenance.ndjson":
            continue
        output = root / name
        if output.is_file() and not output.is_symlink():
            try:
                value = json.loads(output.read_text(encoding="utf-8"))
                canonical = (
                    json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
                ).encode("utf-8")
            except (OSError, TypeError, ValueError) as exc:
                raise sandbox.ResultValidationError("probe evidence JSON is invalid") from exc
            refs.append("state/%s#sha256=%s" % (
                name, hashlib.sha256(canonical).hexdigest(),
            ))
    if not refs:
        raise sandbox.ResultValidationError("hypothesis result has no declared JSON evidence output")
    return {
        "status": status,
        "evidence_class": evidence_class,
        "validation_evidence": sorted(refs),
        "effect": effect,
    }


def _terminate_group(process: subprocess.Popen) -> None:
    """Terminate the whole worker group, including children of an exited leader."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (OSError, ProcessLookupError):
            break
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


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
    validated = probe_guard.validate_spec(probe_id, spec)
    if not explicit and (not validated["read_only"] or not validated["autonomous"]):
        raise ValueError("probe %s requires explicit invocation" % probe_id)
    declared_outputs = _outputs(spec)
    command = _command(spec)
    bound_questions = sorted(set(str(value) for value in (question_ids or []) if value))
    _ensure_registration(spec, bound_questions, probe_id)

    live_state = _state_dir()
    lock_path = live_state / ".lock"
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
                    "budget": validated["budget"],
                }
                _append(result)
                return result
            raise

        started = _utcnow()
        grant = probe_guard.new_grant(probe_id, spec)
        execution_id = str(grant["execution_id"])
        hypothesis_id = str(spec.get("hypothesis_id") or "")
        result_count_before = sum(
            1 for item in provenance.events()
            if item.get("event") == "hypothesis.result"
            and item.get("hypothesis_id") == hypothesis_id
        ) if hypothesis_id else 0
        budget = dict(validated["budget"])
        timeout = max(1, int(budget.get("wall_seconds") or 60))
        status = "failed"
        reason: Optional[str] = None
        returncode: Optional[int] = None
        output = ""
        admitted: List[Dict[str, Any]] = []

        with sandbox.scratch_dir("farm-probe-%s-" % probe_id) as scratch_name:
            scratch = Path(scratch_name).resolve()
            writable = set(declared_outputs) | {"tool_calls.ndjson"}
            if hypothesis_id:
                writable.add("provenance.ndjson")
            projection = sandbox.project_state(
                live_state, scratch, writable, fresh_names={"tool_calls.ndjson"},
            )
            state_view = Path(projection["root"])

            request_read, request_write = os.pipe()
            response_read, response_write = os.pipe()
            client: Optional[mcp.Client] = None
            if budget.get("calls"):
                # Endpoint and TLS context exist only in this trusted process.
                client = mcp.Client()

            def transport(payload: Dict[str, Any], request_timeout: int, retries: int) -> Dict[str, Any]:
                if client is None:
                    raise probe_guard.AuthorizationError("zero-call probe attempted MCP transport")
                params = payload.get("params") or {}
                tool = str(params.get("name") or "")
                trace_id = "%s:%s" % (execution_id, payload.get("id"))
                trace_started = time.monotonic()
                base_trace = {
                    "id": trace_id,
                    "ts": _utcnow(),
                    "tool": tool,
                    "probe_id": execution_id,
                    "registered_probe_id": probe_id,
                    "actor": "probe_broker",
                    "authoritative": True,
                }
                compaction.append_json(
                    live_state / "tool_calls.ndjson",
                    dict(base_trace, event="start", arguments=mcp._safe_arguments(params.get("arguments") or {})),
                    strict=False,
                )
                try:
                    response = client._post(payload, timeout=request_timeout, retries=retries)
                except Exception as exc:
                    compaction.append_json(
                        live_state / "tool_calls.ndjson",
                        dict(base_trace, event="end", ts=_utcnow(), ok=False,
                             duration_ms=round((time.monotonic() - trace_started) * 1000, 1),
                             error=str(exc)[:240]),
                        strict=False,
                    )
                    raise
                compaction.append_json(
                    live_state / "tool_calls.ndjson",
                    dict(base_trace, event="end", ts=_utcnow(), ok=True,
                         duration_ms=round((time.monotonic() - trace_started) * 1000, 1)),
                    strict=False,
                )
                return response

            broker = threading.Thread(
                target=probe_guard.serve,
                args=(request_read, response_write, grant, transport),
                name="probe-broker-%s" % probe_id,
                daemon=True,
            )
            extra_env = {
                probe_guard.ENFORCEMENT_ENV: "1",
                probe_guard.REQUEST_FD_ENV: str(request_write),
                probe_guard.RESPONSE_FD_ENV: str(response_read),
                "FARM_PROBE_ID": execution_id,
                "FARM_TOOL_CALL_LOG": str(state_view / "tool_calls.ndjson"),
            }
            if hypothesis_id:
                extra_env["FARM_HYPOTHESIS_ID"] = hypothesis_id
                extra_env["FARM_EVIDENCE_CLASS"] = str(spec.get("evidence_class") or "holdout")
                extra_env["FARM_PROVENANCE_LOG"] = str(state_view / "provenance.ndjson")
            env = sandbox.environment(scratch, state_view, extra_env)
            process: Optional[subprocess.Popen] = None
            stdout_path, stderr_path = scratch / "worker.stdout", scratch / "worker.stderr"
            stdout_handle = stderr_handle = None
            try:
                wrapped = sandbox.wrap(
                    command, PROJECT, state_view, scratch, allow_processes=False,
                )
                for question_id in bound_questions:
                    questions.set_status(question_id, "probing", run=run, probe_id=probe_id)
                # Regular scratch files let us wait on the direct worker rather
                # than waiting for pipe EOF from descendants it may have forked.
                stdout_handle = stdout_path.open("w", encoding="utf-8")
                stderr_handle = stderr_path.open("w", encoding="utf-8")
                process = subprocess.Popen(
                    wrapped,
                    cwd=str(scratch),
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    pass_fds=(request_write, response_read),
                    start_new_session=True,
                )
                os.close(request_write)
                os.close(response_read)
                request_write = response_read = -1
                broker.start()
                try:
                    returncode = process.wait(timeout=timeout)
                    status = "passed" if returncode == 0 else "failed"
                except subprocess.TimeoutExpired:
                    probe_guard.close(grant)
                    _terminate_group(process)
                    status = "timeout"
                    reason = "wall-time budget exceeded"
            except sandbox.SandboxUnavailable as exc:
                status = "sandbox_unavailable"
                reason = str(exc)
            except OSError as exc:
                status = "failed"
                reason = "sandbox launch failed: %s" % type(exc).__name__
            finally:
                probe_guard.close(grant)
                for handle in (stdout_handle, stderr_handle):
                    if handle is not None:
                        try:
                            handle.close()
                        except OSError:
                            pass
                for fd in (request_write, response_read):
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                if broker.ident is None:
                    for fd in (request_read, response_write):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                else:
                    if process is not None:
                        # A forked descendant can retain the request pipe after the
                        # direct worker exits. Kill the whole group before waiting
                        # for EOF so it cannot hold the global farm lock forever.
                        _terminate_group(process)
                    if broker.is_alive():
                        # Authorized parent transport has its own hard timeout.
                        broker.join(timeout=probe_guard.MAX_BROKER_CALL_SECONDS + 5)
                    if broker.is_alive():
                        status = "broker_timeout"
                        reason = "parent broker exceeded its hard shutdown bound"
                output = (_tail_file(stdout_path) + _tail_file(stderr_path))[-8_000:]

            authoritative_usage = probe_guard.usage(grant)
            if authoritative_usage.get("denials"):
                status = "capability_violation"
                reason = "probe requested authority outside its protected grant"
            if status == "passed":
                try:
                    validation_result = _validated_provenance_result(
                        projection, hypothesis_id, spec, declared_outputs,
                    ) if hypothesis_id else None
                    if hypothesis_id and validation_result is None:
                        admitted = []
                    else:
                        admitted = sandbox.admit_outputs(
                            live_state, projection,
                            [name for name in declared_outputs if name != "provenance.ndjson"],
                        )
                    if validation_result:
                        provenance.record_result(
                            hypothesis_id,
                            validation_result["status"],
                            validation_result["validation_evidence"],
                            validation_result["evidence_class"],
                            validation_result["effect"],
                        )
                except (OSError, TypeError, ValueError, sandbox.ResultValidationError) as exc:
                    status = "result_rejected"
                    reason = "probe result admission failed: %s" % str(exc)[:300]

        if status == "passed" and hypothesis_id:
            result_count_after = sum(
                1 for item in provenance.events()
                if item.get("event") == "hypothesis.result"
                and item.get("hypothesis_id") == hypothesis_id
            )
            if result_count_after <= result_count_before:
                status = "evidence_missing"
                reason = "hypothesis-linked probe did not record a validation result"
        authoritative_usage = probe_guard.usage(grant)
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
            "read_only": bool(validated["read_only"]),
            "explicit": bool(explicit),
            "hypothesis": spec.get("hypothesis"),
            "hypothesis_id": hypothesis_id or None,
            "evidence_class": spec.get("evidence_class"),
            "budget": budget,
            "calls": int(authoritative_usage.get("calls") or 0),
            "coins_reserved": int(authoritative_usage.get("coins") or 0),
            "transport_attempts": int(authoritative_usage.get("transport_attempts") or 0),
            "capability_denials": int(authoritative_usage.get("denials") or 0),
            "tool_usage": authoritative_usage.get("by_tool") or {},
            "admitted_outputs": admitted,
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
