"""Fail-closed macOS Seatbelt runner and read-only state projection.

Git worktrees isolate versions, not authority. This module runs untrusted probe
and candidate code with no network, no ambient credentials, read-only project
and live evidence, and one bounded scratch tree. The trusted parent alone admits
validated result files back into live state.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import compaction

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
MAX_RESULT_BYTES = 2_000_000
MAX_NDJSON_APPEND_BYTES = 1_000_000
ACTIVE_ENV = "FARM_SANDBOX_ACTIVE"


class SandboxUnavailable(RuntimeError):
    """The host cannot provide the required execution boundary."""


class ResultValidationError(RuntimeError):
    """An untrusted worker produced an inadmissible result."""


def available() -> bool:
    return SANDBOX_EXEC.is_file() and os.access(str(SANDBOX_EXEC), os.X_OK)


def _real(path: Path) -> str:
    return os.path.realpath(str(path))


def _scheme(value: str) -> str:
    return json.dumps(value)


def profile(
    project: Path,
    state: Path,
    scratch: Path,
    read_roots: Sequence[Path] = (),
    allow_processes: bool = True,
    executable_paths: Sequence[Path] = (),
) -> str:
    """Allow normal reads/processes, but deny network, secrets, and outside writes."""
    project_path = _real(project)
    state_path = _real(state)
    scratch_path = _real(scratch)
    allowed_reads = list(dict.fromkeys(
        [project_path, state_path, scratch_path] + [_real(Path(path)) for path in read_roots]
    ))
    exclusions = " ".join(
        "(require-not (subpath %s))" % _scheme(path) for path in allowed_reads
    )
    home = _real(Path.home())
    glean_home = _real(Path.home() / ".glean")
    protected_reads = [
        _real(Path.home() / ".config" / "farm"),
        _real(Path.home() / ".ssh"),
        _real(Path.home() / ".aws"),
        _real(Path.home() / ".config" / "gcloud"),
        _real(Path.home() / "Library" / "Keychains"),
    ]
    forms = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        # Permit writes only inside the one canonical scratch tree or /dev/null.
        "(deny file-write* (require-all "
        "(require-not (subpath %s)) (require-not (literal \"/dev/null\"))))" % _scheme(scratch_path),
        # The project is nested under ~/.glean, so deny the rest of that tree while
        # retaining the exact source checkout and projected evidence paths.
        "(deny file-read* (require-all (subpath %s) %s))"
        % (_scheme(glean_home), exclusions),
    ]
    if not allow_processes:
        forms.append("(deny process-fork)")
        forms.append("(deny sysctl-read)")
        forms.append("(deny process-info-listpids)")
        forms.append("(deny process-info-setcontrol)")
        allowed_exec = list(dict.fromkeys(str(path) for path in executable_paths if str(path)))
        if allowed_exec:
            exceptions = " ".join(
                "(require-not (literal %s))" % _scheme(path) for path in allowed_exec
            )
            forms.append("(deny process-exec (require-all %s))" % exceptions)
        else:
            forms.append("(deny process-exec)")
    for denied in protected_reads:
        # Skip a redundant ancestor rule only if it would contain the project.
        if any(path == denied or path.startswith(denied + os.sep) for path in allowed_reads):
            continue
        forms.append("(deny file-read* (subpath %s))" % _scheme(denied))
    # HOME is redirected to scratch, but this protects unexpected absolute reads
    # from other ordinary home paths while allowing the explicit project/state.
    forms.append(
        "(deny file-read* (require-all (subpath %s) %s))"
        % (_scheme(home), exclusions)
    )
    return " ".join(forms)


def _inherited_boundary(scratch: Path) -> bool:
    """Prove the active marker is backed by an inherited write denial."""
    if os.environ.get(ACTIVE_ENV) != "1":
        return False
    inherited_root = os.environ.get("FARM_SANDBOX_SCRATCH")
    if not inherited_root:
        return False
    root = Path(inherited_root).resolve()
    try:
        scratch.resolve().relative_to(root)
    except ValueError:
        return False
    # /private/tmp is user-writable outside Seatbelt but outside every generated
    # scratch subpath. An unsandboxed caller can never fake this denial merely by
    # choosing an unwritable FARM_SANDBOX_SCRATCH parent.
    sentinel = Path("/private/tmp") / (".farm-seatbelt-check-%d" % os.getpid())
    try:
        fd = os.open(str(sentinel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except PermissionError:
        return True
    except OSError:
        return False
    else:
        os.close(fd)
        try:
            sentinel.unlink()
        except OSError:
            pass
        return False


def wrap(
    command: Sequence[str],
    project: Path,
    state: Path,
    scratch: Path,
    read_roots: Sequence[Path] = (),
    allow_processes: bool = True,
) -> List[str]:
    if not command:
        raise ValueError("sandbox command is empty")
    # Seatbelt restrictions are inherited by descendants and macOS refuses a
    # nested sandbox_apply. Skip nesting only after proving the outer profile's
    # write denial; a spoofed environment variable therefore cannot disable it.
    if _inherited_boundary(scratch):
        return [str(part) for part in command]
    if not available():
        raise SandboxUnavailable("/usr/bin/sandbox-exec is unavailable")
    executable_paths: List[Path] = []
    if not allow_processes:
        invoked = Path(str(command[0]))
        executable_paths.extend([invoked, invoked.resolve()])
        # Framework Python re-execs its app binary during startup; allow only that
        # interpreter chain, never general helpers such as ps, env, sh, or curl.
        app = Path(sys.prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
        if app.is_file():
            executable_paths.append(app)
    return [
        str(SANDBOX_EXEC), "-p",
        profile(
            project, state, scratch, read_roots,
            allow_processes=allow_processes,
            executable_paths=executable_paths,
        ),
    ] + [str(part) for part in command]


def environment(
    scratch: Path,
    state: Path,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Build an allowlisted environment; never inherit endpoint or model secrets."""
    home = scratch / "home"
    tmp = scratch / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "FARM_STATE_DIR": str(state),
        "FARM_STATE_READ_ONLY": "1",
        "FARM_SANDBOX_SCRATCH": str(scratch),
    }
    if _inherited_boundary(scratch):
        env[ACTIVE_ENV] = "1"
    for key, value in (extra or {}).items():
        if value is None:
            continue
        name = str(key)
        if not (
            name.startswith("FARM_")
            or name in {"SOURCE_DATE_EPOCH"}
        ):
            raise ValueError("sandbox environment key is not allowlisted: %s" % name)
        env[name] = str(value)
    for forbidden in ("FARM_MCP_URL", "FARM_MCP_ENDPOINT_FILE", "PYTHONPATH"):
        env.pop(forbidden, None)
    return env


def scratch_dir(prefix: str = "farm-sandbox-") -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix=prefix)


def _copy_regular(source: Path, destination: Path) -> int:
    if not source.exists():
        destination.touch(mode=0o600)
        return 0
    mode = source.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ResultValidationError("writable state input is not a regular file: %s" % source.name)
    shutil.copy2(str(source), str(destination))
    return destination.stat().st_size


def project_state(
    live_state: Path,
    scratch: Path,
    writable_names: Iterable[str],
    fresh_names: Iterable[str] = (),
) -> Dict[str, Any]:
    """Project live state read-only and copy only declared result files for writing."""
    live = live_state.resolve()
    root = scratch / "state"
    root.mkdir(parents=True, exist_ok=False)
    writable = {str(name) for name in writable_names}
    fresh = {str(name) for name in fresh_names}
    if not fresh.issubset(writable):
        raise ResultValidationError("fresh state paths must also be writable")
    if any(not name or name.startswith("/") or ".." in Path(name).parts for name in writable):
        raise ResultValidationError("invalid writable state path")
    baselines: Dict[str, Dict[str, Any]] = {}

    for source in live.iterdir():
        name = source.name
        destination = root / name
        # Never point worker lock/recovery sidecars back at live state. Read-only
        # ledger access may open a lock for coordination; it must coordinate only
        # inside the projection and cannot repair or mutate the source ledger.
        if name == ".lock" or name.endswith(".lock") or name.endswith(".txn"):
            destination.touch(mode=0o600)
            continue
        if name in writable:
            size = 0 if name in fresh else _copy_regular(source, destination)
            if name in fresh:
                destination.touch(mode=0o600)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest() if size else hashlib.sha256(b"").hexdigest()
            baselines[name] = {"size": size, "sha256": digest}
        else:
            destination.symlink_to(source)

    for name in writable:
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            destination.touch(mode=0o600)
            baselines[name] = {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    # The worker may coordinate its own local phases, but this lock has no
    # relationship to the live mutation lock held by the trusted parent.
    local_lock = root / ".lock"
    if local_lock.is_symlink():
        local_lock.unlink()
    if not local_lock.exists():
        local_lock.touch(mode=0o600)
    return {"root": root, "baselines": baselines}


def _validate_result_path(root: Path, name: str) -> Path:
    path = root / name
    if path.is_symlink() or not path.exists():
        raise ResultValidationError("result is missing or symlinked: %s" % name)
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ResultValidationError("result is not a regular file: %s" % name)
    resolved = path.resolve()
    if resolved.parent != root.resolve() or resolved.name != Path(name).name:
        raise ResultValidationError("result escapes projected state: %s" % name)
    return path


def admit_outputs(
    live_state: Path,
    projection: Dict[str, Any],
    names: Iterable[str],
) -> List[Dict[str, Any]]:
    """Validate the complete output set before persisting any member of it."""
    root = Path(projection["root"])
    baselines = projection.get("baselines") or {}
    prepared: List[Dict[str, Any]] = []
    live_state.mkdir(parents=True, exist_ok=True)

    # Phase one is pure validation. A malformed later ledger must not leave an
    # earlier JSON result durable while the probe is reported result_rejected.
    for name in sorted(set(str(value) for value in names if value)):
        path = _validate_result_path(root, name)
        destination = live_state / name
        if destination.is_symlink():
            raise ResultValidationError("live result destination is symlinked: %s" % name)
        data = path.read_bytes()
        if name.endswith(".ndjson"):
            baseline = baselines.get(name) or {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
            size = int(baseline.get("size") or 0)
            if len(data) < size or hashlib.sha256(data[:size]).hexdigest() != baseline.get("sha256"):
                raise ResultValidationError("worker rewrote existing ledger prefix: %s" % name)
            suffix = data[size:]
            if len(suffix) > MAX_NDJSON_APPEND_BYTES:
                raise ResultValidationError("worker ledger append exceeds limit: %s" % name)
            rows = []
            for line in suffix.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ResultValidationError("worker ledger row must be an object: %s" % name)
                rows.append(value)
            prepared.append({"path": name, "kind": "ndjson", "rows": rows})
            continue
        if not name.endswith(".json"):
            raise ResultValidationError("unsupported result type: %s" % name)
        if len(data) > MAX_RESULT_BYTES:
            raise ResultValidationError("worker result exceeds limit: %s" % name)
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise ResultValidationError("worker JSON result must be an object: %s" % name)
        prepared.append({"path": name, "kind": "json", "value": value, "bytes": len(data)})

    # Phase two runs only after every declared output validates.
    admitted: List[Dict[str, Any]] = []
    for item in prepared:
        name = str(item["path"])
        if item["kind"] == "ndjson":
            for row in item["rows"]:
                compaction.append_json(live_state / name, row, strict=True)
            admitted.append({"path": name, "kind": "ndjson", "rows": len(item["rows"])})
            continue
        destination = live_state / name
        tmp = Path("%s.tmp.%d" % (destination, os.getpid()))
        tmp.write_text(
            json.dumps(item["value"], indent=2, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(str(tmp), str(destination))
        admitted.append({"path": name, "kind": "json", "bytes": item["bytes"]})
    return admitted
