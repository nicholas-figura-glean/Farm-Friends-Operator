"""launchd liveness and repair. This is what makes the loop keep running.

The failure that motivated this module: the agent was simply not loaded. The
code was correct, the release was published, `--alerts` looked calm, and nothing
ran for half an hour. A loop that cannot notice its own scheduler is dead is not
self-healing.

Two agents watch each other:
  com.nickfigura.farmfriends            the 180s cycle
  com.nickfigura.farmfriends.supervisor the 60s heal pass

Nothing here touches the farm. It only inspects and repairs launchd, and repairs
are rate-limited so a permanently broken plist cannot become a restart loop.
"""

import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import heal, rules

CYCLE_LABEL = "com.nickfigura.farmfriends"
SUPERVISOR_LABEL = "com.nickfigura.farmfriends.supervisor"
# The expansion agent is supervised too. It was NOT, and it silently died: the
# last sprint was killed mid-run and the label ended up unloaded, so herd growth
# fell to the cycle's own ~50/run for two hours while the leader added 10,869
# animals. Anything that matters to the score has to be kept alive.
EXPAND_LABEL = "com.nickfigura.farmfriends.expand"
# The contract watcher and author agent close the self-healing loop: the watcher
# notices the server changing, the author repairs the code. Both are supervised
# for the same reason the expansion agent is -- an agent that silently dies is
# worse than one that was never installed, because the loop keeps reporting green.
CONTRACT_LABEL = "com.nickfigura.farmfriends.contract"
AUTHOR_LABEL = "com.nickfigura.farmfriends.author"
RESEARCH_LABEL = "com.nickfigura.farmfriends.research"
DASHBOARD_LABEL = "com.nickfigura.farmfriends.dashboard"
AGENT_DIR = os.path.expanduser("~/Library/LaunchAgents")
TIMEOUT = 5


def _domain() -> str:
    return "gui/%d" % os.getuid()


def project_root() -> str:
    """The project dir, reachable from any release via the state symlink."""
    here = os.path.dirname(os.path.abspath(__file__))
    running_root = os.path.dirname(here)
    return os.path.realpath(os.path.join(running_root, "state", ".."))


def _run(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=TIMEOUT, check=False
    )


def _match(text: str, pattern: str) -> Optional[str]:
    found = re.search(pattern, text, re.MULTILINE)
    return found.group(1).strip() if found else None


def status(label: str = CYCLE_LABEL) -> Dict[str, Any]:
    """Selected launchd fields. Never returns the full dump."""
    try:
        result = _run(["launchctl", "print", "%s/%s" % (_domain(), label)])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"label": label, "loaded": False, "state": "unknown", "detail": str(exc)[:120]}
    if result.returncode != 0:
        return {"label": label, "loaded": False, "state": "not loaded"}
    text = result.stdout
    pid = _match(text, r"^\s*pid = (\d+)")
    runs = _match(text, r"^\s*runs = (\d+)")
    return {
        "label": label,
        "loaded": True,
        "state": _match(text, r"^\s*state = ([^\n]+)") or "loaded",
        "pid": int(pid) if pid else None,
        "runs": int(runs) if runs else None,
        "last_exit": _match(text, r"^\s*last exit code = ([^\n]+)"),
    }


def plist_path(label: str) -> str:
    return os.path.join(AGENT_DIR, label + ".plist")


def _install_plist(label: str) -> Optional[str]:
    """Render deploy/<label>.plist into ~/Library/LaunchAgents if needed."""
    source = os.path.join(project_root(), "deploy", label + ".plist")
    if not os.path.exists(source):
        return None
    try:
        with open(source) as fh:
            body = fh.read().replace("__PROJECT__", project_root())
    except OSError:
        return None
    target = plist_path(label)
    try:
        current = open(target).read()
    except OSError:
        current = None
    if current == body:
        return target
    try:
        os.makedirs(AGENT_DIR, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, target)
    except OSError:
        return None
    return target


def _repair_budget_ok(store: Dict[str, Any], label: str) -> bool:
    """At most N repairs per hour per label, so a bad plist cannot thrash."""
    book = store.setdefault("scheduler", {}).setdefault(label, {"repairs": []})
    now = datetime.now(timezone.utc)
    kept = []
    for stamp in book.get("repairs") or []:
        try:
            when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - when).total_seconds() < 3600:
            kept.append(stamp)
    book["repairs"] = kept
    return len(kept) < rules.HEAL_SCHEDULER_MAX_REPAIRS_PER_HOUR


def _note_repair(store: Dict[str, Any], label: str) -> None:
    book = store.setdefault("scheduler", {}).setdefault(label, {"repairs": []})
    book.setdefault("repairs", []).append(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    book["last_repair"] = book["repairs"][-1]


def repair(label: str, kick: bool = True) -> List[str]:
    """Load, enable and (optionally) kick a label. Returns what it did."""
    actions: List[str] = []
    store = heal.load()
    if not _repair_budget_ok(store, label):
        heal.save(store)
        return ["repair budget exhausted for %s (>%d/hour)" % (label, rules.HEAL_SCHEDULER_MAX_REPAIRS_PER_HOUR)]

    target = _install_plist(label)
    if target is None:
        heal.save(store)
        return ["no plist available for %s" % label]

    domain = _domain()
    try:
        if not status(label)["loaded"]:
            _run(["launchctl", "bootstrap", domain, target])
            actions.append("bootstrapped %s" % label)
        _run(["launchctl", "enable", "%s/%s" % (domain, label)])
        if kick:
            _run(["launchctl", "kickstart", "%s/%s" % (domain, label)])
            actions.append("kickstarted %s" % label)
    except (OSError, subprocess.TimeoutExpired) as exc:
        actions.append("repair of %s failed: %s" % (label, str(exc)[:100]))

    _note_repair(store, label)
    heal.save(store)
    return actions


def ensure(label: str, stale_seconds: Optional[float] = None, age_seconds: Optional[float] = None) -> Dict[str, Any]:
    """Make sure `label` is loaded, and kick it if its work is overdue."""
    info = status(label)
    actions: List[str] = []
    if not info["loaded"]:
        actions.extend(repair(label, kick=True))
        info = status(label)
    elif (
        stale_seconds is not None
        and age_seconds is not None
        and age_seconds > stale_seconds
        and not info.get("pid")
    ):
        # Loaded, idle, and overdue: nudge it rather than waiting another slot.
        store = heal.load()
        if _repair_budget_ok(store, label):
            _run(["launchctl", "kickstart", "%s/%s" % (_domain(), label)])
            actions.append("kickstarted overdue %s" % label)
            _note_repair(store, label)
        heal.save(store)
    info["actions"] = actions
    return info
