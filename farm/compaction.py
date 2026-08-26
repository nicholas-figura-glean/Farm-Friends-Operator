"""Lossless segmentation and replay for append-only NDJSON ledgers.

Compaction is storage compaction, not semantic summarisation. Closed source rows are
moved byte-for-byte into checksummed gzip segments while a bounded hot tail remains
at the legacy path. Readers replay archived segments plus the active tail, so an
estimator sees exactly the same valid JSON objects before and after rotation.

A sidecar lock coordinates writers and the compactor. A tiny transaction record makes
rotation recoverable across process death: recovery either finishes the prepared
manifest/tail swap or refuses to guess when bytes no longer match the transaction.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_HOT_ROWS = 2_000
# These four high-volume ledgers use the coordinated writer and transparent reader.
# Smaller registries keep their existing append/current-state semantics; adding one
# here requires migrating every reader first so compaction can never hide a live row.
DEFAULT_LEDGERS = (
    "tool_calls.ndjson",
    "observations.ndjson",
    "intents.ndjson",
    "history.ndjson",
)


class CompactionError(RuntimeError):
    """Archived evidence is missing, corrupt, or cannot be recovered safely."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(value: Any) -> Path:
    return value if isinstance(value, Path) else Path(str(value))


def _lock_path(path: Path) -> Path:
    return Path(str(path) + ".ledger.lock")


def _segment_dir(path: Path) -> Path:
    return path.parent / "segments" / path.stem


def _manifest_path(path: Path) -> Path:
    return _segment_dir(path) / "manifest.json"


def _transaction_path(path: Path) -> Path:
    return _segment_dir(path) / ".transaction.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("%s.tmp.%d" % (path, os.getpid()))
    tmp.write_text(
        json.dumps(value, indent=1, sort_keys=True, allow_nan=False, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(str(tmp), str(path))


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, TypeError, ValueError) as exc:
        raise CompactionError("invalid compaction metadata %s: %s" % (path, exc.__class__.__name__))
    if not isinstance(value, dict):
        raise CompactionError("invalid compaction metadata %s" % path)
    return value


def _manifest(path: Path) -> Dict[str, Any]:
    value = _load_json(_manifest_path(path))
    if not value:
        return {"schema_version": SCHEMA_VERSION, "ledger": path.name, "segments": []}
    if value.get("ledger") != path.name or not isinstance(value.get("segments"), list):
        raise CompactionError("manifest does not describe %s" % path.name)
    return value


def _write_manifest(path: Path, manifest: Dict[str, Any]) -> None:
    manifest = dict(manifest)
    manifest["schema_version"] = SCHEMA_VERSION
    manifest["ledger"] = path.name
    manifest["updated_ts"] = _utcnow()
    _atomic_json(_manifest_path(path), manifest)


def _open_lock(path: Path):
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    return open(lock, "a+b")


def _complete_and_tail(data: bytes, hot_rows: int) -> Tuple[bytes, bytes, int]:
    """Split before the hot tail without allocating one object per historical row."""
    keep = max(1, int(hot_rows))
    complete_end = len(data) if data.endswith(b"\n") else data.rfind(b"\n") + 1
    if complete_end <= 0:
        return b"", data, 0
    cut = complete_end
    for _ in range(keep):
        prior_newline = data.rfind(b"\n", 0, max(0, cut - 1))
        if prior_newline < 0:
            cut = 0
            break
        cut = prior_newline + 1
    payload = data[:cut]
    tail = data[cut:]
    return payload, tail, payload.count(b"\n")


def _json_identity(line: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: value.get(key)
        for key in ("event_id", "event", "run", "ts", "id")
        if value.get(key) is not None
    }


def _segment_metadata(path: Path, filename: str, payload: bytes, rows: int) -> Dict[str, Any]:
    lines = payload.splitlines()
    return {
        "file": filename,
        "sha256": _sha(payload),
        "bytes": len(payload),
        "rows": rows,
        "created_ts": _utcnow(),
        "first": _json_identity(lines[0]) if lines else {},
        "last": _json_identity(lines[-1]) if lines else {},
    }


def _write_gzip(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("%s.tmp.%d" % (path, os.getpid()))
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=6, fileobj=raw, mtime=0) as zipped:
            zipped.write(payload)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(str(tmp), str(path))


def _read_segment(path: Path, entry: Dict[str, Any]) -> bytes:
    filename = str(entry.get("file") or "")
    if not filename or Path(filename).name != filename:
        raise CompactionError("unsafe segment filename for %s" % path.name)
    segment = _segment_dir(path) / filename
    try:
        with gzip.open(segment, "rb") as handle:
            payload = handle.read()
    except (FileNotFoundError, OSError, EOFError) as exc:
        raise CompactionError("cannot read segment %s: %s" % (segment, exc.__class__.__name__))
    if _sha(payload) != entry.get("sha256") or len(payload) != int(entry.get("bytes") or -1):
        raise CompactionError("segment checksum mismatch: %s" % segment)
    return payload


def _ensure_manifest_entry(path: Path, entry: Dict[str, Any]) -> None:
    manifest = _manifest(path)
    segments = list(manifest.get("segments") or [])
    matches = [item for item in segments if item.get("file") == entry.get("file")]
    if matches:
        if matches[0].get("sha256") != entry.get("sha256"):
            raise CompactionError("segment identity collision for %s" % entry.get("file"))
        return
    segments.append(entry)
    manifest["segments"] = segments
    _write_manifest(path, manifest)


def _recover_locked(path: Path) -> Optional[Dict[str, Any]]:
    transaction_path = _transaction_path(path)
    transaction = _load_json(transaction_path)
    if not transaction:
        return None
    entry = transaction.get("segment") or {}
    tail_file = _segment_dir(path) / str(transaction.get("tail_file") or "")
    try:
        active = path.read_bytes()
    except FileNotFoundError:
        active = b""
    except OSError as exc:
        raise CompactionError("cannot recover %s: %s" % (path, exc.__class__.__name__))

    active_sha = _sha(active)
    original_sha = transaction.get("original_sha256")
    tail_sha = transaction.get("tail_sha256")
    _ensure_manifest_entry(path, entry)

    if active_sha == original_sha:
        try:
            tail = tail_file.read_bytes()
        except OSError as exc:
            raise CompactionError("prepared tail missing for %s: %s" % (path, exc.__class__.__name__))
        if _sha(tail) != tail_sha:
            raise CompactionError("prepared tail checksum mismatch for %s" % path)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(tail_file), str(path))
    elif active_sha == tail_sha:
        try:
            tail_file.unlink()
        except FileNotFoundError:
            pass
    else:
        # A coordinated append always recovers before writing, so any other bytes
        # mean an uncoordinated writer raced a crashed transaction. Refuse to guess.
        raise CompactionError("active ledger changed during interrupted compaction: %s" % path)

    try:
        transaction_path.unlink()
    except FileNotFoundError:
        pass
    return entry


def recover(path: Any) -> Optional[Dict[str, Any]]:
    ledger = _path(path)
    with _open_lock(ledger) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return _recover_locked(ledger)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def append_json(path: Any, row: Dict[str, Any], strict: bool = True) -> bool:
    """Append one JSON row under the same lock used by rotation."""
    ledger = _path(path)
    try:
        encoded = (
            json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)
            + "\n"
        ).encode("utf-8")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with _open_lock(ledger) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            _recover_locked(ledger)
            with open(ledger, "ab", buffering=0) as handle:
                handle.write(encoded)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return True
    except (CompactionError, OSError, TypeError, ValueError):
        if strict:
            raise
        return False


def _parse(payload: bytes) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def read_rows(path: Any, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Replay valid object rows from archived segments and the active tail."""
    ledger = _path(path)
    recover(ledger)
    with _open_lock(ledger) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            manifest = _manifest(ledger)
            try:
                active_payload = ledger.read_bytes()
            except FileNotFoundError:
                active_payload = b""
            except OSError as exc:
                raise CompactionError("cannot read active ledger %s: %s" % (ledger, exc.__class__.__name__))
            active = _parse(active_payload)
            if limit is not None:
                wanted = max(0, int(limit))
                if wanted == 0:
                    return []
                if len(active) >= wanted:
                    return active[-wanted:]
                chunks: List[List[Dict[str, Any]]] = [active]
                missing = wanted - len(active)
                for entry in reversed(list(manifest.get("segments") or [])):
                    rows = _parse(_read_segment(ledger, entry))
                    chunks.insert(0, rows)
                    missing -= len(rows)
                    if missing <= 0:
                        break
                return [row for chunk in chunks for row in chunk][-wanted:]

            out: List[Dict[str, Any]] = []
            for entry in manifest.get("segments") or []:
                out.extend(_parse(_read_segment(ledger, entry)))
            out.extend(active)
            return out
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def compact_ledger(
    path: Any,
    max_bytes: int = DEFAULT_MAX_BYTES,
    hot_rows: int = DEFAULT_HOT_ROWS,
) -> Dict[str, Any]:
    """Move old complete rows into one immutable segment when the hot file is large."""
    ledger = _path(path)
    with _open_lock(ledger) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            recovered = _recover_locked(ledger)
            try:
                original = ledger.read_bytes()
            except FileNotFoundError:
                return {"ledger": ledger.name, "compacted": False, "reason": "missing"}
            if len(original) < max(1, int(max_bytes)):
                return {
                    "ledger": ledger.name,
                    "compacted": False,
                    "reason": "below threshold",
                    "active_bytes": len(original),
                    "recovered": bool(recovered),
                }
            payload, tail, moved_rows = _complete_and_tail(original, hot_rows)
            if not payload or moved_rows <= 0:
                return {
                    "ledger": ledger.name,
                    "compacted": False,
                    "reason": "insufficient complete rows",
                    "active_bytes": len(original),
                }
            if original != payload + tail:
                raise CompactionError("compaction split was not byte preserving for %s" % ledger)

            manifest = _manifest(ledger)
            sequence = len(manifest.get("segments") or []) + 1
            digest = _sha(payload)
            filename = "%06d-%s.ndjson.gz" % (sequence, digest[:16])
            segment_path = _segment_dir(ledger) / filename
            entry = _segment_metadata(ledger, filename, payload, moved_rows)
            _write_gzip(segment_path, payload)
            if _sha(_read_segment(ledger, entry)) != digest:
                raise CompactionError("segment verification failed for %s" % segment_path)

            tail_name = ".tail-%06d-%s" % (sequence, digest[:12])
            tail_path = _segment_dir(ledger) / tail_name
            tail_path.write_bytes(tail)
            transaction = {
                "schema_version": SCHEMA_VERSION,
                "ledger": ledger.name,
                "created_ts": _utcnow(),
                "original_sha256": _sha(original),
                "tail_sha256": _sha(tail),
                "tail_file": tail_name,
                "segment": entry,
            }
            _atomic_json(_transaction_path(ledger), transaction)
            _ensure_manifest_entry(ledger, entry)
            os.replace(str(tail_path), str(ledger))
            _transaction_path(ledger).unlink()
            return {
                "ledger": ledger.name,
                "compacted": True,
                "segment": filename,
                "rows_moved": moved_rows,
                "bytes_before": len(original),
                "bytes_archived": len(payload),
                "active_bytes": len(tail),
                "sha256": digest,
            }
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def status(path: Any) -> Dict[str, Any]:
    ledger = _path(path)
    recover(ledger)
    manifest = _manifest(ledger)
    try:
        active_bytes = ledger.stat().st_size
    except OSError:
        active_bytes = 0
    entries = list(manifest.get("segments") or [])
    return {
        "ledger": ledger.name,
        "active_bytes": active_bytes,
        "segments": len(entries),
        "archived_bytes": sum(int(item.get("bytes") or 0) for item in entries),
        "archived_rows": sum(int(item.get("rows") or 0) for item in entries),
        "last_segment": entries[-1].get("file") if entries else None,
    }


def maintain(
    state_dir: Any,
    ledgers: Iterable[str] = DEFAULT_LEDGERS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    hot_rows: int = DEFAULT_HOT_ROWS,
) -> List[Dict[str, Any]]:
    """Compact every configured ledger that crossed the hot-size threshold."""
    state = _path(state_dir)
    results: List[Dict[str, Any]] = []
    for name in ledgers:
        results.append(compact_ledger(state / name, max_bytes=max_bytes, hot_rows=hot_rows))
    return results


def state_status(state_dir: Any, ledgers: Iterable[str] = DEFAULT_LEDGERS) -> List[Dict[str, Any]]:
    state = _path(state_dir)
    return [status(state / name) for name in ledgers]
