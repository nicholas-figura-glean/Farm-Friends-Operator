"""Canonical release matrix identity and durable certification status.

Execution lives in the trusted author/release coordinators. This module prevents
those paths from maintaining different gate inventories and lets governance read
the last complete certification without executing candidate code itself.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
STATUS_FILE = "release_gate_health.json"

MATRIX: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("self-test", ("/usr/bin/python3", "run.py", "--self-test")),
    ("knowledge", ("/usr/bin/python3", "deploy/test_knowledge.py")),
    ("governance", ("/usr/bin/python3", "deploy/test_governance.py")),
    ("safety", ("/usr/bin/python3", "deploy/test_safety.py")),
    ("probe-guard", ("/usr/bin/python3", "deploy/test_probe_guard.py")),
    ("sandbox", ("/usr/bin/python3", "deploy/test_sandbox.py")),
    ("mechanics", ("/usr/bin/python3", "deploy/test_mechanics.py")),
    ("strategy", ("/usr/bin/python3", "deploy/test_strategy.py")),
    ("evidence", ("/usr/bin/python3", "deploy/test_evidence.py")),
    ("tool-trace", ("/usr/bin/python3", "deploy/test_tool_trace.py")),
    ("topology", ("/usr/bin/python3", "deploy/test_topology.py")),
    ("architecture", ("/usr/bin/python3", "deploy/test_architecture.py")),
    ("dashboard", ("/usr/bin/python3", "deploy/test_dashboard.py")),
    ("recovery-watch", ("/usr/bin/python3", "deploy/test_recovery_watch.py")),
    ("notifications", ("/usr/bin/python3", "deploy/test_notifications.py")),
    ("degraded-cycle", ("/usr/bin/python3", "deploy/test_degraded_cycle.py")),
    ("contract", ("/usr/bin/python3", "deploy/test_contract.py")),
    ("contract-watch", ("/usr/bin/python3", "deploy/test_contract_watch.py")),
    ("runtime-compat", ("/usr/bin/python3", "deploy/test_runtime_compat.py")),
    ("vcs", ("/usr/bin/python3", "deploy/test_vcs.py")),
    ("author", ("/usr/bin/python3", "deploy/test_author.py")),
    ("dashboard-agent", ("/usr/bin/python3", "deploy/test_dashboard_agent.py")),
    ("mcp-wire", ("/bin/bash", "deploy/test_mcp_wire.sh")),
    ("architecture-js", ("/bin/bash", "deploy/test_architecture_js.sh")),
    ("game", ("/bin/bash", "deploy/test_game.sh")),
)


def commands() -> Tuple[Tuple[str, List[str]], ...]:
    return tuple((name, list(command)) for name, command in MATRIX)


def names() -> List[str]:
    return [name for name, _ in MATRIX]


def fingerprint() -> str:
    encoded = json.dumps(MATRIX, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else Path(__file__).resolve().parent.parent / "state"


def _path() -> Path:
    return Path(os.environ.get("FARM_RELEASE_GATE_HEALTH", str(_state_dir() / STATUS_FILE)))


def _archive_path(revision: str) -> Path:
    safe = "".join(ch for ch in str(revision or "") if ch.isalnum() or ch in "-_.")[:120]
    if not safe:
        raise ValueError("release gate revision is empty")
    return _state_dir() / "release_gate_health" / (safe + ".json")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def load(revision: Optional[str] = None) -> Dict[str, Any]:
    if revision:
        archived = _read(_archive_path(revision))
        if archived:
            return archived
    return _read(_path())


def _write(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("%s.tmp.%d" % (path, os.getpid()))
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def record(
    result: Dict[str, Any],
    revision: str,
    run: Optional[int],
    policy_id: Optional[str],
    inherited_from: Optional[str] = None,
    waived: Optional[List[str]] = None,
    inherited_run: Optional[int] = None,
) -> Dict[str, Any]:
    """Persist one complete matrix result from the trusted coordinator."""
    rows = list(result.get("results") or [])
    observed = [str(row.get("gate") or "") for row in rows]
    complete = observed == names()
    waived_set = sorted(set(str(value) for value in (waived or []) if value))
    failed = [str(row.get("gate") or "") for row in rows if row.get("ok") is False]
    valid_rows = all(
        (row.get("gate") in waived_set and row.get("ok") is None
         and row.get("status") == "inherited")
        or (row.get("gate") not in waived_set and row.get("ok") is True)
        for row in rows
    )
    valid_inheritance = not waived_set or bool(inherited_from and isinstance(inherited_run, int))
    record_value = {
        "schema_version": SCHEMA_VERSION,
        "ts": _utcnow(),
        "run": run,
        "evidence_run": inherited_run if waived_set else run,
        "revision": str(revision or ""),
        "policy_id": policy_id,
        "matrix_fingerprint": fingerprint(),
        "expected": names(),
        "observed": observed,
        "complete": complete,
        "passed": (
            bool(result.get("passed")) and complete and not failed
            and valid_rows and valid_inheritance
        ),
        "failed": failed,
        "inherited_from": inherited_from,
        "waived": waived_set,
        "results": [
            {"gate": row.get("gate"), "ok": row.get("ok"),
             "status": row.get("status") or ("passed" if row.get("ok") is True else None)}
            for row in rows
        ],
    }
    _write(_archive_path(revision), record_value)
    _write(_path(), record_value)
    return record_value


def assess(
    live_revision: Optional[str],
    current_run: Optional[int],
    value: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    record_value = value if value is not None else load(live_revision)
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(current_run, int):
        errors.append("current run identity is unavailable")
    if not record_value:
        errors.append("no durable release gate certification")
    else:
        if record_value.get("schema_version") != SCHEMA_VERSION:
            errors.append("release gate status schema mismatch")
        if record_value.get("matrix_fingerprint") != fingerprint():
            errors.append("release gate matrix changed since certification")
        if record_value.get("observed") != names() or not record_value.get("complete"):
            errors.append("release gate certification is incomplete")
        if not record_value.get("passed") or record_value.get("failed"):
            errors.append("one or more release gates failed or inheritance is invalid")
        waived = set(record_value.get("waived") or [])
        inherited = str(record_value.get("inherited_from") or "")
        if waived and (not inherited or not isinstance(record_value.get("evidence_run"), int)):
            errors.append("waived gates lack a valid inherited certification")
        rows = {str(row.get("gate") or ""): row for row in record_value.get("results") or []}
        if any(
            (rows.get(name) or {}).get("ok") is not None
            or (rows.get(name) or {}).get("status") != "inherited"
            for name in waived
        ):
            errors.append("waived gates are not explicitly marked inherited")
        if live_revision and record_value.get("revision") != live_revision:
            errors.append("release gate certification describes a different revision")
        certified_run = record_value.get("evidence_run", record_value.get("run"))
        if isinstance(current_run, int) and isinstance(certified_run, int):
            age = max(0, current_run - certified_run)
            if age > 40:
                warnings.append("release gate evidence is more than 40 runs old")
        elif current_run is not None:
            warnings.append("release gate certification has no run identity")
    return {
        "status": "fail" if errors else "warn" if warnings else "pass",
        "errors": errors,
        "warnings": warnings,
        "revision": record_value.get("revision") if record_value else None,
        "run": record_value.get("run") if record_value else None,
        "evidence_run": record_value.get("evidence_run") if record_value else None,
        "waived": list(record_value.get("waived") or []) if record_value else [],
        "matrix_fingerprint": record_value.get("matrix_fingerprint") if record_value else None,
        "failed": list(record_value.get("failed") or []) if record_value else [],
        "expected_count": len(MATRIX),
    }
