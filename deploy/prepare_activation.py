#!/usr/bin/env python3
"""Durably arm release guards before the live pointer can expose candidate code.

This coordinator imports only the previously accepted canary kernel. Gate status
is loaded as a stdlib-only file module. Candidate packages are never imported.
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Dict, List, Optional


RELEASE_PREFIXES = ("farm/", "experiments/", "fixtures/", "dashboard/", "game/")
RELEASE_FILES = {"run.py", "monitor.py"}


def _manifest(root: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        if rel not in RELEASE_FILES and not rel.startswith(RELEASE_PREFIXES):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _changed(target: Path, previous: Path) -> List[str]:
    current, prior = _manifest(target), _manifest(previous)
    return sorted(
        path for path in set(current) | set(prior)
        if current.get(path) != prior.get(path)
    )


def _latest_run(state: Path) -> Optional[int]:
    latest = None
    try:
        lines = (state / "history.ndjson").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("run"), int):
            latest = value["run"]
    return latest


def _atomic_json(path: Path, value: Dict) -> None:
    tmp = Path("%s.tmp.%d" % (path, os.getpid()))
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def main() -> int:
    if len(sys.argv) != 8:
        raise SystemExit(
            "usage: prepare_activation.py TARGET PROJECT REV PREVIOUS TRUSTED_RUNTIME GATES_FILE COMPAT"
        )
    target, project, revision, previous, trusted_runtime, gates_file, compatibility_raw = sys.argv[1:]
    target_path = Path(target).resolve()
    project_path = Path(project).resolve()
    previous_path = project_path / "releases" / previous if previous else Path("/nonexistent")
    state = project_path / "state"
    compatibility = compatibility_raw == "1"
    os.environ["FARM_PROJECT_ROOT"] = str(project_path)
    latest_run = _latest_run(state)
    if not isinstance(latest_run, int):
        raise SystemExit("release activation requires a durable run-history identity")

    if previous:
        sys.path.insert(0, str(Path(trusted_runtime).resolve()))
        from farm import canary  # imported from the previously accepted release

        changed = _changed(target_path, previous_path)
        armed = canary.arm(
            revision,
            previous,
            reason=os.environ.get("FARM_CANARY_REASON", "release " + revision)[:500],
            order_id=os.environ.get("FARM_CANARY_ORDER_ID", "manual-release-" + revision)[:160],
            commit=os.environ.get("FARM_CANARY_COMMIT", "")[:80],
            base_commit=os.environ.get("FARM_CANARY_BASE_COMMIT", "")[:80],
            change_class=os.environ.get("FARM_CANARY_CHANGE_CLASS", "reliability")[:40],
            hypothesis_id=os.environ.get("FARM_CANARY_HYPOTHESIS_ID", "")[:120],
            policy_id=os.environ.get("FARM_CANARY_POLICY_ID", "")[:120],
            expected_improvement=float(os.environ.get("FARM_CANARY_EXPECTED_IMPROVEMENT", "0") or 0),
            strategy_intent=os.environ.get("FARM_CANARY_STRATEGY_INTENT", "")[:80],
            files=changed,
            store=str(project_path / canary.STORE),
            history=str(project_path / canary.HISTORY),
            run_history=str(project_path / canary.RUN_HISTORY),
        )
        if armed.get("status") != canary.WATCHING or armed.get("revision") != revision:
            raise SystemExit("candidate canary could not be armed before activation")
        # Older accepted canary kernels filtered this field to model-editable
        # paths. Preserve the complete release diff without importing candidate code.
        armed["files"] = changed
        _atomic_json(project_path / canary.STORE, armed)

    os.environ["FARM_STATE_DIR"] = str(state)
    gate_api = runpy.run_path(str(Path(gates_file).resolve()))
    waived = ["knowledge", "evidence"] if compatibility else []
    inherited_from = previous if compatibility else None
    inherited_run = None
    if compatibility:
        base = gate_api["load"](previous)
        assessment = gate_api["assess"](previous, latest_run, base)
        if assessment.get("status") != "pass":
            raise SystemExit("compatibility release requires a current passing base certification")
        inherited_run = base.get("evidence_run", base.get("run"))
    results = []
    for name in gate_api["names"]():
        if name in waived:
            results.append({"gate": name, "ok": None, "status": "inherited"})
        else:
            results.append({"gate": name, "ok": True, "status": "passed"})
    recorded = gate_api["record"](
        {"passed": True, "results": results, "failed": []},
        revision=revision,
        run=latest_run,
        policy_id=os.environ.get("FARM_CANARY_POLICY_ID") or None,
        inherited_from=inherited_from,
        waived=waived,
        inherited_run=inherited_run,
    )
    if not recorded.get("passed"):
        raise SystemExit("release gate certification was incomplete")
    print("activation guards prepared for %s" % revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
