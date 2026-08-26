#!/usr/bin/env python3
"""Read-only local Farm Friends monitor.

Run with: python3 monitor.py
The monitor only reads state files and launchd metadata. It never calls the
farm API and has no controls that can mutate the farm.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

# Reuse the operator's own modules so the dashboard can never disagree with the
# loop about knobs, cost, or which agents are supposed to exist.
from farm import evidence, growth, heal, progress, release as release_info, rules, scheduler, tokens, topology  # noqa: E402

STATE = PROJECT / "state"
HISTORY = STATE / "history.ndjson"
INTENTS = STATE / "intents.ndjson"
TOOL_CALLS = STATE / "tool_calls.ndjson"
ALERTS = STATE / "alerts.ndjson"
LOG = STATE / "launchd.log"
LABEL = "com.nickfigura.farmfriends"
APP_ID = "farmfriends-monitor"   # identity marker in /api/state, so a launcher can
                                # tell our dashboard from anything else on the port
DEFAULT_PORT = 8765
PORT_SEARCH = 10                 # how far past the requested port to look
CADENCE_SECONDS = 300


def _json_lines(path: Path, limit: int = 100) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    rows: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _json_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_seconds(value: Optional[str]) -> Optional[int]:
    parsed = _timestamp(value)
    if not parsed:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _launchd() -> Dict[str, Any]:
    """Status of every agent, via the operator's own scheduler module.

    This used to report the cycle and the supervisor only. That was fine when they
    were the only two agents, and quietly wrong once five more existed: the contract
    watcher, author, research, expand and recovery agents could all have been unloaded
    behind a fully green page. The author agent is the worst of those to lose silently,
    because "no repairs happened" and "nothing could repair anything" look identical
    from the outside.

    The cycle and supervisor keep their original position in the payload so existing
    panels and tests continue to read them unchanged; `all` carries the full set.
    """
    try:
        cycle_agent = scheduler.status(scheduler.CYCLE_LABEL)
        supervisor = scheduler.status(scheduler.SUPERVISOR_LABEL)
    except Exception as exc:  # noqa: BLE001
        return {"loaded": False, "state": "unknown", "detail": str(exc)[:120], "supervisor": {}}
    cycle_agent["supervisor"] = supervisor
    try:
        from farm import autonomy

        view = autonomy.agents()
        cycle_agent["all"] = view.get("agents") or []
        cycle_agent["live"] = view.get("live")
        cycle_agent["expected"] = view.get("expected")
        cycle_agent["down"] = view.get("down") or []
    except Exception as exc:  # noqa: BLE001
        cycle_agent["all_error"] = str(exc)[:120]
    return cycle_agent


def _match(text: str, pattern: str) -> Optional[str]:
    found = re.search(pattern, text, re.MULTILINE)
    return found.group(1) if found else None


def _current_intent(intents: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Infer the current stage from start/done intent pairs."""
    active: Dict[str, Dict[str, Any]] = {}
    latest: Optional[Dict[str, Any]] = None
    for item in intents:
        action = str(item.get("action", ""))
        latest = item
        if action.endswith("_start"):
            active[action[:-6]] = item
        elif action.endswith("_done"):
            active.pop(action[:-5], None)
    if active:
        key, item = next(reversed(active.items()))
        return {
            "active": True,
            "stage": key.replace("_", " "),
            "ts": item.get("ts"),
            "detail": item.get("detail") or {},
        }
    return {
        "active": False,
        "stage": "idle",
        "ts": latest.get("ts") if latest else None,
        "detail": latest.get("detail") if latest else {},
    }


def _log_tail(limit: int = 12) -> List[str]:
    try:
        return LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    except (FileNotFoundError, OSError):
        return []


def _blockers(
    latest: Dict[str, Any],
    launchd: Dict[str, Any],
    current: Dict[str, Any],
    release: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    blockers: List[Dict[str, str]] = []
    anomalies = latest.get("anomalies") or []
    for anomaly in anomalies:
        blockers.append({"level": "error", "text": str(anomaly)})

    # Not farm failures, but both mean the operator is looking at code other than the
    # current pointer. The stale-process case is stronger: even the HTML and registered
    # routes may be old, which is how the Architecture tab disappeared for 8.5 hours.
    if (release or {}).get("stale"):
        blockers.append(
            {
                "level": "error",
                "text": "Dashboard process is stale: serving "
                        f"{release.get('serving_revision')} while release points to "
                        f"{release.get('pointer_revision')}",
            }
        )
    elif (release or {}).get("diverged"):
        blockers.append(
            {
                "level": "warn",
                "text": "Dashboard is running working-tree code that differs from "
                        f"the live release ({release.get('revision')})",
            }
        )

    feed = latest.get("feed")
    reserve = latest.get("reserve_target")
    if feed is not None and reserve is not None and feed < reserve:
        shortfall = reserve - feed
        tolerance = max(
            rules.FEED_RESERVE_TOLERANCE_MIN,
            int(reserve * rules.FEED_RESERVE_TOLERANCE_FRACTION),
        )
        runway = rules.feed_buffer_minutes(feed, int(latest.get("animals") or 0))
        if shortfall > tolerance or runway < rules.FEED_BUFFER_MIN_MINUTES:
            blockers.append({"level": "error", "text": f"Feed safety breach: {runway:.0f}m runway; {shortfall:,} below reserve"})
        else:
            blockers.append({"level": "ok", "text": f"Feed reserve within concurrency tolerance: {runway:.0f}m runway"})

    if latest.get("rank") not in (None, 1):
        blockers.append({"level": "error", "text": f"Leaderboard position is #{latest.get('rank')}"})

    age = _age_seconds(latest.get("ts"))
    if age is None:
        blockers.append({"level": "error", "text": "No completed farm run recorded"})
    elif age > CADENCE_SECONDS * 2 and not current.get("active"):
        blockers.append({"level": "error", "text": f"Last completed run is {age // 60}m old"})

    if not launchd.get("loaded"):
        blockers.append({"level": "error", "text": "launchd cycle agent is not loaded"})
    if not (launchd.get("supervisor") or {}).get("loaded"):
        blockers.append(
            {"level": "error", "text": "supervisor agent is not loaded - no self-healing"}
        )

    # Autonomy problems belong on the overview, not only on their own tab. An operator
    # glancing at the page should not have to go looking for "the author agent is down"
    # or "the canary wants to roll back"; those stop the loop healing itself, which is
    # at least as serious as anything about feed.
    try:
        from farm import autonomy

        for item in autonomy.blockers():
            blockers.append({
                "level": "error" if item.get("severity") == "critical" else "warn",
                "text": "%s - %s" % (item.get("what"), item.get("why")),
            })
    except Exception as exc:  # noqa: BLE001
        # The autonomy view failing is itself worth showing, since it is the thing
        # that reports on everything else.
        blockers.append({"level": "warn",
                         "text": "autonomy view unavailable: %s" % str(exc)[:120]})

    if not blockers:
        blockers.append({"level": "ok", "text": "No active blockers"})
    return blockers


def snapshot() -> Dict[str, Any]:
    history = _json_lines(HISTORY, 100)
    intents = _json_lines(INTENTS, 80)
    latest = history[-1] if history else {}
    previous = history[-2] if len(history) > 1 else {}
    launchd = _launchd()
    current = _current_intent(intents)
    release = _release_info()
    age = _age_seconds(latest.get("ts"))
    if current["active"]:
        health = "running"
    elif not launchd["loaded"]:
        health = "offline"
    elif age is not None and age <= CADENCE_SECONDS * 2:
        health = "healthy"
    else:
        health = "stale"

    rivals = latest.get("rivals") or {}
    rival_rows = []
    previous_rivals = previous.get("rivals") or {}
    for name, produce in sorted(rivals.items(), key=lambda pair: (-pair[1], pair[0].lower())):
        rival_rows.append(
            {
                "name": name,
                "produce": produce,
                "delta": produce - previous_rivals.get(name, produce),
                "gap": (latest.get("produce") or 0) - produce,
            }
        )

    return {
        "app": APP_ID,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "health": health,
        "cadence_seconds": CADENCE_SECONDS,
        "latest": latest,
        "trend": history[-20:],
        "leaderboard": rival_rows,
        "leaderboard_history": _leaderboard_history(history),
        "current": current,
        "launchd": launchd,
        "blockers": _blockers(latest, launchd, current, release),
        "log_tail": _log_tail(),
        "release": release,
        "tokens": _tokens_summary(),
        "heal": _heal_summary(),
        "pipeline": _pipeline(),
        "trace": _trace(),
        "growth": _growth_summary(),
        "recovery_watch": _json_object(STATE / "recovery_watch.json"),
        "cost": _cost_detail(history),
        "signals": _signals(latest, previous),
        "scene": _scene(latest, previous),
    }


def _leaderboard_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact score snapshots for the Overview race chart.

    History already contains the authoritative leaderboard read from each run;
    this projection adds no farm calls and intentionally keeps only chart fields.
    """
    points: List[Dict[str, Any]] = []
    for row in history:
        scores: Dict[str, int] = {}
        own = row.get("produce")
        if isinstance(own, (int, float)):
            scores["Nick"] = int(own)
        for name, value in (row.get("rivals") or {}).items():
            if isinstance(name, str) and isinstance(value, (int, float)):
                scores[name] = int(value)
        if scores:
            points.append({"run": row.get("run"), "ts": row.get("ts"), "scores": scores})
    return points


def _scene(latest: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    """Numbers the farm view draws itself from, normalised to 0..1 where it helps.

    The panel is a picture, but it is a picture of measurements: every element on
    it is one of these fields and nothing on it is decorative. Doing the
    normalisation here rather than in JavaScript keeps the thresholds next to the
    rules that define them - a silo that looks full at 80% of a reserve target the
    loop has since changed would be a lie told in CSS.
    """
    from farm import rules

    by_kind = latest.get("by_kind") or {}
    species_data = evidence.species()
    species_status = {
        item["kind"]: {
            "share": item.get("share"),
            "recent_units_per_animal_min": item.get("recent_units_per_animal_min"),
            "recent_vs_chicken": item.get("recent_vs_chicken"),
            "verdict": item.get("verdict"),
        }
        for item in species_data.get("table", [])
    }
    feed = latest.get("feed")
    reserve = latest.get("reserve_target")
    hunger = latest.get("max_hunger")
    ready = latest.get("ready_units")
    produce = latest.get("produce")
    before = previous.get("produce") if previous else None
    return {
        "animals": latest.get("animals"),
        "by_kind": by_kind,
        "species_status": species_status,
        "feed": feed,
        "reserve_target": reserve,
        "feed_fill": None if not reserve else max(0.0, min(1.0, (feed or 0) / reserve)),
        "feed_runway_min": rules.feed_buffer_minutes(feed or 0, int(latest.get("animals") or 0)),
        "feed_runway_floor_min": rules.FEED_BUFFER_MIN_MINUTES,
        "hunger": hunger,
        "hunger_stop": rules.HUNGER_STOP,
        "hunger_fill": None if hunger is None else max(0.0, min(1.0, hunger / rules.HUNGER_STOP)),
        "ready_units": ready,
        "coins": latest.get("coins"),
        "rank": latest.get("rank"),
        "produce": produce,
        # Produce per second, for interpolating the counter between polls. The
        # rate is measured over the real interval, not assumed from the cadence.
        "produce_per_sec": (round(latest["produce_per_min"] / 60.0, 3)
                            if isinstance(latest.get("produce_per_min"), (int, float)) else None),
        "produce_delta": (produce - before) if isinstance(produce, int) and isinstance(before, int) else None,
        "ts": latest.get("ts"),
        "fed": latest.get("fed"),
        "harvested": latest.get("harvested"),
        "revenue": latest.get("revenue"),
        "feed_share": latest.get("feed_share"),
    }


def _cost_detail(history: List[Dict[str, Any]], recent: int = 5) -> Dict[str, Any]:
    """Per-run LLM spend, plus what the healer did instead of spending it.

    The ledger is the point of this panel: routine runs write an explicit zero, so
    "it costs nothing" is a measurement rather than a claim. A row only carries a
    cost when an alert survived healing and woke a model.
    """
    try:
        ledger = tokens.tail()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120], "runs": []}

    by_run: Dict[int, Dict[str, Any]] = {}
    for row in ledger:
        run = row.get("run")
        if run is None:
            continue
        entry = by_run.setdefault(
            int(run),
            {"run": int(run), "tokens": 0, "tokens_in": 0, "tokens_out": 0,
             "cost_usd": 0.0, "escalations": 0, "healed": 0, "ts": row.get("ts"),
             "kinds": [], "notes": []},
        )
        entry["tokens"] += int(row.get("tokens") or 0)
        entry["tokens_in"] += int(row.get("tokens_in") or 0)
        entry["tokens_out"] += int(row.get("tokens_out") or 0)
        entry["cost_usd"] += float(row.get("cost_usd") or 0.0)
        entry["escalations"] += 1 if row.get("escalated") else 0
        entry["healed"] += int(row.get("healed") or 0)
        entry["ts"] = row.get("ts") or entry["ts"]
        if row.get("kind"):
            entry["kinds"].append(str(row["kind"]))
        if row.get("note"):
            entry["notes"].append(str(row["note"]))

    # Alerts recorded per run give the "why" behind any non-zero row, and show
    # what was raised even on runs the healer settled for free.
    raised: Dict[int, List[str]] = {}
    for row in _json_lines(ALERTS, 200):
        run = row.get("run")
        if run is None:
            continue
        raised.setdefault(int(run), []).append(str(row.get("alert") or ""))

    runs = sorted(by_run)[-recent:]
    recent_rows = []
    for run in runs:
        entry = dict(by_run[run])
        entry["cost_usd"] = round(entry["cost_usd"], 6)
        entry["kinds"] = sorted(set(entry["kinds"]))
        entry["alerts"] = raised.get(run, [])
        recent_rows.append(entry)

    charged = [r for r in by_run.values() if r["cost_usd"] > 0]
    return {
        "runs": recent_rows[::-1],
        "recent_window": len(recent_rows),
        "recent_cost_usd": round(sum(r["cost_usd"] for r in recent_rows), 6),
        "recent_tokens": sum(r["tokens"] for r in recent_rows),
        "ledger_runs": len(by_run),
        "charged_runs": len(charged),
        "free_runs": len(by_run) - len(charged),
        "first_ts": (ledger[0].get("ts") if ledger else None),
    }


def _signals(latest: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    """The measures the loop actually judges itself by, for the pipeline tab.

    Score rate leads deliberately: lifetime produce accrues as animals produce,
    while collect_produce returns nothing whenever the herd is hungry (the produce
    banks during the feed call instead). Collection counts are therefore a lagging
    proxy, and a run can bank thousands of eggs while recording collected={}.
    """
    from farm import rules  # local import keeps the module list in one place

    rate = latest.get("produce_per_min")
    if rate is None and previous and latest.get("produce") and previous.get("produce"):
        minutes = latest.get("interval_min") or 0
        if minutes:
            rate = round((latest["produce"] - previous["produce"]) / minutes, 1)
    animals = latest.get("animals")
    floor = rules.produce_floor(animals)
    prev_rate = previous.get("produce_per_min") if previous else None
    return {
        "produce_per_min": rate,
        "floor": round(floor, 1),
        "prev_produce_per_min": prev_rate,
        "below_floor": rate is not None and rate < floor,
        "prev_below_floor": prev_rate is not None and prev_rate < floor,
        "hunger": latest.get("max_hunger"),
        "hunger_stop": rules.HUNGER_STOP,
        "hunger_alarm": rules.HUNGER_ALARM,
        "feed": latest.get("feed"),
        "reserve_target": latest.get("reserve_target"),
        "units_collected": latest.get("units_collected"),
        "soft": latest.get("notes_soft") or [],
        "calls": latest.get("calls"),
        "call_rate": latest.get("call_rate"),
    }


def _growth_summary() -> Dict[str, Any]:
    try:
        data = growth.status()
        data["marginal_threshold_pct"] = rules.GROWTH_MIN_MARGINAL_GAIN * 100.0
        return data
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}


def _pipeline() -> Dict[str, Any]:
    try:
        data = progress.read()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120], "steps": []}
    # Median duration per step across recent runs gives each bar a baseline to
    # compare against, which is what makes a slow step obvious at a glance.
    history = _json_lines(HISTORY, 30)
    samples: Dict[str, List[float]] = {}
    for row in history[-12:]:
        for name, seconds in (row.get("phases") or {}).items():
            samples.setdefault(name, []).append(float(seconds))
    baseline = {}
    for name, values in samples.items():
        values.sort()
        baseline[name] = round(values[len(values) // 2], 1)
    data["baseline"] = baseline
    status = str(data.get("status") or "idle")
    updated = _trace_epoch(data.get("updated_ts"))
    timeout_s = float(data.get("timeout_s") or 240)
    stale = status == "running" and updated is not None and time.time() - updated > max(90.0, timeout_s)
    data["effective_status"] = "stalled" if stale else status
    data["stalled"] = stale
    return data


# Before full boundary tracing was added to farm/mcp.py, selected mutations were
# recorded as intent start/end pairs. They remain the fallback for releases that
# predate state/tool_calls.ndjson; the UI labels that coverage "mutations only"
# instead of implying that read-only calls were observed.
INTENT_TOOLS = {
    "sell": ("sell", "sell"),
    "buy_feed": ("buy_feed", "buy_feed"),
    "feed_animals": ("feed_animals", "feed"),
    "backstop_feed": ("feed_animals", "feed"),
    "adopt_batch_start": ("adopt_animal", "adopt"),
    "respond_to_trade": ("respond_to_trade", "trades"),
    "propose_trade": ("propose_trade", "offers"),
}
INTENT_ENDS = {
    "sell_done": "sell",
    "buy_feed_done": "buy_feed",
    "feed_animals_done": "feed_animals",
    "adopt_batch_done": "adopt_batch_start",
}


def _trace_epoch(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _call_step(call_ts: Optional[float], tool: str, pipeline: Dict[str, Any],
               graph: Dict[str, Any]) -> Optional[str]:
    """Assign a boundary span to the measured step containing its start time."""
    if call_ts is not None:
        owner = None
        latest_start = float("-inf")
        for step in pipeline.get("steps") or []:
            start = _trace_epoch(step.get("started_ts"))
            end = _trace_epoch(step.get("ended_ts") or pipeline.get("updated_ts"))
            if (start is not None and call_ts >= start - 0.2
                    and (end is None or call_ts <= end + 0.5)
                    and start > latest_start):
                # Adjacent progress timestamps can be equal to the second. The
                # latest matching start owns the call, not the step that just
                # ended and happened to appear first in the list.
                owner = step.get("name")
                latest_start = start
        if owner:
            return owner
    # A timestamp can land in the sub-second gap between progress writes. Static
    # reachability is an honest fallback only when exactly one step owns the tool.
    owners = []
    for node in graph.get("nodes") or []:
        if node.get("kind") == "tool" and node.get("label") == tool:
            owners = list(node.get("steps") or [])
            break
    return owners[0] if len(owners) == 1 else None


def _boundary_calls(pipeline: Dict[str, Any], graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pair MCP rows belonging to this cycle run, excluding concurrent probes/expansion."""
    run_start = _trace_epoch(pipeline.get("started_ts"))
    run_finish = _trace_epoch(pipeline.get("finished_ts"))
    run_id = pipeline.get("run")
    calls: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    # Read a bounded tail, then filter by exact telemetry context before applying
    # the display cap. Legacy rows without context retain the time-bound fallback.
    for row in _json_lines(TOOL_CALLS, 5000):
        call_id = str(row.get("id") or "")
        if not call_id:
            continue
        ts = _trace_epoch(row.get("ts"))
        actor = row.get("actor")
        recorded_run = row.get("run")
        contextual = actor is not None or recorded_run is not None
        if contextual and actor not in (None, "cycle"):
            continue
        if contextual and run_id is not None and recorded_run not in (None, run_id):
            continue
        if run_start is not None and ts is not None and ts < run_start - 1:
            continue
        if run_finish is not None and ts is not None and ts > run_finish + 1:
            continue
        if row.get("event") == "start":
            calls[call_id] = {
                "id": call_id,
                "tool": str(row.get("tool") or "unknown"),
                "step": row.get("step"),
                "actor": actor,
                "run": recorded_run,
                "worker": row.get("worker"),
                "started_ts": row.get("ts"),
                "ended_ts": None,
                "duration_ms": None,
                "status": "active",
                "arguments": row.get("arguments") or {},
                "result": None,
                "error": None,
                "source": "boundary",
            }
            order.append(call_id)
        elif row.get("event") == "end":
            call = calls.get(call_id)
            if call is None:
                continue
            call.update({
                "ended_ts": row.get("ts"),
                "duration_ms": row.get("duration_ms"),
                "status": "ok" if row.get("ok") else "error",
                "result": row.get("result"),
                "error": row.get("error"),
            })
    result = []
    for call_id in order:
        call = calls.get(call_id)
        if not call:
            continue
        if not call.get("step"):
            call["step"] = _call_step(_trace_epoch(call.get("started_ts")), call["tool"], pipeline, graph)
        result.append(call)
    return result


def _intent_calls(pipeline: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Legacy mutation spans. Useful, but explicitly incomplete."""
    run_start = _trace_epoch(pipeline.get("started_ts"))
    calls: List[Dict[str, Any]] = []
    open_by_action: Dict[str, List[Dict[str, Any]]] = {}
    for index, row in enumerate(_json_lines(INTENTS, 400)):
        ts = _trace_epoch(row.get("ts"))
        if run_start is not None and ts is not None and ts < run_start - 1:
            continue
        action = str(row.get("action") or "")
        if action in INTENT_TOOLS:
            tool, step = INTENT_TOOLS[action]
            call = {
                "id": "intent-%s-%d" % (row.get("ts") or "", index),
                "tool": tool,
                "step": step,
                "started_ts": row.get("ts"),
                "ended_ts": None,
                "duration_ms": None,
                "status": "active",
                "arguments": row.get("detail") or {},
                "result": None,
                "error": None,
                "source": "intent",
            }
            calls.append(call)
            open_by_action.setdefault(action, []).append(call)
            continue
        start_action = INTENT_ENDS.get(action)
        if not start_action:
            continue
        candidates = open_by_action.get(start_action) or []
        if not candidates and start_action == "feed_animals":
            candidates = open_by_action.get("backstop_feed") or []
        if not candidates:
            continue
        call = candidates.pop(0)
        call["ended_ts"] = row.get("ts")
        end = _trace_epoch(row.get("ts"))
        start = _trace_epoch(call.get("started_ts"))
        call["duration_ms"] = round(max(0, end - start) * 1000, 1) if end is not None and start is not None else None
        call["status"] = "ok"
        call["result"] = row.get("detail") or {}
    return calls[-1000:]


def snapshot_trace_fingerprint() -> Optional[str]:
    """The fingerprint /api/state advertises, so both endpoints agree."""
    try:
        return hashlib.sha1(repr(topology.signature()).encode("utf-8")).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return None


def _trace() -> Dict[str, Any]:
    """The small, per-poll half of the 2D run trace.

    Static reachability remains on /api/topology. This payload contains only
    measured run state: paired MCP boundary spans when the deployed client emits
    them, or the older mutation-intent pairs with an explicit coverage warning.
    """
    try:
        fingerprint = snapshot_trace_fingerprint()
        pipeline = progress.read() or {}
        graph = topology.cached_graph()
        boundary = _boundary_calls(pipeline, graph)
        observed_calls = len(boundary)
        truncated = observed_calls > 1000
        calls = boundary[:1000]
        coverage = "partial" if truncated else ("full" if calls else "mutations_only")
        if not calls:
            calls = _intent_calls(pipeline)
            observed_calls = len(calls)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120], "fingerprint": None, "calls": [],
                "coverage": "unavailable", "activity": []}

    # Compatibility for an older dashboard bundle during a monitor restart. New
    # clients consume calls; old ones still turn activity into packets.
    activity = []
    for call in calls:
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        label = call.get("tool") or "tool"
        if arguments.get("item"):
            label = "%s %s" % (label, arguments["item"])
        elif arguments.get("qty") is not None:
            label = "%s %s" % (label, arguments["qty"])
        activity.append({
            "key": call.get("id"),
            "ts": call.get("started_ts"),
            "tool": call.get("tool"),
            "step": call.get("step"),
            "label": label,
        })
    return {
        "fingerprint": fingerprint,
        "calls": calls,
        "coverage": coverage,
        "truncated": truncated,
        "observed_calls": observed_calls,
        "returned_calls": len(calls),
        "activity": activity[-40:],
        "run_started_ts": pipeline.get("started_ts"),
        "run_finished_ts": pipeline.get("finished_ts"),
        "effective_status": pipeline.get("effective_status") or pipeline.get("status"),
    }


def _tokens_summary() -> Dict[str, Any]:
    try:
        return tokens.summary()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}


def _heal_summary() -> Dict[str, Any]:
    try:
        log = heal.recent(40)[::-1]
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:120]}
    # Group by alert class: several queued copies of one alert describe one
    # condition, so the class is the unit an operator actually reasons about.
    classes: Dict[str, Dict[str, Any]] = {}
    for item in log:
        name = str(item.get("class") or "unknown")
        entry = classes.setdefault(
            name, {"class": name, "count": 0, "last_ts": None, "last_run": None,
                   "last_action": None, "alerts": []}
        )
        entry["count"] += 1
        if entry["last_ts"] is None:
            entry["last_ts"] = item.get("ts")
            entry["last_run"] = item.get("run")
            entry["last_action"] = item.get("action")
        alert = item.get("alert")
        if alert and alert not in entry["alerts"]:
            entry["alerts"].append(str(alert)[:160])
    try:
        knobs = heal.effective_knobs()
    except Exception as exc:  # noqa: BLE001
        knobs = {"error": str(exc)[:120]}
    return {
        "knobs": knobs,
        "recent": log[:12],
        "classes": sorted(classes.values(), key=lambda x: -x["count"]),
        "total": len(log),
    }


def _canonical_root() -> Path:
    """Find the checkout even when this process runs inside releases/<revision>/."""
    for candidate in (PROJECT,) + tuple(PROJECT.parents):
        deploy = candidate / "deploy"
        if deploy.is_dir() and any(deploy.glob("com.nickfigura.farmfriends*.plist")):
            return candidate
    return PROJECT


def _revision(root: Path, fallback: str) -> str:
    try:
        return (root / "RELEASED").read_text(encoding="utf-8").strip() or fallback
    except OSError:
        return fallback


def _release_info() -> Dict[str, Any]:
    """Identify both the code this process serves and the current release pointer.

    Those are deliberately separate. The old implementation used PROJECT/release for
    both. When monitor.py itself ran from releases/<rev>/ that path meant
    releases/<rev>/release, which does not exist; it reported ``unpublished`` and, more
    dangerously, ``diverged: false`` because one fingerprint was null. When an older
    hand-started process stayed alive for eight hours it had no way to say that its HTML
    and routes predated the pointer.
    """
    root = _canonical_root()
    pointer = root / "release"
    try:
        pointer_target = pointer.resolve(strict=True)
    except OSError:
        pointer_target = pointer

    serving_revision = _revision(PROJECT, "working-tree")
    pointer_revision = _revision(pointer_target, "unpublished")
    serving_fp = release_info.fingerprint(str(PROJECT))
    pointer_fp = release_info.fingerprint(str(pointer_target))
    comparable = bool(serving_fp and pointer_fp)
    stale = bool(pointer_revision != "unpublished"
                 and serving_revision != pointer_revision)
    return {
        # Backward-compatible: revision/target continue to mean the current pointer.
        "revision": pointer_revision,
        "target": str(pointer_target),
        "pointer_revision": pointer_revision,
        "serving_revision": serving_revision,
        "stale": stale,
        "serving_fingerprint": serving_fp,
        "live_fingerprint": pointer_fp,
        "tree_fingerprint": serving_fp,
        # Unknown is represented as None, never as the falsely reassuring False.
        "diverged": (serving_fp != pointer_fp) if comparable else None,
        "dashboard_root": str(PROJECT),
        "project_root": str(root),
    }


GAME_DIR = PROJECT / "game"
def _game_asset(name: str) -> str:
    """Read a game asset, or return a visible stub rather than a broken tab.

    The game lives in game/ as real .css/.html/.js files instead of inside this
    module's HTML string: it is now an incremental game with a simulation worth
    unit-testing, and a 600-line string literal is not somewhere you can do that.
    Both the dashboard tab and the standalone export compose from these same
    files, so the two can never drift.
    """
    try:
        return (GAME_DIR / name).read_text(encoding="utf-8")
    except OSError as exc:
        return "/* missing game asset %s: %s */" % (name, exc)


GAME_CSS = _game_asset("coop_rush.css")
GAME_MARKUP = _game_asset("coop_rush.html")
GAME_JS = _game_asset("coop_rush.js") + "\n" + _game_asset("coop_rush_ui.js")

DASHBOARD_DIR = PROJECT / "dashboard"


def _dashboard_asset(name: str) -> str:
    """Read a dashboard asset, or a visible stub rather than a broken panel.

    The trace explorer lives in dashboard/ for the same reason the game does:
    span derivation and call-path routing are logic worth unit-testing in
    JavaScriptCore, while logic inside a Python string literal can only be hoped
    about.
    """
    try:
        return (DASHBOARD_DIR / name).read_text(encoding="utf-8")
    except OSError as exc:
        return "/* missing dashboard asset %s: %s */" % (name, exc)


TRACE_CSS = _dashboard_asset("trace_explorer.css")
TRACE_JS = _dashboard_asset("trace_explorer.js")
# The switchboard is the animated companion to the trace: same measured boundary
# spans, drawn as traffic instead of rows. It is a separate asset pair so a throw
# in one cannot take the other's tab down.
WIRE_CSS = _dashboard_asset("mcp_wire.css")
WIRE_JS = _dashboard_asset("mcp_wire.js")
# The architecture tab is a separate asset pair for the same reason: its layout and
# diff rendering are logic worth testing in JavaScriptCore, and a throw while drawing
# the diagram must not take the rest of the page down.
ARCH_CSS = _dashboard_asset("architecture.css")
ARCH_JS = _dashboard_asset("architecture.js")
# Shared operator hierarchy for the dashboard shell and seven standard tabs. The
# Architecture tab uses the same hierarchy through its specialized source-map assets.
OPERATOR_CSS = _dashboard_asset("operator.css")
OPERATOR_JS = _dashboard_asset("operator.js")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nick's Farm Friends Monitor</title>
<style>
:root { color-scheme: dark; --bg:#101714; --panel:#18231e; --panel2:#1e2d26; --line:#30463a; --text:#edf7ef; --muted:#9cb4a4; --green:#72e09a; --yellow:#f5cc75; --red:#ff8a83; --blue:#8fc8ff; }
* { box-sizing:border-box; } body { margin:0; background:radial-gradient(circle at 20% 0%,#1d3528 0,#101714 42%); color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:1400px; margin:0 auto; padding:24px; } header { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:22px; }
h1 { margin:0 0 4px; font-size:26px; letter-spacing:-.03em; } .subtitle { color:var(--muted); } .refresh { color:var(--muted); font-size:12px; text-align:right; }
.grid { display:grid; gap:14px; grid-template-columns:repeat(12,1fr); } .card { min-width:0; background:rgba(24,35,30,.9); border:1px solid var(--line); border-radius:14px; padding:16px; box-shadow:0 8px 28px #0002; }
.status { grid-column:span 3; } .blockers { grid-column:span 5; } .release { grid-column:span 4; } .wide { grid-column:span 8; } .side { grid-column:span 4; } .full { grid-column:1/-1; } .cost { grid-column:span 6; } .healing { grid-column:span 6; }
.card h2 { margin:0 0 13px; font-size:14px; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); } .big { font-size:32px; font-weight:750; letter-spacing:-.04em; }
.pill { display:inline-flex; align-items:center; gap:7px; border-radius:99px; padding:5px 10px; font-weight:700; font-size:12px; text-transform:uppercase; letter-spacing:.06em; } .pill:before { content:""; width:8px; height:8px; border-radius:50%; background:currentColor; box-shadow:0 0 12px currentColor; }
.running,.healthy,.ok { color:var(--green); } .offline,.error,.stale { color:var(--red); } .waiting,.warn { color:var(--yellow); }
.alert.warn .alert-dot { background:var(--yellow); }
.metrics { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; margin-top:16px; } .metric label { display:block; color:var(--muted); font-size:12px; } .metric strong { display:block; font-size:20px; margin-top:2px; }
.kv { display:grid; grid-template-columns:1fr 1fr; gap:8px 18px; } .kv div { display:flex; justify-content:space-between; gap:12px; border-bottom:1px solid #ffffff0d; padding:4px 0; } .kv span:first-child { color:var(--muted); } .kv span:last-child { text-align:right; font-weight:650; }
ul { list-style:none; padding:0; margin:0; } li { padding:8px 0; border-bottom:1px solid #ffffff0d; } li:last-child { border-bottom:0; }
.alert { display:flex; gap:9px; align-items:flex-start; } .alert-dot { flex:0 0 8px; height:8px; margin-top:6px; border-radius:50%; background:var(--red); } .alert.ok .alert-dot { background:var(--green); }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; } th,td { text-align:right; padding:8px 7px; border-bottom:1px solid #ffffff0d; white-space:nowrap; } th:first-child,td:first-child, th:nth-child(2),td:nth-child(2) { text-align:left; } th { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.06em; } td.bad { color:var(--red); } td.good { color:var(--green); }
.chart { height:150px; width:100%; } svg { width:100%; height:100%; overflow:visible; } .axis { stroke:var(--line); stroke-width:1; } .line { fill:none; stroke:var(--green); stroke-width:3; stroke-linecap:round; stroke-linejoin:round; } .empty { color:var(--muted); padding:18px 0; }
.log { color:#b8cabc; font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; max-height:220px; overflow:auto; background:#0b100d; border-radius:9px; padding:10px; }
nav.tabs { display:flex; gap:6px; margin:0 0 18px; border-bottom:1px solid var(--line); overflow-x:auto; scrollbar-width:thin; }
nav.tabs button { appearance:none; flex:0 0 auto; white-space:nowrap; background:none; border:0; border-bottom:2px solid transparent; color:var(--muted); font:600 14px/1 inherit; padding:10px 14px; cursor:pointer; letter-spacing:.01em; }
nav.tabs button:hover { color:var(--text); } nav.tabs button[aria-selected="true"] { color:var(--green); border-bottom-color:var(--green); }
.tab[hidden] { display:none; }
.pipe { display:flex; flex-direction:column; gap:0; }
.pstep { display:grid; grid-template-columns:26px 190px 1fr 84px; gap:12px; align-items:center; padding:9px 0; border-bottom:1px solid #ffffff0d; }
.pstep:last-child { border-bottom:0; }
.dot { width:14px; height:14px; border-radius:50%; border:2px solid var(--line); position:relative; }
.pstep.done .dot { background:var(--green); border-color:var(--green); }
.pstep.active .dot { background:var(--yellow); border-color:var(--yellow); animation:pulse 1.1s ease-in-out infinite; }
.pstep.failed .dot { background:var(--red); border-color:var(--red); }
.pstep.skipped .dot { background:#3a4a41; border-color:#3a4a41; }
@keyframes pulse { 0%,100% { box-shadow:0 0 0 0 #f5cc7566; } 50% { box-shadow:0 0 0 7px #f5cc7500; } }
.pname { font-weight:650; } .pname small { display:block; color:var(--muted); font-weight:400; font-size:11.5px; }
.pstep.pending .pname, .pstep.pending .pmeta { opacity:.45; }
.pmeta { color:var(--muted); font-size:12.5px; }
.pmeta b { color:var(--text); font-weight:650; }
.pbar { height:6px; background:#ffffff12; border-radius:99px; overflow:hidden; margin-top:5px; }
.pbar i { display:block; height:100%; background:var(--green); border-radius:99px; }
.pstep.active .pbar i { background:var(--yellow); }
.psec { text-align:right; font-variant-numeric:tabular-nums; font-weight:650; }
.budget { height:10px; background:#ffffff12; border-radius:99px; overflow:hidden; margin-top:10px; }
.budget i { display:block; height:100%; background:linear-gradient(90deg,var(--green),var(--yellow)); }
.budget.over i { background:var(--red); }
.btn { appearance:none; background:var(--panel2); color:var(--text); border:1px solid var(--line); border-radius:9px; padding:9px 13px; font:650 13px/1 inherit; cursor:pointer; }
.btn:hover { border-color:var(--green); color:var(--green); } .btn:active { transform:translateY(1px); }
ul.rules li { padding:6px 0; color:var(--muted); } ul.rules b { color:var(--text); }
code { background:#ffffff10; border-radius:4px; padding:1px 5px; font-size:12px; }
.tagrow { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
.tag { background:#ffffff10; border:1px solid var(--line); border-radius:99px; padding:3px 9px; font-size:11.5px; color:var(--muted); }
.healclass { border-bottom:1px solid #ffffff0d; padding:10px 0; } .healclass:last-child { border-bottom:0; }
.healclass .top { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
.healclass .cls { font-weight:700; } .healclass .n { color:var(--yellow); font-variant-numeric:tabular-nums; }
.healclass .what { color:var(--muted); font-size:12.5px; margin-top:3px; }

/* ---- hero strip -------------------------------------------------------
 * The top of the page used to be four small metrics in a card. The farm's whole
 * job is a number that goes up, so that number is now the largest thing on the
 * page and it moves between polls (interpolated from the measured rate) rather
 * than jumping every 2 seconds. A dashboard for a live system should look live.
 */
.hero { grid-column:1/-1; display:grid; grid-template-columns:repeat(auto-fit,minmax(184px,1fr)); gap:0;
  background:rgba(24,35,30,.96); border:1px solid var(--line); border-radius:14px; overflow:hidden; }
.hero-cell { background:rgba(24,35,30,.96); border-right:1px solid var(--line); border-bottom:1px solid var(--line); padding:14px 16px; min-width:0; }
.hero-cell label { display:block; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.09em; }
.hero-cell strong { display:block; font-size:29px; font-weight:750; letter-spacing:-.035em;
  font-variant-numeric:tabular-nums; margin-top:3px; line-height:1.1; }
.hero-cell small { display:block; color:var(--muted); font-size:11.5px; margin-top:3px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hero-cell.lead strong { color:var(--green); }
.hero-cell.warnish strong { color:var(--yellow); }
.hero-cell.badish strong { color:var(--red); }
.spark { display:block; height:26px; margin-top:6px; }
.spark svg { width:100%; height:100%; overflow:visible; }
.spark path.fill { fill:url(#sparkfill); stroke:none; }
.spark path.stroke { fill:none; stroke:var(--green); stroke-width:1.8; stroke-linejoin:round; stroke-linecap:round; }
.spark circle { fill:var(--green); }

/* ---- the farm view ----------------------------------------------------
 * Everything drawn here is a field from the live state payload. The two species measured to
 * produce nothing are drawn greyed out and labelled, because "we own 200 animals
 * that do nothing" is the single most important fact about this farm and a
 * number in a table never once made anybody notice it.
 */
.farmview { grid-column:1/-1; }
.scene { position:relative; border-radius:12px; padding:14px; overflow:hidden;
  background:linear-gradient(180deg,#16352a 0%,#12251d 58%,#132a1e 58%,#0f2018 100%);
  border:1px solid var(--line); }
.scene-sky { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
.scene-sun { font-size:23px; }
.scene-rank { text-align:right; }
.scene-rank b { display:block; font-size:19px; }
.scene-rank span { color:var(--muted); font-size:11.5px; }
.pens { display:grid; grid-template-columns:repeat(auto-fit,minmax(126px,1fr)); gap:9px; margin-top:12px; }
.pen { background:#0b1a13b8; border:1px solid var(--line); border-radius:10px; padding:9px 10px; min-width:0; }
.pen.idle { border-style:dashed; opacity:.72; }
.pen-top { display:flex; justify-content:space-between; align-items:baseline; gap:8px; }
.pen-top b { font-size:13px; text-transform:capitalize; }
.pen-count { font-variant-numeric:tabular-nums; font-weight:750; color:var(--green); font-size:13px; }
.pen.idle .pen-count { color:var(--muted); }
.herd { display:flex; flex-wrap:wrap; gap:2px; margin-top:7px; min-height:34px; align-content:flex-start; }
.herd i { font-size:13px; font-style:normal; line-height:1; animation:bob 2.6s ease-in-out infinite; }
.pen.idle .herd i { filter:grayscale(1) opacity(.5); animation:none; }
@keyframes bob { 0%,100% { transform:translateY(0); } 50% { transform:translateY(-2.5px); } }
.pen-note { display:block; color:var(--muted); font-size:10.5px; margin-top:6px; line-height:1.35; }
.pen.idle .pen-note { color:var(--red); }
.scene-ground { display:grid; grid-template-columns:repeat(auto-fit,minmax(168px,1fr)); gap:10px; margin-top:12px; }
.race { display:flex; flex-direction:column; gap:12px; }
.racer { min-width:0; }
.race-label { display:flex; justify-content:space-between; gap:10px; font-size:12.5px; }
.race-label b { font-variant-numeric:tabular-nums; }
.race-track { height:8px; background:#ffffff10; border-radius:99px; overflow:hidden; margin-top:5px; }
.race-track i { display:block; height:100%; border-radius:99px; background:#557063; transition:width .45s ease; }
.racer.self .race-track i { background:linear-gradient(90deg,#3f9d6b,var(--green)); }
.racer small { display:block; color:var(--muted); font-size:10.5px; margin-top:3px; text-align:right; }

/* ---- Produce Grand Prix ------------------------------------------------
 * A historical leaderboard race built only from snapshots the cycle already
 * records. Every player remains in the legend; endpoint labels are limited to
 * the front pack so a crowded field stays readable.
 */
.grand-prix { position:relative; overflow:hidden; background:
  radial-gradient(circle at 82% 10%,#8fc8ff12 0,transparent 34%),
  radial-gradient(circle at 12% 110%,#72e09a12 0,transparent 40%),rgba(24,35,30,.94); }
.grand-prix:before { content:""; position:absolute; inset:0; pointer-events:none; opacity:.16;
  background:repeating-linear-gradient(115deg,transparent 0 32px,#ffffff08 33px,#ffffff08 34px); }
.gp-head { position:relative; display:flex; justify-content:space-between; align-items:flex-start; gap:18px; flex-wrap:wrap; }
.gp-head h2 { margin-bottom:3px; color:var(--text); font-size:17px; letter-spacing:.04em; }
.gp-kicker { color:var(--green); font-size:10px; font-weight:750; text-transform:uppercase; letter-spacing:.16em; }
.gp-controls { display:flex; gap:14px; flex-wrap:wrap; align-items:flex-start; }
.gp-controls .chips { margin:0; }
.gp-chart { position:relative; height:310px; margin-top:12px; }
.gp-chart svg { width:100%; height:100%; overflow:visible; }
.gp-line { fill:none; stroke-width:2.4; stroke-linecap:round; stroke-linejoin:round; opacity:.72; }
.gp-line.self { stroke-width:4; opacity:1; filter:drop-shadow(0 0 5px #72e09a55); }
.gp-point { stroke:var(--panel); stroke-width:2; }
.gp-end-label { font-size:10.5px; font-weight:700; paint-order:stroke; stroke:#101714; stroke-width:4px; stroke-linejoin:round; }
.gp-legend { position:relative; display:grid; grid-template-columns:repeat(auto-fit,minmax(176px,1fr)); gap:7px; margin-top:10px; }
.gp-racer { display:grid; grid-template-columns:10px minmax(0,1fr) auto; gap:8px; align-items:center;
  background:#0b100d8c; border:1px solid #ffffff0d; border-radius:9px; padding:7px 9px; min-width:0; }
.gp-racer.self { border-color:#72e09a55; background:#173124a8; }
.gp-swatch { width:9px; height:9px; border-radius:50%; box-shadow:0 0 9px currentColor; background:currentColor; }
.gp-racer b { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px; }
.gp-racer b small { color:var(--muted); font-weight:500; margin-left:4px; }
.gp-racer span:last-child { text-align:right; font-variant-numeric:tabular-nums; font-size:11.5px; }
.gp-racer span:last-child small { display:block; color:var(--muted); font-size:10px; }
.gp-note { position:relative; margin-top:10px; color:var(--muted); font-size:11.5px; }
.gauge { background:#0b1a13b8; border:1px solid var(--line); border-radius:10px; padding:10px 11px; }
.gauge label { display:flex; justify-content:space-between; gap:10px; color:var(--muted); font-size:11px;
  text-transform:uppercase; letter-spacing:.07em; }
.gauge label b { color:var(--text); font-variant-numeric:tabular-nums; letter-spacing:0; }
.gtrack { height:9px; background:#ffffff12; border-radius:99px; overflow:hidden; margin-top:7px; position:relative; }
.gtrack i { display:block; height:100%; border-radius:99px; background:var(--green); transition:width .4s ease; }
.gtrack.hunger i { background:linear-gradient(90deg,var(--green),var(--yellow) 60%,var(--red)); }
.gtrack u { position:absolute; top:-3px; width:2px; height:15px; background:var(--red); text-decoration:none; }
.gauge small { display:block; color:var(--muted); font-size:11px; margin-top:6px; }

/* ---- interactive chart ------------------------------------------------- */
.chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
.chip { appearance:none; background:var(--panel2); border:1px solid var(--line); color:var(--muted);
  border-radius:99px; padding:5px 12px; font:650 12px/1 inherit; cursor:pointer; }
.chip:hover { color:var(--text); }
.chip[aria-pressed="true"] { background:var(--green); border-color:var(--green); color:#0d1a12; }
.chart { height:190px; width:100%; }
.chart svg { width:100%; height:100%; overflow:visible; }
.grid-line { stroke:#ffffff0f; stroke-width:1; }
.area { fill:url(#areafill); stroke:none; }
.dot { fill:var(--green); }
.dot-hit { fill:transparent; cursor:crosshair; }
.chart-label { fill:var(--muted); font-size:10.5px; }

/* ---- run drill-down ---------------------------------------------------- */
#runs, #cost-recent { max-width:100%; overflow-x:auto; }
tr.runrow { cursor:pointer; }
tr.runrow:hover td { background:#ffffff08; }
tr.runrow.open td { background:#ffffff0d; }
tr.rundetail td { padding:0 0 14px; }
.detail { display:grid; grid-template-columns:repeat(auto-fit,minmax(228px,1fr)); gap:14px;
  background:#0b100db8; border:1px solid var(--line); border-radius:11px; padding:13px 15px; }
.detail h3 { margin:0 0 7px; font-size:11px; text-transform:uppercase; letter-spacing:.09em; color:var(--muted); }
.detail .kv { grid-template-columns:1fr; }
.phasebars i { display:block; height:5px; border-radius:99px; background:var(--green); }
.phaserow { display:grid; grid-template-columns:78px 1fr 46px; gap:9px; align-items:center;
  font-size:12px; padding:3px 0; }
.phaserow span:first-child { color:var(--muted); }
.phaserow span:last-child { text-align:right; font-variant-numeric:tabular-nums; }

/* ---- token & cost history ----------------------------------------------
 * The tab distinguishes two evidence classes visually: green is booked ledger
 * data, amber is the measured pre-Python counterfactual, and its translucent
 * band is the honest 150k-600k input-token uncertainty rather than fake precision.
 */
.cost-hero .hero-cell strong { font-size:27px; }
.history-title { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; flex-wrap:wrap; }
.history-title h2 { margin-bottom:4px; }
.history-title .method { margin:0; max-width:650px; }
.history-controls { display:flex; flex-direction:column; align-items:flex-end; gap:7px; }
.history-controls .chips { margin:0; justify-content:flex-end; }
.costcurve { width:100%; height:300px; margin-top:10px; }
.costcurve svg { width:100%; height:100%; overflow:visible; }
.cost-band { fill:var(--yellow); opacity:.11; }
.cost-old { fill:none; stroke:var(--yellow); stroke-width:2.5; stroke-linecap:round; stroke-linejoin:round; }
.cost-actual { fill:none; stroke:var(--green); stroke-width:3.5; stroke-linecap:round; stroke-linejoin:round; }
.cost-wakeup { fill:none; stroke:var(--red); stroke-width:2.5; stroke-linecap:round; stroke-linejoin:round; }
.cost-point { fill:var(--green); stroke:var(--panel); stroke-width:2; }
.cost-point.old { fill:var(--yellow); }
.cost-marker { stroke:var(--blue); stroke-width:1; stroke-dasharray:4 4; opacity:.8; }
.cost-legend { display:flex; flex-wrap:wrap; gap:16px; align-items:center; color:var(--muted); font-size:11.5px; }
.cost-legend span { display:inline-flex; gap:7px; align-items:center; }
.cost-legend i { width:22px; height:3px; border-radius:99px; background:var(--green); }
.cost-legend i.old { background:var(--yellow); }
.cost-legend i.band { height:9px; opacity:.25; }
.cost-legend i.wakeup { background:var(--red); }
.change-list { display:flex; flex-direction:column; position:relative; }
.change-list:before { content:""; position:absolute; left:22px; top:22px; bottom:22px; width:2px; background:var(--line); }
.change-step { position:relative; display:grid; grid-template-columns:46px minmax(0,1fr); gap:12px; padding:9px 0 13px; }
.change-icon { position:relative; z-index:1; display:flex; width:44px; height:44px; align-items:center; justify-content:center;
  border-radius:50%; background:var(--panel2); border:2px solid var(--line); font-size:20px; }
.change-step.before .change-icon { border-color:var(--red); }
.change-step.cutover .change-icon { border-color:var(--yellow); box-shadow:0 0 20px #f5cc7522; }
.change-step.python .change-icon, .change-step.proof .change-icon { border-color:var(--green); }
.change-head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }
.change-head b { font-size:14px; }
.change-head span { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.07em; }
.change-body p { margin:4px 0; color:var(--muted); font-size:12.5px; line-height:1.5; }
.change-impact { color:var(--green); font-size:12px; font-weight:650; }
.change-code { color:var(--blue); font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; margin-top:4px; }
.monthly label { display:block; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
.monthly strong { display:block; font-size:34px; letter-spacing:-.04em; margin:3px 0; font-variant-numeric:tabular-nums; }
.monthly small { display:block; color:var(--muted); font-size:11.5px; }
.monthly i { display:block; height:1px; background:var(--line); margin:16px 0; }
.source-bars { display:flex; flex-direction:column; gap:10px; }
.source-row { display:grid; grid-template-columns:minmax(0,1fr) 62px; gap:8px; }
.source-row label { font-size:12px; text-transform:capitalize; }
.source-row b { text-align:right; font-size:12px; font-variant-numeric:tabular-nums; }
.source-row small { grid-column:1/-1; color:var(--muted); font-size:10.5px; margin-top:-3px; }
.source-track { grid-column:1/-1; height:8px; background:#ffffff10; border-radius:99px; overflow:hidden; }
.source-track i { display:block; height:100%; border-radius:99px; background:linear-gradient(90deg,#557063,var(--green)); }
.ledger-dots { display:grid; grid-template-columns:repeat(auto-fill,minmax(11px,1fr)); gap:4px; margin:15px 0; }
.ledger-dot { display:block; aspect-ratio:1; min-height:8px; max-height:14px; border-radius:3px; background:var(--green); opacity:.8; }
.ledger-dot.healed { background:var(--yellow); opacity:1; }
.ledger-dot.charged { background:var(--red); opacity:1; box-shadow:0 0 8px #ff8a8366; }

/* ---- findings tab ------------------------------------------------------ */
.finding { grid-column:1/-1; }
.claim { font-size:15.5px; line-height:1.5; font-weight:600; margin:0 0 4px; }
.claim em { color:var(--green); font-style:normal; }
.method { color:var(--muted); font-size:12.5px; line-height:1.55; margin:8px 0 0; }
.scatter { height:250px; width:100%; margin-top:6px; }
.scatter svg { width:100%; height:100%; overflow:visible; }
.scatter .pt { fill:var(--green); opacity:.85; }
.scatter .pt.plateau { fill:var(--yellow); }
.scatter .fitline { stroke:var(--blue); stroke-width:2; stroke-dasharray:5 4; }
.scatter .divider { stroke:var(--red); stroke-width:1; stroke-dasharray:3 3; }
.twoup { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }
.verdictbox { border:1px solid var(--line); border-radius:11px; padding:12px 14px; background:#ffffff06; }
.verdictbox b { display:block; font-size:13px; margin-bottom:5px; }
.verdictbox.good { border-color:#4d7a60; background:#20342a; }
.verdictbox.bad { border-color:#7a4d4d; background:#34201f; }
.slider { width:100%; margin:10px 0 4px; accent-color:var(--green); }
.counter { font-size:33px; font-weight:750; letter-spacing:-.035em; font-variant-numeric:tabular-nums; }
.counter.saved { color:var(--green); }
.timeline { position:relative; padding-left:24px; }
.timeline:before { content:""; position:absolute; left:6px; top:6px; bottom:6px; width:2px; background:var(--line); }
.tl { position:relative; padding:9px 0 9px 4px; }
.tl:before { content:""; position:absolute; left:-22px; top:15px; width:10px; height:10px; border-radius:50%;
  background:var(--panel); border:2px solid var(--green); }
.tl.experiment:before { border-color:var(--yellow); }
.tl.detector:before { border-color:var(--blue); }
.tl.correction:before { border-color:var(--red); }
.tl-run { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; }
.tl b { display:block; font-size:13.5px; margin:2px 0 3px; }
.tl span { color:var(--muted); font-size:12.5px; line-height:1.5; }
.bars { display:flex; flex-direction:column; gap:7px; margin-top:4px; }
.bar { display:grid; grid-template-columns:96px 1fr 108px; gap:10px; align-items:center; font-size:12.5px; }
.bar span:first-child { color:var(--muted); text-transform:capitalize; }
.bar span:last-child { text-align:right; font-variant-numeric:tabular-nums; font-weight:650; }
.bartrack { height:16px; background:#ffffff10; border-radius:5px; overflow:hidden; }
.bartrack i { display:block; height:100%; background:linear-gradient(90deg,#3f9d6b,var(--green)); border-radius:5px;
  transition:width .4s ease; }
.bartrack i.zero { background:var(--red); min-width:2px; }
.hint { color:var(--muted); font-size:11.5px; }
kbd { background:#ffffff12; border:1px solid var(--line); border-bottom-width:2px; border-radius:5px;
  padding:1px 5px; font:600 11px/1.5 ui-monospace,Menlo,monospace; }

@media (prefers-reduced-motion: reduce) {
  .herd i, .pstep.active .dot { animation:none; }
  .gtrack i, .bartrack i, .pbar i { transition:none; }
}
@media (max-width:900px) { .status,.blockers,.release,.wide,.side,.cost,.healing { grid-column:1/-1; } header { display:block; } .refresh { text-align:left; margin-top:10px; } .history-controls { align-items:flex-start; margin-top:12px; } .history-controls .chips { justify-content:flex-start; } .costcurve { height:240px; } .gp-chart { height:250px; } .gp-controls { gap:7px; } }
__TRACE_CSS__
__WIRE_CSS__
__ARCH_CSS__
__GAME_CSS__
__OPERATOR_CSS__
</style>
</head>
<body><main>
<header class="app-header">
  <div class="app-brand"><span class="brand-mark" aria-hidden="true">🌱</span><div class="brand-copy"><div class="brand-kicker">Autonomous farm operations</div><h1>Farm Friends</h1><div class="subtitle">Read-only control room · every claim links back to measured state</div></div></div>
  <div class="header-meta"><span class="system-state watch" id="global-status">Connecting</span><div class="refresh" id="updated">Connecting…<br>State refresh: 2s</div></div>
</header>
<section class="autonomy-ribbon" aria-label="Autonomous system status">
  <div class="auto-cell primary watch" id="autonomy-primary"><span class="auto-icon">●</span><div class="auto-copy"><small>Operating mode</small><b id="autonomy-state-title">Connecting</b><span id="autonomy-state-detail">Loading control-plane evidence</span></div></div>
  <div class="auto-cell" id="autonomy-services"><span class="auto-icon">◫</span><div class="auto-copy"><small>Control loops</small><b id="autonomy-agents">Loading services</b><span id="autonomy-agents-detail">Checking launchd state</span></div></div>
  <div class="auto-cell" id="autonomy-cycle-cell"><span class="auto-icon">↻</span><div class="auto-copy"><small>Execution</small><b id="autonomy-cycle">Waiting for run state</b><span id="autonomy-cycle-detail">Deterministic cadence</span></div></div>
  <div class="auto-cell" id="autonomy-action-cell"><span class="auto-icon">✓</span><div class="auto-copy"><small>Latest autonomous change</small><b id="autonomy-action">Loading activity ledger</b><span id="autonomy-action-detail">Observe · decide · act · verify</span></div></div>
</section>
<nav class="tabs" role="tablist" aria-label="Monitor views">
  <button role="tab" data-tab="overview" aria-selected="true"><span class="tab-icon">⌂</span><span>Overview</span><kbd class="tab-key">O</kbd></button>
  <button role="tab" data-tab="pipeline" aria-selected="false"><span class="tab-icon">↳</span><span>Pipeline</span><kbd class="tab-key">P</kbd></button>
  <button role="tab" data-tab="cost" aria-selected="false"><span class="tab-icon">↻</span><span>Healing</span><kbd class="tab-key">C</kbd></button>
  <button role="tab" data-tab="history" aria-selected="false"><span class="tab-icon">◒</span><span>Cost history</span><kbd class="tab-key">T</kbd></button>
  <button role="tab" data-tab="findings" aria-selected="false"><span class="tab-icon">⌕</span><span>Findings</span><kbd class="tab-key">F</kbd></button>
  <button role="tab" data-tab="game" aria-selected="false"><span class="tab-icon">◉</span><span>Coop Rush</span><kbd class="tab-key">G</kbd></button>
  <button role="tab" data-tab="wire" aria-selected="false"><span class="tab-icon">⌁</span><span>MCP traffic</span><kbd class="tab-key">W</kbd></button>
  <button role="tab" data-tab="architecture" aria-selected="false"><span class="tab-icon">◇</span><span>Architecture</span><kbd class="tab-key">A</kbd></button>
</nav>
<div class="tab operator-tab" id="tab-overview">
<section class="grid">
  <div class="page-hero">
    <div><div class="page-kicker">Live autonomous operation</div><h2>Farm command center</h2><p>One view of the objective, the latest measured change, the decision the system made, and the evidence that the result was verified.</p><div class="delta-row" id="overview-deltas"></div></div>
    <div class="hero-verdict watch" id="overview-verdict-box"><b id="overview-verdict">Connecting</b><span id="overview-verdict-detail">Waiting for the first measured cycle</span></div>
  </div>
  <div class="hero" aria-label="Live farm summary">
    <div class="hero-cell lead"><label>Lifetime produce · live estimate</label><strong id="hero-produce">—</strong><small id="hero-produce-sub">waiting for a measured rate</small><span class="spark" id="spark-produce"></span></div>
    <div class="hero-cell"><label>Production rate</label><strong id="hero-rate">—</strong><small id="hero-rate-sub">score delta over the real interval</small><span class="spark" id="spark-rate"></span></div>
    <div class="hero-cell"><label>Leaderboard</label><strong id="hero-rank">—</strong><small id="hero-gap">waiting for rivals</small><span class="spark" id="spark-rank"></span></div>
    <div class="hero-cell"><label>Herd</label><strong id="hero-animals">—</strong><small id="hero-herd-sub">growth policy loading</small><span class="spark" id="spark-animals"></span></div>
    <div class="hero-cell"><label>Safety headroom</label><strong id="hero-hunger">—</strong><small id="hero-hunger-sub">production stops at 70 hunger</small><span class="spark" id="spark-hunger"></span></div>
  </div>
  <div class="card farmview overview-farm"><h2>Live farm <small>Every object is measured telemetry</small></h2><div class="scene" id="farm-scene"><div class="empty">Waiting for farm state…</div></div></div>
  <div class="card cycle-story-card"><h2><span id="cycle-story-summary">Latest cycle</span> <small>Observe → decide → act → verify</small></h2><div id="cycle-story"><div class="empty">Loading decision evidence…</div></div></div>
  <div class="card overview-support"><h2>Loop status</h2><div id="health"><span class="pill waiting">connecting</span></div><div class="metrics"><div class="metric"><label>Last run</label><strong id="last-run">—</strong></div><div class="metric"><label>Run age</label><strong id="run-age">—</strong></div><div class="metric"><label>Stage</label><strong id="stage">—</strong></div><div class="metric"><label>Cadence</label><strong id="cadence">—</strong></div></div></div>
  <div class="card overview-support"><h2>Attention queue</h2><ul id="blockers"><li class="empty">Loading guardrails…</li></ul></div>
  <div class="card overview-support"><h2>Scheduler & release</h2><div class="kv" id="system"></div></div>
  <div class="card full grand-prix">
    <div class="gp-head"><div><div class="gp-kicker">Measured competition</div><h2>Produce Grand Prix</h2><div class="subtitle">The same recorded leaderboard snapshots, with no additional farm calls.</div></div>
      <div class="gp-controls"><div class="chips" id="gp-modes" aria-label="Leaderboard chart mode"><button class="chip" data-gpmode="absolute" aria-pressed="true">Total score</button><button class="chip" data-gpmode="gain" aria-pressed="false">Window gain</button></div><div class="chips" id="gp-ranges" aria-label="Leaderboard chart range"><button class="chip" data-gprange="20" aria-pressed="false">20 runs</button><button class="chip" data-gprange="50" aria-pressed="false">50</button><button class="chip" data-gprange="100" aria-pressed="true">100</button></div></div></div>
    <div class="competition-workspace"><div class="gp-chart" id="gp-chart"><div class="empty">Waiting for leaderboard history…</div></div><aside class="competition-side"><h3>Live standings</h3><div id="leaderboard"></div></aside></div>
    <details class="gp-standings"><summary>Show every recorded racer</summary><div class="gp-legend" id="gp-legend"></div></details><div class="gp-note" id="gp-note">Scores come directly from the run ledger.</div>
  </div>
  <div class="card trend-card"><h2 id="chart-title">Farm trend</h2><div class="chips" id="chart-metrics" aria-label="Chart metric">
    <button class="chip" data-metric="produce" aria-pressed="true">Produce</button><button class="chip" data-metric="produce_per_min" aria-pressed="false">Rate</button><button class="chip" data-metric="animals" aria-pressed="false">Animals</button><button class="chip" data-metric="max_hunger" aria-pressed="false">Hunger</button><button class="chip" data-metric="feed" aria-pressed="false">Feed</button><button class="chip" data-metric="coins" aria-pressed="false">Coins</button>
    </div><div class="chart" id="chart"></div><div class="hint" id="chart-note">Choose a metric · hover a point for the run</div></div>
  <div class="card strategy-card"><h2>Growth decision</h2><div id="growth-verdict"></div><div class="kv" id="growth" style="margin-top:12px"></div></div>
  <details class="card audit-drawer"><summary>Farm inventory & current intent <span>Raw state and the in-flight mutation intent</span></summary><div class="drawer-body"><div class="twoup"><div><h2>Farm state</h2><div class="kv" id="farm"></div></div><div><h2>Current intent</h2><div class="kv" id="intent"></div></div></div></div></details>
  <details class="card audit-drawer"><summary>Recent run ledger <span>Phase timing, actions, decision evidence and token rows</span></summary><div class="drawer-body"><div id="runs"></div></div></details>
  <details class="card audit-drawer"><summary>Launchd log tail <span>Raw local scheduler output</span></summary><div class="drawer-body"><div class="log" id="log">Loading…</div></div></details>
</section>
</div>
<div class="tab operator-tab" id="tab-pipeline" hidden>
<section class="grid">
  <div class="page-hero"><div><div class="page-kicker">Measured live execution</div><h2>Run workspace</h2><p>Follow one deterministic cycle from observation through verification. Deviations, budget pressure and boundary failures are promoted automatically.</p></div><div class="hero-verdict watch" id="pipeline-hero-verdict"><b id="pipeline-hero-state">Connecting</b><span id="pipeline-hero-detail">Waiting for progress telemetry</span></div></div>
  <div class="card run-command">
    <section class="run-primary"><h2>Current run</h2><div id="pipe-status"><span class="pill waiting">connecting</span></div><div class="metrics"><div class="metric"><label>Run</label><strong id="pipe-run">—</strong></div><div class="metric"><label>Elapsed</label><strong id="pipe-elapsed">—</strong></div><div class="metric"><label>Active step</label><strong id="pipe-active">—</strong></div><div class="metric"><label>Progress</label><strong id="pipe-count">—</strong></div><div class="metric"><label>Next run</label><strong id="pipe-next">—</strong></div></div><div class="subtitle" id="pipe-heartbeat" style="margin-top:9px;font-size:10px">—</div></section>
    <section><h2>Bounded execution budget</h2><div class="kv" id="pipe-budget"></div><div class="budget" id="pipe-budget-bar"><i style="width:0%"></i></div><div class="subtitle" style="margin-top:8px;font-size:10px">Adoption yields at the soft budget; the hard timeout protects the next cadence slot.</div></section>
    <section><h2>Last completed outcome</h2><div class="kv" id="pipe-summary"></div></section>
  </div>
  <div class="card pipeline-decision"><div class="decision-copy"><div class="page-kicker">Current autonomous decision</div><h3 id="pipe-decision-title">Compiling plan…</h3><p id="pipe-decision-body">Waiting for decision evidence.</p></div><div id="pipe-lifecycle"></div></div>
  <div class="card trace">
    <div class="head"><div><h2>Execution trace <small>Measured spans + source-derived reachability</small></h2><div class="trace-sub">Run Trace shows what happened when. Tool Matrix shows which pipeline steps can reach each external MCP tool. Select a span or cell for measured arguments, result, source and call path.</div><details class="trace-explain"><summary>How to interpret measured time versus static code</summary><div class="trace-sub">Step and MCP spans share one measured clock. Python functions are static reachability only, never as invented runtime spans.</div></details></div></div>
    <div id="trace-explorer" class="trace-explorer" aria-live="polite" aria-label="Pipeline execution trace and MCP tool matrix"><div class="te-empty">Loading execution trace…</div></div>
  </div>
  <div class="card judgement-card"><h2>Guardrails deciding whether the run is healthy</h2><div id="signal-verdict"></div><div class="guardrail-grid" id="pipe-guardrails" style="margin-top:12px"></div><div class="kv signal-kv" id="signals"></div></div>
  <details class="card audit-drawer"><summary>Recorded step timings <span>Every step, including skipped paths and recent medians</span></summary><div class="drawer-body"><div class="pipe" id="pipe-steps"><div class="empty">Loading…</div></div></div></details>
</section>
</div>
<div class="tab operator-tab" id="tab-cost" hidden>
<section class="grid">
  <div class="page-hero"><div><div class="page-kicker">Exception-only intelligence</div><h2>Healing control room</h2><p>See what the supervisor detected, which bounded safeguard it changed, how the result is verified, and whether a model ever had to wake up.</p></div><div class="hero-verdict" id="healing-hero-verdict"><b id="healing-verdict">Loading healing state</b><span id="healing-verdict-detail">Reading the remedy ledger</span></div></div>
  <div class="card healing-outcomes">
    <div class="outcome primary"><small>Estimated exception cost</small><b id="cost-total">—</b><span id="cost-total-sub">Ledger loading</span></div>
    <div class="outcome"><small>Explicit zero-cost runs</small><b id="cost-free">—</b><span><span id="cost-charged">—</span> runs carried a charge</span></div>
    <div class="outcome"><small>Handled locally</small><b id="heal-local-count">—</b><span><span id="cost-total-tokens">—</span> estimated tokens all time</span></div>
    <div class="outcome"><small>Model wake-ups</small><b id="cost-total-esc">—</b><span><span id="heal-active-count">—</span> active safeguard overrides · <span id="heal-active-detail">loading</span></span></div>
  </div>
  <div class="card latest-remedy"><h2>Latest intervention</h2><div id="healing-latest"><div class="empty">Loading the latest remedy…</div></div></div>
  <div class="card safeguards"><h2>Active safeguards</h2><div class="kv" id="knobs"></div><div class="subtitle" style="margin-top:9px;font-size:10px">Every value is clamped. Healing can slow work down; it cannot spend coins, adopt, sell, trade or gift.</div></div>
  <div class="card healing-loop-card"><h2>Closed-loop response</h2><div id="healing-loop"><div class="empty">Loading detect → diagnose → remedy → verify…</div></div></div>
  <div class="card healing-classes-card"><h2>Conditions the supervisor has learned to handle</h2><div id="heal-classes"></div></div>
  <div class="card healing-runs-card"><h2>Last five cost rows</h2><div id="cost-recent"></div><div class="subtitle" style="margin-top:9px;font-size:10px">Every routine cycle writes an explicit zero. A cost appears only when a surviving alert wakes a model.</div></div>
  <details class="card audit-drawer"><summary>Upper-bound avoided wake-up estimate <span>Methodology, not booked or billed savings</span></summary><div class="drawer-body"><div class="big" id="cost-avoided">—</div><p class="method">Alert batching means this deliberately overstates avoided model wake-ups.</p><div class="kv" id="cost-detail"></div></div></details>
  <details class="card audit-drawer"><summary>Full remedy ledger <span>Every bounded adjustment and relaxation event</span></summary><div class="drawer-body"><ul id="heal-log"></ul></div></details>
</section>
</div>
<div class="tab operator-tab" id="tab-history" hidden>
<section class="grid">
  <div class="page-hero"><div><div class="page-kicker">Measured before / after</div><h2>The economics of autonomy</h2><p>Booked exception estimates stay separate from the measured old-loop counterfactual. Every zero and every model wake-up is an explicit ledger row.</p><div class="delta-row" id="history-impact-deltas"></div></div><div class="hero-verdict" id="history-hero-verdict"><b id="history-verdict">Loading audited history</b><span id="history-verdict-detail">Reading token and healing ledgers</span></div></div>
  <div class="hero cost-hero" aria-label="Token and cost history summary">
    <div class="hero-cell lead"><label>Exception cost estimate</label><strong id="hist-actual-cost">—</strong><small id="hist-actual-cost-sub">modeled, not provider billing</small></div>
    <div class="hero-cell"><label>Estimated tokens</label><strong id="hist-actual-tokens">—</strong><small>input + assumed output after cutover</small></div>
    <div class="hero-cell"><label>Audited runs</label><strong id="hist-runs">—</strong><small id="hist-runs-sub">every zero is explicit</small></div>
    <div class="hero-cell warnish"><label>Old-loop cost avoided</label><strong id="hist-avoided">—</strong><small id="hist-avoided-sub">measured range</small></div>
    <div class="hero-cell"><label>Cost reduction</label><strong id="hist-reduction">—</strong><small>routine execution, like for like</small></div>
    <div class="hero-cell"><label>Model wake-ups</label><strong id="hist-wakeups">—</strong><small id="hist-wakeups-sub">after local healing</small></div>
  </div>
  <div class="card history-chart-card"><div class="history-title"><div><h2 id="hist-chart-title">Cumulative cost over audited runs</h2><p class="method">Green is the exception ledger. Amber is the measured pre-Python counterfactual—not an invoice.</p></div><div class="history-controls"><div class="chips" id="hist-metrics"><button class="chip" data-hmetric="cost" aria-pressed="true">Cumulative cost</button><button class="chip" data-hmetric="tokens" aria-pressed="false">Cumulative tokens</button><button class="chip" data-hmetric="per_run" aria-pressed="false">Per run</button><button class="chip" data-hmetric="healing" aria-pressed="false">Healing</button></div><div class="chips" id="hist-range"><button class="chip" data-hrange="all" aria-pressed="true">All</button><button class="chip" data-hrange="100" aria-pressed="false">100 runs</button><button class="chip" data-hrange="50" aria-pressed="false">50 runs</button></div></div></div><div class="costcurve" id="hist-chart"><div class="empty">Loading ledger…</div></div><div class="cost-legend" id="hist-legend"></div><p class="method" id="hist-chart-note"></p></div>
  <div class="card history-story"><h2>Changes that moved the curve <small>Select a milestone to locate it on the chart</small></h2><div class="change-list" id="hist-changes"><div class="empty">Loading execution history…</div></div></div>
  <div class="card history-method"><h2>At operating cadence</h2><div class="monthly"><label>Old LLM loop · projected monthly</label><strong id="hist-old-monthly">—</strong><small id="hist-old-monthly-range">—</small><i></i><label>Python exception estimate · all time</label><strong class="good" id="hist-new-monthly">$0</strong><small>Routine cycles are zero-token; this is cumulative exception spend, not a monthly rate.</small></div><h2 style="margin-top:20px">Where tokens went before</h2><div class="source-bars" id="hist-sources"></div></div>
  <div class="card full"><h2>Run-by-run zero-cost proof</h2><p class="claim">Every square is a row in <code>state/tokens.ndjson</code>: green cost $0, amber handled locally, red woke a model.</p><div class="ledger-dots" id="hist-ledger"></div><div class="metrics" id="hist-ledger-stats"></div></div>
  <details class="card audit-drawer"><summary>Counterfactual methodology & disclosure <span>Assumptions, token composition and uncertainty</span></summary><div class="drawer-body"><p class="method" id="hist-disclosure"></p></div></details>
</section>
</div>
<div class="tab operator-tab" id="tab-findings" hidden>
<section class="grid">
  <div class="page-hero"><div><div class="page-kicker">Strategy as a living evidence system</div><h2>Knowledge control plane</h2><p>Current conclusions, their confidence and freshness, open uncertainties, and the autonomous work moving questions toward policy.</p></div><div class="hero-verdict watch" id="findings-hero-verdict"><b id="findings-verdict">Loading strategy evidence</b><span id="findings-verdict-detail">Fetching the immutable ledger projection</span></div></div>
  <div class="card knowledge-flow" id="knowledge-flow"><div class="knowledge-node"><small>Questions</small><b>—</b><span>loading</span></div></div>
  <div class="card finding finding-model"><h2>Authoritative herd / output model <small>The finding currently steering growth</small></h2><p class="claim" id="ev-ceiling-summary">Loading measured evidence…</p><details class="claim-disclosure"><summary>Read the full scientific claim</summary><p class="method" id="ev-ceiling-claim">Loading claim text…</p></details><div class="scatter" id="ev-ceiling-chart"></div><div class="metrics" id="ev-ceiling-stats"></div><p class="method" id="ev-ceiling-method"></p></div>
  <div class="card strategy-brief"><h2>Current strategy verdict</h2><div id="ev-strategy-brief"><div class="empty">Compiling accepted claims…</div></div><div class="sidebar-research"><h2>Research in motion <small>Autonomous scans and work orders</small></h2><div id="research-activity"><div class="empty">Loading activity…</div></div></div></div>
  <div class="card knowledge-control"><div class="history-title"><div><h2>Claims, questions & runtime policy</h2><p class="method">Accepted evidence is the default view. Superseded claims remain auditable without dominating the page.</p></div><div class="metrics" id="ev-knowledge-stats"></div></div><div class="twoup" style="margin-top:14px"><div><div class="claim-toolbar"><h2>Claims</h2><div class="chips"><button class="chip" data-claim-filter="accepted" aria-pressed="true">Accepted</button><button class="chip" data-claim-filter="recheck" aria-pressed="false">Needs recheck</button><button class="chip" data-claim-filter="superseded" aria-pressed="false">Superseded</button><button class="chip" data-claim-filter="all" aria-pressed="false">All</button></div></div><div class="claim-list filter-accepted" id="ev-claims"><div class="empty">Loading claims…</div></div></div><div><h2>Highest-priority open questions</h2><div class="question-list" id="ev-questions"><div class="empty">Loading questions…</div></div><button class="btn question-toggle" id="question-toggle" data-question-toggle="1">Show every open question</button><p class="method" id="ev-policy"></p></div></div></div>
  <details class="card audit-drawer secondary-findings"><summary>Species evidence & retired dead ends <span>Composition, bounded probes and scoped negative results</span></summary><div class="drawer-body"><div class="twoup"><div><h2>Species evidence</h2><div class="bars" id="ev-species"></div><p class="method" id="ev-species-note"></p></div><div><h2>Dead ends retired</h2><div id="ev-dead-ends"><div class="empty">Loading…</div></div></div></div></div></details>
  <details class="card audit-drawer secondary-findings"><summary>Counterfactual prompt cost <span>Why strategy moved into executable rules</span></summary><div class="drawer-body"><p class="claim">The old loop re-read a growing farm state across ~21 turns. The current loop executes the same decisions as arithmetic in <code>rules.py</code>.</p><label class="method" for="ev-runs-slider">Counterfactual LLM runs per day: <b id="ev-runs-label">288</b></label><input class="slider" id="ev-runs-slider" type="range" min="1" max="480" value="288"><div class="twoup"><div><label class="method">Old loop · estimated monthly cost</label><div class="counter" id="ev-old-cost">—</div></div><div><label class="method">Current loop · measured exception cost</label><div class="counter saved" id="ev-new-cost">$0.00</div></div></div><p class="method" id="ev-cost-note"></p></div></details>
  <details class="card audit-drawer secondary-findings"><summary>Detector redesigns & model history <span>What was wrong, what replaced it, and every evidence transition</span></summary><div class="drawer-body"><div class="twoup"><div><h2>Detector redesigns</h2><div id="ev-detectors"><div class="empty">Loading…</div></div></div><div><h2>How the model changed</h2><div class="timeline" id="ev-timeline"><div class="empty">Loading evidence…</div></div></div></div></div></details>
</section>
</div>
<!--GAME_MARKUP_START-->
<div class="tab operator-tab" id="tab-game" hidden>
__GAME_MARKUP__
</div>
<!--GAME_MARKUP_END-->
<!-- Replaced entirely by renderArchitecture from /api/architecture. Keep a visible
     initial state: an empty container hid the block-scope loader bug by presenting a
     blank tab with no explanation. Derived content still has no hand-maintained copy. -->
<div class="tab architecture-tab" id="tab-architecture" hidden>
  <div class="arch-shell" data-arch-loading><section class="page-hero arch-hero"><div>
    <span class="page-kicker">Source-derived control plane</span><h2>Architecture control plane</h2>
    <p>Deriving the current system shape, service posture, change history, and execution paths…</p>
  </div><div class="hero-verdict watch"><b>Building live model</b><span>Reading local source and append-only state</span></div></section></div>
</div>
<div class="tab operator-tab" id="tab-wire" hidden>
<section class="grid">
  <div class="page-hero wire-brief"><div><div class="page-kicker">Measured process boundary</div><h2>MCP traffic</h2><p>Every packet is one recorded JSON-RPC call. The view makes concurrency, latency, call mix and silence visible without inventing activity.</p><div class="delta-row" id="wire-deltas"></div></div><div class="hero-verdict watch" id="wire-hero-verdict"><b id="wire-hero-state">Waiting for boundary traffic</b><span id="wire-hero-detail">Loading measured call spans</span></div></div>
  <div class="card wire"><div class="head"><div><h2>Live boundary replay <small>Traffic view + static diagnostics</small></h2><details class="wire-context"><summary>How to read this view</summary><div class="wire-sub">Colour identifies the issuing pipeline step. Flight time is measured <code>duration_ms</code> at the selected replay speed. In-flight calls pulse, and reachable tools not used by this run remain available in the collapsed silent-tool group.</div></details></div></div><div id="mcp-wire" class="mcp-wire" aria-live="polite" aria-label="Live MCP call switchboard"><div class="mw-empty">Waiting for MCP boundary telemetry…</div></div></div>
</section>
</div>
</main>
<script>
/*HELPERS_START*/
const $ = id => document.getElementById(id);
const nf = new Intl.NumberFormat();
const esc = value => String(value ?? "—").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const num = value => value == null ? "—" : nf.format(value);
const fixed = (value, digits=2) => value == null ? "—" : Number(value).toFixed(digits);
function time(value) { if (!value) return "—"; const d = new Date(value); return isNaN(d) ? value : d.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"}); }
function age(seconds) { if (seconds == null) return "—"; if (seconds < 60) return `${seconds}s`; return `${Math.floor(seconds/60)}m ${seconds%60}s`; }
function kv(items) { return items.map(([k,v]) => `<div><span>${esc(k)}</span><span>${v}</span></div>`).join(""); }
/*HELPERS_END*/
// One panel must never be able to silence the others. render() used to be a
// single unguarded chain, so a throw in an early panel left the pipeline,
// signals, chart and log tail frozen at their last values while the overview
// kept refreshing -- a stuck page that looks alive.
let LAST = null, LAST_FETCH_MS = null, FETCH_ERROR = null;
let EVIDENCE = null, EVIDENCE_LOADING = false, EVIDENCE_LAST_FETCH_MS = null;
let ACTIVE_TAB = "overview";
const EVIDENCE_REFRESH_MS = 60000, ARCHITECTURE_REFRESH_MS = 30000;
let CHART_METRIC = "produce", ACTIVE_RUN = null;
let RACE_MODE = "absolute", RACE_RANGE = 100;
let COST_HISTORY_METRIC = "cost", COST_HISTORY_RANGE = "all";
let PANEL_ERRORS = [];
const METRICS = {
  produce: {label:"Lifetime produce", unit:"", digits:0},
  produce_per_min: {label:"Production rate", unit:" / min", digits:0},
  animals: {label:"Animals", unit:"", digits:0},
  max_hunger: {label:"Maximum hunger", unit:" / 100", digits:0},
  feed: {label:"Feed reserve", unit:"", digits:0},
  coins: {label:"Coins", unit:"c", digits:0},
};
const KIND_ICON = {chicken:"🐔", pig:"🐷", beehive:"🐝", sheep:"🐑", cow:"🐄"};
function safe(label, fn) {
  try { fn(); return true; }
  catch (error) {
    PANEL_ERRORS.push(`${label}: ${error && error.message ? error.message : error}`);
    if (typeof console !== "undefined" && console.error) console.error(`panel ${label} failed`, error);
    return false;
  }
}
function render(data) {
  LAST = data; LAST_FETCH_MS = Date.now(); FETCH_ERROR = null; PANEL_ERRORS = [];
  safe("overview", () => renderOverview(data));
  safe("leaderboard race", () => renderLeaderboardHistory(data.leaderboard_history || []));
  safe("cost", () => renderCost(data.tokens || {}, data.cost || {}));
  safe("healing", () => renderHealing(data.heal || {}));
  safe("growth", () => renderGrowth(data.growth || {}, data.recovery_watch || {}));
  safe("pipeline", () => renderPipeline(data.pipeline || {}, data.signals || {}, data.cadence_seconds));
  // The execution trace owns its topology cache and interaction state. A poll
  // only hands it measured spans; if it failed to mount, safe() keeps that local.
  safe("trace", () => { if (window.TracePanel) window.TracePanel.update(data); });
  // The switchboard consumes the same measured spans. It is painted separately so
  // a throw in either boundary view cannot blank the other.
  safe("switchboard", () => { if (window.MCPWirePanel) window.MCPWirePanel.update(data); });
  safe("signals", () => renderSignals(data.signals || {}, data.growth || {}));
  safe("chart", () => renderChart(data.trend || []));
  safe("log", () => { $("log").textContent = (data.log_tail || []).join("\n") || "No launchd log yet"; });
  safe("operator narrative", () => renderOperator(data, OP_AUTONOMY));
  renderHeartbeat();
}
// Re-render the time-dependent panels on a local 1s tick, not only when a poll
// lands: the pipeline's elapsed clock, step timers and next-run countdown are
// functions of now(), so they must advance even when the payload is unchanged.
function tick() {
  if (!LAST) return;
  safe("hero", () => renderHero(LAST));
  safe("pipeline", () => renderPipeline(LAST.pipeline || {}, LAST.signals || {}, LAST.cadence_seconds));
  safe("trace", () => { if (window.TracePanel) window.TracePanel.paint(); });
  safe("switchboard", () => { if (window.MCPWirePanel) window.MCPWirePanel.paint(); });
  safe("operator clocks", () => renderOperatorTick(LAST));
  safe("run-age", () => {
    const ts = LAST.latest && LAST.latest.ts ? new Date(LAST.latest.ts).getTime() : null;
    $("run-age").textContent = age(ts ? Math.max(0, Math.floor((Date.now() - ts) / 1000)) : null);
  });
  renderHeartbeat();
}
// Proof of life for the page itself, so "nothing is changing" can be told apart
// from "the poll died" or "a panel threw".
function renderHeartbeat() {
  const bits = [];
  if (FETCH_ERROR) bits.push(`<span class="bad">disconnected: ${esc(FETCH_ERROR)}</span>`);
  else if (LAST_FETCH_MS) {
    const dataAge = Math.max(0, Math.round((Date.now() - LAST_FETCH_MS) / 1000));
    bits.push(`Updated ${new Date(LAST_FETCH_MS).toLocaleTimeString()} (${dataAge}s ago)`);
  }
  bits.push("Refresh: state 2s · autonomy 30s · findings 60s · redraw 1s");
  if (typeof OP_AUTONOMY_LAST_FETCH_MS!=="undefined" && OP_AUTONOMY_LAST_FETCH_MS) {
    bits.push(`Autonomy refreshed ${Math.max(0,Math.round((Date.now()-OP_AUTONOMY_LAST_FETCH_MS)/1000))}s ago`);
  }
  if ((ACTIVE_TAB==="findings"||ACTIVE_TAB==="history") && EVIDENCE_LAST_FETCH_MS) {
    bits.push(`Findings refreshed ${Math.max(0,Math.round((Date.now()-EVIDENCE_LAST_FETCH_MS)/1000))}s ago`);
  }
  if (ACTIVE_TAB==="architecture" && typeof ARCH_LAST_FETCH_MS!=="undefined" && ARCH_LAST_FETCH_MS) {
    bits.push(`Architecture refreshed ${Math.max(0,Math.round((Date.now()-ARCH_LAST_FETCH_MS)/1000))}s ago`);
  }
  if (PANEL_ERRORS.length) bits.push(`<span class="bad">${PANEL_ERRORS.length} panel error(s): ${esc(PANEL_ERRORS.join("; "))}</span>`);
  const html = bits.join("<br>");
  const updated = $("updated"); if (updated) updated.innerHTML = html;
  const beat = $("pipe-heartbeat"); if (beat) beat.innerHTML = html;
}
function spark(values, label) {
  values = (values || []).filter(v=>v!==null&&v!==undefined).map(Number).filter(Number.isFinite);
  if (!values.length) return `<span class="empty">no history</span>`;
  const w=180, h=26, min=Math.min(...values), max=Math.max(...values), span=Math.max(1,max-min);
  const pts=values.map((v,i) => [values.length === 1 ? w : i/(values.length-1)*w, h-3-(v-min)/span*(h-6)]);
  const line=pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const area=`M0,${h} L${pts.map(p => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" L")} L${w},${h} Z`;
  const end=pts[pts.length-1];
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(label)} recent trend"><path class="fill" d="${area}" style="fill:var(--green);opacity:.09"></path><polyline class="stroke" points="${line}"></polyline><circle cx="${end[0]}" cy="${end[1]}" r="2.5"></circle></svg>`;
}
function renderHero(data) {
  const r=data.latest || {}, s=data.scene || {}, signal=data.signals || {}, rows=data.trend || [];
  const rateSec=Number.isFinite(Number(s.produce_per_sec)) ? Number(s.produce_per_sec)
    : (Number.isFinite(Number(signal.produce_per_min)) ? Number(signal.produce_per_min)/60 : 0);
  const base=Number(s.produce != null ? s.produce : r.produce);
  const stamp=s.ts || r.ts, elapsed=stamp ? Math.max(0,(Date.now()-new Date(stamp).getTime())/1000) : 0;
  // It is explicitly labelled an estimate: the authoritative score is only read
  // once per cycle, but production accrues continuously on the server.
  const cap=Math.max(0,Number(data.cadence_seconds || 180)*1.25);
  const live=Number.isFinite(base) ? base + rateSec*Math.min(elapsed,cap) : null;
  $("hero-produce").textContent=live == null ? "—" : num(Math.floor(live));
  $("hero-produce-sub").textContent=rateSec > 0 ? `+${num(Math.round(rateSec))}/s estimated between score reads` : "waiting for a measured rate";
  const perMin=Number(s.produce_per_sec)*60 || Number(signal.produce_per_min);
  $("hero-rate").textContent=Number.isFinite(perMin) ? `${num(Math.round(perMin))}/min` : "—";
  $("hero-rate-sub").textContent="leaderboard lifetime-score delta over the real interval";
  $("hero-rank").textContent=r.rank == null ? "—" : `#${r.rank}${r.rank===1 ? " 👑" : ""}`;
  const rivals=Array.isArray(data.leaderboard) ? data.leaderboard : [];
  const closest=rivals.slice().sort((a,b) => Math.abs(a.gap)-Math.abs(b.gap))[0];
  $("hero-gap").textContent=closest ? `${closest.gap>=0?"+":""}${num(closest.gap)} ahead of ${closest.name}` : "no rival score available";
  $("hero-animals").textContent=num(s.animals != null ? s.animals : r.animals);
  $("hero-herd-sub").textContent=data.growth && data.growth.saturated ? `maintenance mode · ${num(data.growth.cap)} adoptions/run` : `growing · up to ${num((data.growth||{}).cap)} adoptions/run`;
  const hunger=s.hunger != null ? s.hunger : r.max_hunger;
  $("hero-hunger").textContent=hunger == null ? "—" : `${hunger} / ${s.hunger_stop || 70}`;
  $("hero-hunger-sub").textContent=hunger == null ? "production stops at 70" : hunger >= (s.hunger_stop || 70) ? "production stopped" : `${(s.hunger_stop || 70)-hunger} points of headroom`;
  $("spark-produce").innerHTML=spark(rows.map(x=>x.produce),"produce");
  $("spark-rate").innerHTML=spark(rows.map(x=>x.produce_per_min),"production rate");
  $("spark-rank").innerHTML=spark(rows.map(x=>x.rank == null ? null : -x.rank),"rank");
  $("spark-animals").innerHTML=spark(rows.map(x=>x.animals),"herd");
  $("spark-hunger").innerHTML=spark(rows.map(x=>x.max_hunger),"hunger");
}
function renderScene(s, rows) {
  const kinds=Object.entries(s.by_kind || {}), statuses=s.species_status||{};
  const pens=kinds.map(([kind,count],ki) => {
    // Logarithmic marks: a pen with 11,000 chickens should look much fuller than
    // 100 cows without attempting to place eleven thousand DOM nodes.
    const marks=Math.max(1,Math.min(24,Math.round(Math.log10(Math.max(1,count))*6)));
    const herd=Array.from({length:marks},(_,i)=>`<i style="animation-delay:${((i+ki)%8)*-.19}s">${KIND_ICON[kind] || "🐾"}</i>`).join("");
    const status=statuses[kind]||{}, ratio=Number(status.recent_vs_chicken);
    const note=kind==="chicken"?"promoted engine · strongest recent output per purchase coin":Number.isFinite(ratio)?`${fixed(ratio,2)}× chicken raw output/animal · nonzero observed`:`${esc(status.verdict||"nonzero observed output")}`;
    return `<div class="pen"><div class="pen-top"><b>${esc(kind)}</b><span class="pen-count">${num(count)}</span></div><div class="herd">${herd}</div><span class="pen-note">${note}</span></div>`;
  }).join("") || `<div class="empty">No species data</div>`;
  const feedPct=s.feed_fill==null?0:Math.round(s.feed_fill*100), hungerPct=s.hunger_fill==null?0:Math.round(s.hunger_fill*100);
  const readyMax=Math.max(1,...(rows||[]).map(x=>Number(x.ready_units)||0)), readyPct=Math.min(100,Math.round((Number(s.ready_units)||0)/readyMax*100));
  $("farm-scene").innerHTML=`<div class="scene-sky"><span class="scene-sun">${Number(s.hunger||0)>50?"🌥️":"☀️"}</span><div class="scene-rank"><b>${s.rank==null?"—":`#${s.rank}${s.rank===1?" 👑":""}`}</b><span>${num(s.produce)} lifetime produce</span></div></div><div class="pens">${pens}</div><div class="scene-ground"><div class="gauge"><label>Feed silo <b>${num(s.feed)} / ${num(s.reserve_target)}</b></label><div class="gtrack"><i style="width:${feedPct}%"></i></div><small>${fixed(s.feed_runway_min,0)}m runway · ${num(s.feed_runway_floor_min)}m floor</small></div><div class="gauge"><label>Hunger pressure <b>${num(s.hunger)} / ${num(s.hunger_stop)}</b></label><div class="gtrack hunger"><i style="width:${hungerPct}%"></i><u style="left:100%"></u></div><small>${Math.max(0,(s.hunger_stop||70)-(s.hunger||0))} points before production stops</small></div><div class="gauge"><label>Barn backlog <b>${num(s.ready_units)}</b></label><div class="gtrack"><i style="width:${readyPct}%"></i></div><small>${s.produce_delta==null?"waiting for a score delta":`${num(s.produce_delta)} produced since last run`}</small></div></div>`;
}
function renderOverview(data) {
  const r = data.latest || {}, current = data.current || {}, ld = data.launchd || {};
  const health = data.health || "offline";
  $("health").innerHTML = `<span class="pill ${esc(health)}">${esc(health)}</span>`;
  $("last-run").textContent = r.run == null ? "—" : `#${r.run}`;
  $("run-age").textContent = age(data.latest && data.latest.ts ? Math.max(0, Math.floor((Date.now()-new Date(data.latest.ts).getTime())/1000)) : null);
  $("stage").textContent = current.active ? current.stage : "idle";
  $("cadence").textContent = `${data.cadence_seconds}s`;
  renderHero(data);
  renderScene(data.scene || {}, data.trend || []);

  const blockers=data.blockers || [];
  $("blockers").innerHTML = blockers.length ? blockers.map(b => `<li class="alert ${esc(b.level)}"><span class="alert-dot"></span><span>${esc(b.text)}</span></li>`).join("")
    : `<li class="alert ok"><span class="alert-dot"></span><span>No operator action required</span></li>`;
  $("system").innerHTML = kv([
    ["cycle agent", ld.loaded ? `${esc(ld.state)}${ld.pid ? ` · pid ${ld.pid}` : ""}` : "not loaded"],
    ["supervisor", ld.supervisor?.loaded ? `${esc(ld.supervisor.state)}` : "not loaded"],
    ["agent runs", num(ld.runs)],
    ["last exit", esc(ld.last_exit || "—")],
    ["release pointer", esc(data.release?.pointer_revision || data.release?.revision || "—")],
    ["code in this view", data.release?.stale
      ? `<span style="color:var(--red)">stale · ${esc(data.release?.serving_revision || "unknown")}</span>`
      : data.release?.diverged
        ? `<span style="color:var(--yellow)">working tree (unreleased)</span>`
        : `matches release`],
    ["server calls", num(r.calls)],
  ]);
  $("farm").innerHTML = kv([
    ["leaderboard", r.rank == null ? "—" : `#${r.rank}`],
    ["lifetime produce", num(r.produce)],
    ["animals", num(r.animals)],
    ["by kind", Object.entries(r.by_kind || {}).map(([k,v]) => `${esc(k)} ${num(v)}`).join(" · ") || "—"],
    ["coins", num(r.coins)],
    ["feed / reserve", `${num(r.feed)} / ${num(r.reserve_target)}`],
    ["hunger", r.max_hunger == null ? "—" : `${r.max_hunger} / 70`],
    ["ready produce", num(r.ready_units)],
    ["collection throughput (trailing proxy)", r.units_per_chicken_min == null ? "—" : `${fixed(r.units_per_chicken_min,4)} / chicken / min`],
    ["last collected", Object.entries(r.collected || {}).map(([k,v]) => `${esc(k)} ${num(v)}`).join(" · ") || "none"],
    ["adopted", `${num(r.adopted)} / ${num(r.adopt_requested)}`],
    ["cycle duration", r.duration_s == null ? "—" : `${fixed(r.duration_s,1)}s`],
  ]);

  const board = Array.isArray(data.leaderboard) ? data.leaderboard : [];
  const racers=[{name:"Nick",produce:Number(r.produce)||0,gap:0,self:true},...board]
    .sort((a,b)=>b.produce-a.produce), top=Math.max(1,...racers.map(x=>x.produce));
  const leaders=racers.slice(0,5);
  $("leaderboard").innerHTML = leaders.length ? `<div class="race">${leaders.map((x,i)=>`<div class="racer${x.self?" self":""}"><div class="race-label"><span>${i===0?"👑 ":""}${esc(x.name)}</span><b>${num(x.produce)}</b></div><div class="race-track"><i style="width:${Math.max(2,x.produce/top*100).toFixed(1)}%"></i></div>${x.self?"":`<small>${x.gap>=0?num(x.gap)+" behind Nick":num(Math.abs(x.gap))+" ahead"}</small>`}</div>`).join("")}</div>${racers.length>leaders.length?`<div class="hint" style="margin-top:9px">${num(racers.length-leaders.length)} more racers in full standings</div>`:""}` : `<div class="empty">No leaderboard data</div>`;
  $("intent").innerHTML = kv([
    ["status", current.active ? "active" : "idle"],
    ["stage", esc(current.stage)],
    ["started", time(current.ts)],
    ["detail", Object.entries(current.detail || {}).map(([k,v]) => `${esc(k)}=${esc(v)}`).join(" · ") || "—"],
  ]);

  renderRuns(data.trend || [], (data.tokens && data.tokens.per_run) || {});
}
function renderLeaderboardHistory(history) {
  const host=$("gp-chart"), legend=$("gp-legend"), note=$("gp-note");
  const source=Array.isArray(history)?history:[], rows=source.slice(-RACE_RANGE);
  if (rows.length<2) {
    host.innerHTML=`<div class="empty">Leaderboard history will appear after two measured runs</div>`;
    legend.innerHTML="";
    note.textContent="The chart uses recorded leaderboard snapshots and makes zero additional farm calls.";
    return;
  }
  const nameSet=new Set();
  rows.forEach(row=>Object.keys(row.scores||{}).forEach(name=>nameSet.add(name)));
  const raw=[...nameSet].map(name=>{
    const points=rows.map((row,index)=>{
      const value=Number((row.scores||{})[name]);
      return Number.isFinite(value)?{index,row,value}:null;
    }).filter(Boolean);
    if (!points.length) return null;
    const first=points[0], last=points[points.length-1];
    return {name,points,first,last,latest:last.value,gain:last.value-first.value};
  }).filter(Boolean).sort((a,b)=>b.latest-a.latest||a.name.localeCompare(b.name));
  if (!raw.length) { host.innerHTML=`<div class="empty">No scores in this window</div>`; legend.innerHTML=""; return; }
  const palette=["#72e09a","#8fc8ff","#f5cc75","#ff8a83","#c9a7ff","#58d8d0","#ff9ed1","#d6e676","#ffb86b","#9aa7ff","#7cc47c","#d69b72","#9fb3c8","#e6e6e6"];
  const nickIndex=raw.findIndex(item=>item.name==="Nick");
  if (nickIndex>0) raw.unshift(raw.splice(nickIndex,1)[0]);
  raw.forEach((item,index)=>{item.color=palette[index%palette.length];item.rank=[...raw].sort((a,b)=>b.latest-a.latest).findIndex(x=>x===item)+1;});
  const plotted=raw.flatMap(item=>item.points.map(point=>RACE_MODE==="gain"?point.value-item.first.value:point.value));
  let low=RACE_MODE==="gain"?Math.min(0,...plotted):0, high=Math.max(...plotted);
  if (high===low) high=low+1;
  const w=1000,h=285,p={l:68,r:92,t:16,b:30},cw=w-p.l-p.r,ch=h-p.t-p.b,span=high-low;
  const x=index=>p.l+(rows.length===1?cw:index/(rows.length-1)*cw);
  const y=value=>p.t+(high-value)/span*ch;
  const score=(item,point)=>RACE_MODE==="gain"?point.value-item.first.value:point.value;
  const grid=[0,.25,.5,.75,1].map(f=>{const value=high-span*f,yy=p.t+ch*f;return `<line class="grid-line" x1="${p.l}" y1="${yy}" x2="${p.l+cw}" y2="${yy}"></line><text class="chart-label" x="0" y="${yy+4}">${RACE_MODE==="gain"&&value>0?"+":""}${esc(short(value))}</text>`}).join("");
  const sampleEvery=Math.max(1,Math.ceil(rows.length/24));
  const lines=raw.map(item=>{
    const coords=item.points.map(point=>`${x(point.index).toFixed(1)},${y(score(item,point)).toFixed(1)}`).join(" ");
    const sampled=item.points.filter((point,index)=>index%sampleEvery===0||index===item.points.length-1).map(point=>`<circle cx="${x(point.index)}" cy="${y(score(item,point))}" r="${point===item.last?4:2.2}" fill="${item.color}" class="gp-point"><title>${esc(item.name)} · run ${esc(point.row.run)} · ${RACE_MODE==="gain"?(score(item,point)>=0?"+":"")+num(score(item,point))+" gained":num(point.value)+" total"}</title></circle>`).join("");
    return `<polyline class="gp-line${item.name==="Nick"?" self":""}" stroke="${item.color}" points="${coords}"><title>${esc(item.name)} · ${num(item.latest)} current · ${item.gain>=0?"+":""}${num(item.gain)} this window</title></polyline>${sampled}`;
  }).join("");
  const front=[...raw].sort((a,b)=>b.latest-a.latest).slice(0,Math.min(5,raw.length));
  const labelRows=front.map(item=>({item,point:item.last,target:y(score(item,item.last))})).sort((a,b)=>a.target-b.target);
  labelRows.forEach((row,index)=>{row.labelY=Math.max(12,row.target,index?labelRows[index-1].labelY+14:12);});
  const overflow=(labelRows[labelRows.length-1].labelY||0)-(h-10);
  if (overflow>0) labelRows.forEach(row=>{row.labelY-=overflow;});
  const labels=labelRows.map(row=>`<text class="gp-end-label" fill="${row.item.color}" x="${Math.min(w-86,x(row.point.index)+7)}" y="${row.labelY}">${row.item.rank}. ${esc(row.item.name)}</text>`).join("");
  host.innerHTML=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Leaderboard scores over ${rows.length} recorded runs">${grid}${lines}${labels}<text class="chart-label" x="${p.l}" y="${h-5}">run ${esc(rows[0].run)}</text><text class="chart-label" x="${p.l+cw}" y="${h-5}" text-anchor="end">run ${esc(rows[rows.length-1].run)}</text></svg>`;
  legend.innerHTML=[...raw].sort((a,b)=>a.rank-b.rank).map(item=>`<div class="gp-racer${item.name==="Nick"?" self":""}"><i class="gp-swatch" style="color:${item.color}"></i><b>${esc(item.name)}<small>#${item.rank}</small></b><span>${num(item.latest)}<small>${item.gain>=0?"+":""}${num(item.gain)} window</small></span></div>`).join("");
  const standings=[...raw].sort((a,b)=>b.latest-a.latest), leader=standings[0], chase=standings.find(item=>item.name!==leader.name);
  note.textContent=`${rows.length} recorded runs · ${raw.length} racers · ${leader.name} leads${chase?` by ${num(leader.latest-chase.latest)} over ${chase.name}`:""} · zero additional farm calls`;
  document.querySelectorAll("#gp-modes .chip").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.gpmode===RACE_MODE)));
  document.querySelectorAll("#gp-ranges .chip").forEach(button=>button.setAttribute("aria-pressed",String(Number(button.dataset.gprange)===RACE_RANGE)));
}
function renderRuns(rows, perRun) {
  const host=$("runs"), usd=value=>value==null?"—":`$${Number(value).toFixed(4)}`;
  if (!rows.length) { host.innerHTML=`<div class="empty">No runs recorded</div>`; return; }
  const body=[];
  for (const x of rows.slice().reverse()) {
    const t=perRun[x.run] || {tokens:0,cost_usd:0}, open=String(ACTIVE_RUN)===String(x.run);
    body.push(`<tr class="runrow${open?" open":""}" data-run="${esc(x.run)}" aria-expanded="${open}"><td>#${esc(x.run)}</td><td>${time(x.ts)}</td><td>${x.rank===1?"#1 👑":"#"+esc(x.rank)}</td><td>${num(x.animals)}</td><td>${num(x.produce)}</td><td class="${x.produce_per_min!=null&&x.produce_per_min>0?"good":""}">${x.produce_per_min==null?fixed(x.units_per_chicken_min,4):num(Math.round(x.produce_per_min))+"/m"}</td><td>${esc(x.max_hunger)}</td><td>${num(x.adopted)}</td><td class="${t.tokens?"bad":"good"}">${num(t.tokens)}</td><td class="${t.cost_usd?"bad":"good"}">${usd(t.cost_usd)}</td><td class="${(x.anomalies||[]).length?"bad":"good"}">${(x.anomalies||[]).length||"OK"}</td></tr>`);
    if (open) body.push(`<tr class="rundetail"><td colspan="11">${runDetail(x)}</td></tr>`);
  }
  host.innerHTML=`<table><thead><tr><th>Run</th><th>Time</th><th>Rank</th><th>Animals</th><th>Produce</th><th>Rate</th><th>Hunger</th><th>Adopted</th><th>Tokens</th><th>Cost</th><th>Alerts</th></tr></thead><tbody>${body.join("")}</tbody></table><div class="hint">Click a run to inspect phase timing, actions and decision inputs.</div>`;
}
function runDetail(x) {
  const phases=Object.entries(x.phases||{}).sort((a,b)=>Number(b[1])-Number(a[1]));
  const max=Math.max(1,...phases.map(p=>Number(p[1])||0));
  const bars=phases.length?phases.map(([name,sec])=>`<div class="phaserow"><span>${esc(name)}</span><span class="phasebars"><i style="width:${(Number(sec)/max*100).toFixed(1)}%"></i></span><span>${fixed(sec,1)}s</span></div>`).join(""):`<div class="empty">No phase timing recorded</div>`;
  const notes=[...(x.notes||[]),...(x.notes_soft||[])];
  return `<div class="detail"><div><h3>Where the run spent time</h3>${bars}</div><div><h3>Actions</h3><div class="kv">${kv([["collected",num(x.units_collected)],["fed",num(x.fed)],["feed bought",num(x.feed_bought)],["revenue",x.revenue==null?"—":num(x.revenue)+"c"],["server calls",num(x.calls)],["verified",x.verified?"yes":"no"]])}</div></div><div><h3>Decision evidence</h3><div class="kv">${kv([["growth",esc((x.growth||{}).reason||x.adopt_stopped||"—")],["plan",Object.entries(x.plan||{}).map(([k,v])=>`${esc(k)}=${esc(v)}`).join(" · ")||"—"],["notes",notes.length?notes.map(esc).join(" · "):"none"]])}</div></div></div>`;
}
const STATUS_PILL = {running:"running", ok:"healthy", failed:"offline", idle:"waiting"};
function secs(value) { return value == null ? "—" : `${Number(value).toFixed(1)}s`; }
function renderGrowth(g, recovery={}) {
  const saturated = !!g.saturated;
  const recent = g.recent_units_per_min, window = g.smaller_units_per_min;
  const gain = recent && window ? ((recent / window - 1) * 100) : null;
  $("growth-verdict").innerHTML = `<span class="pill ${saturated ? "waiting" : "healthy"}">`
    + `${saturated ? "growth gate active · maintenance" : "growing"}</span>`
    + `<div class="subtitle" style="margin-top:10px">${esc(g.reason || "no measurement yet")}</div>`;
  $("growth").innerHTML = kv([
    ["adoptions allowed", g.cap == null ? "—" : num(g.cap)],
    ["herd", num(g.herd)],
    ["output now", recent == null ? "—" : `${num(recent)} units/min`],
    ["output at smaller herd", window == null ? "—" : `${num(window)} units/min`],
    ["marginal gain", gain == null ? "—" : `${gain >= 0 ? "+" : ""}${gain.toFixed(1)}% (noise floor ${fixed(g.marginal_threshold_pct,0)}%)`],
    ["plateau", g.plateau == null ? "—" : `${num(g.plateau)} units/min`],
    ["samples", `${num(g.recent_samples)} now / ${num(g.smaller_samples)} smaller`],
    ["decided", g.changed_run == null ? "—" : `run ${esc(g.changed_run)}`],
    ["30-minute recovery agent", recovery.status ? `${esc(recovery.status)} · ${num(recovery.checks)} checks` : "not armed"],
    ["recovery last checked", recovery.last_checked_ts ? esc(recovery.last_checked_ts) : "—"],
  ]);
}
function renderSignals(s, growth) {
  const rate = s.produce_per_min, floor = s.floor;
  const stalled = s.below_floor && s.prev_below_floor;
  const watching = s.below_floor && !s.prev_below_floor;
  const level = stalled ? "error" : (watching ? "waiting" : "healthy");
  const label = stalled ? "production stalled · escalating"
    : (watching ? "one low window · watching" : "producing normally");
  $("signal-verdict").innerHTML = `<span class="pill ${level}">${esc(label)}</span>`
    + `<div class="subtitle" style="margin-top:10px">Score rate ${rate == null ? "—" : num(Math.round(rate)) + " produce/min"}`
    + ` against a ${num(Math.round(floor))}/min floor. Escalation needs two consecutive low windows — single windows are lumpy.</div>`;
  const hunger = s.hunger, stop = s.hunger_stop;
  $("signals").innerHTML = kv([
    ["score rate (this run)", rate == null ? "—" : `${num(Math.round(rate))} / min`],
    ["score rate (previous)", s.prev_produce_per_min == null ? "—" : `${num(Math.round(s.prev_produce_per_min))} / min`],
    ["floor for this herd", `${num(Math.round(floor))} / min`],
    ["hunger vs production stop", hunger == null ? "—" : `${hunger} / ${stop}`],
    ["feed / reserve", `${num(s.feed)} / ${num(s.reserve_target)}`],
    ["units collected (trailing)", num(s.units_collected)],
    ["server calls", `${num(s.calls)} at ${fixed(s.call_rate,2)}/s`],
    ["adoption", growth && growth.saturated ? `maintenance cap: ${num((growth||{}).cap)}` : `allowed: ${num((growth||{}).cap)}`],
  ]);
  const soft = s.soft || [];
  if (soft.length) {
    $("signals").innerHTML += `<div class="tagrow">${soft.map(x => `<span class="tag">${esc(x)}</span>`).join("")}</div>`;
  }
}
function renderPipeline(p, signals, cadenceSeconds) {
  const steps = p.steps || [];
  const status = p.status || "idle";
  const started = p.started_ts ? new Date(p.started_ts).getTime() : null;
  const now = Date.now();
  // A running pipeline is measured against the wall clock so the active step
  // ticks upward between polls; a finished one uses its recorded end time.
  const endRef = status === "running" ? now : (p.finished_ts ? new Date(p.finished_ts).getTime() : null);
  const elapsed = started && endRef ? Math.max(0, (endRef - started) / 1000) : null;
  const done = steps.filter(s => s.status === "done").length;
  const skipped = steps.filter(s => s.status === "skipped").length;

  // Between runs the whole tab is legitimately static for ~3 of every ~4.3
  // minutes, which is indistinguishable from a dead page. So an idle pipeline
  // shows a live countdown to the next expected run, and a "running" pipeline
  // whose progress writes have stopped (a hard-killed process never calls
  // finish()) is reported as stalled instead of ticking forever.
  const cadenceMs = Number(cadenceSeconds || 0) * 1000;
  const finished = p.finished_ts ? new Date(p.finished_ts).getTime() : null;
  const updated = p.updated_ts ? new Date(p.updated_ts).getTime() : null;
  const timeoutS = Number(p.timeout_s || 0) || 240;
  const staleFor = status === "running" && updated ? (now - updated) / 1000 : null;
  const stalled = staleFor != null && staleFor > Math.max(90, timeoutS);
  const untilNext = status !== "running" && finished && cadenceMs ? (finished + cadenceMs - now) / 1000 : null;

  let pill = STATUS_PILL[status] || "waiting", label = status;
  if (stalled) { pill = "offline"; label = `stalled - no progress write for ${age(Math.round(staleFor))}`; }
  else if (status === "running") { label = `running - ${p.active || "starting"}`; }
  else if (untilNext != null && untilNext > 0) { pill = "waiting"; label = `${status} - waiting for next run`; }
  else if (untilNext != null) { pill = "warn"; label = `${status} - next run overdue by ${age(Math.abs(Math.round(untilNext)))}`; }

  $("pipe-status").innerHTML = `<span class="pill ${esc(pill)}">${esc(label)}</span>`;
  $("pipe-run").textContent = p.run == null ? "—" : `#${p.run}${p.dry ? " (dry)" : ""}`;
  $("pipe-elapsed").textContent = elapsed == null ? "—" : secs(elapsed);
  $("pipe-active").textContent = p.active ? esc(p.active) : (status === "running" ? "—" : "idle");
  $("pipe-next").textContent = status === "running" ? "after this run"
    : untilNext == null ? "—"
    : untilNext > 0 ? `in ${age(Math.ceil(untilNext))}`
    : `overdue ${age(Math.abs(Math.round(untilNext)))}`;
  $("pipe-count").textContent = `${done}/${steps.length}${skipped ? ` (+${skipped} skipped)` : ""}`;

  const budget = Number(p.budget_s || 0), timeout = Number(p.timeout_s || 0);
  const pct = budget && elapsed != null ? Math.min(100, (elapsed / budget) * 100) : 0;
  $("pipe-budget").innerHTML = kv([
    ["elapsed", elapsed == null ? "—" : secs(elapsed)],
    ["adoption budget", budget ? `${budget}s` : "—"],
    ["hard timeout", timeout ? `${timeout}s` : "—"],
    ["headroom", budget && elapsed != null ? secs(Math.max(0, budget - elapsed)) : "—"],
  ]);
  const bar = $("pipe-budget-bar");
  bar.className = `budget${budget && elapsed > budget ? " over" : ""}`;
  bar.innerHTML = `<i style="width:${pct.toFixed(1)}%"></i>`;

  const summary = p.summary || {};
  $("pipe-summary").innerHTML = Object.keys(summary).length ? kv([
    ["run", summary.run == null ? "—" : `#${summary.run}`],
    ["duration", secs(summary.duration_s)],
    ["rank", summary.rank == null ? "—" : `#${summary.rank}`],
    ["animals", num(summary.animals)],
    ["revenue", summary.revenue == null ? "—" : `${num(summary.revenue)}c`],
    ["anomalies", summary.anomalies == null ? "—" : num(summary.anomalies)],
  ]) : `<div class="empty">No completed run recorded yet</div>`;

  const baseline = p.baseline || {};
  const widest = Math.max(1, ...steps.map(s => Number(s.seconds) || 0), ...Object.values(baseline).map(Number));
  $("pipe-steps").innerHTML = steps.length ? steps.map(s => {
    const live = s.status === "active" && s.started_ts ? Math.max(0, (Date.now() - new Date(s.started_ts).getTime()) / 1000) : null;
    const shown = s.status === "active" ? live : (s.seconds == null ? null : Number(s.seconds));
    const width = shown == null ? 0 : Math.min(100, (shown / widest) * 100);
    const detail = Object.entries(s.detail || {}).filter(([k]) => k !== "seconds")
      .map(([k, v]) => `${esc(k)} <b>${esc(v)}</b>`).join(" · ");
    const note = s.note ? `<span style="color:var(--muted)">${esc(s.note)}</span>` : "";
    const base = baseline[s.name] != null ? `<span style="color:var(--muted)"> · median ${secs(baseline[s.name])}</span>` : "";
    return `<div class="pstep ${esc(s.status)}"><div class="dot"></div>`
      + `<div class="pname">${esc(s.label)}<small>${esc(s.hint || "")}</small></div>`
      + `<div class="pmeta">${detail || note || "—"}${base}${shown != null ? `<div class="pbar"><i style="width:${width.toFixed(1)}%"></i></div>` : ""}</div>`
      + `<div class="psec">${s.status === "skipped" ? "skipped" : (shown == null ? "—" : secs(shown))}</div></div>`;
  }).join("") : `<div class="empty">No pipeline data yet</div>`;
}
function renderCost(t, c) {
  const usd = value => `$${Number(value || 0).toFixed(4)}`;
  $("cost-total").textContent = usd(t.total_cost_usd);
  $("cost-total").className = `big ${Number(t.total_cost_usd || 0) > 0 ? "" : ""}`;
  $("cost-total-sub").textContent = `${num(c.ledger_runs)} runs in the ledger`
    + (c.first_ts ? ` since ${time(c.first_ts)}` : "");
  $("cost-total-tokens").textContent = num(t.total_tokens || 0);
  $("cost-total-esc").textContent = num(t.total_escalations || 0);
  $("cost-charged").textContent = num(c.charged_runs);
  $("cost-free").textContent = num(c.free_runs);
  $("cost-avoided").textContent = usd(t.avoided_cost_usd);
  $("cost-detail").innerHTML = kv([
    ["alerts healed in python", num(t.total_healed || 0)],
    ["cost per wake-up", usd(t.cost_per_escalation_usd)],
    ["escalations (24h)", num(t.window_escalations || 0)],
    ["cost (24h)", usd(t.window_cost_usd)],
    ["last 5 runs", usd(c.recent_cost_usd)],
    ["tokens, last 5 runs", num(c.recent_tokens)],
  ]);
  const rows = c.runs || [];
  $("cost-recent").innerHTML = rows.length
    ? `<table><thead><tr><th>Run</th><th>Time</th><th>Tokens in</th><th>Out</th><th>Cost</th><th>Wake-ups</th><th>Healed</th></tr></thead><tbody>`
      + rows.map(r => `<tr><td>#${esc(r.run)}</td><td>${time(r.ts)}</td><td>${num(r.tokens_in)}</td><td>${num(r.tokens_out)}</td>`
        + `<td class="${r.cost_usd ? "bad" : "good"}">${usd(r.cost_usd)}</td>`
        + `<td class="${r.escalations ? "bad" : "good"}">${r.escalations || "0"}</td>`
        + `<td class="${r.healed ? "good" : ""}">${r.healed || "—"}</td></tr>`
        + (r.alerts && r.alerts.length ? `<tr><td colspan="7" style="padding-top:0"><div class="tagrow">${r.alerts.map(a => `<span class="tag">${esc(a)}</span>`).join("")}</div></td></tr>` : "")).join("")
      + `</tbody></table>`
    : `<div class="empty">No ledger rows yet</div>`;
}
function renderHealing(h) {
  const k = h.knobs || {};
  const overrides = k.overrides || {};
  const mark = (name, value) => name in overrides ? `${value} <span style="color:var(--yellow)">(healed)</span>` : `${value}`;
  $("knobs").innerHTML = kv([
    ["call rate ceiling", mark("rate_ceiling", `${fixed(k.rate_ceiling,2)}/s`)],
    ["adopt cap (safety)", mark("adopt_cap", num(k.adopt_cap))],
    ["adopt workers", mark("adopt_workers", num(k.adopt_workers))],
    ["collect passes", mark("collect_passes", num(k.collect_passes))],
    ["active overrides", Object.keys(overrides).length ? esc(Object.keys(overrides).join(", ")) : "none (all defaults)"],
  ]);
  const classes = (h.classes || []).filter(c => c.class !== "relax");
  const relax = (h.classes || []).find(c => c.class === "relax");
  $("heal-classes").innerHTML = (classes.length
    ? classes.map(c => `<details class="healclass"><summary><span class="top"><span class="cls">${esc(c.class)}</span>`
        + `<span class="n">${num(c.count)}× · last run ${esc(c.last_run)}</span></span>`
        + `<span class="what">${esc(c.last_action || "—")}</span></summary>`
        + (c.alerts && c.alerts.length ? `<div class="heal-evidence"><div class="tagrow">${c.alerts.map(a => `<span class="tag">${esc(a)}</span>`).join("")}</div></div>` : "")
        + `</details>`).join("")
    : `<div class="empty">Nothing has needed healing</div>`)
    + (relax ? `<div class="healclass relax-row"><div class="top"><span class="cls">relax</span><span class="n">${num(relax.count)}×</span></div>`
        + `<div class="what">Knobs stepping back toward default after quiet runs — ${esc(relax.last_action || "")}</div></div>` : "");
  const log = h.recent || [];
  $("heal-log").innerHTML = log.length ? log.map(x => `<li><span style="color:var(--muted)">${time(x.ts)} run ${esc(x.run)}</span> <strong>${esc(x.class)}</strong>: ${esc(x.action)}${x.alert ? `<div class="what" style="color:var(--muted);font-size:12.5px">${esc(x.alert)}</div>` : ""}</li>`).join("") : `<li class="empty">No remedies applied yet</li>`;
}
function renderChart(rows) {
  const host=$("chart"), meta=METRICS[CHART_METRIC]||METRICS.produce;
  const clean=(rows||[]).map(r=>({row:r,value:Number(r[CHART_METRIC])})).filter(p=>Number.isFinite(p.value));
  if (!clean.length) { host.innerHTML=`<div class="empty">No ${esc(meta.label.toLowerCase())} data in these runs</div>`; return; }
  const values=clean.map(p=>p.value), min=Math.min(...values), max=Math.max(...values), span=Math.max(1,max-min);
  const w=900,h=180,pad={l:52,r:12,t:12,b:24}, cw=w-pad.l-pad.r,ch=h-pad.t-pad.b;
  const pts=clean.map((p,i)=>({x:pad.l+(clean.length===1?cw:i/(clean.length-1)*cw),y:pad.t+(max-p.value)/span*ch,...p}));
  const poly=pts.map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  const area=`M${pad.l},${pad.t+ch} L${pts.map(p=>`${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" L")} L${pad.l+cw},${pad.t+ch} Z`;
  const grid=[0,.25,.5,.75,1].map(f=>{const y=pad.t+ch*f,v=max-span*f;return `<line class="grid-line" x1="${pad.l}" y1="${y}" x2="${pad.l+cw}" y2="${y}"></line><text class="chart-label" x="0" y="${y+4}">${esc(short(v))}</text>`}).join("");
  const dots=pts.map(p=>`<circle class="dot" cx="${p.x}" cy="${p.y}" r="2.8"><title>Run ${esc(p.row.run)} · ${meta.label}: ${num(Math.round(p.value))}${meta.unit} · ${time(p.row.ts)}</title></circle><circle class="dot-hit" cx="${p.x}" cy="${p.y}" r="9"><title>Run ${esc(p.row.run)} · ${meta.label}: ${num(Math.round(p.value))}${meta.unit}</title></circle>`).join("");
  host.innerHTML=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(meta.label)} trend"><defs><linearGradient id="areafill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#72e09a" stop-opacity=".28"></stop><stop offset="1" stop-color="#72e09a" stop-opacity="0"></stop></linearGradient></defs>${grid}<path class="area" d="${area}"></path><polyline class="line" points="${poly}"></polyline>${dots}<text class="chart-label" x="${pad.l}" y="${h-4}">run ${esc(clean[0].row.run)}</text><text class="chart-label" x="${pad.l+cw}" y="${h-4}" text-anchor="end">run ${esc(clean[clean.length-1].row.run)}</text></svg>`;
  $("chart-title").textContent=`${meta.label} trend`;
  $("chart-note").textContent=`${clean.length} runs · min ${num(Math.round(min))}${meta.unit} · latest ${num(Math.round(values[values.length-1]))}${meta.unit} · max ${num(Math.round(max))}${meta.unit}`;
  document.querySelectorAll("#chart-metrics .chip").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.metric===CHART_METRIC)));
}
function short(v) {
  const n=Number(v), a=Math.abs(n);
  if (!Number.isFinite(n)) return "—";
  if (a>=1e9) return `${(n/1e9).toFixed(1)}B`;
  if (a>=1e6) return `${(n/1e6).toFixed(1)}M`;
  if (a>=1e3) return `${(n/1e3).toFixed(a>=1e4?0:1)}K`;
  return String(Math.round(n));
}
function money(value, digits=2) {
  const n=Number(value||0);
  return `$${n.toLocaleString(undefined,{minimumFractionDigits:digits,maximumFractionDigits:digits})}`;
}
function renderCostHistory(h) {
  const stats=h.stats||{}, assumption=h.per_run_assumption||{}, points=h.points||[];
  $("hist-actual-cost").textContent=money(stats.actual_cost,2);
  $("hist-actual-cost-sub").textContent=`estimated across ${num(stats.ledger_runs)} ledger runs · not provider billing`;
  $("hist-actual-tokens").textContent=num(stats.actual_tokens);
  $("hist-runs").textContent=num(stats.ledger_runs);
  $("hist-runs-sub").textContent=`runs ${num(stats.first_run)}-${num(stats.last_run)} · ${num(stats.zero_runs)} explicit zeros`;
  $("hist-avoided").textContent=money(Math.max(0,Number(stats.counterfactual_cost_mid||0)-Number(stats.actual_cost||0)),0);
  $("hist-avoided-sub").textContent=`net range ${money(Math.max(0,Number(stats.counterfactual_cost_low||0)-Number(stats.actual_cost||0)),0)}-${money(Math.max(0,Number(stats.counterfactual_cost_high||0)-Number(stats.actual_cost||0)),0)}`;
  $("hist-reduction").textContent=stats.reduction_pct==null?"—":`${fixed(stats.reduction_pct,3)}%`;
  $("hist-wakeups").textContent=num(stats.escalations);
  $("hist-wakeups-sub").textContent=`${num(stats.healed)} recorded local dispositions, including stale/covered alerts`;
  $("hist-old-monthly").textContent=money(stats.old_monthly_cost_mid,0);
  $("hist-old-monthly-range").textContent=`measured range ${money(stats.old_monthly_cost_low,0)}-${money(stats.old_monthly_cost_high,0)} at a 5-minute cadence`;
  $("hist-new-monthly").textContent=money(stats.actual_cost,2);

  const sources=h.token_sources||[], sourceMax=Math.max(1,...sources.map(x=>Number(x.tokens)||0));
  $("hist-sources").innerHTML=sources.map(x=>`<div class="source-row"><label>${esc(x.name)}</label><b>${short(x.tokens)}</b><div class="source-track"><i style="width:${Math.max(2,Number(x.tokens)/sourceMax*100).toFixed(1)}%"></i></div><small>${esc(x.note)}</small></div>`).join("")||`<div class="empty">No baseline composition</div>`;
  $("hist-changes").innerHTML=(h.changes||[]).map((x,index)=>`<button class="change-step ${esc(x.kind)}${index===HISTORY_CHANGE_INDEX?" selected":""}" data-history-change="${index}" aria-pressed="${index===HISTORY_CHANGE_INDEX}"><span class="change-icon">${esc(x.icon)}</span><span class="change-body"><span class="change-head"><b>${esc(x.era)}</b><span>${esc(x.when)}</span></span><span class="change-copy">${esc(x.change)}</span><span class="change-impact">${esc(x.impact)}</span><span class="change-code">${esc(x.code)}</span></span></button>`).join("")||`<div class="empty">No execution history</div>`;

  $("hist-ledger").innerHTML=points.map(p=>`<i class="ledger-dot${p.actual_cost||p.escalations?" charged":p.healed?" healed":""}" title="Run ${esc(p.run)} · ${p.actual_tokens?num(p.actual_tokens)+" tokens":p.healed?num(p.healed)+" alert(s) healed locally":"explicit zero tokens"}"></i>`).join("");
  $("hist-ledger-stats").innerHTML=[
    ["explicit zero-cost runs",`${num(stats.zero_runs)} / ${num(stats.ledger_runs)}`],
    ["local alert dispositions",num(stats.healed)],
    ["model wake-ups",num(stats.escalations)],
    ["per-alert upper bound (not billed savings)",money(stats.healing_cost_avoided,4)],
  ].map(([k,v])=>`<div class="metric"><label>${esc(k)}</label><strong>${esc(v)}</strong></div>`).join("");
  $("hist-disclosure").textContent=h.disclosure||"";
  renderCostHistoryChart(h);
  operatorHistory(h);
}
function renderCostHistoryChart(h) {
  const all=h.points||[], count=COST_HISTORY_RANGE==="all"?all.length:Number(COST_HISTORY_RANGE)||all.length;
  const points=all.slice(-count), assumption=h.per_run_assumption||{};
  const host=$("hist-chart");
  if (!points.length) { host.innerHTML=`<div class="empty">No token ledger rows yet</div>`; return; }
  let actual=[], old=[], low=[], high=[], title="", actualLabel="Actual ledger", oldLabel="Old-loop midpoint", note="";
  if (COST_HISTORY_METRIC==="tokens") {
    actual=points.map(p=>Number(p.cumulative_actual_tokens)||0);
    old=points.map(p=>Number(p.counterfactual_tokens_mid)||0);
    low=points.map(p=>Number(p.counterfactual_tokens_low)||0);
    high=points.map(p=>Number(p.counterfactual_tokens_high)||0);
    title="Cumulative tokens over audited runs";
    note=`Actual ${num(actual[actual.length-1])} tokens vs ${short(old[old.length-1])} at the old-loop midpoint.`;
  } else if (COST_HISTORY_METRIC==="per_run") {
    actual=points.map(p=>Number(p.actual_cost)||0);
    old=points.map(()=>Number(assumption.cost_mid)||0);
    low=points.map(()=>Number(assumption.cost_low)||0);
    high=points.map(()=>Number(assumption.cost_high)||0);
    title="Cost per run";
    note=`Latest run is ${money(actual[actual.length-1],2)}; routine cycles are zero and estimated exception runs appear as spikes. The old-loop range was ${money(assumption.cost_low,2)}-${money(assumption.cost_high,2)} per cycle.`;
  } else if (COST_HISTORY_METRIC==="healing") {
    actual=points.map(p=>Number(p.cumulative_healed)||0);
    old=points.map(p=>Number(p.cumulative_escalations)||0);
    low=[]; high=[]; title="Python healing vs model wake-ups";
    actualLabel="Alerts healed in Python"; oldLabel="Model wake-ups";
    note=`${num(actual[actual.length-1])} alerts healed locally; ${num(old[old.length-1])} model wake-ups booked.`;
  } else {
    actual=points.map(p=>Number(p.cumulative_actual_cost)||0);
    old=points.map(p=>Number(p.counterfactual_cost_mid)||0);
    low=points.map(p=>Number(p.counterfactual_cost_low)||0);
    high=points.map(p=>Number(p.counterfactual_cost_high)||0);
    title="Cumulative cost over audited runs";
    note=`Actual ${money(actual[actual.length-1],2)} vs old-loop midpoint ${money(old[old.length-1],0)}; measured range ${money(low[low.length-1],0)}-${money(high[high.length-1],0)}.`;
  }
  const w=900,hgt=280,pad={l:66,r:18,t:20,b:30},cw=w-pad.l-pad.r,ch=hgt-pad.t-pad.b;
  const ceiling=Math.max(1,...actual,...old,...high), x=i=>pad.l+(points.length===1?cw:i/(points.length-1)*cw), y=v=>pad.t+(ceiling-Number(v))/ceiling*(ch-3);
  const line=values=>values.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  let band="";
  if (low.length&&high.length) band=`<path class="cost-band" d="M${low.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" L")} L${high.slice().reverse().map((v,j)=>{const i=high.length-1-j;return `${x(i).toFixed(1)},${y(v).toFixed(1)}`}).join(" L")} Z"></path>`;
  const grid=[0,.25,.5,.75,1].map(f=>{const v=ceiling*(1-f),yy=pad.t+ch*f;return `<line class="grid-line" x1="${pad.l}" y1="${yy}" x2="${w-pad.r}" y2="${yy}"></line><text class="chart-label" x="0" y="${yy+4}">${esc(historyValue(v,COST_HISTORY_METRIC))}</text>`}).join("");
  const sampleEvery=Math.max(1,Math.floor(points.length/12));
  const dots=points.map((row,i)=>i%sampleEvery===0||i===points.length-1?`<circle class="cost-point" cx="${x(i)}" cy="${y(actual[i])}" r="4"><title>Run ${esc(row.run)} · actual ${historyValue(actual[i],COST_HISTORY_METRIC)}</title></circle><circle class="cost-point old" cx="${x(i)}" cy="${y(old[i])}" r="3"><title>Run ${esc(row.run)} · counterfactual ${historyValue(old[i],COST_HISTORY_METRIC)}</title></circle>`:"").join("");
  const gateIndex=points.findIndex(row=>Number(row.run)===46), marker=gateIndex>=0?`<line class="cost-marker" x1="${x(gateIndex)}" y1="${pad.t}" x2="${x(gateIndex)}" y2="${pad.t+ch}"></line><text class="chart-label" x="${x(gateIndex)+5}" y="${pad.t+10}">run 46 · historical false-plateau gate (superseded)</text>`:"";
  const selectedChange=(h.changes||[])[HISTORY_CHANGE_INDEX]||{}, selectedRun=Number(selectedChange.run), selectedIndex=Number.isFinite(selectedRun)?points.findIndex(row=>Number(row.run)>=selectedRun):-1;
  const selectedMarker=selectedIndex>=0?`<line class="cost-marker selected" x1="${x(selectedIndex)}" y1="${pad.t}" x2="${x(selectedIndex)}" y2="${pad.t+ch}"></line><text class="chart-label" x="${Math.min(w-190,x(selectedIndex)+6)}" y="${pad.t+24}">${esc(selectedChange.era||"selected milestone")}</text>`:"";
  host.innerHTML=`<svg viewBox="0 0 ${w} ${hgt}" role="img" aria-label="${esc(title)}">${grid}${band}${marker}${selectedMarker}<polyline class="cost-old${COST_HISTORY_METRIC==="healing"?" cost-wakeup":""}" points="${line(old)}"></polyline><polyline class="cost-actual" points="${line(actual)}"></polyline>${dots}<text class="chart-label" x="${pad.l}" y="${hgt-5}">run ${esc(points[0].run)}</text><text class="chart-label" x="${w-pad.r}" y="${hgt-5}" text-anchor="end">run ${esc(points[points.length-1].run)}</text></svg>`;
  $("hist-chart-title").textContent=title;
  $("hist-chart-note").textContent=note;
  $("hist-legend").innerHTML=`<span><i></i>${esc(actualLabel)}</span><span><i class="${COST_HISTORY_METRIC==="healing"?"wakeup":"old"}"></i>${esc(oldLabel)}</span>${low.length?`<span><i class="old band"></i>Measured low-high range</span>`:""}`;
  document.querySelectorAll("#hist-metrics .chip").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.hmetric===COST_HISTORY_METRIC)));
  document.querySelectorAll("#hist-range .chip").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.hrange===COST_HISTORY_RANGE)));
}
function historyValue(value, metric) {
  if (metric==="cost"||metric==="per_run") return money(value,metric==="per_run"?2:0);
  if (metric==="tokens") return short(value);
  return num(Math.round(Number(value)||0));
}
function renderEvidence(ev) {
  if (!ev || ev.error) {
    const message=`Evidence unavailable: ${ev&&ev.error?ev.error:"empty response"}`;
    $("ev-ceiling-claim").textContent=message;
    $("hist-chart").innerHTML=`<div class="empty">${esc(message)}</div>`;
    $("hist-actual-cost-sub").textContent=message;
    return;
  }
  renderCostHistory(ev.cost_history||{});
  const c=ev.ceiling||{}, buckets=c.buckets||[];
  $("ev-ceiling-claim").textContent=c.claim||"No claim recorded.";
  const scalingSummary=(c.scaling||{}).exponent;
  $("ev-ceiling-summary").textContent=scalingSummary==null?"The growth verdict is waiting for enough comparable runs.":c.saturating
    ? `Growth is showing saturation: exponent ${fixed(scalingSummary,3)} is below the 0.95 gate across ${num(c.samples)} comparable samples.`
    : `Growth is still paying: exponent ${fixed(scalingSummary,3)} remains above the 0.95 saturation gate across ${num(c.samples)} comparable samples. The association is descriptive, not causal.`;
  $("ev-ceiling-chart").innerHTML=evidenceChart(buckets,c.regression_from||8000);
  const below=c.regression_below||{}, above=c.regression||{};
  const scaling=c.scaling||{}, scalingB=c.scaling_bucketed||{}, wfit=c.regression_bucketed_weighted||{};
  const expLabel=scaling.exponent==null?"—":fixed(scaling.exponent,3)+(c.saturating?" saturating":" scaling");
  $("ev-ceiling-stats").innerHTML=[
    ["samples",num(c.samples)],
    ["growth slope below 8k",below.slope==null?"—":`${below.slope>=0?"+":""}${fixed(below.slope*1000,1)} units/min per +1k animals`],
    ["slope above 8k",above.slope==null?"—":`${above.slope>=0?"+":""}${fixed(above.slope*1000,1)} units/min per +1k animals`],
    ["correlation above 8k",above.r==null?"—":fixed(above.r,3)],
    // The exponent decides whether growth still pays: <0.95 is saturation. It is
    // shown next to r because r alone cannot distinguish the two directions.
    ["scaling exponent",expLabel],
    ["exponent on bucket means",scalingB.exponent==null?"—":fixed(scalingB.exponent,3)],
    ["bucket r · unweighted vs weighted",`${(c.regression_bucketed||{}).r==null?"—":fixed((c.regression_bucketed||{}).r,3)} vs ${wfit.r==null?"—":fixed(wfit.r,3)}`],
    ["median output above 8k",c.median_above_threshold==null?"—":`${num(Math.round(c.median_above_threshold))}/min`],
  ].map(([k,v])=>`<div class="metric"><label>${esc(k)}</label><strong style="font-size:15px">${esc(v)}</strong></div>`).join("");
  $("ev-ceiling-method").textContent=`Each point is a regime-filtered herd-size bucket from the full immutable ledger. Growth is judged by the scaling exponent (below 0.95 means each extra animal returns less than the last); straight-line r is reported but cannot tell saturation from super-linear growth. ${(c.confound&&c.confound.note)||""} The metric is ${(c.confound&&c.confound.metric_measures)||"collection throughput"}. Cohort ${c.cohort&&c.cohort.sha256?c.cohort.sha256.slice(0,12):"—"}.`;

  const registry=ev.claims||{}, persisted=ev.persisted_claims||{}, claimRows=registry.claims||[], questionBook=ev.questions||{}, priorityRank={critical:0,high:1,medium:2,low:3}, questionRows=(questionBook.questions||[]).filter(q=>q.status==="open"||q.status==="probing").sort((a,b)=>(priorityRank[a.priority]==null?9:priorityRank[a.priority])-(priorityRank[b.priority]==null?9:priorityRank[b.priority])||Number(b.occurrences||0)-Number(a.occurrences||0));
  const runtime=(ev.policy&&ev.policy.runtime)||{}, semantic=(ev.research&&ev.research.semantic_audit)||{};
  const accepted=claimRows.filter(x=>x.status==="accepted").length, superseded=claimRows.filter(x=>x.status==="superseded").length, overdue=claimRows.filter(x=>x.refresh&&x.refresh.state==="overdue").length;
  $("ev-knowledge-stats").innerHTML=[
    ["persisted registry",`v${num(persisted.registry_version)} · run ${num(persisted.generated_run)}`],
    ["live recomputation",`v${num(registry.registry_version)} preview · run ${num(registry.generated_run)}`],
    ["accepted claims",num(accepted)],
    ["superseded claims",num(superseded)],
    ["overdue rechecks",num(overdue)],
    ["open questions",num(questionRows.length)],
    ["semantic contract",semantic.ok?((semantic.warnings||[]).length?`passing with ${(semantic.warnings||[]).length} warning(s)`:"passing"):"FAILED"],
  ].map(([k,v])=>`<div class="metric"><label>${esc(k)}</label><strong style="font-size:15px">${esc(v)}</strong></div>`).join("");
  $("ev-claims").innerHTML=claimRows.map(x=>{const refresh=(x.refresh||{}).state, filter=x.status==="superseded"?"superseded":(refresh==="overdue"?"recheck":x.status);return `<div class="verdictbox claim-card ${x.status==="accepted"&&refresh!=="overdue"?"good":x.status==="superseded"?"":"bad"}" data-claim-status="${esc(filter)}"><b>${esc(x.id)} · ${esc(x.status)}</b><div class="method">${esc(x.statement)}</div><div class="method">confidence ${esc((x.confidence||{}).level)} · freshness ${esc(refresh)}</div></div>`;}).join("")||`<div class="empty">No claims registered</div>`;
  $("ev-questions").innerHTML=questionRows.map(q=>`<div class="verdictbox question-card ${q.priority==="critical"?"bad":""}"><b>${esc(q.id)} · ${esc(q.class)}</b><div class="method">${esc(q.hypothesis)}</div><div class="method">${esc(q.priority||"normal")} priority · seen ${esc(q.occurrences)}x · last run ${esc(q.last_seen_run)} · next: ${esc(q.status||"open")}</div></div>`).join("")||`<div class="verdictbox good"><b>No open strategy questions</b><div class="method">Every registered uncertainty is answered or abandoned.</div></div>`;
  $("ev-policy").textContent=`Runtime policy ${runtime.policy_id||"unversioned"} · ${runtime.compatible?"compatible with promoted claims and rules":"not promoted/compatible: "+(runtime.errors||[]).join("; ")}. Counterfactual replay made ${num((ev.research&&ev.research.counterfactual||{}).mcp_calls)} MCP calls.`;

  const sp=ev.species||{}, species=sp.table||[], max=Math.max(1,...species.map(x=>Number(x.collected)||0)), probe=sp.probe||{};
  $("ev-species").innerHTML=species.map(x=>`<div class="bar"><span>${KIND_ICON[x.kind]||"🐾"} ${esc(x.kind)}</span><span class="bartrack"><i class="${x.collected?"":"zero"}" style="width:${x.collected?Math.max(1,x.collected/max*100).toFixed(2)+"%":"2px"}"></i></span><span>${fixed(Number(x.share||0)*100,3)}% · ${x.recent_units_per_animal_min==null?"—":fixed(x.recent_units_per_animal_min,4)+"/animal/min"} · ${x.recent_vs_chicken==null?"—":fixed(x.recent_vs_chicken,2)+"× chicken"}</span></div>`).join("")||`<div class="empty">No species measurements</div>`;
  const probeText=probe.status?` Bounded probe: ${num(probe.batch)} beehives, ${num(probe.windows)} windows, median ${fixed(probe.median_ratio,3)}×, minimum ${fixed(probe.minimum_ratio,3)}×; decision ${esc(probe.decision||"pending")}.`:"";
  $("ev-species-note").textContent=`${num(sp.runs_observed)} runs observed; ${num(sp.recent_windows)} recent healthy windows normalized by exposure. ${sp.claim||"Collection mix is separate from lifetime-score scaling."}${probeText}`;

  const crops=ev.crops||{}, collection=ev.collection||{};
  $("ev-dead-ends").innerHTML=`<div class="verdictbox"><b>🌾 Crops: scoped negative result</b>${esc(crops.claim||"No crop result")}</div><div class="verdictbox bad" style="margin-top:9px"><b>🧺 Collected units as the score: retired</b>${esc(collection.claim||"No collection result")}</div><div class="verdictbox good" style="margin-top:9px"><b>✅ Authoritative replacement</b>${esc(collection.consequence||"Measure lifetime score and keep the herd fed.")}</div>`;

  const det=ev.detectors||[];
  $("ev-detectors").innerHTML=det.map(d=>`<div class="verdictbox" style="margin-bottom:9px"><b>${esc(d.name)}</b><div class="method"><span class="bad">Was:</span> ${esc(d.was)}</div><div class="method"><span class="good">Now:</span> ${esc(d.fix)}</div></div>`).join("")||`<div class="empty">No detector history</div>`;
  $("ev-timeline").innerHTML=(ev.timeline||[]).map(x=>`<div class="tl ${esc(x.kind)}"><span class="tl-run">Run ${esc(x.run)} · ${esc(x.kind)} · ${esc(x.status||"recorded")}</span><b>${esc(x.title)}</b><span>${esc(x.text)}</span></div>`).join("")||`<div class="empty">No findings yet</div>`;

  const slider=$("ev-runs-slider");
  slider.oninput=()=>paintCostCounterfactual(ev.cost||{},Number(slider.value));
  paintCostCounterfactual(ev.cost||{},Number(slider.value));
  operatorFindings(ev,OP_AUTONOMY);
}
function evidenceChart(buckets, divider) {
  if (!buckets.length) return `<div class="empty">No comparable runs yet</div>`;
  const w=900,h=240,p={l:56,r:16,t:16,b:30}, maxX=Math.max(...buckets.map(b=>Number(b.herd)||0)), maxY=Math.max(1,...buckets.map(b=>Number(b.units_per_min)||0));
  const x=v=>p.l+Number(v)/maxX*(w-p.l-p.r), y=v=>p.t+(maxY-Number(v))/maxY*(h-p.t-p.b);
  const grid=[0,.25,.5,.75,1].map(f=>`<line class="grid-line" x1="${p.l}" y1="${y(maxY*f)}" x2="${w-p.r}" y2="${y(maxY*f)}"></line><text class="chart-label" x="0" y="${y(maxY*f)+4}">${short(maxY*f)}/m</text>`).join("");
  const line=buckets.map(b=>`${x(b.herd)},${y(b.units_per_min)}`).join(" ");
  const pts=buckets.map(b=>`<circle class="pt ${b.herd>=divider?"plateau":""}" cx="${x(b.herd)}" cy="${y(b.units_per_min)}" r="${Math.max(4,Math.min(9,3+Math.sqrt(b.samples||1)))}"><title>${esc(b.label)} animals · ${num(b.units_per_min)}/min · ${num(b.samples)} runs</title></circle>`).join("");
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Herd size versus production rate">${grid}<polyline class="line" points="${line}"></polyline>${pts}<line class="divider" x1="${x(divider)}" y1="${p.t}" x2="${x(divider)}" y2="${h-p.b}"></line><text class="chart-label" x="${x(divider)+5}" y="${p.t+9}">8k: test marginal herd here</text><text class="chart-label" x="${p.l}" y="${h-5}">0 animals</text><text class="chart-label" x="${w-p.r}" y="${h-5}" text-anchor="end">${short(maxX)} animals</text></svg>`;
}
function paintCostCounterfactual(cost, runsPerDay) {
  const old=cost.llm_era||{}, inAvg=(Number(old.input_tokens_low||0)+Number(old.input_tokens_high||0))/2;
  const perRun=inAvg/1e6*Number(cost.price_per_mtok_in||0)+Number(old.thinking_tokens||0)/1e6*Number(cost.price_per_mtok_out||0);
  const monthly=perRun*runsPerDay*30, exceptionCost=Number((cost.now||{}).estimated_exception_cost||0);
  $("ev-runs-label").textContent=num(runsPerDay);
  $("ev-old-cost").textContent=`$${monthly.toLocaleString(undefined,{maximumFractionDigits:0})}`;
  $("ev-new-cost").textContent=`$${exceptionCost.toFixed(2)} all time`;
  $("ev-cost-note").textContent=`At ${num(runsPerDay)} runs/day, the measured old-loop midpoint (${num(Math.round(inAvg))} input/tool tokens plus ${num(old.thinking_tokens)} thinking/output tokens per run) implies about $${perRun.toFixed(2)} per cycle. Routine cycles are explicit zero-token rows; ${money(exceptionCost,2)} is the estimated exception cost to date.`;
}
/*OPERATOR_JS_START*/
__OPERATOR_JS__
/*OPERATOR_JS_END*/
async function loadEvidence(force=false) {
  if ((!force && EVIDENCE) || EVIDENCE_LOADING) return;
  EVIDENCE_LOADING=true;
  try {
    const response=await fetch(`/api/evidence?t=${Date.now()}`,{cache:"no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    EVIDENCE=await response.json();
    EVIDENCE_LAST_FETCH_MS=Date.now();
    safe("findings",()=>renderEvidence(EVIDENCE));
  } catch(error) {
    safe("findings",()=>renderEvidence({error:error&&error.message?error.message:String(error)}));
  } finally { EVIDENCE_LOADING=false; }
}
async function load() {
  try {
    const response = await fetch(`/api/state?t=${Date.now()}`, {cache:"no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    const now=Date.now();
    if (!OP_AUTONOMY_LAST_FETCH_MS || now-OP_AUTONOMY_LAST_FETCH_MS>=OP_AUTONOMY_REFRESH_MS) loadOperatorAutonomy(!!OP_AUTONOMY);
    if (!EVIDENCE) loadEvidence();
    else if ((ACTIVE_TAB==="findings"||ACTIVE_TAB==="history") &&
             (!EVIDENCE_LAST_FETCH_MS || now-EVIDENCE_LAST_FETCH_MS>=EVIDENCE_REFRESH_MS)) loadEvidence(true);
    if (ACTIVE_TAB==="architecture" && typeof loadArchitecture==="function" &&
        (typeof ARCH_LAST_FETCH_MS==="undefined" || !ARCH_LAST_FETCH_MS ||
         now-ARCH_LAST_FETCH_MS>=ARCHITECTURE_REFRESH_MS)) loadArchitecture(true);
  } catch (error) {
    // Keep polling: a monitor restart or a half-written read must not end the
    // refresh loop. The heartbeat shows the page is trying, not dead.
    FETCH_ERROR = error && error.message ? error.message : String(error);
    $("health").innerHTML = `<span class="pill offline">disconnected</span>`;
    renderHeartbeat();
  }
}
function activateTab(name, writeHash) {
  const allowed=["overview","pipeline","cost","history","findings","game","wire","architecture"];
  if (!allowed.includes(name)) name="overview";
  ACTIVE_TAB=name;
  document.querySelectorAll("nav.tabs button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.tab===name)));
  document.querySelectorAll(".tab").forEach(panel=>{panel.hidden=panel.id!==`tab-${name}`;});
  if (writeHash && typeof location!=="undefined" && location.hash!==`#${name}`) location.hash=name;
  if (name==="findings"||name==="history") { if (EVIDENCE) safe(name,()=>renderEvidence(EVIDENCE)); loadEvidence(true); }
  // Coop Rush watches its own panel's hidden flag: an idle game must keep
  // simulating in the background, it just stops painting.
  if (window.CRUI && name==="game") window.CRUI.paint();
  // Repaint on entry so the active span's local clock is current immediately.
  if (window.TracePanel && name==="pipeline") window.TracePanel.paint();
  // The switchboard's packets are CSS animations, and a hidden panel gets no
  // layout: rebuild the replay on entry so it starts from the top of the loop.
  if (window.MCPWirePanel && name==="wire") window.MCPWirePanel.rebuild();
  // Refetched on every entry rather than cached like the topology graph. The
  // architecture is cheap to re-render but its liveness overlay goes stale the
  // moment an agent dies, and a diagram that shows a dead agent as healthy is worse
  // than one that admits it does not know.
  if (name==="architecture") {
    const host=document.getElementById("tab-architecture");
    const renderer=window.renderArchitecture, loader=window.loadArchitecture;
    if (typeof renderer!=="function" || typeof loader!=="function") {
      if (host) host.innerHTML=`<div class="arch-shell"><section class="page-hero arch-hero"><div><span class="page-kicker">Architecture telemetry unavailable</span><h2>Architecture control plane</h2><p>The specialized renderer did not initialize; the rest of the read-only dashboard remains available.</p></div><div class="hero-verdict attention"><b>Renderer unavailable</b><span>Expected the source-map bundle to initialize</span></div></section></div>`;
    } else {
      if (window.ARCH) safe(name,()=>renderer(window.ARCH,window.AUTONOMY));
      Promise.resolve(loader(true)).catch(error=>safe(name,()=>renderer({error:error && error.message ? error.message : String(error)},null)));
    }
  }
}
document.querySelectorAll("nav.tabs button").forEach(button=>button.addEventListener("click",()=>activateTab(button.dataset.tab,true)));
if (typeof document.addEventListener==="function") document.addEventListener("click",event=>{
  const metric=event.target && event.target.closest ? event.target.closest("#chart-metrics [data-metric]") : null;
  if (metric) { CHART_METRIC=metric.dataset.metric; if (LAST) safe("chart",()=>renderChart(LAST.trend||[])); return; }
  const gpMode=event.target && event.target.closest ? event.target.closest("#gp-modes [data-gpmode]") : null;
  if (gpMode) { RACE_MODE=gpMode.dataset.gpmode; if (LAST) safe("leaderboard race",()=>renderLeaderboardHistory(LAST.leaderboard_history||[])); return; }
  const gpRange=event.target && event.target.closest ? event.target.closest("#gp-ranges [data-gprange]") : null;
  if (gpRange) { RACE_RANGE=Number(gpRange.dataset.gprange)||100; if (LAST) safe("leaderboard race",()=>renderLeaderboardHistory(LAST.leaderboard_history||[])); return; }
  const hmetric=event.target && event.target.closest ? event.target.closest("#hist-metrics [data-hmetric]") : null;
  if (hmetric) { COST_HISTORY_METRIC=hmetric.dataset.hmetric; if (EVIDENCE) safe("history chart",()=>renderCostHistoryChart(EVIDENCE.cost_history||{})); return; }
  const hrange=event.target && event.target.closest ? event.target.closest("#hist-range [data-hrange]") : null;
  if (hrange) { COST_HISTORY_RANGE=hrange.dataset.hrange; if (EVIDENCE) safe("history range",()=>renderCostHistoryChart(EVIDENCE.cost_history||{})); return; }
  const row=event.target && event.target.closest ? event.target.closest("tr.runrow") : null;
  if (row) { ACTIVE_RUN=String(ACTIVE_RUN)===String(row.dataset.run)?null:row.dataset.run; if (LAST) renderRuns(LAST.trend||[],(LAST.tokens&&LAST.tokens.per_run)||{}); return; }
  // Trace interactions are delegated by the explorer on its own stable root.
});
if (typeof window.addEventListener==="function") {
  window.addEventListener("hashchange",()=>activateTab((location.hash||"").slice(1),false));
  window.addEventListener("keydown",event=>{
    if (event.metaKey||event.ctrlKey||event.altKey) return;
    if (event.target && /input|textarea|select/i.test(event.target.tagName||"")) return;
    const map={o:"overview",p:"pipeline",c:"cost",t:"history",f:"findings",g:"game",w:"wire",a:"architecture"}, tab=map[String(event.key||"").toLowerCase()];
    if (tab) activateTab(tab,true);
  });
}
if (typeof location!=="undefined" && location.hash) activateTab(location.hash.slice(1),false);

// The trace explorer is evaluated BEFORE the polling bootstrap: mount() must
// exist by the time the first payload lands, and a throw here must not be able to
// stop load() from ever running. Hence the try/catch and guarded mount.
try {
/*TRACE_JS_START*/
__TRACE_JS__
/*TRACE_JS_END*/
  if (window.TracePanel) window.TracePanel.mount({rootId:"trace-explorer"});
} catch (error) {
  if (typeof console !== "undefined" && console.error) console.error("trace panel failed to load", error);
  const stage = document.getElementById("trace-explorer");
  if (stage) stage.innerHTML = `<div class="te-empty">The execution trace failed to load: ${esc(error && error.message ? error.message : error)}<br>Step timings below are unaffected.</div>`;
}

// Same contract as the trace: mounted before the poll, isolated from it.
try {
/*WIRE_JS_START*/
__WIRE_JS__
/*WIRE_JS_END*/
  if (window.MCPWirePanel) window.MCPWirePanel.mount({rootId:"mcp-wire"});
} catch (error) {
  if (typeof console !== "undefined" && console.error) console.error("switchboard failed to load", error);
  const stage = document.getElementById("mcp-wire");
  if (stage) stage.innerHTML = `<div class="mw-empty">The switchboard failed to load: ${esc(error && error.message ? error.message : error)}<br>Every other tab is unaffected.</div>`;
}

// Architecture is intentionally at global script scope. It was once injected inside
// the Switchboard try block above. Browser compatibility rules leaked ordinary function
// declarations (renderArchitecture) out of that block but kept async declarations
// (loadArchitecture/loadAutonomy) block-scoped. The button therefore existed while its
// loader was undefined. The asset has definitions only; rendering is still guarded by
// activateTab and safe().
/*ARCH_JS_START*/
__ARCH_JS__
/*ARCH_JS_END*/

// The dashboard's own refresh is started BEFORE the game bundle is evaluated.
// It used to come last, so any top-level throw in the game (or a failed game
// bundle substitution) stopped load() and setInterval from ever running and
// froze every tab at "connecting". Do not write the game placeholder token in
// a comment here: it is substituted textually, so a mention becomes a copy.
load(); setInterval(load, 2000); setInterval(tick, 1000);

try {
/*GAME_JS_START*/
__GAME_JS__
/*GAME_JS_END*/
} catch (error) {
  // The game is a tab, not the product. It must not be able to break monitoring.
  if (typeof console !== "undefined" && console.error) console.error("Coop Rush failed to load", error);
  const panel = document.getElementById("tab-game");
  if (panel) panel.innerHTML = `<div class="card"><h2>Coop Rush</h2><div class="empty">The game failed to load: ${esc(error && error.message ? error.message : error)}<br>The dashboard is unaffected.</div></div>`;
}
</script></body></html>"""


# Composed once at import: the page is static apart from the /api/state poll, and
# an import-time build keeps HTML a plain module constant for anything that reads it.
HTML = (
    HTML_TEMPLATE.replace("__TRACE_CSS__", TRACE_CSS)
    .replace("__WIRE_CSS__", WIRE_CSS)
    .replace("__ARCH_CSS__", ARCH_CSS)
    .replace("__GAME_CSS__", GAME_CSS)
    .replace("__OPERATOR_CSS__", OPERATOR_CSS)
    .replace("__GAME_MARKUP__", GAME_MARKUP)
    .replace("__TRACE_JS__", TRACE_JS)
    .replace("__WIRE_JS__", WIRE_JS)
    .replace("__ARCH_JS__", ARCH_JS)
    .replace("__OPERATOR_JS__", OPERATOR_JS)
    .replace("__GAME_JS__", GAME_JS)
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            payload = json.dumps(snapshot(), separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        elif path == "/api/topology":
            # The call graph is a function of the source on disk, so it is fetched
            # once and re-fetched only when the fingerprint in /api/state changes.
            # Serving it inside the 2s poll would re-send ~25KB of identical graph
            # 1,800 times an hour.
            try:
                graph: Dict[str, Any] = dict(topology.cached_graph())
                graph["fingerprint"] = (snapshot_trace_fingerprint())
            except Exception as exc:  # noqa: BLE001
                graph = {"error": str(exc)[:200], "nodes": [], "edges": []}
            payload = json.dumps(graph, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        elif path == "/api/evidence":
            # Its own endpoint because none of it changes at the 2s poll rate, and
            # folding it into /api/state would re-send several KB of unchanged
            # findings 1,800 times an hour.
            try:
                body: Dict[str, Any] = evidence.report()
            except Exception as exc:  # noqa: BLE001
                body = {"error": str(exc)[:200]}
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        elif path == "/api/autonomy":
            # Separate from /api/state for the same reason as /api/evidence: ~13KB of
            # self-healing state that changes on agent cadences (60s to 60min), not on
            # the 2s poll. It also shells out to launchctl and git, which must not run
            # 1,800 times an hour.
            try:
                from farm import autonomy

                body = autonomy.report()
                body["blockers"] = autonomy.blockers(body)
            except Exception as exc:  # noqa: BLE001
                body = {"error": str(exc)[:200]}
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        elif path == "/api/architecture":
            # Parses every module and shells out to git several times, so it is fetched
            # on tab activation rather than on the poll. The signature lets the client
            # tell whether the shape moved since it last looked.
            try:
                from farm import architecture

                body = architecture.report()
            except Exception as exc:  # noqa: BLE001
                body = {"error": str(exc)[:200]}
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
        elif path in ("/", "/index.html"):
            payload = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # The page is composed at import time from game/ and dashboard/ assets,
            # so restarting the monitor after editing them changes this document.
            # Without this header a browser happily keeps the previous bundle and
            # the new code appears not to work at all, which is exactly how stale
            # embedded assets look while iterating on the dashboard.
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _serve(port: int, search: int) -> Optional[ThreadingHTTPServer]:
    """Bind the first free port at or after `port`.

    8765 is a popular number: it was already taken by an unrelated local app on
    this machine. Dying with "Address already in use" is a worse answer than
    landing on 8766 and saying so.
    """
    last: Optional[OSError] = None
    for candidate in range(port, port + max(1, search) + 1):
        try:
            return ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
        except OSError as exc:
            last = exc
            continue
    if last:
        print(f"could not bind {port}-{port + search}: {last}", file=sys.stderr)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Farm Friends monitor")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--strict-port", action="store_true", help="fail instead of trying the next free port"
    )
    args = parser.parse_args()
    # The farm modules address state with project-relative paths.
    os.chdir(PROJECT)
    server = _serve(args.port, 0 if args.strict_port else PORT_SEARCH)
    if server is None:
        return 1
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    if port != args.port:
        print(f"port {args.port} was busy; using {port}")
    print(f"Farm monitor: {url}")
    print("Read-only; press Ctrl-C to stop.")
    if not args.no_open:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nFarm monitor stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
