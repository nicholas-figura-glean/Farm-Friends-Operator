"""One read-only view of the self-healing machinery, for the dashboard.

Every subsystem built for autonomous operation -- the contract watcher, the author
agent, the research agent, work orders, canary probation, version control, the model
budget -- kept its own state and its own CLI flag. None of it reached the dashboard.
For a while the operator could see the farm in detail while the machinery *changing*
the farm was entirely invisible: a canary came within 2.7% of reverting a good
release, and the only place that was visible was `run.py --canary-status`.

Two rules this module follows, because a dashboard aggregator that breaks the
dashboard is worse than no aggregator:

* every section is independently guarded. A subsystem that raises, or whose state
  file is missing or half-written, degrades to an `error` string in its own section
  and cannot take the page down.
* it is strictly read-only. Nothing here arms, claims, resolves, commits or reverts
  anything. The dashboard is allowed to be wrong about the farm; it is not allowed to
  change it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import control

PROJECT = control.project_root(Path(__file__).resolve().parent.parent)

# This public alias remains for callers and tests, but the declarations themselves
# live in the shared control manifest used by supervision and architecture too.
AGENTS: List[Dict[str, Any]] = [dict(service) for service in control.SERVICES]


def _guard(fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Run a section, converting any failure into a reportable value.

    Bare `except Exception` is deliberate. The whole point is that an unanticipated
    failure in one subsystem's state must not blank the operator's only view of the
    other six.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}


def _age_seconds(ts: Optional[str]) -> Optional[int]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _tail(path: Path, limit: int) -> List[Dict[str, Any]]:
    """Last `limit` valid JSON objects from an append-only log.

    A partially written final line is normal: these files are appended to by other
    processes while the dashboard reads them. Bad lines are skipped rather than
    raising, because one torn write should not blank a panel.
    """
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


def agents() -> Dict[str, Any]:
    """Liveness of every required agent and the browser-facing monitor process."""
    from farm import scheduler

    out: List[Dict[str, Any]] = []
    down: List[str] = []
    for spec in AGENTS:
        row = dict(spec)
        try:
            st = scheduler.status(spec["label"])
            row.update({
                "loaded": bool(st.get("loaded")),
                "state": st.get("state") or "unknown",
                "runs": st.get("runs"),
                "last_exit": st.get("last_exit"),
            })
            # A nonzero last exit is worth surfacing but is not itself an outage:
            # these are periodic agents that exit between runs, and several use a
            # nonzero code to mean "nothing to do".
            if not row["loaded"]:
                down.append(spec["key"])
        except Exception as exc:  # noqa: BLE001
            row.update({"loaded": False, "state": "error", "detail": str(exc)[:120]})
            down.append(spec["key"])
        out.append(row)
    return {"agents": out, "down": down, "expected": len(AGENTS), "live": len(AGENTS) - len(down)}


def canary_state() -> Dict[str, Any]:
    """Probation status of the current provisional release."""
    from farm import canary

    st = canary.status()
    verdict = st.get("verdict") or {}
    # canary.status() is a flat record with a nested verdict. Older prototypes
    # returned the record under ``canary``; accept both so the operator view never
    # says "watching None" while a real release is on probation.
    info = st.get("canary") or st
    return {
        "status": verdict.get("status") or st.get("status") or "inactive",
        "revision": info.get("revision"),
        "previous": info.get("previous"),
        "order_id": info.get("order_id"),
        "reason": verdict.get("reason"),
        "runs_observed": verdict.get("runs_observed"),
        # The per-animal figures are the ones that decide a revert; absolute rate is
        # kept only because it is what an operator recognises at a glance.
        "baseline_per_animal": verdict.get("baseline_per_animal"),
        "observed_per_animal": verdict.get("observed_per_animal"),
        "threshold": verdict.get("threshold"),
        "excluded_runs": verdict.get("excluded_runs") or [],
        "excluded_reason": verdict.get("excluded_reason"),
        "armed_ts": info.get("armed_ts"),
        "resolved_ts": info.get("resolved_ts"),
        "armed_age_seconds": _age_seconds(info.get("armed_ts")),
        "resolution": info.get("resolution"),
        "change_class": info.get("change_class") or "reliability",
        "efficacy": info.get("efficacy") or {},
    }


def orders_state(limit: int = 12) -> Dict[str, Any]:
    """Work-order queue: what the machinery has been asked to fix."""
    from farm import workorders

    summary = workorders.summary()
    rows = []
    for order in workorders.current().values():
        rows.append({
            "id": order.get("id"),
            "status": order.get("status"),
            "kind": order.get("kind"),
            "severity": order.get("severity"),
            "summary": (order.get("summary") or "")[:180],
            "attempts": order.get("attempts") or 0,
            "claimed_by": order.get("claimed_by"),
            "age_seconds": _age_seconds(order.get("created_ts") or order.get("ts")),
        })
    # Open and blocked work first: a finished order is history, an unclaimed one is a
    # question about whether the loop is keeping up.
    rank = {"open": 0, "claimed": 1, "failed": 2, "published": 3, "rejected": 4}
    rows.sort(key=lambda r: (rank.get(str(r.get("status")), 9), -(r.get("age_seconds") or 0)))
    pending = [r for r in rows if r.get("status") in {"open", "claimed"}]
    repairs = [r for r in pending if r.get("severity") in {"breaking", "shape", "degraded"}]
    research = [r for r in pending if r not in repairs]
    summary = dict(summary)
    summary["oldest_open_age_seconds"] = max(
        (int(r.get("age_seconds") or 0) for r in pending), default=None
    )
    summary["repair_open"] = len(repairs)
    summary["research_open"] = len(research)
    summary["oldest_repair_age_seconds"] = max(
        (int(r.get("age_seconds") or 0) for r in repairs), default=None
    )
    return {"summary": summary, "orders": rows[:limit], "total": len(rows)}


def contract_state(limit: int = 8) -> Dict[str, Any]:
    """Endpoint drift: what the last scans saw at the MCP boundary.

    Note the shape of a scan row: `changes` is a *count* and `detail` holds the list.
    Reading `changes` as the list yields "'int' object is not subscriptable", which is
    how this was first written.
    """
    from farm import contract

    rows = contract.history(limit=limit)
    latest = rows[-1] if rows else {}
    detail = latest.get("detail")
    detail = detail if isinstance(detail, list) else []
    return {
        "last_scan_age_seconds": _age_seconds(latest.get("ts")),
        "tools_seen": latest.get("tools"),
        "fingerprint": (latest.get("fingerprint") or "")[:12],
        "changes": [
            {"kind": c.get("kind"), "tool": c.get("tool"),
             "detail": (c.get("detail") or c.get("summary") or "")[:160],
             "severity": c.get("severity")}
            for c in detail[:6] if isinstance(c, dict)
        ],
        "change_count": int(latest.get("changes") or 0),
        "actionable": int(latest.get("actionable") or 0),
        "absorbed": int(latest.get("absorbed") or 0),
        "orders_filed": int(latest.get("orders_filed") or 0),
        "history": [
            {"ts": r.get("ts"), "tools": r.get("tools"),
             "changes": int(r.get("changes") or 0),
             "actionable": int(r.get("actionable") or 0)}
            for r in rows
        ],
    }


def vcs_state() -> Dict[str, Any]:
    """Branch, head and release tags -- the record of what is actually running."""
    from farm import vcs

    if not vcs.available():
        return {"available": False}
    recent = vcs.recent(limit=10)
    dirty = vcs.dirty_paths(include_untracked=True)
    dirty_source = [path for path in dirty if control.is_release_source(path)]

    def _git(args: List[str]) -> str:
        # vcs exposes no "what branch am I on" helper -- `branch_name(order_id)` builds
        # an author branch name, which is a different question entirely.
        try:
            return vcs._run(args).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    tags = [t.strip() for t in
            _git(["tag", "--list", "release/*", "--sort=-creatordate"]).splitlines()
            if t.strip()][:8]
    head_sha = vcs.head() or ""
    remote_tracking_sha = _git(["rev-parse", "refs/remotes/origin/main"])
    return {
        "available": True,
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]) or None,
        "head": vcs.short(head_sha),
        "remote": "origin/main",
        "remote_head": vcs.short(remote_tracking_sha),
        "remote_tracking_synced": bool(head_sha and head_sha == remote_tracking_sha),
        "clean": not dirty,
        "dirty_paths": dirty[:8],
        "dirty_source_paths": dirty_source[:12],
        "subject": recent[0]["subject"] if recent else None,
        "recent": recent,
        "release_tags": tags,
        "commits": int(_git(["rev-list", "--count", "HEAD"]) or 0) or None,
        "stale_worktrees": vcs.stale_worktrees(),
    }


def research_state(limit: int = 6) -> Dict[str, Any]:
    """Findings ledger, including the errors the loop found in itself."""
    rows = _tail(PROJECT / "state" / "research_findings.ndjson", 200)
    errors: List[Dict[str, Any]] = []
    for row in rows:
        for err in row.get("errors_found") or []:
            if isinstance(err, dict):
                errors.append({
                    "id": err.get("id"),
                    "severity": err.get("severity"),
                    "detail": (err.get("detail") or "")[:400],
                    "ts": row.get("ts"),
                })
    recent = [
        {"ts": r.get("ts"), "event": r.get("event"), "subject": r.get("subject"),
         "outcome": (r.get("outcome") or "")[:160],
         "errors": len(r.get("errors_found") or [])}
        for r in rows[-limit:]
    ]
    return {
        "rows": len(rows),
        "errors_total": len(errors),
        "errors": errors[-limit:],
        "recent": list(reversed(recent)),
        "last_age_seconds": _age_seconds(rows[-1].get("ts")) if rows else None,
    }


def capabilities_state() -> Dict[str, Any]:
    """Validated adaptive policies and their most recent executed outcomes."""
    from farm import compaction, mechanics

    view = mechanics.status()
    history = compaction.read_rows(PROJECT / "state" / "history.ndjson", limit=80)
    actions: List[Dict[str, Any]] = []
    for row in reversed(history):
        for action in reversed(row.get("mechanic_actions") or []):
            if not isinstance(action, dict):
                continue
            actions.append({
                "run": row.get("run"),
                "ts": row.get("ts"),
                "tool": action.get("tool"),
                "kind": action.get("kind"),
                "status": action.get("status"),
                "reason": action.get("reason"),
                "verification": action.get("verification"),
            })
            if len(actions) >= 8:
                break
        if len(actions) >= 8:
            break
    view["recent_actions"] = actions
    view["last_action"] = actions[0] if actions else None
    return view


def governance_state() -> Dict[str, Any]:
    """Latest broad run-based review of execution, healing, learning, and safety."""
    from farm import governance

    return governance.status()


def llm_state() -> Dict[str, Any]:
    """Model budget and reachability.

    Reachability is read from the cached credential, never by making a call: a
    dashboard poll every 2s must not spend money or wake a dormant gateway.
    """
    from farm import llm, rules

    avail = llm.availability()
    passes = cost = None
    try:
        # Spend accounting lives with the agent that does the spending.
        sys_path_added = str(PROJECT) not in os.sys.path
        if sys_path_added:
            os.sys.path.insert(0, str(PROJECT))
        from experiments import author_agent

        passes, cost = author_agent.spend_today()
    except Exception:  # noqa: BLE001
        pass
    return {
        "available": bool(avail.get("available")),
        "dormant": bool(avail.get("dormant")),
        "reason": avail.get("reason"),
        "expires_ts": avail.get("expires_ts"),
        "remaining_seconds": avail.get("remaining_seconds"),
        "passes_today": passes,
        "spend_today": cost,
        "budget": getattr(rules, "AUTHOR_MAX_COST_USD_PER_DAY", None),
        "max_passes": getattr(rules, "AUTHOR_MAX_ORDERS_PER_DAY", None),
        "surge_max_passes": getattr(rules, "AUTHOR_MAX_SURGE_ORDERS_PER_DAY", None),
        "backlog_surge_age_seconds": getattr(rules, "AUTHOR_BACKLOG_SURGE_AGE_SECONDS", None),
    }


def activity_state(limit: int = 18) -> Dict[str, Any]:
    """Normalize the append-only control-plane ledgers into one operator story.

    These rows are deliberately a projection, not new instrumentation. The source
    ledgers remain authoritative and every event retains its source and reference.
    Keeping this in the slow autonomy endpoint lets every operational tab explain
    observe -> decide -> act -> verify without bloating the two-second farm poll.
    """
    events: List[Dict[str, Any]] = []

    def add(ts: Any, phase: str, actor: str, title: str, detail: Any,
            status: str = "recorded", source: str = "", ref: Any = None) -> None:
        if not isinstance(ts, str) or not ts:
            return
        events.append({
            "ts": ts,
            "phase": phase,
            "actor": actor,
            "title": str(title or "activity")[:180],
            "detail": str(detail or "")[:320],
            "status": status,
            "source": source,
            "ref": ref,
        })

    for row in _tail(PROJECT / "state" / "heal.ndjson", 12):
        cls = str(row.get("class") or "remedy")
        relaxing = cls == "relax"
        add(
            row.get("ts"), "verify" if relaxing else "act", "Supervisor",
            "Safeguard relaxed toward default" if relaxing else "Bounded %s remedy applied" % cls,
            row.get("action") or row.get("alert"),
            "verified" if relaxing else "handled", "state/heal.ndjson", row.get("run"),
        )

    for row in _tail(PROJECT / "state" / "canary.ndjson", 8):
        event = str(row.get("event") or row.get("status") or "canary")
        watching = event == "armed" or row.get("status") == "watching"
        add(
            row.get("ts") or row.get("armed_ts"), "verify", "Canary agent",
            "Release %s entered probation" % (row.get("revision") or "unknown")
            if watching else "Release %s canary resolved" % (row.get("revision") or "unknown"),
            row.get("reason") or row.get("resolution") or row.get("order_id"),
            "watching" if watching else str(row.get("status") or "resolved"),
            "state/canary.ndjson", row.get("revision"),
        )

    for row in _tail(PROJECT / "state" / "workorders.ndjson", 16):
        status = str(row.get("status") or "open")
        phase = "decide" if status == "open" else ("act" if status == "claimed" else "verify")
        actor = str(row.get("actor") or row.get("source") or "Work-order queue").replace("_", " ").title()
        add(
            row.get("ts"), phase, actor,
            "Work order %s: %s" % (status, row.get("summary") or row.get("id") or "untitled"),
            row.get("note") or row.get("intent") or row.get("kind"),
            status, "state/workorders.ndjson", row.get("id"),
        )

    for row in _tail(PROJECT / "state" / "contract.ndjson", 8):
        changes = int(row.get("changes") or 0)
        absorbed = int(row.get("absorbed") or 0)
        actionable = int(row.get("actionable") or 0)
        if not changes and not absorbed:
            continue
        detail = row.get("detail") if isinstance(row.get("detail"), list) else []
        summary = (detail[0].get("summary") if detail and isinstance(detail[0], dict) else None)
        add(
            row.get("ts"), "observe", "Contract watcher",
            "Boundary scan classified %d change%s" % (changes, "" if changes == 1 else "s"),
            summary or "%d actionable · %d absorbed · %d work orders filed" % (
                actionable, absorbed, int(row.get("orders_filed") or 0)),
            "recovering" if actionable else "verified", "state/contract.ndjson",
            (row.get("fingerprint") or "")[:12],
        )

    for row in _tail(PROJECT / "state" / "governance_reviews.ndjson", 6):
        summary = row.get("summary") or {}
        add(
            row.get("ts"), "verify", "Governance reviewer",
            "Periodic systems review %s" % str(row.get("status") or "recorded"),
            "%s pass · %s warn · %s fail · %s action(s)" % (
                summary.get("pass", 0), summary.get("warn", 0), summary.get("fail", 0),
                len(row.get("actions") or []),
            ),
            str(row.get("status") or "recorded"),
            "state/governance_reviews.ndjson", row.get("run"),
        )

    for row in _tail(PROJECT / "state" / "research_findings.ndjson", 8):
        event = str(row.get("event") or "finding")
        item = row.get("item") if isinstance(row.get("item"), dict) else {}
        title = item.get("title") or row.get("subject") or row.get("finding") or event.replace("_", " ")
        detail = row.get("outcome") or row.get("answer") or row.get("verdict") or item.get("hypothesis")
        add(
            row.get("ts"), "research", "Research agent",
            "%s: %s" % (event.replace("_", " ").title(), title), detail,
            "recovering" if row.get("errors_found") else "recorded",
            "state/research_findings.ndjson", row.get("question_id") or row.get("subject"),
        )

    # ISO-8601 UTC timestamps sort lexicographically. Dedupe repeated queue states
    # by the full visible identity; transitions with a new timestamp remain visible.
    events.sort(key=lambda item: item["ts"], reverse=True)
    seen = set()
    unique: List[Dict[str, Any]] = []
    for event in events:
        identity = (event["ts"], event["phase"], event["title"], event.get("ref"))
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(event)
    counts: Dict[str, int] = {}
    for event in unique:
        counts[event["phase"]] = counts.get(event["phase"], 0) + 1
    return {"events": unique[:limit], "counts": counts, "sources": 6}


def report() -> Dict[str, Any]:
    """The whole autonomy view. Each section independently guarded."""
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agents": _guard(agents),
        "canary": _guard(canary_state),
        "orders": _guard(orders_state),
        "contract": _guard(contract_state),
        "vcs": _guard(vcs_state),
        "research": _guard(research_state),
        "capabilities": _guard(capabilities_state),
        "governance": _guard(governance_state),
        "llm": _guard(llm_state),
        "activity": _guard(activity_state),
    }


# Building the full view costs ~185ms, nearly all of it spawning `launchctl` seven
# times and `git` several more. That is fine on the Autonomy endpoint, which is fetched
# when an operator opens a tab, and unacceptable on the 2s dashboard poll: it would be
# 1,800 rounds of subprocess churn an hour to answer a question whose answer changes on
# the scale of minutes.
_CACHE: Dict[str, Any] = {"at": 0.0, "view": None}
CACHE_TTL_SECONDS = 30.0


def cached_report(max_age_seconds: float = CACHE_TTL_SECONDS) -> Dict[str, Any]:
    """`report()` with a short TTL, for callers on a hot path.

    30s is chosen against what the freshness is *for*: noticing that an agent died.
    The fastest agent runs every 60s, so a 30s window cannot hide a missed run, and an
    operator watching the page sees an outage within half a minute either way.
    """
    now = time.monotonic()
    view = _CACHE.get("view")
    if view is not None and (now - float(_CACHE.get("at") or 0.0)) < max_age_seconds:
        return view
    view = report()
    _CACHE["view"] = view
    _CACHE["at"] = now
    return view


def blockers(view: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Conditions currently owned by recovery, containment, or verification agents.

    The historical function name is retained for API compatibility. Rows describe
    autonomous ownership; none is an instruction for a person. A watching canary or
    an open research order is normal operation and is not included.
    """
    view = view if view is not None else cached_report()
    out: List[Dict[str, Any]] = []

    ag = view.get("agents") or {}
    for key in ag.get("down") or []:
        spec = next((a for a in AGENTS if a["key"] == key), {})
        out.append({
            "severity": "critical" if spec.get("critical") else "warn",
            "what": "agent %s is not loaded" % spec.get("label", key),
            "why": spec.get("lost", "unknown effect"),
        })

    can = view.get("canary") or {}
    if can.get("status") == "regressed" and not can.get("resolution"):
        out.append({"severity": "critical",
                    "what": "automatic rollback active for regressed release %s" % can.get("revision"),
                    "why": can.get("reason") or "canary owns rollback to the last verified release"})

    orders = (view.get("orders") or {}).get("summary") or {}
    failed = int(orders.get("failed") or 0)
    open_count = int(orders.get("open") or 0)
    repair_count = int(orders.get("repair_open") or 0)
    oldest = orders.get("oldest_repair_age_seconds")
    if failed:
        out.append({"severity": "warn",
                    "what": "%d work order(s) safely contained after bounded attempts" % failed,
                    "why": "the verified release remains active while research seeks an alternate approach"})
    if repair_count and isinstance(oldest, int) and oldest > 3600:
        out.append({"severity": "warn",
                    "what": "%d repair(s) queued; oldest is %d minutes old" % (
                        repair_count, oldest // 60),
                    "why": "a loaded author is not enough; the repair queue is not making progress"})

    vcs_view = view.get("vcs") or {}
    dirty_source = vcs_view.get("dirty_source_paths") or []
    if dirty_source:
        out.append({"severity": "warn",
                    "what": "working source differs from main; autonomous authoring is safely paused",
                    "why": "%d release-source file(s) are contained outside the live release, including %s" % (
                        len(dirty_source), ", ".join(str(path) for path in dirty_source[:3]))})
    if (vcs_view.get("available") and vcs_view.get("remote_head")
            and vcs_view.get("remote_tracking_synced") is False):
        out.append({"severity": "warn",
                    "what": "local main differs from last-known origin/main",
                    "why": "publication is automatically held until remote verification succeeds"})

    con = view.get("contract") or {}
    age = con.get("last_scan_age_seconds")
    if isinstance(age, int) and age > 3600:
        out.append({"severity": "warn",
                    "what": "no endpoint scan for %d minutes" % (age // 60),
                    "why": "schema drift would go undetected"})

    capability_view = view.get("capabilities") or {}
    if capability_view.get("errors"):
        out.append({
            "severity": "critical",
            "what": "adaptive capability policy failed validation",
            "why": "; ".join(str(value) for value in capability_view.get("errors")[:3]),
        })

    governance_view = view.get("governance") or {}
    governance_last = governance_view.get("last") or {}
    for check in governance_last.get("checks") or []:
        if check.get("status") != "fail":
            continue
        out.append({
            "severity": "critical" if check.get("id") in {
                "execution.progress", "runtime.services", "knowledge.policy", "safety.lineage"
            } else "warn",
            "what": "governance review failed %s" % check.get("id"),
            "why": check.get("summary") or "periodic invariant failed",
        })

    llm_view = view.get("llm") or {}
    if llm_view.get("available") is False:
        out.append({"severity": "warn",
                    "what": "model gateway unavailable",
                    "why": "mechanical repairs still work; reasoned patches do not"})
    passes = llm_view.get("passes_today")
    maximum = llm_view.get("max_passes")
    if repair_count and isinstance(passes, int) and isinstance(maximum, int) and passes >= maximum:
        out.append({"severity": "warn",
                    "what": "author pass budget is exhausted with %d repair(s) queued" % repair_count,
                    "why": "%d/%d real author passes used in the last 24 hours" % (passes, maximum)})

    for section in ("agents", "canary", "orders", "contract", "vcs", "research", "capabilities", "governance", "llm"):
        err = (view.get(section) or {}).get("error")
        if err:
            out.append({"severity": "warn",
                        "what": "autonomy view '%s' failed to read" % section,
                        "why": err})
    return out
