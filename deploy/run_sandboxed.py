#!/usr/bin/env python3
"""Run one release gate against a source tree under the trusted Seatbelt policy."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_PROJECT))

from farm import sandbox  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--project-root")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("sandboxed gate requires a command")
    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit("sandboxed gate root is missing: %s" % root)
    state = (root / "state").resolve()
    if not state.is_dir():
        raise SystemExit("sandboxed gate state is missing: %s" % state)
    canonical = Path(args.project_root).resolve() if args.project_root else root
    if not (canonical / "deploy" / "release.sh").is_file():
        raise SystemExit("canonical project root is not deployable: %s" % canonical)
    canonical_state = (canonical / "state").resolve()
    if state != canonical_state:
        raise SystemExit("candidate state link does not resolve to canonical state")
    with sandbox.scratch_dir("farm-release-gate-") as scratch_name:
        scratch = Path(scratch_name).resolve()
        env = sandbox.environment(
            scratch,
            state,
            {"FARM_PROJECT_ROOT": str(canonical), sandbox.ACTIVE_ENV: "1"},
        )
        completed = subprocess.run(
            sandbox.wrap(command, root, state, scratch, read_roots=[canonical]),
            cwd=str(root),
            env=env,
            text=True,
            timeout=max(1, min(1800, int(args.timeout))),
            check=False,
        )
        return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
