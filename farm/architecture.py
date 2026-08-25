"""The architecture of the running system, derived rather than drawn.

A hand-drawn diagram of a self-modifying system is wrong within a day. The agents in
this project add modules, rewire calls, and revert each other's work without asking,
so any architecture view that is maintained by hand is a description of the past.

So this module computes the architecture from what is actually on disk -- module
imports, LaunchAgent plists, state files, the captured MCP contract -- and hashes the
result into a signature. When the signature changes, the architecture changed, and
that is recorded as a new version with whatever caused it: a work order, a release, a
canary revert.

What this deliberately is *not*: `farm/topology.py` already builds a 122-node
function-level call graph, which is the right tool for tracing one cycle's execution
and the wrong one for answering "what are the parts of this system". This module works
at subsystem level -- roughly, things an operator would name in conversation -- and
aggregates the function graph up to module edges.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import plistlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

def _project_root() -> Path:
    """The real project root, even when running from an immutable release copy.

    The agents execute from `releases/<rev>/`, which contains the runtime but not
    `deploy/` or `.git`. Taking the parent of this module therefore gave a different
    answer depending on who was scanning, and the ledger flip-flopped between "8
    LaunchAgents" from the working tree and "0 LaunchAgents" from the release, minting a
    spurious architecture version every 15 minutes.

    The architecture being described is that of the system, not of whichever copy of the
    code happens to be executing, so the root is resolved by looking for the markers that
    identify the project: a `deploy/` directory holding LaunchAgent plists. `state/` is
    already symlinked back here for the same reason.
    """
    here = Path(__file__).resolve().parent.parent
    for candidate in (here, *here.parents):
        if (candidate / "deploy").is_dir() and any(
                (candidate / "deploy").glob("com.nickfigura.farmfriends*.plist")):
            return candidate
    return here


PROJECT = _project_root()
LEDGER = PROJECT / "state" / "architecture.ndjson"

# Layers, outermost first. The order is the story the diagram tells: the game is
# outside our control, the loop plays it, and everything above the loop exists to keep
# the loop correct without a human.
LAYERS: List[Dict[str, str]] = [
    {"id": "world", "name": "The game",
     "note": "outside our control; changes without warning"},
    {"id": "play", "name": "Play loop",
     "note": "deterministic; no model calls on the hot path"},
    {"id": "observe", "name": "Observation & evidence",
     "note": "turns raw responses into measurements"},
    {"id": "detect", "name": "Drift detection",
     "note": "notices the game changing underneath us"},
    {"id": "decide", "name": "Authoring & research",
     "note": "the only layer allowed to call a model"},
    {"id": "guard", "name": "Safety & rollback",
     "note": "agents may not modify anything in this layer"},
    {"id": "operate", "name": "Scheduling & operator view",
     "note": "keeps it running and makes it legible"},
]

# Which layer each module belongs to. Explicit because layer membership encodes intent
# -- what a module is *for* -- and that cannot be recovered from its imports. Anything
# unlisted falls back to "observe", and `unmapped` in the snapshot reports it so a new
# module shows up as a gap to classify rather than silently landing in a default.
MODULE_LAYER: Dict[str, str] = {
    "cycle": "play", "rules": "play", "mcp": "play", "parse": "play",
    "planner": "play", "actions": "play", "policy": "play", "growth": "play",
    "journal": "observe", "analysis": "observe", "evidence": "observe",
    "knowledge": "observe", "ledger": "observe", "questions": "observe",
    "topology": "observe", "tool_trace": "observe", "claims": "observe",
    "probes": "observe", "report": "observe", "tokens": "observe",
    "contract": "detect", "watch": "detect",
    "llm": "decide", "research": "decide",
    "canary": "guard", "workorders": "guard", "vcs": "guard", "heal": "guard",
    "scheduler": "operate", "autonomy": "operate", "architecture": "operate",
    "progress": "operate", "release": "operate",
}

# Protected files, mirrored from the author agent's policy. Shown in the diagram
# because "which parts may the machine rewrite" is the single most important thing an
# operator needs to know about a self-modifying system, and it is invisible in code.
PROTECTED = {
    "farm/canary.py", "farm/workorders.py", "farm/llm.py", "farm/rules.py",
    "farm/vcs.py", "deploy/release.sh",
    "experiments/author_agent.py", "experiments/research_agent.py",
}


def _git(args: List[str]) -> str:
    try:
        # TZ=UTC because every other timestamp in this project is UTC, and git's
        # default `%cd` is local time. Mixing the two silently mis-sorts the merged
        # event stream: releases cut at 21:32 UTC sorted in as 15:32, landing them
        # hours before findings they actually came after.
        env = dict(os.environ, TZ="UTC")
        out = subprocess.run(["git", "-C", str(PROJECT)] + args,
                             capture_output=True, text=True, timeout=15, env=env)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _imports(source: str) -> List[str]:
    """Local `farm.*` / `experiments.*` modules this source depends on.

    Parsed rather than grepped so that a module named in a string, a comment, or this
    very docstring is not mistaken for a dependency.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod in ("farm", "experiments"):
                found.extend(a.name for a in node.names)
            elif mod.startswith(("farm.", "experiments.")):
                found.append(mod.split(".", 1)[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(("farm.", "experiments.")):
                    found.append(alias.name.split(".", 1)[1])
    return sorted(set(found))


def _agents() -> List[Dict[str, Any]]:
    """LaunchAgents, read from the plists that define them.

    Read from `deploy/*.plist` rather than `launchctl` so the architecture describes
    the system as designed. Liveness is a different question, answered by
    `farm/autonomy.py`; a dead agent is still part of the architecture.
    """
    out: List[Dict[str, Any]] = []
    for path in sorted((PROJECT / "deploy").glob("com.nickfigura.farmfriends*.plist")):
        try:
            with path.open("rb") as handle:
                data = plistlib.load(handle)
        except Exception:  # noqa: BLE001
            continue
        args = [str(a) for a in (data.get("ProgramArguments") or [])]
        entry = next((a for a in args if a.endswith(".py")), "")
        interval = data.get("StartInterval")
        out.append({
            "label": str(data.get("Label") or path.stem),
            "entry": os.path.relpath(entry, str(PROJECT)) if entry.startswith("/") else entry,
            "interval_seconds": int(interval) if isinstance(interval, int) else None,
            "plist": path.name,
        })
    return out


def _stores() -> List[Dict[str, Any]]:
    """Durable state files, with size. These are the system's memory."""
    out: List[Dict[str, Any]] = []
    state = PROJECT / "state"
    if not state.exists():
        return out
    for path in sorted(state.glob("*.ndjson")) + sorted(state.glob("*.json")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        # Raw response dumps are bulk, not architecture.
        if path.name.startswith("raw_"):
            continue
        out.append({"name": path.name, "bytes": size,
                    "kind": "append-only" if path.suffix == ".ndjson" else "snapshot"})
    return out


def _tools() -> List[str]:
    """MCP tools, from the captured contract baseline."""
    try:
        from farm import contract

        base = contract.load_baseline()
        tools = (base or {}).get("tools") or {}
        return sorted(tools.keys())
    except Exception:  # noqa: BLE001
        return []


def _modules() -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], List[str]]:
    """Module components and the import edges between them."""
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, str]] = []
    unmapped: List[str] = []
    sources: Dict[str, str] = {}

    for path in sorted((PROJECT / "farm").glob("*.py")):
        if path.name == "__init__.py":
            continue
        sources[path.stem] = path.read_text(encoding="utf-8", errors="replace")
    for path in sorted((PROJECT / "experiments").glob("*_agent.py")):
        sources[path.stem] = path.read_text(encoding="utf-8", errors="replace")

    for name, source in sources.items():
        rel = ("farm/%s.py" % name) if (PROJECT / "farm" / (name + ".py")).exists() \
            else ("experiments/%s.py" % name)
        if name.endswith("_agent"):
            layer = "decide"
        else:
            layer = MODULE_LAYER.get(name, "observe")
            if name not in MODULE_LAYER:
                unmapped.append(name)
        doc = ""
        try:
            doc = (ast.get_docstring(ast.parse(source)) or "").strip().split("\n")[0]
        except SyntaxError:
            pass
        nodes.append({
            "id": name,
            "kind": "agent" if name.endswith("_agent") else "module",
            "path": rel,
            "layer": layer,
            "loc": source.count("\n") + 1,
            "doc": doc[:150],
            "protected": rel in PROTECTED,
        })
        for dep in _imports(source):
            if dep in sources and dep != name:
                edges.append({"source": name, "target": dep})

    nodes.sort(key=lambda n: (n["layer"], n["id"]))
    edges.sort(key=lambda e: (e["source"], e["target"]))
    return nodes, edges, sorted(unmapped)


def signature(nodes: Iterable[Dict[str, Any]], edges: Iterable[Dict[str, str]],
              agents: Iterable[Dict[str, Any]]) -> str:
    """Stable hash of the shape of the system.

    Deliberately excludes line counts and docstrings: editing a comment is not an
    architecture change, and if it counted as one the version history would fill with
    noise and stop being readable. What counts is which parts exist, what depends on
    what, and which agents run.
    """
    payload = {
        "nodes": sorted((n["id"], n["layer"], n["kind"], bool(n.get("protected")))
                        for n in nodes),
        "edges": sorted((e["source"], e["target"]) for e in edges),
        "agents": sorted((a["label"], a.get("interval_seconds")) for a in agents),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def snapshot() -> Dict[str, Any]:
    """The architecture as it exists right now."""
    nodes, edges, unmapped = _modules()
    agents = _agents()
    tools = _tools()
    stores = _stores()
    sig = signature(nodes, edges, agents)

    by_layer: Dict[str, int] = {}
    for node in nodes:
        by_layer[node["layer"]] = by_layer.get(node["layer"], 0) + 1

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "signature": sig,
        "short": sig[:12],
        "layers": LAYERS,
        "nodes": nodes,
        "edges": edges,
        "agents": agents,
        "tools": tools,
        "stores": stores,
        "unmapped": unmapped,
        "stats": {
            "modules": sum(1 for n in nodes if n["kind"] == "module"),
            "agent_modules": sum(1 for n in nodes if n["kind"] == "agent"),
            "protected": sum(1 for n in nodes if n.get("protected")),
            "edges": len(edges),
            "launch_agents": len(agents),
            "tools": len(tools),
            "loc": sum(int(n.get("loc") or 0) for n in nodes),
            "by_layer": by_layer,
            "state_bytes": sum(int(s.get("bytes") or 0) for s in stores),
        },
        "commit": _git(["rev-parse", "--short", "HEAD"]) or None,
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]) or None,
        # Recorded so that a scan taken from the wrong root is visible in the ledger
        # rather than silently producing a different shape. This is how the
        # release-versus-working-tree flip-flop was eventually diagnosed.
        "root": str(PROJECT),
    }


def _diff_nodes(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, List[str]]:
    old_ids = {n["id"] for n in old.get("nodes") or []}
    new_ids = {n["id"] for n in new.get("nodes") or []}
    old_edges = {(e["source"], e["target"]) for e in old.get("edges") or []}
    new_edges = {(e["source"], e["target"]) for e in new.get("edges") or []}
    old_agents = {a["label"] for a in old.get("agents") or []}
    new_agents = {a["label"] for a in new.get("agents") or []}
    return {
        "added": sorted(new_ids - old_ids),
        "removed": sorted(old_ids - new_ids),
        "edges_added": sorted("%s->%s" % e for e in (new_edges - old_edges)),
        "edges_removed": sorted("%s->%s" % e for e in (old_edges - new_edges)),
        "agents_added": sorted(new_agents - old_agents),
        "agents_removed": sorted(old_agents - new_agents),
    }


def record(snap: Optional[Dict[str, Any]] = None, trigger: str = "scan",
           ledger: str = str(LEDGER)) -> Dict[str, Any]:
    """Append a version if the architecture actually changed.

    Returns the recorded row, or `{"recorded": False}` when the signature is unchanged.
    Unchanged scans are not written: an agent running every 15 minutes would otherwise
    produce 96 identical rows a day and bury the handful that mean something.
    """
    snap = snap if snap is not None else snapshot()
    rows = history(limit=1, ledger=ledger)
    previous = rows[-1] if rows else None
    if previous and previous.get("signature") == snap["signature"]:
        return {"recorded": False, "signature": snap["signature"],
                "reason": "architecture unchanged"}

    version = int((previous or {}).get("version") or 0) + 1
    row = {
        "version": version,
        "ts": snap["generated_at"],
        "signature": snap["signature"],
        "short": snap["short"],
        "commit": snap.get("commit"),
        "trigger": trigger,
        "root": snap.get("root"),
        "stats": snap["stats"],
        # The full node and edge set is stored, not just the diff, so any past version
        # can be rendered without replaying history from version 1.
        "nodes": [{k: n[k] for k in ("id", "kind", "layer", "path", "protected")}
                  for n in snap["nodes"]],
        "edges": snap["edges"],
        "agents": snap["agents"],
    }
    if previous:
        row["diff"] = _diff_nodes(previous, row)
    Path(ledger).parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    out = dict(row)
    out["recorded"] = True
    return out


def history(limit: int = 50, ledger: str = str(LEDGER)) -> List[Dict[str, Any]]:
    path = Path(ledger)
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows[-limit:]


def backfill(ledger: str = str(LEDGER)) -> Dict[str, Any]:
    """Reconstruct version history from git, for releases that predate this module.

    Uses `git ls-tree` and `git show` rather than checking anything out: a checkout
    would move the working tree, and the working tree is what `deploy/release.sh`
    ships. Rebuilding history must not be able to change what is running.

    Only file *presence* and imports are recovered, which is enough for the signature.
    """
    tags = [t for t in _git(["tag", "--list", "release/*", "--sort=creatordate"]).splitlines()
            if t.strip()]
    if not tags:
        return {"backfilled": 0, "reason": "no release tags"}

    existing = {r.get("commit") for r in history(limit=1000, ledger=ledger)}
    written = 0
    for tag in tags:
        commit = _git(["rev-list", "-n", "1", tag])
        short = commit[:7]
        if not commit or short in existing:
            continue
        listing = _git(["ls-tree", "-r", "--name-only", tag])
        files = [f for f in listing.splitlines()
                 if f.startswith(("farm/", "experiments/", "deploy/com."))]
        mods = [f for f in files
                if f.endswith(".py") and not f.endswith("__init__.py")
                and (f.startswith("farm/") or f.endswith("_agent.py"))]
        nodes = []
        edges = []
        for rel in mods:
            name = Path(rel).stem
            layer = "decide" if name.endswith("_agent") else MODULE_LAYER.get(name, "observe")
            nodes.append({"id": name, "kind": "agent" if name.endswith("_agent") else "module",
                          "layer": layer, "path": rel, "protected": rel in PROTECTED})
            source = _git(["show", "%s:%s" % (tag, rel)])
            for dep in _imports(source):
                if any(Path(m).stem == dep for m in mods) and dep != name:
                    edges.append({"source": name, "target": dep})
        agents = [{"label": Path(f).stem, "entry": "", "interval_seconds": None,
                   "plist": Path(f).name}
                  for f in files if f.startswith("deploy/com.")]
        nodes.sort(key=lambda n: (n["layer"], n["id"]))
        edges.sort(key=lambda e: (e["source"], e["target"]))
        sig = signature(nodes, edges, agents)
        rows = history(limit=1, ledger=ledger)
        previous = rows[-1] if rows else None
        if previous and previous.get("signature") == sig:
            continue
        row = {
            "version": int((previous or {}).get("version") or 0) + 1,
            "ts": _git(["log", "-1", "--format=%cd",
                        "--date=format-local:%Y-%m-%dT%H:%M:%SZ", tag]),
            "signature": sig, "short": sig[:12], "commit": short,
            "trigger": "release %s" % tag.split("/", 1)[-1],
            "release": tag.split("/", 1)[-1],
            "stats": {"modules": sum(1 for n in nodes if n["kind"] == "module"),
                      "agent_modules": sum(1 for n in nodes if n["kind"] == "agent"),
                      "protected": sum(1 for n in nodes if n.get("protected")),
                      "edges": len(edges), "launch_agents": len(agents)},
            "nodes": nodes, "edges": edges, "agents": agents,
            "reconstructed": True,
        }
        if previous:
            row["diff"] = _diff_nodes(previous, row)
        Path(ledger).parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        written += 1
    return {"backfilled": written, "tags": len(tags)}


def events(limit: int = 60) -> List[Dict[str, Any]]:
    """One chronological stream of everything that changed the system.

    The signature only moves when *structure* moves -- a module appears, an edge is
    added, an agent is installed. That is the right rule for versioning (otherwise
    editing a comment mints a version), but on its own it makes a misleading history:
    six releases in one evening produced a single structural version, so a timeline of
    versions alone suggests nothing happened while the loop was in fact repeatedly
    detecting drift, patching itself, and reverting bad releases.

    So versions are merged with the events that caused them -- releases, canary
    verdicts, published work orders, recorded findings -- and each is tagged with
    whether it changed the architecture's shape or only its behaviour.
    """
    out: List[Dict[str, Any]] = []

    for row in history(limit=200):
        diff = row.get("diff") or {}
        out.append({
            "ts": row.get("ts"), "kind": "version", "structural": True,
            "title": "architecture v%s" % row.get("version"),
            "detail": row.get("trigger") or "",
            "version": row.get("version"), "commit": row.get("commit"),
            "added": diff.get("added") or [], "removed": diff.get("removed") or [],
            "agents_added": diff.get("agents_added") or [],
        })

    for tag in _git(["tag", "--list", "release/*", "--sort=creatordate"]).splitlines():
        tag = tag.strip()
        if not tag:
            continue
        ts = _git(["log", "-1", "--format=%cd",
                   "--date=format-local:%Y-%m-%dT%H:%M:%SZ", tag])
        subject = _git(["log", "-1", "--format=%s", tag])
        out.append({"ts": ts, "kind": "release", "structural": False,
                    "title": "release %s" % tag.split("/", 1)[-1],
                    "detail": subject[:160],
                    "commit": _git(["rev-parse", "--short", tag])})

    canary_log = PROJECT / "state" / "canary.ndjson"
    if canary_log.exists():
        for row in _ndjson(canary_log)[-40:]:
            event = row.get("event")
            if event not in ("armed", "resolved"):
                continue
            healthy = row.get("reverted") is False or row.get("status") == "healthy"
            out.append({
                "ts": row.get("ts"), "kind": "canary", "structural": False,
                "title": "canary %s %s" % (event, (row.get("revision") or "")[:20]),
                "detail": (str(row.get("reason") or row.get("resolution") or ""))[:160],
                "ok": healthy if event == "resolved" else None,
            })

    try:
        from farm import workorders

        for order in workorders.current().values():
            if order.get("status") not in ("published", "failed"):
                continue
            out.append({
                "ts": order.get("resolved_ts") or order.get("ts"),
                "kind": "order", "structural": False,
                "title": "%s %s" % (order.get("status"), order.get("id")),
                "detail": (order.get("summary") or "")[:160],
                "ok": order.get("status") == "published",
            })
    except Exception:  # noqa: BLE001
        pass

    findings = PROJECT / "state" / "research_findings.ndjson"
    if findings.exists():
        for row in _ndjson(findings)[-30:]:
            errors = row.get("errors_found") or []
            if not errors and not row.get("event"):
                continue
            out.append({
                "ts": row.get("ts"), "kind": "finding", "structural": False,
                "title": "%s: %s" % (row.get("event") or "finding",
                                     row.get("subject") or ""),
                "detail": (row.get("outcome") or (errors[0].get("detail") if errors else "") or "")[:200],
                "errors": len(errors),
            })

    out = [e for e in out if e.get("ts")]
    out.sort(key=lambda e: str(e.get("ts")), reverse=True)
    return out[:limit]


def _ndjson(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def report(limit: int = 40) -> Dict[str, Any]:
    """Current architecture plus its version history, for the dashboard."""
    snap = snapshot()
    rows = history(limit=limit)
    # Trim the heavy per-version node lists out of the timeline. The tab fetches one
    # full version on demand rather than shipping all of them on every load.
    timeline = [
        {"version": r.get("version"), "ts": r.get("ts"), "short": r.get("short"),
         "commit": r.get("commit"), "trigger": r.get("trigger"),
         "release": r.get("release"), "reconstructed": bool(r.get("reconstructed")),
         "stats": r.get("stats") or {}, "diff": r.get("diff") or {}}
        for r in rows
    ]
    return {
        "current": snap,
        "timeline": list(reversed(timeline)),
        "events": events(),
        "versions": len(rows),
        "live_matches_recorded": bool(rows) and rows[-1].get("signature") == snap["signature"],
    }
