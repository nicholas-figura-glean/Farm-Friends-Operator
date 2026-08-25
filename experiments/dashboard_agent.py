#!/usr/bin/env python3
"""Keeps the operator's view honest.

The dashboard already polls live, so nothing here exists to "refresh" it -- a page
that re-reads state every 2s does not need an agent to push data at it. Two jobs do
need doing periodically, and neither belongs in an HTTP handler:

1. **Record architecture versions.** The system rewrites itself, so its architecture
   has a history. Computing that history means parsing every module and shelling out
   to git, which is far too slow for a request handler and pointless to repeat on
   every poll. This agent computes it, and appends a version only when the shape
   actually changed.

2. **Verify every tab has data.** This is the part that matters. Each tab reads a
   different subsystem, and a tab whose data source has quietly started raising shows
   an empty panel or a stale number -- which looks exactly like "nothing is happening"
   rather than "this readout is broken". That failure mode already bit us: five of the
   seven agents were unmonitored while the page showed green, and a canary came within
   2.7% of reverting a good release with no indication anywhere on the dashboard.

   So each data source is exercised the way the page exercises it, and checked for
   freshness against how often its writer is supposed to run. A broken or stale
   readout files a work order like any other defect.

Exit codes: 0 clean, 1 problems found and filed, 2 the agent itself failed.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import architecture, autonomy, workorders  # noqa: E402

LEDGER = PROJECT / "state" / "dashboard_health.ndjson"

# Each dashboard readout, how it is produced, and how stale it may be before that is
# a defect. The staleness budgets come from the cadence of whatever writes the data:
# the cycle runs every 180s, the contract watcher every 900s, the research agent
# hourly. A budget well above the writer's period avoids flagging normal jitter.
#
# `critical` marks readouts whose silence would hide something dangerous. A stale
# Findings tab is embarrassing; a stale canary readout means a bad release could be
# live with nobody able to see it.
CHECKS: List[Dict[str, Any]] = [
    {"tab": "overview / pipeline / cost", "source": "monitor.snapshot",
     "max_age": 900, "critical": True},
    {"tab": "Findings", "source": "evidence.report",
     "max_age": 3600, "critical": False},
    {"tab": "MCP Switchboard", "source": "topology.cached_graph",
     "max_age": None, "critical": False},
    {"tab": "Autonomy", "source": "autonomy.report",
     "max_age": 900, "critical": True},
    {"tab": "Architecture", "source": "architecture.report",
     "max_age": None, "critical": False},
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _probe(source: str) -> Dict[str, Any]:
    """Exercise one data source the way the dashboard does.

    Timed, because a readout that takes 8s to build is a defect even when correct:
    the page polls every 2s and would spend its life waiting.
    """
    started = time.time()
    try:
        if source == "monitor.snapshot":
            import monitor

            payload = monitor.snapshot()
            age = payload.get("latest", {}).get("age_seconds")
            if age is None:
                # Fall back to the run timestamp when the snapshot does not carry an age.
                age = None
            return {"ok": True, "bytes": len(json.dumps(payload)), "age": age,
                    "ms": int((time.time() - started) * 1000)}
        if source == "evidence.report":
            from farm import evidence

            payload = evidence.report()
            return {"ok": True, "bytes": len(json.dumps(payload)),
                    "age": None, "ms": int((time.time() - started) * 1000)}
        if source == "topology.cached_graph":
            from farm import topology

            payload = dict(topology.cached_graph())
            nodes = len(payload.get("nodes") or [])
            return {"ok": nodes > 0, "bytes": len(json.dumps(payload)),
                    "age": None, "ms": int((time.time() - started) * 1000),
                    "detail": "%d nodes" % nodes,
                    "error": None if nodes else "graph has no nodes"}
        if source == "autonomy.report":
            payload = autonomy.report()
            broken = [k for k, v in payload.items()
                      if isinstance(v, dict) and v.get("error")]
            return {"ok": not broken, "bytes": len(json.dumps(payload)),
                    "age": None, "ms": int((time.time() - started) * 1000),
                    "detail": "%d sections" % (len(payload) - 1),
                    "error": ("sections failed: %s" % ", ".join(broken)) if broken else None}
        if source == "architecture.report":
            payload = architecture.report()
            versions = int(payload.get("versions") or 0)
            return {"ok": versions > 0, "bytes": len(json.dumps(payload)),
                    "age": None, "ms": int((time.time() - started) * 1000),
                    "detail": "%d versions, %d events" % (versions, len(payload.get("events") or [])),
                    "error": None if versions else "no architecture versions recorded"}
        return {"ok": False, "error": "unknown source %s" % source}
    except Exception as exc:  # noqa: BLE001
        # A traceback is kept because the whole point of this agent is to explain a
        # broken readout, and "KeyError: 'branch'" without a line number does not.
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
                "trace": traceback.format_exc(limit=3)[-600:],
                "ms": int((time.time() - started) * 1000)}


def _staleness() -> Dict[str, Any]:
    """How old the underlying writers' data is, independent of whether it renders."""
    from farm import journal

    out: Dict[str, Any] = {}
    try:
        rows = journal.history(limit=1)
        if rows:
            ts = rows[-1].get("ts")
            parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            out["cycle_age"] = int((datetime.now(timezone.utc) - parsed).total_seconds())
    except Exception as exc:  # noqa: BLE001
        out["cycle_age_error"] = str(exc)[:120]
    view = autonomy.report()
    con = view.get("contract") or {}
    out["contract_age"] = con.get("last_scan_age_seconds")
    res = view.get("research") or {}
    out["research_age"] = res.get("last_age_seconds")
    return out


def main() -> int:
    problems: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []

    for check in CHECKS:
        probe = _probe(check["source"])
        row = dict(check)
        row.update(probe)
        results.append(row)
        if not probe.get("ok"):
            problems.append({
                "severity": "breaking" if check.get("critical") else "degraded",
                "what": "dashboard readout '%s' is broken" % check["tab"],
                "why": probe.get("error") or "probe returned not-ok",
                "source": check["source"],
            })
        elif probe.get("ms", 0) > 4000:
            problems.append({
                "severity": "degraded",
                "what": "dashboard readout '%s' is too slow" % check["tab"],
                "why": "%dms to build; the page polls every 2s" % probe["ms"],
                "source": check["source"],
            })

    stale = _staleness()
    for key, tab, budget in (("cycle_age", "overview", 900),
                             ("contract_age", "drift detection", 3600),
                             ("research_age", "Findings", 86400 * 2)):
        age = stale.get(key)
        if isinstance(age, int) and age > budget:
            problems.append({
                "severity": "degraded",
                "what": "%s data is %d minutes old" % (tab, age // 60),
                "why": "its writer should run well inside %ds" % budget,
                "source": key,
            })

    # Architecture versioning. Recorded after the probes so that a snapshot which
    # cannot even be computed is reported as a broken readout first.
    arch_result: Dict[str, Any] = {}
    try:
        arch_result = architecture.record(trigger="dashboard agent scan")
    except Exception as exc:  # noqa: BLE001
        arch_result = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
        problems.append({"severity": "degraded",
                         "what": "architecture version could not be recorded",
                         "why": arch_result["error"], "source": "architecture.record"})

    autonomy_blockers = autonomy.blockers()

    row = {
        "ts": _now(),
        "checks": [{k: v for k, v in r.items() if k != "trace"} for r in results],
        "problems": problems,
        "staleness": stale,
        "architecture": {k: v for k, v in arch_result.items()
                         if k in ("recorded", "version", "short", "reason", "error")},
        "autonomy_blockers": autonomy_blockers,
        "ok": not problems,
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")

    # One order per distinct broken readout. The id is derived from the source so a
    # readout that stays broken updates its existing order instead of filing a new one
    # every 15 minutes.
    filed = 0
    for problem in problems:
        order_id = "dashboard-%s" % str(problem["source"]).replace(".", "-")
        try:
            submitted = workorders.submit(
                order_id=order_id,
                kind="dashboard",
                severity=problem["severity"],
                summary="%s: %s" % (problem["what"], problem["why"]),
                evidence={"source": problem["source"], "detail": problem["why"]},
            )
            if submitted:
                filed += 1
        except Exception as exc:  # noqa: BLE001
            print("could not file order for %s: %s" % (problem["source"], exc))

    ok = sum(1 for r in results if r.get("ok"))
    print("DASHBOARD %d/%d readouts ok" % (ok, len(results)))
    for r in results:
        mark = "ok  " if r.get("ok") else "FAIL"
        print("  %s %-26s %-24s %5sms %s"
              % (mark, r["tab"][:26], r["source"], r.get("ms", "?"),
                 r.get("detail") or r.get("error") or ""))
    if arch_result.get("recorded"):
        print("  architecture v%s recorded (%s)"
              % (arch_result.get("version"), arch_result.get("short")))
    elif arch_result.get("reason"):
        print("  architecture unchanged")
    for problem in problems:
        print("  PROBLEM [%s] %s -- %s"
              % (problem["severity"], problem["what"], problem["why"]))
    if autonomy_blockers:
        for blocker in autonomy_blockers:
            print("  AUTONOMY [%s] %s -- %s"
                  % (blocker["severity"], blocker["what"], blocker["why"]))
    if filed:
        print("  filed %d work order(s)" % filed)
    return 1 if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
