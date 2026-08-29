"""Live pipeline position, for the dashboard.

The loop already recorded per-phase durations *after* a run finished, which is
useless for watching a run happen. This module records where the pipeline is
right now: which step is active, which are done, which were skipped and why.

Design constraints:

- **Monitoring must never break the farm.** Every public function swallows its
  own errors. A full disk or a bad permission costs visibility, not a cycle.
- **Atomic writes only.** The dashboard polls this file every second or two, so
  a half-written file would be read constantly. tmp + os.replace, never in place.
- **The last completed run stays readable** after it finishes, so the tab shows a
  finished pipeline rather than going blank between runs.

The alternative considered was inferring progress from state/raw/latest/ file
mtimes, which needs no instrumentation but cannot tell which run a file belongs
to, cannot see steps that make no server call, and cannot distinguish "skipped"
from "not reached yet".
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

STATE_FILE = os.path.join("state", "progress.json")

# The canonical pipeline, in execution order. Kept here (not derived from the
# code path) so the dashboard can render the whole shape of a run before the run
# has reached its later steps.
STEPS: List[Dict[str, str]] = [
    {"name": "tools", "label": "Handshake", "detail": "tools/list - drift check"},
    {"name": "recon", "label": "Gap reconnaissance", "detail": "standings before mutation after a blind window"},
    {"name": "collect", "label": "Collect produce", "detail": "empty while hungry; banks at feed"},
    {"name": "read", "label": "Read farm state", "detail": "authoritative snapshot"},
    {"name": "harvest", "label": "Harvest crops", "detail": "crop timers never advance"},
    {"name": "feed", "label": "Feed herd", "detail": "the only lever left on score"},
    {"name": "board", "label": "Leaderboard", "detail": "pre-action rank, rival herds, coins, and produce"},
    {"name": "events", "label": "Risk events", "detail": "wolves, sickness, storms, spoilage, and automatic bills"},
    {"name": "novelty", "label": "Activity sentinel", "detail": "hold affected strategy domains before acting in a new regime"},
    {"name": "mechanics", "label": "Adaptive mechanics", "detail": "contract-backed prestige and active-crisis decisions with outcome verification"},
    {"name": "plots", "label": "Plot strategy", "detail": "maintain only evidence-backed flower/crop targets under the league plot cap"},
    {"name": "trades", "label": "Incoming trades", "detail": "adversarial value, reserve, and novelty gates"},
    {"name": "sell", "label": "Sell produce", "detail": "never sells feed"},
    {"name": "plan", "label": "Plan expansion", "detail": "joint feed + adoption solve"},
    {"name": "adopt", "label": "Adopt chickens", "detail": "versioned growth policy and safety bounds"},
    {"name": "buy_feed", "label": "Top up feed", "detail": "restore the reserve"},
    {"name": "offers", "label": "Maintain offers", "detail": "outgoing trade offers"},
    {"name": "verify", "label": "Verify", "detail": "full re-read on cadence"},
    {"name": "finish", "label": "Record run", "detail": "score rate, history, alerts"},
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _blank(run: Optional[int], dry: bool, budget_s: int, timeout_s: int) -> Dict[str, Any]:
    return {
        "run": run,
        "dry": bool(dry),
        "status": "running",
        "started_ts": _utcnow(),
        "updated_ts": _utcnow(),
        "finished_ts": None,
        "budget_s": budget_s,
        "timeout_s": timeout_s,
        "active": None,
        "steps": [
            {
                "name": step["name"],
                "label": step["label"],
                "hint": step["detail"],
                "status": "pending",
                "started_ts": None,
                "ended_ts": None,
                "seconds": None,
                "detail": {},
                "note": None,
            }
            for step in STEPS
        ],
    }


def _load() -> Optional[Dict[str, Any]]:
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save(data: Dict[str, Any]) -> None:
    data["updated_ts"] = _utcnow()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = "%s.tmp.%d" % (STATE_FILE, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def _find(data: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for step in data.get("steps") or []:
        if step.get("name") == name:
            return step
    return None


def begin(run: Optional[int], dry: bool, budget_s: int, timeout_s: int) -> None:
    try:
        _save(_blank(run, dry, budget_s, timeout_s))
    except OSError:
        pass


def start(name: str, **detail: Any) -> None:
    try:
        data = _load()
        if not data:
            return
        step = _find(data, name)
        if step is None:
            return
        step["status"] = "active"
        step["started_ts"] = _utcnow()
        if detail:
            step["detail"].update(detail)
        data["active"] = name
        _save(data)
    except OSError:
        pass


def done(name: str, seconds: Optional[float] = None, **detail: Any) -> None:
    try:
        data = _load()
        if not data:
            return
        step = _find(data, name)
        if step is None:
            return
        step["status"] = "done"
        step["ended_ts"] = _utcnow()
        step["seconds"] = round(seconds, 1) if seconds is not None else None
        if detail:
            step["detail"].update({k: v for k, v in detail.items() if v is not None})
        if data.get("active") == name:
            data["active"] = None
        _save(data)
    except OSError:
        pass


def skip(name: str, note: str = "") -> None:
    """A step that was correctly not run. Visibly different from 'not reached'."""
    try:
        data = _load()
        if not data:
            return
        step = _find(data, name)
        if step is None:
            return
        step["status"] = "skipped"
        step["ended_ts"] = _utcnow()
        step["note"] = note or None
        if data.get("active") == name:
            data["active"] = None
        _save(data)
    except OSError:
        pass


def fail(name: str, error: str) -> None:
    try:
        data = _load()
        if not data:
            return
        step = _find(data, name)
        if step is not None:
            step["status"] = "failed"
            step["ended_ts"] = _utcnow()
            step["note"] = (error or "")[:200]
        data["status"] = "failed"
        data["finished_ts"] = _utcnow()
        _save(data)
    except OSError:
        pass


def finish(status: str = "ok", **detail: Any) -> None:
    try:
        data = _load()
        if not data:
            return
        data["status"] = status
        data["finished_ts"] = _utcnow()
        data["active"] = None
        if detail:
            data.setdefault("summary", {}).update(
                {k: v for k, v in detail.items() if v is not None}
            )
        _save(data)
    except OSError:
        pass


def read() -> Dict[str, Any]:
    """Current pipeline state for the dashboard, or an idle skeleton."""
    data = _load()
    if not data:
        blank = _blank(None, False, 0, 0)
        blank["status"] = "idle"
        blank["started_ts"] = None
        return blank
    return data
