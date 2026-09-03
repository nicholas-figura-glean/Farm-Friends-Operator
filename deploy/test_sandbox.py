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
        (live / "nested").mkdir()
        (live / "nested" / "evidence.json").write_text('{"nested":true}\n', encoding="utf-8")
        scratch = root / "scratch"
        scratch.mkdir()
        projection = sandbox.project_state(
            live, scratch, {"result.json", "events.ndjson", "tool_calls.ndjson"},
            fresh_names={"tool_calls.ndjson"},
        )
        view = Path(projection["root"])
        suite.check((view / "history.ndjson").is_symlink(),
                    "undeclared regular state is projected by pinned descriptor")
        suite.check(
            json.loads((view / "nested" / "evidence.json").read_text()).get("nested") is True
            and not (view / "nested").is_symlink(),
            "nested state evidence is snapshot-copied without directory-fd traversal",
        )
        pinned_roots = projection.get("read_roots") or []
        pinned_fds = projection.get("read_fds") or ()
        suite.check(
            len(pinned_roots) == len(pinned_fds) == 1
            and str(pinned_roots[0]).startswith("/dev/fd/")
            and os.fstat(pinned_fds[0]).st_ino == (live / "history.ndjson").stat().st_ino,
            "projection pins exact read-only inputs by inherited descriptor",
            {"roots": pinned_roots, "fds": pinned_fds},
        )
        projected_profile = sandbox.profile(
            PROJECT, view, scratch, read_roots=projection.get("read_roots") or (),
            allow_processes=False,
        )
        suite.check(
            str(pinned_roots[0]) in projected_profile
            and str((live / "result.json").resolve()) not in projected_profile,
            "Seatbelt receives descriptor-pinned inputs but not copied writable outputs",
        )
        (view / "result.json").write_text('{"accepted":true}\n', encoding="utf-8")
        with (view / "events.ndjson").open("a", encoding="utf-8") as handle:
            handle.write('{"event":"new"}\n')
        admitted = sandbox.admit_outputs(live, projection, ["result.json", "events.ndjson"])
        suite.check(json.loads((live / "result.json").read_text())["accepted"] is True,
                    "trusted parent admits a validated JSON result", admitted)
        suite.check(len((live / "events.ndjson").read_text().splitlines()) == 2,
                    "trusted parent appends only the new NDJSON suffix", admitted)
        sandbox.close_projection(projection)

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
        sandbox.close_projection(atomic)

        semantic_scratch = root / "semantic"
        semantic_scratch.mkdir()
        semantic = sandbox.project_state(live, semantic_scratch, {"result.json"})
        semantic_view = Path(semantic["root"])
        semantic_view.joinpath("result.json").write_text('{"forged":true}\n', encoding="utf-8")
        suite.raises(
            sandbox.ResultValidationError,
            lambda: sandbox.admit_outputs(
                live,
                semantic,
                ["result.json"],
                validator=lambda prepared: (_ for _ in ()).throw(
                    sandbox.ResultValidationError("semantic mismatch")
                ),
            ),
            "trusted semantic rejection occurs before any worker byte becomes live",
        )
        suite.check(
            json.loads((live / "result.json").read_text()).get("accepted") is True,
            "semantically rejected output leaves live evidence unchanged",
        )
        sandbox.close_projection(semantic)

        nonfinite_scratch = root / "nonfinite"
        nonfinite_scratch.mkdir()
        nonfinite = sandbox.project_state(live, nonfinite_scratch, {"result.json", "events.ndjson"})
        nonfinite_view = Path(nonfinite["root"])
        nonfinite_view.joinpath("result.json").write_text('{"forged":true}\n', encoding="utf-8")
        with nonfinite_view.joinpath("events.ndjson").open("a", encoding="utf-8") as handle:
            handle.write('{"value":NaN}\n')
        suite.raises(
            sandbox.ResultValidationError,
            lambda: sandbox.admit_outputs(live, nonfinite, ["result.json", "events.ndjson"]),
            "non-finite NDJSON is rejected before multi-output admission begins",
        )
        suite.check(
            json.loads((live / "result.json").read_text()).get("accepted") is True,
            "later non-finite output cannot leave an earlier JSON result live",
        )
        sandbox.close_projection(nonfinite)

        commit_failure_scratch = root / "commit-failure"
        commit_failure_scratch.mkdir()
        commit_failure = sandbox.project_state(
            live, commit_failure_scratch, {"result.json", "events.ndjson"}
        )
        commit_failure_view = Path(commit_failure["root"])
        commit_failure_view.joinpath("result.json").write_text('{"forged":true}\n', encoding="utf-8")
        with commit_failure_view.joinpath("events.ndjson").open("a", encoding="utf-8") as handle:
            handle.write('{"event":"valid-but-write-fails"}\n')
        saved_append_json = sandbox.compaction.append_json
        try:
            sandbox.compaction.append_json = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
            suite.raises(
                OSError,
                lambda: sandbox.admit_outputs(
                    live, commit_failure, ["result.json", "events.ndjson"]
                ),
                "audit commit failure aborts before policy-driving JSON replacement",
            )
        finally:
            sandbox.compaction.append_json = saved_append_json
        suite.check(
            json.loads((live / "result.json").read_text()).get("accepted") is True,
            "failed audit commit cannot leave a JSON result live",
        )
        sandbox.close_projection(commit_failure)

        rewrite_scratch = root / "rewrite"
        rewrite_scratch.mkdir()
        rewritten = sandbox.project_state(live, rewrite_scratch, {"events.ndjson"})
        (Path(rewritten["root"]) / "events.ndjson").write_text('{"event":"forged"}\n', encoding="utf-8")
        suite.raises(
            sandbox.ResultValidationError,
            lambda: sandbox.admit_outputs(live, rewritten, ["events.ndjson"]),
            "worker cannot rewrite an existing evidence prefix",
        )
        sandbox.close_projection(rewritten)

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
        sandbox.close_projection(linked)

        swapped_live = root / "swapped-live"
        swapped_live.mkdir()
        (swapped_live / "input.json").write_text('{"safe":true}\n', encoding="utf-8")
        swapped_scratch = root / "swapped-scratch"
        swapped_scratch.mkdir()
        swapped = sandbox.project_state(swapped_live, swapped_scratch, set())
        (swapped_live / "input.json").unlink()
        (swapped_live / "input.json").symlink_to(Path.home() / ".ssh")
        suite.check(
            json.loads((Path(swapped["root"]) / "input.json").read_text()).get("safe") is True,
            "a post-projection path swap cannot retarget a descriptor-pinned input",
        )
        sandbox.close_projection(swapped)

        escaped_live = root / "escaped-live"
        escaped_live.mkdir()
        (escaped_live / "outside").symlink_to(Path.home() / ".ssh")
        escaped_scratch = root / "escaped-scratch"
        escaped_scratch.mkdir()
        suite.raises(
            sandbox.ResultValidationError,
            lambda: sandbox.project_state(escaped_live, escaped_scratch, set()),
            "live-state symlinks cannot widen projected read authority",
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
