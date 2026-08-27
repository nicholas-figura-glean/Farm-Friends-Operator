#!/usr/bin/env python3
"""Post the deterministic 5 PM Mountain Time Farm Friends sundown report."""

from __future__ import annotations

import fcntl
import json
import os
import sys
from collections import Counter
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import analysis, ledger, notify, rules  # noqa: E402

STATE = PROJECT / "state"
STORE = STATE / "eod_report.json"
EVENTS = STATE / "notification_events.ndjson"
LOCK = STATE / ".eod-report.lock"
MOUNTAIN = ZoneInfo("America/Denver")
CHANNEL_NAME = "#farm-friends"
CHANNEL_ID = "C0BRMBGN7QA"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_ndjson(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: List[Dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def day_bounds(now: Optional[datetime] = None) -> Tuple[str, datetime, datetime]:
    local = (now or datetime.now(timezone.utc)).astimezone(MOUNTAIN)
    start_local = datetime.combine(local.date(), time.min, tzinfo=MOUNTAIN)
    return local.date().isoformat(), start_local.astimezone(timezone.utc), local.astimezone(timezone.utc)


def in_window(row: Dict[str, Any], start: datetime, end: datetime) -> bool:
    stamp = parse_ts(row.get("ts"))
    return stamp is not None and start <= stamp <= end


def history_window(rows: List[Dict[str, Any]], start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Include the last pre-midnight snapshot as the day's score baseline."""
    before = [row for row in rows if parse_ts(row.get("ts")) and parse_ts(row.get("ts")) < start]
    today = [row for row in rows if in_window(row, start, end)]
    if before and today:
        return [before[-1]] + today
    return today


def _delta(after: Dict[str, Any], before: Dict[str, Any], key: str) -> int:
    return int(after.get(key) or 0) - int(before.get(key) or 0)


def _signed(value: int) -> str:
    return ("+" if int(value) >= 0 else "-") + format(abs(int(value)), ",")


def healing_summary(rows: Iterable[Dict[str, Any]]) -> List[str]:
    entries = list(rows)
    if not entries:
        return ["The machinery had a quiet chore list—no remedies or safeguard changes were needed. 🌤️"]
    routed_rows = [row for row in entries if "routed to autonomous" in str(row.get("action") or "")]
    routed = len(routed_rows)
    relaxed = sum(1 for row in entries if str(row.get("class") or "") == "relax")
    remedy_classes = Counter(
        str(row.get("class") or "other")
        for row in entries
        if "routed to autonomous" not in str(row.get("action") or "")
        and str(row.get("class") or "") not in {"relax", "operator"}
    )
    remedies = sum(remedy_classes.values())
    notes: List[str] = []
    if remedies > 0:
        top = ", ".join("%s ×%d" % (name.replace("_", " "), count) for name, count in remedy_classes.most_common(3))
        notes.append("The rig made %d bounded field adjustment(s): %s. 🛠️" % (remedies, top))
    if relaxed:
        notes.append("After the weather cleared, it eased/restored safeguards %d time(s) instead of leaving the farm throttled. 🌱" % relaxed)
    if routed:
        notes.append("It sent %d odd condition(s) to the autonomous research/repair queue rather than guessing in the pasture. 🧭" % routed)
    if not remedies and not relaxed:
        notes.append("Automatic safeguards held steady; no throttles or speculative remedies were applied. 👀")
    return notes or ["The machinery watched the rows and held steady; no unsafe automatic move was taken. 👀"]


def build_report(
    rows: List[Dict[str, Any]],
    heal_rows: List[Dict[str, Any]],
    notification_rows: List[Dict[str, Any]],
    day: str,
) -> str:
    if not rows:
        return (
            "🌾 *Farm Friends Sundown Report — %s (MT)*\n\n"
            "The field book has no completed runs for today, so I’m not going to make up a harvest. The scheduler and outage guard own the next check. 👀"
            % day
        )

    first, last = rows[0], rows[-1]
    completed = rows[1:] if len(rows) > 1 and parse_ts(first.get("ts")) and parse_ts(first.get("ts")) < parse_ts(rows[1].get("ts")) else rows
    produce_gain = _delta(last, first, "produce")
    animal_gain = _delta(last, first, "animals")
    revenue = sum(int(row.get("revenue") or 0) for row in completed)
    adopted = sum(int(row.get("adopted") or 0) for row in completed)
    peak_hunger = max(int(row.get("max_hunger") or 0) for row in completed)
    calls = sum(int(row.get("calls") or 0) for row in completed)
    runway = rules.feed_buffer_minutes(int(last.get("feed") or 0), int(last.get("animals") or 0))

    rivals_before = first.get("rivals") or {}
    rivals_after = last.get("rivals") or {}
    rival_rows = []
    for name, score in rivals_after.items():
        rival_rows.append((str(name), int(score or 0), int(score or 0) - int(rivals_before.get(name, score) or 0)))
    rival_rows.sort(key=lambda item: (-item[1], item[0].lower()))
    front = rival_rows[:5]
    rival_lines = ["• *%s:* %s total (%s today)" % (name, format(score, ","), _signed(gain)) for name, score, gain in front]
    if len(rival_rows) > len(front):
        field = rival_rows[len(front):]
        best = max(field, key=lambda item: item[2])
        rival_lines.append(
            "• *Rest of the field:* %d neighbors at %s or less; best daily move was %s (%s)."
            % (len(field), format(max(item[1] for item in field), ","), best[0], _signed(best[2]))
        )

    healed = healing_summary(heal_rows)
    outages = [row for row in notification_rows if row.get("event") == "outage"]
    recoveries = [row for row in notification_rows if row.get("event") == "recovered"]
    if outages or recoveries:
        healed.append("Outage fence: %d confirmed, %d recovered during the day. 🚧" % (len(outages), len(recoveries)))

    rank_move = "held rank #%s" % last.get("rank") if first.get("rank") == last.get("rank") else "moved from rank #%s to #%s" % (first.get("rank"), last.get("rank"))
    lines = [
        "🌾 *Farm Friends Sundown Report — %s (MT)*" % day,
        "",
        "🐓 *Our patch*",
        "We %s and stacked *%s* more lifetime produce. We welcomed %s new birds; after ordinary farm losses, the flock netted *%s* head to *%s*. Produce sales brought in %s coins." % (
            rank_move, format(produce_gain, ","), format(adopted, ","), _signed(animal_gain),
            format(int(last.get("animals") or 0), ","), format(revenue, ","),
        ),
        "The crew finished %d run(s), made %s bounded server calls, peaked at hunger %d/%d, and tucked away about %.0f minutes of feed runway. 🌽" % (
            len(completed), format(calls, ","), peak_hunger, rules.HUNGER_STOP, runway,
        ),
        "",
        "🏁 *How the neighboring farms worked their rows*",
    ]
    lines.extend(rival_lines or ["• No rival snapshots were available today."])
    lines.extend(["", "🛠️ *How the machinery tended itself*"])
    lines.extend("• " + note for note in healed)
    lines.extend([
        "",
        "🌅 *Sundown read:* We’re closing at rank #%s with %s lifetime produce. Barn doors are latched, the supervisor is still making rounds, and tomorrow’s seed is already in the drill. 🚜✨" % (
            last.get("rank"), format(int(last.get("produce") or 0), ","),
        ),
    ])
    return "\n".join(lines)


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("EOD REPORT skipped: previous pass still active")
        return 0

    day, start, end = day_bounds()
    stored = read_json(STORE)
    if stored.get("last_sent_day") == day:
        print("EOD REPORT inert: %s already sent" % day)
        return 0

    rows = history_window(analysis.history_rows(), start, end)
    heal_rows = [row for row in read_ndjson(STATE / "heal.ndjson") if in_window(row, start, end)]
    notification_rows = [row for row in read_ndjson(EVENTS) if in_window(row, start, end)]
    message = build_report(rows, heal_rows, notification_rows, day)
    try:
        result = notify.send(message)
    except (notify.NotificationConfigError, notify.NotificationDeliveryError) as exc:
        stored.update({"schema_version": 1, "last_attempt_day": day, "last_attempt_ts": utcnow(), "delivery_error": str(exc)})
        write_json(STORE, stored)
        print("EOD REPORT delivery failed: %s" % exc)
        return 0

    stored.update({
        "schema_version": 1,
        "last_sent_day": day,
        "last_sent_ts": utcnow(),
        "last_run": rows[-1].get("run") if rows else None,
        "channel": {"name": CHANNEL_NAME, "id": CHANNEL_ID},
        "delivery": result,
        "delivery_error": None,
    })
    write_json(STORE, stored)
    with EVENTS.open("a", encoding="utf-8") as event_log:
        event_log.write(json.dumps({"ts": utcnow(), "event": "eod_sent", "detail": {"day": day, "run": stored.get("last_run")}}, sort_keys=True) + "\n")
    ledger.record("notification.eod_sent", {"channel": CHANNEL_NAME, "day": day}, actor="eod_report", run=stored.get("last_run"))
    print("EOD REPORT sent day=%s run=%s bytes=%s" % (day, stored.get("last_run"), result.get("bytes")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
