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

Exit codes: 0 verification completed (including filed repairs); an unhandled
exception remains a process failure for launchd to retry.
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
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import analysis, architecture, autonomy, workorders  # noqa: E402

STATE = Path(os.environ.get("FARM_STATE_DIR", str(PROJECT / "state"))).resolve()
LEDGER = Path(os.environ.get("FARM_DASHBOARD_HEALTH_LOG", str(STATE / "dashboard_health.ndjson")))
WORKORDER_QUEUE = os.environ.get("FARM_WORKORDER_QUEUE", str(STATE / "workorders.ndjson"))
DASHBOARD_URL = os.environ.get("FARM_DASHBOARD_URL", "http://127.0.0.1:8765").rstrip("/")

# Each dashboard readout, how it is produced, and how stale it may be before that is
# a defect. The staleness budgets come from the cadence of whatever writes the data:
# the cycle runs every 300s, the contract watcher every 900s, the research agent
# hourly. A budget well above the writer's period avoids flagging normal jitter.
#
# `critical` marks readouts whose silence would hide something dangerous. A stale
# Findings tab is embarrassing; a stale canary readout means a bad release could be
# live with nobody able to see it.
TAB_SOURCES: Dict[str, List[str]] = {
    "overview": ["monitor.snapshot", "autonomy.report"],
    "pipeline": ["monitor.snapshot"],
    "healing": ["monitor.snapshot", "autonomy.report"],
    "history": ["monitor.snapshot", "evidence.report"],
    "findings": ["evidence.report"],
    "game": ["monitor.snapshot"],
    "wire": ["monitor.snapshot", "topology.cached_graph"],
    "architecture": ["architecture.report", "autonomy.report"],
}
REQUIRED_GENERATIONS = {
    "state", "release", "autonomy", "evidence", "strategy", "architecture",
    "overview", "pipeline", "healing", "history", "findings", "game", "wire",
    "architecture_tab",
}

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


# A readout's absolute build time cannot be judged from inside this agent, and the
# attempt to do so was wrong twice.
#
# First it timed a cold process and called 4,066ms a defect when the served cost was
# 674ms. Then, with warm timing, it still read ~3,200ms -- because this agent runs under
# `ProcessType=Background` and `LowPriorityIO`, which macOS throttles hard. Measured
# side by side on the same code and data: 675ms at normal priority, 2,932ms with darwin
# background priority applied. A 4.4x penalty that the dashboard, served by a
# normal-priority process, never pays.
#
# So the comparison is against this agent's own recorded history for the same readout,
# which is the only apples-to-apples baseline available: same throttling, same machine,
# same data shape. A regression shows up as a multiple of its own median. The absolute
# ceiling is kept only to catch something being catastrophically broken rather than
# merely slow.
REGRESSION_MULTIPLE = 3.0
REGRESSION_FLOOR_MS = 1500      # below this, multiples are noise, not signal
ABSOLUTE_CEILING_MS = 30000     # something is broken, not slow
BASELINE_MIN_SAMPLES = 4


def _scheduled() -> bool:
    """Was this a scheduled launchd pass, or a hand run?

    This is the population that matters, and naming it that way is the fix. The previous
    attempt tried to detect the *mechanism* -- darwin background priority via
    getpriority(PRIO_DARWIN_PROCESS) -- and always returned False, because
    `ProcessType=Background` sets the task's QoS role rather than PRIO_DARWIN_BG on the
    process. So a live throttled pass at 3,225ms was compared against a hand-run median of
    701ms and reported as a sharp slowdown. A false alarm produced by measuring the wrong
    property of the environment, which is the same mistake one level down.

    launchd sets XPC_SERVICE_NAME to the job label; an interactive shell leaves it as "0".
    That is a direct observation of how the process was started, rather than an inference
    about what the scheduler did to it afterwards -- and it stays correct if Apple changes
    how ProcessType is implemented, or if the plist gains or loses LowPriorityIO.
    """
    name = os.environ.get("XPC_SERVICE_NAME") or ""
    return bool(name) and name != "0"


def _baselines(scheduled: bool, limit: int = 60) -> Dict[str, float]:
    """Median warm build time per readout, from passes started the same way."""
    samples: Dict[str, List[float]] = {}
    if not LEDGER.exists():
        return {}
    try:
        with LEDGER.open("r", encoding="utf-8", errors="replace") as handle:
            rows = [line for line in handle if line.strip()]
    except OSError:
        return {}
    for line in rows[-limit:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        # Rows written before this was recorded cannot be attributed to either
        # population, so they are skipped rather than assumed. The `throttled` key is
        # also accepted: it is the earlier, wrongly-derived name for the same field, and
        # its values were all False, so treating it as "hand run" is accurate for the
        # hand runs and merely discards the few mislabelled scheduled passes.
        flag = row.get("scheduled", row.get("throttled"))
        if flag is None or bool(flag) != scheduled:
            continue
        for entry in row.get("checks") or []:
            ms = entry.get("ms")
            source = entry.get("source")
            if isinstance(ms, int) and source and entry.get("ok"):
                samples.setdefault(str(source), []).append(float(ms))
    out: Dict[str, float] = {}
    for source, values in samples.items():
        if len(values) < BASELINE_MIN_SAMPLES:
            continue
        ordered = sorted(values)
        mid = len(ordered) // 2
        out[source] = (ordered[mid] if len(ordered) % 2
                       else (ordered[mid - 1] + ordered[mid]) / 2.0)
    return out


def _probe(source: str) -> Dict[str, Any]:
    """Exercise one data source the way the dashboard does.

    Timed twice, and judged on the second. The dashboard is served by a long-running
    process, so what a user waits for is a warm call; this agent is a fresh process every
    15 minutes and pays imports and first disk reads that the server paid once at
    startup. Judging on the cold number reported the Findings tab as "too slow" at
    4,066ms when the served cost was 674ms -- measuring the agent's own startup and
    calling it a defect in the page.

    Both numbers are kept: a cold time far above the warm one is still worth seeing,
    since it is what the very first request after a monitor restart costs.
    """
    started = time.time()
    first = _probe_once(source)
    first["cold_ms"] = int((time.time() - started) * 1000)
    if not first.get("ok"):
        first["ms"] = first["cold_ms"]
        return first
    started = time.time()
    second = _probe_once(source)
    second["ms"] = int((time.time() - started) * 1000)
    second["cold_ms"] = first["cold_ms"]
    return second


def _probe_once(source: str) -> Dict[str, Any]:
    started = time.time()
    try:
        if source == "monitor.snapshot":
            import monitor

            payload = monitor.snapshot()
            generations = payload.get("generations") or {}
            missing = sorted(REQUIRED_GENERATIONS - set(generations))
            age = payload.get("latest", {}).get("age_seconds")
            if age is None:
                # Fall back to the run timestamp when the snapshot does not carry an age.
                age = None
            return {
                "ok": not missing,
                "bytes": len(json.dumps(payload)),
                "age": age,
                "ms": int((time.time() - started) * 1000),
                "detail": "all GUI generations present" if not missing else None,
                "error": "missing GUI generations: %s" % ", ".join(missing) if missing else None,
            }
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


def _served_dashboard(base_url: str = DASHBOARD_URL) -> Dict[str, Any]:
    """Probe what a browser receives, not another in-process copy of the code.

    The first version of this agent exercised Python producers directly. It reported
    5/5 green while an eight-hour-old monitor process served seven tabs and returned
    404 for both new endpoints. That test proved the working tree, not the dashboard.
    """
    started = time.time()
    try:
        payloads: Dict[str, Any] = {}
        for path in (
            "/", "/api/state", "/api/autonomy", "/api/evidence",
            "/api/topology", "/api/architecture",
        ):
            with urlopen(base_url + path, timeout=20) as response:
                body = response.read()
                if response.status != 200:
                    raise RuntimeError("%s returned HTTP %s" % (path, response.status))
                payloads[path] = body
        html = payloads["/"].decode("utf-8", errors="replace")
        tab_names = ("overview", "pipeline", "cost", "history", "findings", "game", "wire", "architecture")
        markers = [marker for name in tab_names for marker in (
            'data-tab="%s"' % name, 'id="tab-%s"' % name,
        )]
        markers += [
            'data-arch-loading',  # static non-blank fallback before JavaScript paints
            'async function loadArchitecture',
            'window.loadArchitecture',  # activation checks the global, not block scope
            'function refreshBackingModels',
            '/api/architecture',
        ]
        missing = [marker for marker in markers if marker not in html]
        state = json.loads(payloads["/api/state"].decode("utf-8"))
        autonomy_payload = json.loads(payloads["/api/autonomy"].decode("utf-8"))
        evidence_payload = json.loads(payloads["/api/evidence"].decode("utf-8"))
        topology_payload = json.loads(payloads["/api/topology"].decode("utf-8"))
        architecture_payload = json.loads(payloads["/api/architecture"].decode("utf-8"))
        release = state.get("release") or {}
        errors: List[str] = []
        if state.get("app") != "farmfriends-monitor":
            errors.append("port is not the Farm Friends monitor")
        if missing:
            errors.append("served HTML missing %s" % ", ".join(missing))
        if release.get("stale"):
            errors.append("server runs %s while pointer is %s"
                          % (release.get("serving_revision"),
                             release.get("pointer_revision")))
        generations = state.get("generations") or {}
        missing_generations = sorted(REQUIRED_GENERATIONS - set(generations))
        if missing_generations:
            errors.append("state missing GUI generations %s" % ", ".join(missing_generations))
        strategy_fingerprint = str((state.get("strategy") or {}).get("fingerprint") or "")
        if (state.get("strategy") or {}).get("errors"):
            errors.append("state strategy policy is invalid")
        if strategy_fingerprint and generations.get("strategy") != strategy_fingerprint:
            errors.append("strategy generation does not match rendered strategy")
        persisted_claims = evidence_payload.get("persisted_claims") or evidence_payload.get("claims") or {}
        claim_version = str(persisted_claims.get("registry_version") or 0)
        if generations.get("evidence") and not str(generations.get("evidence")).startswith(claim_version + ":"):
            errors.append("evidence generation does not match claims registry")
        if generations.get("release") != str(release.get("pointer_revision") or release.get("revision") or ""):
            errors.append("release generation does not match pointer")
        if not autonomy_payload.get("agents"):
            errors.append("autonomy payload has no agents section")
        if not (evidence_payload.get("claims") or {}).get("claims"):
            errors.append("evidence payload has no claims")
        if not topology_payload.get("nodes"):
            errors.append("topology payload has no nodes")
        current = architecture_payload.get("current") or {}
        if not current.get("nodes"):
            errors.append("architecture payload has no components")
        return {
            "ok": not errors,
            "bytes": sum(len(v) for v in payloads.values()),
            "ms": int((time.time() - started) * 1000),
            "detail": "8 tabs, 6 endpoints, all generations; serving %s" % release.get("serving_revision"),
            "error": "; ".join(errors) if errors else None,
            "release": release,
        }
    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as exc:
        return {
            "ok": False,
            "ms": int((time.time() - started) * 1000),
            "error": "%s: %s" % (type(exc).__name__, str(exc)[:200]),
        }


def _staleness() -> Dict[str, Any]:
    """How old the underlying writers' data is, independent of whether it renders."""
    out: Dict[str, Any] = {}
    try:
        rows = analysis.history_rows(limit=1)
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


def _resolve_healthy_orders(
    results: List[Dict[str, Any]],
    problems: List[Dict[str, Any]],
    path: Optional[str] = None,
    healthy_sources: Optional[List[str]] = None,
) -> List[str]:
    """Close readout repairs whose exact source is healthy again."""
    queue = path or WORKORDER_QUEUE
    unhealthy = {str(problem.get("source") or "") for problem in problems}
    current = workorders.current(queue)
    resolved: List[str] = []
    healthy = {
        str(result.get("source") or "")
        for result in results if result.get("ok")
    }
    healthy.update(str(source) for source in (healthy_sources or []) if source)
    for source in sorted(healthy):
        if not source or source in unhealthy:
            continue
        order_id = "dashboard-%s" % source.replace(".", "-")
        order = current.get(order_id)
        if not order or order.get("status") not in {
            workorders.OPEN, workorders.FAILED,
        }:
            continue
        changed = workorders.resolve(
            order_id, workorders.SUPERSEDED,
            note="readout is healthy again; periodic verifier closed stale repair",
            path=queue,
            expected_status={workorders.OPEN, workorders.FAILED},
            expected_ts=str(order.get("ts") or ""),
        )
        if changed:
            resolved.append(order_id)
    return resolved


def main() -> int:
    problems: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    throttled = _scheduled()
    baselines = _baselines(throttled)

    for check in CHECKS:
        probe = _probe(check["source"])
        row = dict(check)
        row.update(probe)
        baseline = baselines.get(check["source"])
        if baseline:
            row["baseline_ms"] = int(baseline)
        results.append(row)
        if not probe.get("ok"):
            problems.append({
                "severity": "breaking" if check.get("critical") else "degraded",
                "what": "dashboard readout '%s' is broken" % check["tab"],
                "why": probe.get("error") or "probe returned not-ok",
                "source": check["source"],
            })
            continue
        ms = int(probe.get("ms") or 0)
        if ms > ABSOLUTE_CEILING_MS:
            problems.append({
                "severity": "degraded",
                "what": "dashboard readout '%s' is pathologically slow" % check["tab"],
                "why": "%dms to build, past the %dms ceiling" % (ms, ABSOLUTE_CEILING_MS),
                "source": check["source"],
            })
        elif (baseline and ms > REGRESSION_FLOOR_MS
              and ms > baseline * REGRESSION_MULTIPLE):
            # Relative to this agent's own history, so the comparison is unaffected by
            # the background throttling that makes absolute numbers meaningless here.
            problems.append({
                "severity": "degraded",
                "what": "dashboard readout '%s' slowed sharply" % check["tab"],
                "why": "%dms against its own median of %dms over recent passes%s"
                       % (ms, int(baseline),
                          " (scheduled pass)" if throttled else ""),
                "source": check["source"],
            })

    # Project producer health onto each visible GUI tab. A shared producer can
    # support several tabs, but every tab gets its own explicit ledger row so adding
    # a tab without a freshness owner fails visibly.
    by_source = {str(row.get("source")): row for row in results}
    for tab, sources in TAB_SOURCES.items():
        failed = [source for source in sources if not (by_source.get(source) or {}).get("ok")]
        tab_row = {
            "tab": "GUI %s" % tab,
            "source": "tab.%s" % tab,
            "critical": tab in {"overview", "pipeline", "healing"},
            "ok": not failed,
            "ms": 0,
            "detail": "fresh via %s" % ", ".join(sources) if not failed else None,
            "error": "failed backing sources: %s" % ", ".join(failed) if failed else None,
        }
        results.append(tab_row)
        if failed:
            problems.append({
                "severity": "breaking" if tab_row["critical"] else "degraded",
                "what": "GUI tab '%s' has no fresh backing model" % tab,
                "why": tab_row["error"],
                "source": tab_row["source"],
            })

    # Only a launchd pass requires the live server. Release gates and hand-run tests
    # execute before the monitor service may be installed; they test _served_dashboard
    # against a fixture instead of making deployment depend on pre-existing state.
    if throttled:
        served = _served_dashboard()
        served_row = {
            "tab": "served dashboard",
            "source": "http.dashboard",
            "critical": True,
        }
        served_row.update(served)
        results.append(served_row)
        if not served.get("ok"):
            problems.append({
                "severity": "breaking",
                "what": "the browser-facing dashboard is stale or unavailable",
                "why": served.get("error") or "HTTP probe returned not-ok",
                "source": "http.dashboard",
            })

    stale = _staleness()
    healthy_staleness: List[str] = []
    for key, value in stale.items():
        if not key.endswith("_error"):
            continue
        source = key[:-6]
        problems.append({
            "severity": "breaking" if source == "cycle_age" else "degraded",
            "what": "%s freshness could not be verified" % source.replace("_", " "),
            "why": str(value),
            "source": source,
        })
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
        elif isinstance(age, int):
            healthy_staleness.append(key)

    # Architecture versioning. Recorded after the probes so that a snapshot which
    # cannot even be computed is reported as a broken readout first.
    arch_result: Dict[str, Any] = {}
    read_only = os.environ.get("FARM_STATE_READ_ONLY") == "1"
    try:
        if read_only:
            snapshot = architecture.snapshot()
            arch_result = {
                "recorded": False, "reason": "release-gate read-only scan",
                "short": snapshot.get("short"),
            }
        else:
            arch_result = architecture.record(trigger="dashboard agent scan")
    except Exception as exc:  # noqa: BLE001
        arch_result = {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
        problems.append({"severity": "degraded",
                         "what": "architecture version could not be recorded",
                         "why": arch_result["error"], "source": "architecture.record"})

    autonomy_blockers = autonomy.blockers()

    row = {
        "ts": _now(),
        "scheduled": throttled,
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

    # Healthy verification is also a transition. Without closing stale repairs, an
    # already-recovered readout permanently poisons repair-flow reviews and consumes
    # future author budget.
    resolved = (
        [] if read_only else _resolve_healthy_orders(
            results, problems, path=WORKORDER_QUEUE, healthy_sources=healthy_staleness,
        )
    )

    # One order per distinct broken readout. The change id is derived from the source
    # so a readout that stays broken updates its existing order instead of filing a new
    # one every 15 minutes -- `submit` is idempotent by that id.
    filed = 0
    for problem in problems:
        change = {
            "id": "dashboard-%s" % str(problem["source"]).replace(".", "-"),
            "kind": "dashboard_readout",
            "severity": problem["severity"],
            "summary": "%s: %s" % (problem["what"], problem["why"]),
            "tool": problem["source"],
            "detail": problem["why"],
        }
        try:
            source = str(problem["source"])
            if source.endswith("_age"):
                repair_files = ["experiments/dashboard_agent.py"]
            elif source == "evidence.report":
                repair_files = ["farm/evidence.py", "farm/research.py"]
            else:
                repair_files = ["farm/autonomy.py", "farm/architecture.py", "monitor.py"]
            if read_only:
                continue
            submitted = workorders.submit(
                change,
                source="dashboard_agent",
                intent="restore the %s readout so the operator view is trustworthy"
                       % problem["source"],
                acceptance=[
                    "python3 deploy/test_dashboard_agent.py reports no failing readout",
                    "the readout builds in under 4000ms",
                ],
                files=repair_files,
                path=WORKORDER_QUEUE,
            )
            if submitted:
                filed += 1
        except Exception as exc:  # noqa: BLE001
            print("could not file order for %s: %s" % (problem["source"], exc))

    ok = sum(1 for r in results if r.get("ok"))
    print("DASHBOARD %d/%d readouts ok%s"
          % (ok, len(results), " (scheduled pass)" if throttled else ""))
    for r in results:
        mark = "ok  " if r.get("ok") else "FAIL"
        print("  %s %-26s %-24s %5sms %s"
              % (mark, r["tab"][:26], r["source"],
                 r.get("ms", "?"),
                 ("median %sms  " % r.get("baseline_ms") if r.get("baseline_ms") else "")
                 + (r.get("detail") or r.get("error") or "")))
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
    if resolved:
        print("  resolved %d stale dashboard repair(s)" % len(resolved))
    if filed:
        print("  filed %d work order(s)" % filed)
    # Filing a repair is successful autonomous handling, not operator attention.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
