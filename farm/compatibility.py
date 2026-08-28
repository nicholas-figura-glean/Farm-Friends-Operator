"""Trusted routing and scope proofs for response-format compatibility repairs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

from . import contract, workorders

ADAPTER_FILE = "farm/format_compat.py"
PACKAGED_ROOTS = ("run.py", "monitor.py", "farm", "experiments", "fixtures", "dashboard", "game")
STEP_TO_TOOL = {
    "recon": "leaderboard",
    "collect": "collect_produce",
    "read": "list_farm",
    "harvest": "harvest",
    "feed": "feed_animals",
    "board": "leaderboard",
    "events": "farm_events",
    "trades": "respond_to_trade",
    "sell": "sell",
    "adopt": "adopt_animal",
    "buy_feed": "buy_feed",
    "offers": "propose_trade",
    "verify": "list_farm",
}


def failed_step(progress_state: Dict[str, Any]) -> Optional[str]:
    """Return the step whose parser failed, even after deferred finalization.

    Some optional reads (notably the pre-action leaderboard) deliberately record
    ``available=false`` and let the cycle continue before the same ParseDrift is
    raised at the final fail-closed boundary. By then the step is marked ``done``
    and ``active`` is clear. Treat that explicit unavailable/error pair as the
    failed step so containment can still create a compatibility work order.
    """
    active = str(progress_state.get("active") or "")
    if active:
        return active
    steps = list(progress_state.get("steps") or [])
    failed = [
        str(row.get("name") or "")
        for row in steps
        if row.get("status") == "failed"
    ]
    if failed:
        return failed[-1]
    for row in reversed(steps):
        detail = row.get("detail") or {}
        if (isinstance(detail, dict)
                and detail.get("available") is False
                and detail.get("error")):
            return str(row.get("name") or "") or None
    return None


def deferred_parse_error(progress_state: Dict[str, Any]) -> Optional[str]:
    """Return a parser error deliberately deferred so routine care could finish."""
    step = failed_step(progress_state)
    if not step:
        return None
    for row in reversed(list(progress_state.get("steps") or [])):
        if str(row.get("name") or "") != step:
            continue
        detail = row.get("detail") or {}
        if (isinstance(detail, dict)
                and detail.get("available") is False
                and detail.get("error")):
            return str(detail.get("error"))[:500]
    return None


def latest_sample(tool: str, raw_dir: Path) -> Optional[Path]:
    """Newest already-captured response for a tool; never makes an MCP call."""
    candidates = []
    try:
        paths = list(raw_dir.glob("*.txt"))
    except OSError:
        return None
    for path in paths:
        if contract._tool_for_raw(path.stem) == tool:
            candidates.append(path)
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    except OSError:
        return None


def structural_excerpt(tool: str, text: str, limit: int = 4000) -> str:
    """Bounded parser evidence with user-controlled trade prose excluded."""
    lines = []
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if tool == "list_farm" and stripped.lower().startswith("open trades:"):
            break
        # No parser needs an unbounded prose line. This also removes quoted trade
        # pitches if a future format places them outside the ordinary section.
        line = raw[:240]
        if " — \"" in line:
            line = line.split(" — \"", 1)[0]
        lines.append(line)
        if sum(len(value) + 1 for value in lines) >= limit:
            break
    return "\n".join(lines)[:limit]


def _shape_id(tool: str, text: str) -> str:
    shape = contract.shape_of(text)
    material = json.dumps({"tool": tool, "shape": shape}, sort_keys=True)
    return "runtime-parse-%s-%s" % (
        tool.replace("_", "-")[:28],
        hashlib.sha256(material.encode("utf-8")).hexdigest()[:12],
    )


def _packaged_files(root: Path) -> Dict[str, str]:
    """Content digests for code/assets that an activated release can execute."""
    files: Dict[str, str] = {}
    for name in PACKAGED_ROOTS:
        path = root / name
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = [item for item in path.rglob("*") if item.is_file()]
        else:
            candidates = []
        for item in candidates:
            rel = item.relative_to(root).as_posix()
            if "__pycache__" in item.parts or rel in {"RELEASED", "farm-strategy-journal.md"}:
                continue
            try:
                files[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
            except OSError:
                files[rel] = "<unreadable>"
    return files


def overlay_proof(base_root: Path, source_root: Path) -> Dict[str, Any]:
    """Prove a compatibility candidate changes only the narrow adapter.

    This is the reason a red scientific strategy gate can never be used as a
    pretext to ship other code: any added, removed, or changed packaged byte
    outside the adapter rejects the overlay before gates or staging.
    """
    base = _packaged_files(Path(base_root))
    source = _packaged_files(Path(source_root))
    changed = sorted(
        rel for rel in set(base) | set(source)
        if base.get(rel) != source.get(rel)
    )
    return {
        "ok": changed == [ADAPTER_FILE],
        "changed": changed,
        "adapter_present": ADAPTER_FILE in source,
        "reason": (
            "adapter-only overlay"
            if changed == [ADAPTER_FILE]
            else "packaged changes outside compatibility adapter: %s"
                 % ", ".join(changed[:12])
        ),
    }


def route_parse_drift(
    error: Exception,
    progress_state: Dict[str, Any],
    raw_dir: Path,
    queue_path: str = workorders.QUEUE,
) -> Optional[Dict[str, Any]]:
    """Turn a captured runtime parser failure into one idempotent repair order."""
    step = failed_step(progress_state)
    tool = STEP_TO_TOOL.get(str(step or ""))
    if not tool:
        return None
    sample = latest_sample(tool, raw_dir)
    if sample is None:
        return None
    try:
        text = sample.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    order_id = _shape_id(tool, text)
    change = {
        "id": order_id,
        # The full cycle is already contained. This must outrank stale dashboard
        # degradation and speculative research in the author queue.
        "severity": "breaking",
        "kind": "runtime_parse_drift",
        "tool": tool,
        "summary": "%s rejected captured %s response: %s"
        % (type(error).__name__, tool, str(error)[:220]),
        "we_use_it": True,
        "sites": ["farm/parse.py"],
        "detail": {
            "step": step,
            "sample": sample.name,
            "parser_error": "%s: %s" % (type(error).__name__, str(error)[:240]),
            "shape": contract.shape_of(text),
            "structural_excerpt": structural_excerpt(tool, text),
        },
    }
    return workorders.submit(
        change,
        source="runtime_parse_drift",
        intent=(
            "The live `%s` response no longer passes the core parser. Normalize "
            "the captured format in `%s` without changing semantic values, parser "
            "invariants, strategy, or mutation policy." % (tool, ADAPTER_FILE)
        ),
        acceptance=[
            "the newest captured %s sample passes its protected parser" % tool,
            "legacy fixtures still pass unchanged",
            "normalization is pure and bounded",
            "no strategy, pricing, reserve, trade, or mutation rule changes",
        ],
        files=[ADAPTER_FILE],
        path=queue_path,
        provenance={"change_class": "compatibility", "tool": tool, "sample": sample.name},
    )
