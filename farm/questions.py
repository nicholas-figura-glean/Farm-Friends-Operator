"""Persistent strategy-question registry.

`questions.ndjson` is a current registry: one line per stable question identity,
atomically rewritten under a file lock. `question_events.ndjson` is the append-only
audit trail. Repeated alerts bump the existing question instead of creating the
seven indistinguishable pages that motivated this layer.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import rules

SCHEMA_VERSION = 1
VALID_STATUSES = {"open", "probing", "answered", "abandoned"}

TEMPLATES: Dict[str, Dict[str, Any]] = {
    "activity_novelty_trade": {
        "hypothesis": "A new trade pattern may transfer more compounding value to a counterparty than its nominal liquidation margin returns to us.",
        "settle": "Replay resource flows by trade ID and counterparty, compare each side with neutral store/market alternatives, and correlate transfers with subsequent rival coin, herd, and produce changes.",
        "priority": "high",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 1, "wall_seconds": 60},
    },
    "activity_novelty_rival": {
        "hypothesis": "A rival entered a materially different capital, adoption, or production regime that changes the best response.",
        "settle": "Measure the rival's herd, coins, produce slope, recent transfers, and sustainable growth; distinguish organic production from activity we funded.",
        "priority": "high",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 1, "wall_seconds": 60},
    },
    "activity_novelty_risk": {
        "hypothesis": "A newly observed loss mechanic changes the safe reserve or growth policy.",
        "settle": "Bound its frequency, maximum loss, affected resource, and whether current reserves absorb it before restoring affected strategic actions.",
        "priority": "high",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 60},
    },
    "activity_novelty_tools": {
        "hypothesis": "A capability-surface change invalidates assumptions about available reads, mutations, or parser contracts.",
        "settle": "Diff the capability names and schemas, exercise changed read-only paths, and prove existing mutation arguments remain compatible.",
        "priority": "critical",
        "page_on_open": True,
        "budget": {"coins": 0, "calls": 10, "wall_seconds": 120},
    },
    "strategy_stale": {
        "hypothesis": "A standing growth decision is suppressing a profitable strategy.",
        "settle": "Replay output and capital outcomes under neighbouring growth thresholds, then run a bounded growth cohort if the replay disagrees.",
        "priority": "high",
        "page_on_open": False,
        "budget": {"coins": 5_000, "calls": 500, "wall_seconds": 300},
    },
    "idle_capital": {
        "hypothesis": "Capital is accumulating because a policy gate, not affordability, is blocking the scoring action.",
        "settle": "Compare the live adoption cap, affordable adoptions, score slope, and feed runway; identify the binding constraint.",
        "priority": "high",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 20},
    },
    "knob_age": {
        "hypothesis": "A decision constant has outlived the evidence regime that justified it.",
        "settle": "Recompute its estimator on a fresh labelled cohort and compare counterfactual neighbouring values.",
        "priority": "medium",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 30},
    },
    "rival_wake": {
        "hypothesis": "A dormant rival entered a new production or adoption regime.",
        "settle": "Measure the rival's herd, coins, produce rate, composition if observable, and whether herd growth or feeding explains the wake-up.",
        "priority": "medium",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 5, "wall_seconds": 60},
    },
    "rank_lost": {
        "hypothesis": "The current policy no longer maximizes the objective quickly enough to hold first place.",
        "settle": "Build a win-path bundle from score lead, both growth rates, feed runway, idle capital, and counterfactual policy settings.",
        "priority": "critical",
        "page_on_open": True,
        "budget": {"coins": 0, "calls": 5, "wall_seconds": 60},
    },
    "no_path_to_win": {
        "hypothesis": "Under current growth and score rates, waiting cannot overtake the leader.",
        "settle": "Find the minimum safe herd/adoption policy that restores a positive crossover root, or prove the objective infeasible.",
        "priority": "critical",
        "page_on_open": True,
        "budget": {"coins": 0, "calls": 5, "wall_seconds": 120},
    },
    "win_eta": {
        "hypothesis": "The current policy reaches first place too slowly for the accepted objective horizon.",
        "settle": "Recompute the forecast under live and neighbouring adoption/feed policies with uncertainty bounds.",
        "priority": "high",
        "page_on_open": True,
        "budget": {"coins": 0, "calls": 5, "wall_seconds": 120},
    },
    "threat": {
        "hypothesis": "A rival's sustained score gain is now strategically material.",
        "settle": "Separate herd growth from production recovery and compare their sustainable rate with ours.",
        "priority": "high",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 5, "wall_seconds": 60},
    },
    "overtaken": {
        "hypothesis": "A rival crossed our lifetime-produce score.",
        "settle": "Establish whether the crossover is transient and whether the promoted policy still has a safe path back.",
        "priority": "critical",
        "page_on_open": True,
        "budget": {"coins": 0, "calls": 5, "wall_seconds": 90},
    },
    "rival_growing": {
        "hypothesis": "A material rival resumed herd growth, invalidating the frozen-rival forecast.",
        "settle": "Measure its new animals/min and recompute the win projection with that growth regime.",
        "priority": "high",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 5, "wall_seconds": 60},
    },
    "tools_changed": {
        "hypothesis": "The server capability surface changed and may invalidate parser or policy assumptions.",
        "settle": "Diff tool names and schemas, then exercise changed read-only capabilities before permitting mutation.",
        "priority": "critical",
        "page_on_open": True,
        "budget": {"coins": 0, "calls": 10, "wall_seconds": 120},
    },
    "hunger_wall": {
        "hypothesis": "Herd size is approaching or exceeding the capacity of the current feed transport path.",
        "settle": "Fit max hunger and feed completion against herd size on the latest regime and establish a safety-bounded ceiling.",
        "priority": "critical",
        "page_on_open": True,
        "budget": {"coins": 0, "calls": 5, "wall_seconds": 120},
    },
    "model_drift": {
        "hypothesis": "A promoted game-mechanics claim no longer predicts recent observations.",
        "settle": "Segment the new regime, re-estimate the claim, and falsify either the old or candidate model on a pinned cohort.",
        "priority": "high",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 60},
    },
    "policy_drift": {
        "hypothesis": "Runtime rules and the promoted policy snapshot no longer describe the same behavior.",
        "settle": "Recompile the policy, inspect the parameter and claim diff, run semantic gates, and explicitly promote or revert.",
        "priority": "critical",
        "page_on_open": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 120},
    },
}

_PRIORITY = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else Path(__file__).resolve().parent.parent / "state"


def _paths() -> Tuple[Path, Path, Path]:
    current = Path(os.environ.get("FARM_QUESTIONS_FILE", str(_state_dir() / "questions.ndjson")))
    events = Path(os.environ.get("FARM_QUESTION_EVENTS_FILE", str(_state_dir() / "question_events.ndjson")))
    lock = Path(str(current) + ".lock")
    return current, events, lock


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    rows: List[Dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("id"):
            rows.append(value)
    return rows


def load_all() -> List[Dict[str, Any]]:
    current, _, _ = _paths()
    return sorted(_read(current), key=lambda item: (item.get("opened_run") or 0, item.get("id")))


def _write_current(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("%s.tmp.%d" % (path, os.getpid()))
    body = "".join(json.dumps(row, sort_keys=True, allow_nan=False, default=str) + "\n" for row in rows)
    tmp.write_text(body, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _append_event(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, allow_nan=False, default=str) + "\n")


def _normalize_subject(alert_class: str, value: str) -> str:
    target = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if alert_class == "knob_age":
        # Alert prose carries a changing age ("unchanged for 41 runs", then 42),
        # but the uncertainty is the knob or claim before that phrase.
        target = re.split(
            r"\s+(?:unchanged\s+for|evidence\s+is\s+overdue|evidence\s+overdue|last\s+validated)\b",
            target,
            maxsplit=1,
        )[0]
        # A knob's identity is its name, not whichever value happened to be in the
        # alert. Legacy alerts alternated between ``adopt_cap`` and ``adopt_cap=30``.
        target = re.sub(r"\s*=\s*[-+]?\d+(?:\.\d+)?(?:/s)?$", "", target)
    elif alert_class == "model_drift":
        target = re.split(
            r"\s+(?:recent|changed|drifted|no\s+longer\s+predicts)\b",
            target,
            maxsplit=1,
        )[0]
    return target.strip(" :;,.") or "farm"


def _subject(alert_class: str, alert: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return _normalize_subject(alert_class, explicit)
    patterns = [
        r"^RIVAL WAKE:\s*([^:]+?)(?:\s+recent|\s+rate|\s+herd|$)",
        r"^RIVAL GROWING:\s*([^:]+?)(?:\s+herd|$)",
        r"^THREAT:\s*([^:]+?)(?:\s+gained|$)",
        r"^([^:]+?)\s+has passed us",
    ]
    for pattern in patterns:
        found = re.search(pattern, alert or "", re.IGNORECASE)
        if found:
            return _normalize_subject(alert_class, found.group(1))
    if alert_class in {"knob_age", "model_drift", "policy_drift"}:
        found = re.search(r"(?:KNOB AGE|MODEL DRIFT|POLICY DRIFT):\s*([^:;,]+)", alert or "", re.I)
        if found:
            return _normalize_subject(alert_class, found.group(1))
    return "farm"


def identity(alert_class: str, alert: str, subject: Optional[str] = None) -> Tuple[str, str]:
    target = _subject(alert_class, alert, subject)
    key = "%s:%s" % (alert_class, target)
    return "q-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], key


def _collapse_duplicates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Merge legacy rows that describe the same class + canonical subject."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for original in rows:
        row = dict(original)
        alert_class = str(row.get("class") or "unknown")
        qid, key = identity(alert_class, str(row.get("alert") or ""), str(row.get("subject") or ""))
        row["_canonical_id"] = qid
        row["_canonical_key"] = key
        groups.setdefault(qid, []).append(row)

    collapsed: List[Dict[str, Any]] = []
    migrations: List[Dict[str, Any]] = []
    for qid, members in groups.items():
        # Prefer an already-canonical row, otherwise the oldest durable record.
        winner = next((dict(row) for row in members if row.get("id") == qid), None)
        if winner is None:
            winner = dict(sorted(members, key=lambda row: (
                row.get("opened_run") if isinstance(row.get("opened_run"), int) else 10**18,
                str(row.get("opened_ts") or ""), str(row.get("id") or ""),
            ))[0])
        key = str(winner.pop("_canonical_key", "%s:farm" % winner.get("class")))
        winner.pop("_canonical_id", None)
        removed = [str(row.get("id")) for row in members if row.get("id") != qid]
        winner["id"] = qid
        winner["key"] = key
        winner["subject"] = key.split(":", 1)[1]
        winner["occurrences"] = sum(max(1, int(row.get("occurrences") or 1)) for row in members)
        winner["generation"] = max(int(row.get("generation") or 1) for row in members)
        winner["evidence_refs"] = sorted({
            str(ref) for row in members for ref in (row.get("evidence_refs") or []) if ref
        })
        priorities = [str(row.get("priority") or "medium") for row in members]
        winner["priority"] = max(priorities, key=lambda value: _PRIORITY.get(value, 0))
        active = [row for row in members if row.get("status") in {"open", "probing"}]
        if active:
            winner["status"] = "probing" if any(row.get("status") == "probing" for row in active) else "open"
            winner["answer"] = None
            winner["closed_run"] = None
            winner["closed_ts"] = None
        latest = max(members, key=lambda row: (
            row.get("last_seen_run") if isinstance(row.get("last_seen_run"), int) else -1,
            str(row.get("last_seen_ts") or ""),
        ))
        for field in ("last_seen_run", "last_seen_ts", "alert", "decision_bundle"):
            if latest.get(field) is not None:
                winner[field] = latest.get(field)
        opened_runs = [row.get("opened_run") for row in members if isinstance(row.get("opened_run"), int)]
        opened_times = [str(row.get("opened_ts")) for row in members if row.get("opened_ts")]
        winner["opened_run"] = min(opened_runs) if opened_runs else winner.get("opened_run")
        winner["opened_ts"] = min(opened_times) if opened_times else winner.get("opened_ts")
        collapsed.append(winner)
        if len(members) > 1 or removed or any(row.get("id") != qid for row in members):
            migrations.append({
                "question_id": qid,
                "class": winner.get("class"),
                "subject": winner.get("subject"),
                "merged": len(members),
                "removed_ids": removed,
            })

    collapsed.sort(key=lambda value: (value.get("opened_run") or 0, value.get("id")))
    return collapsed, migrations


def reconcile_duplicates(run: Optional[int] = None) -> Dict[str, Any]:
    """Persist canonical identities while retaining an append-only migration audit."""
    current_path, events_path, lock_path = _paths()
    current_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        original = load_all()
        rows, migrations = _collapse_duplicates(original)
        if migrations:
            _write_current(current_path, rows)
            for migration in migrations:
                _append_event(events_path, dict(
                    migration,
                    schema_version=SCHEMA_VERSION,
                    event="deduplicated",
                    ts=_utcnow(),
                    run=run,
                ))
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return {
        "before": len(original),
        "after": len(rows),
        "merged_groups": len(migrations),
        "removed": len(original) - len(rows),
    }


def template(alert_class: str) -> Dict[str, Any]:
    return dict(TEMPLATES.get(alert_class) or {
        "hypothesis": "The alert reflects a durable strategic uncertainty rather than an operational fault.",
        "settle": "Collect the smallest bounded measurement that distinguishes the competing explanations.",
        "priority": "medium",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 60},
    })


def open_or_update(
    alert_class: str,
    alert: str,
    row: Optional[Dict[str, Any]] = None,
    item: Optional[Dict[str, Any]] = None,
    subject: Optional[str] = None,
    hypothesis: Optional[str] = None,
    settle_measurement: Optional[str] = None,
    cost_bound: Optional[Dict[str, Any]] = None,
    decision_bundle: Optional[Dict[str, Any]] = None,
    evidence_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    row, item = row or {}, item or {}
    current_path, events_path, lock_path = _paths()
    current_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    qid, key = identity(alert_class, alert, subject)
    defaults = template(alert_class)
    run = item.get("run") if isinstance(item.get("run"), int) else row.get("run")
    ts = item.get("ts") or row.get("ts") or _utcnow()

    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        rows, migrations = _collapse_duplicates(load_all())
        existing = next((value for value in rows if value.get("id") == qid), None)
        opened = existing is None
        terminal = bool(existing and existing.get("status") in {"answered", "abandoned"})
        closed_run = (existing or {}).get("closed_run")
        critical_recurrence = alert_class in {
            "rank_lost", "overtaken", "no_path_to_win",
            "activity_novelty_trade", "activity_novelty_rival",
            "activity_novelty_risk", "activity_novelty_tools",
        }
        enough_new_runs = (
            not isinstance(run, int)
            or not isinstance(closed_run, int)
            or run - closed_run >= rules.QUESTION_REOPEN_RUNS
        )
        reopened = bool(terminal and (critical_recurrence or enough_new_runs))
        if existing is None:
            record: Dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "id": qid,
                "key": key,
                "class": alert_class,
                "subject": key.split(":", 1)[1],
                "status": "open",
                "priority": defaults["priority"],
                "opened_run": run,
                "opened_ts": ts,
                "last_seen_run": run,
                "last_seen_ts": ts,
                "occurrences": 1,
                "alert": alert,
                "hypothesis": hypothesis or defaults["hypothesis"],
                "settle_measurement": settle_measurement or defaults["settle"],
                "cost_bound": cost_bound or defaults["budget"],
                "decision_bundle": decision_bundle or {},
                "evidence_refs": list(evidence_refs or []),
                "generation": 1,
                "answer": None,
                "closed_run": None,
                "closed_ts": None,
            }
            rows.append(record)
            event_name = "opened"
        else:
            record = existing
            record["occurrences"] = int(record.get("occurrences") or 0) + 1
            record["last_seen_run"] = run
            record["last_seen_ts"] = ts
            record["alert"] = alert
            if decision_bundle:
                record["decision_bundle"] = decision_bundle
            if evidence_refs:
                record["evidence_refs"] = sorted(set((record.get("evidence_refs") or []) + evidence_refs))
            candidate_priority = defaults["priority"]
            if _PRIORITY.get(candidate_priority, 0) > _PRIORITY.get(str(record.get("priority")), 0):
                record["priority"] = candidate_priority
            if reopened:
                record["status"] = "open"
                record["generation"] = int(record.get("generation") or 1) + 1
                record["answer"] = None
                record["closed_run"] = None
                record["closed_ts"] = None
                event_name = "reopened"
            else:
                event_name = "updated"
        rows.sort(key=lambda value: (value.get("opened_run") or 0, value.get("id")))
        _write_current(current_path, rows)
        for migration in migrations:
            _append_event(events_path, dict(
                migration,
                schema_version=SCHEMA_VERSION,
                event="deduplicated",
                ts=_utcnow(),
                run=run,
            ))
        _append_event(
            events_path,
            {
                "schema_version": SCHEMA_VERSION,
                "event": event_name,
                "ts": _utcnow(),
                "question_id": qid,
                "run": run,
                "occurrences": record.get("occurrences"),
                "alert": alert,
            },
        )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return {
        "question": record,
        "opened": opened,
        "reopened": reopened,
        "page_on_open": bool(defaults.get("page_on_open")) and (opened or reopened),
    }


def set_status(
    question_id: str,
    status: str,
    answer: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    run: Optional[int] = None,
    probe_id: Optional[str] = None,
    result_status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if status not in VALID_STATUSES:
        raise ValueError("invalid question status: %s" % status)
    current_path, events_path, lock_path = _paths()
    current_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        rows = load_all()
        record = next((value for value in rows if value.get("id") == question_id), None)
        if record is None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            return None
        record["status"] = status
        if answer is not None:
            record["answer"] = answer
        if evidence_refs:
            record["evidence_refs"] = sorted(set((record.get("evidence_refs") or []) + evidence_refs))
        if status == "probing":
            record["active_probe_id"] = probe_id
            record["probe_started_run"] = run
            record["closed_run"] = None
            record["closed_ts"] = None
        elif status == "open":
            record["active_probe_id"] = None
            record["probe_started_run"] = None
            record["closed_run"] = None
            record["closed_ts"] = None
        elif status in {"answered", "abandoned"}:
            record["active_probe_id"] = None
            record["probe_started_run"] = None
            record["closed_run"] = run
            record["closed_ts"] = _utcnow()
        if result_status is not None:
            record["probe_result_status"] = result_status
        _write_current(current_path, rows)
        _append_event(
            events_path,
            {
                "schema_version": SCHEMA_VERSION,
                "event": status,
                "ts": _utcnow(),
                "question_id": question_id,
                "run": run,
                "answer": answer,
                "evidence_refs": evidence_refs or [],
                "probe_id": probe_id,
                "result_status": result_status,
            },
        )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return record


def open_questions() -> List[Dict[str, Any]]:
    return [row for row in load_all() if row.get("status") in {"open", "probing"}]


def summary() -> Dict[str, Any]:
    rows = load_all()
    open_rows = [row for row in rows if row.get("status") in {"open", "probing"}]
    return {
        "schema_version": SCHEMA_VERSION,
        "total": len(rows),
        "open": len(open_rows),
        "critical": sum(1 for row in open_rows if row.get("priority") == "critical"),
        "questions": rows,
    }
