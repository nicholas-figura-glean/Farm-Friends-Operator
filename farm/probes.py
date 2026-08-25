"""Budgeted probe execution under the existing farm mutation lock."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import analysis, rules

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


def run_probe(probe_id: str, explicit: bool = False, run: Optional[int] = None) -> Dict[str, Any]:
    registry = _registry()
    if probe_id not in registry:
        raise ValueError("unknown probe: %s" % probe_id)
    spec = registry[probe_id]
    if not explicit and (not spec.get("read_only") or not spec.get("autonomous")):
        raise ValueError("probe %s requires explicit invocation" % probe_id)

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
        tool_log = _state_dir() / "tool_calls.ndjson"
        before_size = tool_log.stat().st_size if tool_log.exists() else 0
        budget = dict(spec.get("budget") or {})
        timeout = max(1, int(budget.get("wall_seconds") or 60))
        env = dict(os.environ)
        env["FARM_PROBE_ID"] = probe_id
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
        after_size = tool_log.stat().st_size if tool_log.exists() else 0
        if int(budget.get("calls") or 0) == 0 and after_size != before_size:
            status = "budget_violation"
            reason = "read-only probe wrote MCP tool telemetry"
        result = {
            "schema_version": SCHEMA_VERSION,
            "ts": _utcnow(),
            "started_ts": started,
            "run": run,
            "probe_id": probe_id,
            "status": status,
            "returncode": returncode,
            "reason": reason,
            "read_only": bool(spec.get("read_only")),
            "explicit": bool(explicit),
            "hypothesis": spec.get("hypothesis"),
            "budget": budget,
            "stop_condition": spec.get("stop_condition"),
            "evidence_destination": spec.get("evidence_destination"),
            "output": output,
        }
        _append(result)
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
    """Run at most one autonomous read-only probe per configured run interval."""
    if not isinstance(run, int) or not open_questions:
        return None
    events = _recent_events()
    last_run = max(
        [event.get("run") for event in events if isinstance(event.get("run"), int)],
        default=None,
    )
    if isinstance(last_run, int) and run - last_run < rules.PROBE_MIN_INTERVAL_RUNS:
        return None
    for probe_id, spec in sorted(_registry().items()):
        if not spec.get("read_only") or not spec.get("autonomous"):
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
            return run_probe(probe_id, explicit=False, run=run)
    return None
