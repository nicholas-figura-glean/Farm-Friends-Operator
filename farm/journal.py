"""Python-generated strategy journal and alert queue.

The journal used to be prose written by an LLM once every twelve runs. Every
figure in it was already in history.ndjson, so it is generated here instead and
costs nothing. The alert queue is the only channel that should ever wake a model.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import rules

JOURNAL = "farm-strategy-journal.md"
ALERTS = os.path.join("state", "alerts.ndjson")


def record_alerts(row: Dict[str, Any], anomalies: List[str]) -> None:
    if not anomalies:
        return
    os.makedirs(os.path.dirname(ALERTS), exist_ok=True)
    with open(ALERTS, "a") as fh:
        for item in anomalies:
            fh.write(
                json.dumps(
                    {
                        "ts": row.get("ts"),
                        "run": row.get("run"),
                        "alert": item,
                        "rank": row.get("rank"),
                        "animals": row.get("animals"),
                        "max_hunger": row.get("max_hunger"),
                    }
                )
                + "\n"
            )


def pending_alerts(
    acked_ts: Optional[str],
    healed_keys: Optional[set] = None,
    now: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Unacknowledged, un-remediated, and still RECENT alerts.

    `healed_keys` is passed in rather than imported so this module stays free of
    any dependency on the healer (which depends on the rules, which depend on
    nothing).

    Staleness matters as much as the other two filters. An alert describes the
    farm at one instant, but a remedy is applied to the farm as it is now, so an
    alert that outlives its conditions makes the healer act on evidence that is
    no longer true. Unbounded, this queue re-threw an alert from **run 28** at a
    farm 350 runs later, and the healer duly cut the call-rate ceiling and adopt
    workers on the strength of it - twice, after both had already been reset,
    because the throttle's justification was immortal.

    Anything older than rules.ALERT_STALE_HOURS is left in the file as history
    but is no longer actionable.
    """
    try:
        with open(ALERTS) as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return []
    if acked_ts:
        rows = [r for r in rows if (r.get("ts") or "") > acked_ts]
    if healed_keys:
        rows = [
            r for r in rows if "%s:%s" % (r.get("run"), r.get("alert")) not in healed_keys
        ]
    cutoff = _stale_cutoff(now)
    if cutoff:
        rows = [r for r in rows if (r.get("ts") or "") >= cutoff]
    return rows


def _stale_cutoff(now: Optional[str] = None) -> Optional[str]:
    """Timestamp before which an alert is history rather than a work item."""
    hours = getattr(rules, "ALERT_STALE_HOURS", 0)
    if not hours:
        return None
    from datetime import datetime, timedelta, timezone

    if now:
        try:
            base = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            base = datetime.now(timezone.utc)
    else:
        base = datetime.now(timezone.utc)
    return (base - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mean(values: List[float]) -> Optional[float]:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _rate_stats(rows: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    rates = [
        r.get("units_per_chicken_min")
        for r in rows
        if r.get("units_collected") and (r.get("interval_min") or 0) >= rules.MIN_INTERVAL_FOR_RATE_CHECK
    ]
    rates = [r for r in rates if r]
    if not rates:
        return None, None, None
    return _mean(rates), min(rates), max(rates)


def build_entry(rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    """Deterministic journal entry for the runs in this window."""
    if not rows:
        return ""
    first, last = rows[0], rows[-1]
    produce_gain = (last.get("produce") or 0) - (first.get("produce") or 0)
    animal_gain = (last.get("animals") or 0) - (first.get("animals") or 0)
    revenue = sum(r.get("revenue") or 0 for r in rows)
    feed_spend = sum(r.get("feed_bought") or 0 for r in rows)
    adopted = sum(r.get("adopted") or 0 for r in rows)
    requested = sum(r.get("adopt_requested") or 0 for r in rows)
    budget_stops = sum(1 for r in rows if r.get("adopt_stopped") == "budget")
    mean_rate, min_rate, max_rate = _rate_stats(rows)
    max_hunger = max([r.get("max_hunger") or 0 for r in rows])
    durations = [r.get("duration_s") or 0 for r in rows]
    calls = sum(r.get("calls") or 0 for r in rows)

    rivals_now = last.get("rivals") or {}
    rivals_then = first.get("rivals") or {}
    rival_lines = []
    threat = None
    for name, produce in sorted(rivals_now.items(), key=lambda kv: -kv[1]):
        gained = produce - rivals_then.get(name, produce)
        share = (100.0 * gained / produce_gain) if produce_gain > 0 else 0.0
        rival_lines.append(
            "  - %s: %d lifetime (+%d this window, %.1f%% of our gain)"
            % (name, produce, gained, share)
        )
        if share >= rules.THREAT_SHARE * 100 and threat is None:
            threat = name

    alerts = [a for r in rows for a in (r.get("anomalies") or [])]
    observations = []
    if budget_stops:
        observations.append(
            "Adoption hit the %ds wall-clock budget in %d/%d runs (%d of %d planned "
            "chickens bought); coins roll forward, so this throttles compounding. "
            "Levers are MAX_CALLS_PER_SECOND and ADOPT_WORKERS."
            % (rules.CYCLE_BUDGET_SECONDS, budget_stops, len(rows), adopted, requested)
        )
    idle = [r for r in rows if (r.get("coins") or 0) >= 10 and r.get("adopt_stopped") == "complete"]
    if idle:
        observations.append(
            "%d run(s) ended with >=10 idle coins after a complete adoption plan, "
            "which means the feed reserve is binding rather than coins." % len(idle)
        )
    if mean_rate is not None and not (
        rules.UNITS_PER_CHICKEN_MIN_BAND[0] <= mean_rate <= rules.UNITS_PER_CHICKEN_MIN_BAND[1]
    ):
        observations.append(
            "Mean throughput %.3f units/chicken/min sits outside the %.2f-%.2f band; "
            "the band or the husbandry assumption needs revisiting."
            % (
                mean_rate,
                rules.UNITS_PER_CHICKEN_MIN_BAND[0],
                rules.UNITS_PER_CHICKEN_MIN_BAND[1],
            )
        )
    if max_hunger >= rules.FEED_AT_HUNGER:
        observations.append(
            "Peak hunger %d reached the %d feeding threshold, so feeding is now "
            "actually firing rather than sitting idle." % (max_hunger, rules.FEED_AT_HUNGER)
        )
    if not observations:
        observations.append("Nothing anomalous; rules unchanged.")

    lines = [
        "",
        "## %s - runs %s-%s (generated)" % (last.get("ts"), first.get("run"), last.get("run")),
        "",
        "- Rank: #%s, lifetime produce %s (+%d this window)"
        % (last.get("rank"), last.get("produce"), produce_gain),
        "- Animals: %s (%s), +%d this window"
        % (
            last.get("animals"),
            ", ".join(
                "%s %d" % (k, v) for k, v in sorted((last.get("by_kind") or {}).items())
            ),
            animal_gain,
        ),
        "- Output: %s units/chicken/min mean (min %s, max %s) over %d measurable runs"
        % (
            "%.3f" % mean_rate if mean_rate else "n/a",
            "%.3f" % min_rate if min_rate else "n/a",
            "%.3f" % max_rate if max_rate else "n/a",
            len([r for r in rows if r.get("units_collected")]),
        ),
        "- Economy: %d coins revenue, %d spent on feed (%.1f%%), %d chickens adopted "
        "of %d planned"
        % (
            revenue,
            feed_spend,
            (100.0 * feed_spend / revenue) if revenue else 0.0,
            adopted,
            requested,
        ),
        "- Husbandry: peak hunger %d against threshold %d (stop %d); feed %s vs reserve %s"
        % (
            max_hunger,
            rules.FEED_AT_HUNGER,
            rules.HUNGER_STOP,
            last.get("feed"),
            last.get("reserve_target"),
        ),
        "- Throughput: %d calls, %.0fs mean / %.0fs max per run, rate limit %s/s"
        % (
            calls,
            _mean(durations) or 0.0,
            max(durations) if durations else 0.0,
            meta.get("call_rate"),
        ),
        "- Rivals:",
    ]
    lines.extend(rival_lines)
    lines.append(
        "- Threat check: %s"
        % (
            "%s exceeded %d%% of our gain" % (threat, int(rules.THREAT_SHARE * 100))
            if threat
            else "no rival above %d%% of our gain" % int(rules.THREAT_SHARE * 100)
        )
    )
    lines.append("- Alerts this window: %s" % ("; ".join(alerts[:5]) if alerts else "none"))
    for note in observations:
        lines.append("- %s" % note)
    lines.append(
        "- Active rules: engine %s, feed at %d, reserve %d/animal, budget %ds, "
        "call rate %.1f/s, %d adopt workers, verify every %d runs, food crops %s"
        % (
            rules.PRIMARY_KIND,
            rules.FEED_AT_HUNGER,
            rules.FEED_PER_ANIMAL_RESERVE,
            rules.CYCLE_BUDGET_SECONDS,
            rules.MAX_CALLS_PER_SECOND,
            rules.ADOPT_WORKERS,
            rules.VERIFY_EVERY,
            "banned" if rules.FOOD_CROPS_BANNED else "allowed",
        )
    )
    return "\n".join(lines) + "\n"


def append_entry(rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> bool:
    entry = build_entry(rows, meta)
    if not entry.strip():
        return False
    with open(JOURNAL, "a") as fh:
        fh.write(entry)
    return True
