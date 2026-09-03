"""Authoritative trust boundary and service registry for the autonomous farm.

The same declarations drive four independent consumers:

* the author agent's edit policy
* launchd supervision
* the autonomy health view
* the architecture diagram

Keeping these facts in one dependency-free module prevents the operator view from
claiming a file is protected or a service is supervised when enforcement disagrees.
This module is itself part of the trusted computing base and may not be rewritten by
an autonomous authoring pass.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LABEL_PREFIX = "com.nickfigura.farmfriends"

# Every process required for unattended operation. ``entry`` is the source path, not
# the rendered plist argument (which normally runs through the immutable release
# pointer). ``layer`` places the service in the system map; source modules retain their
# own independently derived dependency edges.
SERVICES: List[Dict[str, Any]] = [
    {
        "key": "cycle",
        "label": LABEL_PREFIX,
        "entry": "run.py",
        "layer": "play",
        "role": "plays the farm",
        "lost": "the farm stops playing entirely",
        "critical": True,
    },
    {
        "key": "supervisor",
        "label": LABEL_PREFIX + ".supervisor",
        "entry": "run.py",
        "layer": "operate",
        "role": "watches and repairs the control loop",
        "lost": "failed services and stale runs stop being recovered",
        "critical": True,
    },
    {
        "key": "expand",
        "label": LABEL_PREFIX + ".expand",
        "entry": "experiments/expand.py",
        "layer": "play",
        "role": "buys bounded scoring capacity",
        "lost": "the herd stops growing at the expansion cadence",
        "critical": False,
    },
    {
        "key": "recovery",
        "label": LABEL_PREFIX + ".recovery",
        "entry": "experiments/recovery_watch.py",
        "layer": "guard",
        "role": "verifies recovery from a production outage",
        "lost": "an outage recovery loses its independent verification",
        "critical": False,
    },
    {
        "key": "outage",
        "label": LABEL_PREFIX + ".outage",
        "entry": "experiments/outage_notifier.py",
        "layer": "guard",
        "role": "confirms external outages and reports recovery",
        "lost": "confirmed outages stop reaching Farm Friends",
        "critical": False,
    },
    {
        "key": "eod",
        "label": LABEL_PREFIX + ".eod",
        "entry": "experiments/eod_report.py",
        "layer": "operate",
        "role": "posts the daily Mountain Time sundown report",
        "lost": "the daily progress and healing digest is not delivered",
        "critical": False,
    },
    {
        "key": "contract",
        "label": LABEL_PREFIX + ".contract",
        "entry": "experiments/contract_watch.py",
        "layer": "detect",
        "role": "detects endpoint drift",
        "lost": "server contract changes go unnoticed",
        "critical": False,
    },
    {
        "key": "author",
        "label": LABEL_PREFIX + ".author",
        "entry": "experiments/author_agent.py",
        "layer": "decide",
        "role": "writes and publishes bounded repairs",
        "lost": "drift is detected but never repaired",
        "critical": True,
    },
    {
        "key": "research",
        "label": LABEL_PREFIX + ".research",
        "entry": "experiments/research_agent.py",
        "layer": "decide",
        "role": "turns strategic uncertainty into bounded probes",
        "lost": "new strategy is no longer explored",
        "critical": False,
    },
    {
        "key": "dashboard",
        "label": LABEL_PREFIX + ".dashboard",
        "entry": "experiments/dashboard_agent.py",
        "layer": "operate",
        "role": "verifies every operator readout",
        "lost": "stale or broken dashboard tabs stop being detected",
        "critical": False,
    },
    {
        "key": "monitor",
        "label": LABEL_PREFIX + ".monitor",
        "entry": "monitor.py",
        "layer": "operate",
        "role": "serves the operator view",
        "lost": "the dashboard URL and all tabs are unavailable",
        "critical": True,
    },
]

# The trusted computing base. These paths define budgets, permissions, evidence
# identity, promotion, supervision, release, and rollback. Letting a model rewrite any
# one of them would also let it weaken the mechanism that judges its next change.
TRUSTED_PATHS = frozenset(
    {
        "run.py",
        "farm/__init__.py",
        "farm/analysis.py",
        "farm/architecture.py",
        "farm/autonomy.py",
        "farm/canary.py",
        "farm/claims.py",
        "farm/compaction.py",
        "farm/compatibility.py",
        "farm/contract.py",
        "farm/control.py",
        "farm/cycle.py",
        "farm/evaluation.py",
        "farm/evidence.py",
        "farm/format_compat.py",
        "farm/gates.py",
        "farm/heal.py",
        "farm/governance.py",
        "farm/journal.py",
        "farm/ledger.py",
        "farm/llm.py",
        "farm/mcp.py",
        "farm/mechanics.py",
        "farm/notify.py",
        "farm/novelty.py",
        "farm/observability.py",
        "farm/parse.py",
        "farm/policy.py",
        "farm/probe_guard.py",
        "farm/probes.py",
        "farm/provenance.py",
        "farm/questions.py",
        "farm/research.py",
        "farm/rules.py",
        "farm/sandbox.py",
        "farm/scheduler.py",
        "farm/staged_verify.py",
        "farm/strategy.py",
        "farm/tokens.py",
        "farm/vcs.py",
        "farm/watch.py",
        "farm/workorders.py",
        "monitor.py",
        "experiments/__init__.py",
        "experiments/activity_probe.py",
        "experiments/author_agent.py",
        "experiments/capability_policies.py",
        "experiments/contract_watch.py",
        "experiments/crop_score_probe.py",
        "experiments/crop_timer_probe.py",
        "experiments/dashboard_agent.py",
        "experiments/dual_cap_audit.py",
        "experiments/endgame.py",
        "experiments/eod_report.py",
        "experiments/expand.py",
        "experiments/outage_notifier.py",
        "experiments/recovery_watch.py",
        "experiments/registry.py",
        "experiments/rescue_feed.py",
        "experiments/research_agent.py",
        "experiments/rival_regime_probe.py",
        "experiments/strategy_policy.py",
        "deploy/install.sh",
        "deploy/prepare_activation.py",
        "deploy/release.sh",
        "deploy/run_sandboxed.py",
        "deploy/test_architecture_js.sh",
        "deploy/test_mcp_wire.sh",
        "deploy/test_probe_guard.py",
        "deploy/test_sandbox.py",
    }
    | {"deploy/%s.plist" % service["label"] for service in SERVICES}
)

# Autonomous patches stay in existing Python implementation files. ``monitor.py`` is
# intentionally editable: the independent dashboard verifier and release gates can
# safely judge a renderer/route repair, while the health and architecture truth it
# displays remain protected above. Compatibility work orders offer only
# ``farm/format_compat.py``; adapter-only activation independently proves that no
# parser, strategy, policy, or control-plane byte changed with that repair.
# Model judgement is confined to sandboxed, non-autonomous experiment scripts.
# Deterministic mechanical rewrites retain a wider inspected surface, but their
# exact transformation is protected code and still passes the full matrix.
MECHANICAL_EDITABLE_PREFIXES = ("farm/", "experiments/")
MECHANICAL_EDITABLE_FILES = ("monitor.py",)
AUTHOR_EDITABLE_PREFIXES = ("experiments/",)
AUTHOR_EDITABLE_FILES: tuple = ()

# Files that define a release. A dirty strategy journal is intentionally excluded: it
# is linked as live evidence rather than packaged code and must not prevent repairs.
RELEASE_SOURCE_PREFIXES = ("farm/", "experiments/", "fixtures/", "dashboard/", "game/", "deploy/")
RELEASE_SOURCE_FILES = ("run.py", "monitor.py")


def normalize_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    # Remove an ordinary relative prefix without turning "../../etc" into "etc".
    # ``str.lstrip('./')`` removes any run of either character and therefore erases
    # traversal evidence before the permission check can see it.
    while text.startswith("./"):
        text = text[2:]
    return text


def is_protected(path: str) -> bool:
    return normalize_path(path) in TRUSTED_PATHS


def is_release_source(path: str) -> bool:
    rel = normalize_path(path)
    return rel in RELEASE_SOURCE_FILES or any(rel.startswith(prefix) for prefix in RELEASE_SOURCE_PREFIXES)


def mechanically_editable(path: str) -> bool:
    """Can the deterministic endpoint-rename backend inspect this source file?"""
    rel = normalize_path(path)
    if not rel.endswith(".py") or rel.startswith("/") or ".." in rel.split("/"):
        return False
    return rel in MECHANICAL_EDITABLE_FILES or any(
        rel.startswith(prefix) for prefix in MECHANICAL_EDITABLE_PREFIXES
    )


def author_editable(path: str) -> bool:
    rel = normalize_path(path)
    if not rel.endswith(".py") or rel.startswith("/") or ".." in rel.split("/"):
        return False
    offered = rel in AUTHOR_EDITABLE_FILES or any(
        rel.startswith(prefix) for prefix in AUTHOR_EDITABLE_PREFIXES
    )
    return offered and not is_protected(rel)


def service(value: str) -> Optional[Dict[str, Any]]:
    """Find a service by key or launchd label."""
    return next((dict(item) for item in SERVICES if value in (item["key"], item["label"])), None)


def restart_service(value: str) -> Dict[str, Any]:
    """Kickstart an installed launchd service from the authoritative registry."""
    declared = service(value)
    if not declared:
        return {"restarted": False, "restart_error": "unknown service: %s" % value}
    label = str(declared["label"])
    domain = "gui/%d/%s" % (os.getuid(), label)
    try:
        installed = subprocess.run(
            ["/bin/launchctl", "print", domain],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if installed.returncode != 0:
            return {"restarted": False, "restart": "not_loaded", "label": label}
        restarted = subprocess.run(
            ["/bin/launchctl", "kickstart", "-k", domain],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"restarted": False, "label": label,
                "restart_error": "%s: %s" % (exc.__class__.__name__, str(exc)[:160])}
    if restarted.returncode != 0:
        detail = (restarted.stderr or restarted.stdout or "launchctl kickstart failed").strip()
        return {"restarted": False, "label": label, "restart_error": detail[:200]}
    return {"restarted": True, "restart": "kickstarted", "label": label}


def labels(exclude: Iterable[str] = ()) -> List[str]:
    omitted = set(exclude)
    return [str(item["label"]) for item in SERVICES if item["key"] not in omitted]


def project_root(runtime_root: Optional[Path] = None) -> Path:
    """Resolve the editable checkout from a working tree or immutable release.

    ``FARM_PROJECT_ROOT`` is injected into the author LaunchAgent and fails closed if
    it does not identify a deployable checkout. Other callers can discover the same
    root by walking above a release copy. A release directory itself is never returned
    merely because it contains ``farm/``.
    """
    explicit = os.environ.get("FARM_PROJECT_ROOT")
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not (candidate / "deploy" / "release.sh").is_file():
            raise RuntimeError("FARM_PROJECT_ROOT is not a deployable project: %s" % candidate)
        return candidate

    start = Path(runtime_root or Path(__file__).resolve().parent.parent).resolve()
    for candidate in (start, *start.parents):
        if (candidate / "deploy" / "release.sh").is_file() and (candidate / "farm").is_dir():
            return candidate
    raise RuntimeError("could not resolve editable project root from %s" % start)
