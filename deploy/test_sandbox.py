#!/usr/bin/env python3
"""Host-level failure-injection checks for candidate and probe isolation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, List

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import sandbox  # noqa: E402


class Suite:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: List[str] = []

    def check(self, condition: bool, label: str, detail: Any = "") -> None:
        self.checks += 1
        passed = bool(condition)
        print("  %-4s %s%s" % ("ok" if passed else "FAIL", label,
                                 ("  [%s]" % detail) if detail and not passed else ""))
        if not passed:
            self.failures.append(label + ((" [%s]" % detail) if detail else ""))

    def raises(self, exc_type, fn, label: str) -> None:
        try:
            fn()
        except exc_type:
            self.check(True, label)
        except Exception as exc:
            self.check(False, label, "%s: %s" % (type(exc).__name__, exc))
        else:
            self.check(False, label, "did not raise")


def main() -> int:
    suite = Suite()
    suite.check(sandbox.available(), "required macOS sandbox is available")
    if not sandbox.available():
        print("\nSANDBOX TEST FAILED: host has no fail-closed runner")
        return 1

    project_escape = PROJECT / ".sandbox-escape-test"
    state_escape = PROJECT / "state" / ".sandbox-state-escape-test"
    for path in (project_escape, state_escape):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    with tempfile.TemporaryDirectory(prefix="farm-sandbox-test-") as tmp:
        scratch = Path(tmp).resolve()
        state = PROJECT / "state"
        env = sandbox.environment(
            scratch,
            state,
            {"FARM_MCP_URL": "https://must-not-survive.invalid/secret"},
        )
        suite.check("FARM_MCP_URL" not in env and "PYTHONPATH" not in env,
                    "sandbox environment strips endpoint and import-path authority")
        suite.check(Path(env["HOME"]).is_relative_to(scratch)
                    and Path(env["TMPDIR"]).is_relative_to(scratch),
                    "sandbox redirects HOME and TMPDIR into scratch")
        prior_active = os.environ.get(sandbox.ACTIVE_ENV)
        prior_root = os.environ.get("FARM_SANDBOX_SCRATCH")
        try:
            os.environ[sandbox.ACTIVE_ENV] = "1"
            os.environ["FARM_SANDBOX_SCRATCH"] = str(scratch)
            spoofed = sandbox.wrap([sys.executable, "-c", "pass"], PROJECT, state, scratch)
        finally:
            if prior_active is None:
                os.environ.pop(sandbox.ACTIVE_ENV, None)
            else:
                os.environ[sandbox.ACTIVE_ENV] = prior_active
            if prior_root is None:
                os.environ.pop("FARM_SANDBOX_SCRATCH", None)
            else:
                os.environ["FARM_SANDBOX_SCRATCH"] = prior_root
        marker_safe = (
            spoofed[0] != str(sandbox.SANDBOX_EXEC)
            if prior_active == "1"
            else spoofed[0] == str(sandbox.SANDBOX_EXEC)
        )
        suite.check(marker_safe,
                    "active marker skips nesting only under a proven outer Seatbelt boundary",
                    spoofed)
        probe_profile = sandbox.profile(PROJECT, state, scratch, allow_processes=False)
        suite.check("(deny process-fork)" in probe_profile
                    and "(deny process-exec)" in probe_profile
                    and "(deny sysctl-read)" in probe_profile,
                    "probe profile denies child creation, helper exec, and process inspection")

        allowed = scratch / "worker-result.json"
        endpoint = Path.home() / ".config" / "farm" / "endpoint"
        script = (
            "import json, socket\n"
            "from pathlib import Path\n"
            "result={}\n"
            f"result['project_read']=Path({str(PROJECT / 'README.md')!r}).is_file()\n"
            f"Path({str(allowed)!r}).write_text('{{\"ok\":true}}')\n"
            f"\ntry: Path({str(project_escape)!r}).write_text('bad'); result['project_write']=True\n"
            "except OSError: result['project_write']=False\n"
            f"\ntry: Path({str(state_escape)!r}).write_text('bad'); result['state_write']=True\n"
            "except OSError: result['state_write']=False\n"
            f"\ntry: Path({str(endpoint)!r}).read_text(); result['endpoint_read']=True\n"
            "except OSError: result['endpoint_read']=False\n"
            "s=socket.socket(); s.settimeout(.2)\n"
            "try: s.connect(('1.1.1.1',443)); result['network']=True\n"
            "except OSError: result['network']=False\n"
            "print(json.dumps(result, sort_keys=True))\n"
        )
        completed = subprocess.run(
            sandbox.wrap([sys.executable, "-c", script], PROJECT, state, scratch),
            cwd=str(scratch), env=env, capture_output=True, text=True, timeout=10,
        )
        try:
            result = json.loads(completed.stdout.strip())
        except (TypeError, ValueError):
            result = {"stdout": completed.stdout, "stderr": completed.stderr}
        suite.check(completed.returncode == 0 and result.get("project_read") is True,
                    "sandboxed worker can read explicit source", result)
        suite.check(allowed.is_file(), "sandboxed worker can write its bounded result channel")
        suite.check(result.get("project_write") is False and not project_escape.exists(),
                    "sandboxed worker cannot mutate source", result)
        suite.check(result.get("state_write") is False and not state_escape.exists(),
                    "sandboxed worker cannot mutate live state", result)
        suite.check(result.get("endpoint_read") is False,
                    "sandboxed worker cannot read the MCP endpoint", result)
        suite.check(result.get("network") is False,
                    "sandboxed worker cannot open a direct network connection", result)

        read_fd, write_fd = os.pipe()
        pipe_script = "import os; os.write(%d, b'pipe-ok')" % write_fd
        process = subprocess.Popen(
            sandbox.wrap([sys.executable, "-c", pipe_script], PROJECT, state, scratch),
            cwd=str(scratch), env=env, pass_fds=(write_fd,),
        )
        os.close(write_fd)
        pipe_value = os.read(read_fd, 64)
        os.close(read_fd)
        suite.check(process.wait(timeout=5) == 0 and pipe_value == b"pipe-ok",
                    "sandbox preserves the inherited broker channel", pipe_value)

        release = (PROJECT / "release").resolve()
        if (release / "farm" / "control.py").is_file():
            staged = subprocess.run(
                [
                    sys.executable, str(PROJECT / "deploy" / "run_sandboxed.py"),
                    "--timeout", "30", "--project-root", str(PROJECT), str(release), "--",
                    sys.executable, "-c",
                    "from farm import control; print(control.project_root())",
                ],
                capture_output=True, text=True, timeout=40,
            )
            suite.check(staged.returncode == 0 and str(PROJECT) in staged.stdout,
                        "staged tree retains the canonical deployment root", {
                            "rc": staged.returncode, "stdout": staged.stdout, "stderr": staged.stderr,
                        })

        fake_root = scratch / "candidate-with-forged-state"
        fake_root.mkdir()
        fake_state = scratch / "credential-shaped-state"
        fake_state.mkdir()
        (fake_root / "state").symlink_to(fake_state)
        forged_state = subprocess.run(
            [
                sys.executable, str(PROJECT / "deploy" / "run_sandboxed.py"),
                "--timeout", "10", "--project-root", str(PROJECT), str(fake_root), "--",
                sys.executable, "-c", "print('must not run')",
            ],
            capture_output=True, text=True, timeout=20,
        )
        suite.check(forged_state.returncode != 0
                    and "does not resolve to canonical state" in forged_state.stderr,
                    "candidate-controlled state symlink cannot widen readable roots", {
                        "rc": forged_state.returncode, "stderr": forged_state.stderr,
                    })

    # Parent-only result admission preserves existing ledger prefixes and rejects
    # symlink or rewrite tricks. When this suite itself runs inside the release
    # sandbox, its synthetic "live" tree is still under the outer scratch path;
    # Seatbelt remains the authority while the fixture exercises parent writes.
    outer_read_only = os.environ.pop("FARM_STATE_READ_ONLY", None)
    with tempfile.TemporaryDirectory(prefix="farm-state-admission-") as tmp:
        root = Path(tmp)
        live = root / "live"
        live.mkdir()
        (live / "history.ndjson").write_text('{"run":1}\n', encoding="utf-8")
        (live / "result.json").write_text('{"old":true}\n', encoding="utf-8")
        (live / "events.ndjson").write_text('{"event":"old"}\n', encoding="utf-8")
        scratch = root / "scratch"
        scratch.mkdir()
        projection = sandbox.project_state(
            live, scratch, {"result.json", "events.ndjson", "tool_calls.ndjson"},
            fresh_names={"tool_calls.ndjson"},
        )
        view = Path(projection["root"])
        suite.check((view / "history.ndjson").is_symlink(),
                    "undeclared state is projected read-only by reference")
        (view / "result.json").write_text('{"accepted":true}\n', encoding="utf-8")
        with (view / "events.ndjson").open("a", encoding="utf-8") as handle:
            handle.write('{"event":"new"}\n')
        admitted = sandbox.admit_outputs(live, projection, ["result.json", "events.ndjson"])
        suite.check(json.loads((live / "result.json").read_text())["accepted"] is True,
                    "trusted parent admits a validated JSON result", admitted)
        suite.check(len((live / "events.ndjson").read_text().splitlines()) == 2,
                    "trusted parent appends only the new NDJSON suffix", admitted)

        atomic_scratch = root / "atomic"
        atomic_scratch.mkdir()
        atomic = sandbox.project_state(live, atomic_scratch, {"result.json", "events.ndjson"})
        atomic_view = Path(atomic["root"])
        atomic_view.joinpath("result.json").write_text('{"forged":true}\n', encoding="utf-8")
        with atomic_view.joinpath("events.ndjson").open("a", encoding="utf-8") as handle:
            handle.write("not-json\n")
        suite.raises(
            (sandbox.ResultValidationError, json.JSONDecodeError),
            lambda: sandbox.admit_outputs(live, atomic, ["result.json", "events.ndjson"]),
            "one malformed output rejects the complete output set",
        )
        suite.check(json.loads((live / "result.json").read_text()).get("accepted") is True,
                    "rejected output set leaves earlier live evidence unchanged")

        rewrite_scratch = root / "rewrite"
        rewrite_scratch.mkdir()
        rewritten = sandbox.project_state(live, rewrite_scratch, {"events.ndjson"})
        (Path(rewritten["root"]) / "events.ndjson").write_text('{"event":"forged"}\n', encoding="utf-8")
        suite.raises(
            sandbox.ResultValidationError,
            lambda: sandbox.admit_outputs(live, rewritten, ["events.ndjson"]),
            "worker cannot rewrite an existing evidence prefix",
        )

        link_scratch = root / "link"
        link_scratch.mkdir()
        linked = sandbox.project_state(live, link_scratch, {"result.json"})
        result_path = Path(linked["root"]) / "result.json"
        result_path.unlink()
        result_path.symlink_to(live / "result.json")
        suite.raises(
            sandbox.ResultValidationError,
            lambda: sandbox.admit_outputs(live, linked, ["result.json"]),
            "worker result symlinks are rejected",
        )
    if outer_read_only is not None:
        os.environ["FARM_STATE_READ_ONLY"] = outer_read_only

    print()
    if suite.failures:
        print("SANDBOX TEST FAILED: %d of %d checks" % (len(suite.failures), suite.checks))
        for failure in suite.failures:
            print("  - " + failure)
        return 1
    print("SANDBOX TEST PASSED: %d checks" % suite.checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
