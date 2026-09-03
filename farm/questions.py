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
    "strategy.unbacked_parameter": {
        "hypothesis": "A decision-sensitive constant lacks a current claim or invariant that justifies its selected value.",
        "settle": "Name the competing values and falsifier, then bind the smallest safe replay or holdout that can justify a claim or a conservative fixed-policy rationale.",
        "priority": "high",
        "page_on_open": False,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 120},
    },
}

VALID_OWNERS = {"research", "supervisor", "author", "cycle", "human"}

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
    if explicit and _normalize_subject(alert_class, explicit) != "farm":
        return _normalize_subject(alert_class, explicit)
    if alert_class == "activity_novelty_tools":
        found = re.search(r"added=\[([^\]]*)\]\s+removed=\[([^\]]*)\]", alert or "", re.I)
        if found:
            added = sorted(value.lower() for value in re.findall(r"['\"]([^'\"]+)['\"]", found.group(1)))
            removed = sorted(value.lower() for value in re.findall(r"['\"]([^'\"]+)['\"]", found.group(2)))
            return "added:%s;removed:%s" % (",".join(added), ",".join(removed))
        return "server capability surface"
    if alert_class == "activity_novelty_rival":
        names: List[str] = []
        for label in ("new players", "material rival changes"):
            found = re.search(r"%s=\[([^\]]*)\]" % re.escape(label), alert or "", re.I)
            if found:
                names.extend(re.findall(r"['\"]([^'\"]+)['\"]", found.group(1)))
        if names:
            return ",".join(sorted(set(_normalize_subject(alert_class, name) for name in names)))
    if alert_class == "activity_novelty_trade":
        found = re.search(r"trade ids \[([^\]]+)\]", alert or "", re.I)
        if found:
            ids = sorted(set(re.findall(r"\d+", found.group(1))), key=int)
            if ids:
                return "trade-" + ",".join(ids)
    if alert_class == "activity_novelty_risk":
        found = re.search(r"unknown:([a-z_]+)", alert or "", re.I)
        if found:
            return _normalize_subject(alert_class, found.group(1))
        found = re.search(r"new risk event kind\(s\) \[([^\]]+)\]", alert or "", re.I)
        if found:
            names = re.findall(r"['\"]([^'\"]+)['\"]", found.group(1))
            if names:
                return ",".join(sorted(set(_normalize_subject(alert_class, name) for name in names)))
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
        generation_runs = [
            row.get("generation_opened_run") for row in members
            if isinstance(row.get("generation_opened_run"), int)
        ]
        if generation_runs:
            winner["generation_opened_run"] = max(generation_runs)
        winner["evidence_refs"] = sorted({
            str(ref) for row in members for ref in (row.get("evidence_refs") or []) if ref
        })
        priorities = [str(row.get("priority") or "medium") for row in members]
        winner["priority"] = max(priorities, key=lambda value: _PRIORITY.get(value, 0))
        active = [row for row in members if row.get("status") in {"open", "probing"}]
        if active:
            winner["status"] = "probing" if any(row.get("status") == "probing" for row in active) else "open"
            active_latest = max(active, key=lambda row: (
                row.get("last_seen_run") if isinstance(row.get("last_seen_run"), int) else -1,
                str(row.get("last_seen_ts") or ""),
            ))
            for field in ("owner", "next_step", "next_step_due_run", "active_probe_id", "probe_started_run"):
                if active_latest.get(field) is not None:
                    winner[field] = active_latest.get(field)
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
        if not isinstance(winner.get("generation_opened_run"), int):
            winner["generation_opened_run"] = winner.get("opened_run")
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
    owner: Optional[str] = None,
    next_step: Optional[str] = None,
    next_step_due_run: Optional[int] = None,
) -> Dict[str, Any]:
    row, item = row or {}, item or {}
    current_path, events_path, lock_path = _paths()
    current_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    qid, key = identity(alert_class, alert, subject)
    defaults = template(alert_class)
    run = item.get("run") if isinstance(item.get("run"), int) else row.get("run")
    ts = item.get("ts") or row.get("ts") or _utcnow()
    selected_owner = str(owner or "research")
    if selected_owner not in VALID_OWNERS:
        raise ValueError("invalid question owner: %s" % selected_owner)
    selected_next_step = str(next_step or settle_measurement or defaults["settle"]).strip()
    if not selected_next_step:
        raise ValueError("question requires a next step")
    due_run = next_step_due_run
    if due_run is None and isinstance(run, int):
        due_run = run + rules.QUESTION_MAX_AGE_RUNS
    if due_run is not None and (not isinstance(due_run, int) or not isinstance(run, int)
                                or due_run > run + rules.QUESTION_MAX_AGE_RUNS):
        raise ValueError("question next step exceeds the learning SLO")

    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        rows, migrations = _collapse_duplicates(load_all())
        existing = next((value for value in rows if value.get("id") == qid), None)
        opened = existing is None
        terminal = bool(existing and existing.get("status") in {"answered", "abandoned"})
        closed_run = (existing or {}).get("closed_run")
        previous_seen = (existing or {}).get("last_seen_run")
        monotonic_update = bool(
            existing is None
            or (isinstance(run, int) and (
                not isinstance(previous_seen, int) or run >= previous_seen
            ))
            or (not isinstance(run, int) and not isinstance(previous_seen, int)
                and str(ts) >= str((existing or {}).get("last_seen_ts") or ""))
        )
        newer_than_close = bool(
            not isinstance(run, int)
            or not isinstance(closed_run, int)
            or run > closed_run
        )
        reopened = bool(terminal and monotonic_update and newer_than_close)
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
                "generation_opened_run": run,
                "owner": selected_owner,
                "next_step": selected_next_step,
                "next_step_due_run": due_run,
                "last_seen_run": run,
                "last_seen_ts": ts,
                "occurrences": 1,
                "alert": alert,
                "hypothesis": hypothesis or defaults["hypothesis"],
                "settle_measurement": settle_measurement or defaults["settle"],
                "cost_bound": cost_bound or defaults["budget"],
                "decision_bundle": decision_bundle or {},
                "evidence_refs": list(evidence_refs or []),
                "generation_evidence_refs": [],
                "generation": 1,
                "answer": None,
                "probe_result_status": None,
                "resolution_kind": None,
                "residual_uncertainty": None,
                "evidence_cutoff_run": None,
                "closed_run": None,
                "closed_ts": None,
            }
            rows.append(record)
            event_name = "opened"
        else:
            record = existing
            record["occurrences"] = int(record.get("occurrences") or 0) + 1
            if monotonic_update:
                record["last_seen_run"] = run
                record["last_seen_ts"] = ts
                record["alert"] = alert
                if decision_bundle:
                    record["decision_bundle"] = decision_bundle
                if evidence_refs:
                    record["evidence_refs"] = sorted(set((record.get("evidence_refs") or []) + evidence_refs))
                # Deterministically migrate legacy active rows as they recur.
                record["owner"] = selected_owner if owner or not record.get("owner") else record.get("owner")
                record["next_step"] = selected_next_step if next_step or not record.get("next_step") else record.get("next_step")
                if next_step_due_run is not None or not isinstance(record.get("next_step_due_run"), int):
                    record["next_step_due_run"] = due_run
                if not isinstance(record.get("generation_opened_run"), int):
                    record["generation_opened_run"] = record.get("opened_run")
                candidate_priority = defaults["priority"]
                if _PRIORITY.get(candidate_priority, 0) > _PRIORITY.get(str(record.get("priority")), 0):
                    record["priority"] = candidate_priority
            if reopened:
                record["status"] = "open"
                record["generation"] = int(record.get("generation") or 1) + 1
                record["generation_opened_run"] = run
                record["owner"] = selected_owner
                record["next_step"] = selected_next_step
                record["next_step_due_run"] = due_run
                record["answer"] = None
                record["probe_result_status"] = None
                record["resolution_kind"] = None
                record["residual_uncertainty"] = None
                record["evidence_cutoff_run"] = None
                record["generation_evidence_refs"] = []
                record["active_probe_id"] = None
                record["probe_started_run"] = None
                record["closed_run"] = None
                record["closed_ts"] = None
                event_name = "reopened"
            else:
                event_name = "updated" if monotonic_update else "delayed_update"
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
                "generation": record.get("generation"),
                "occurrences": record.get("occurrences"),
                "owner": record.get("owner"),
                "next_step": record.get("next_step"),
                "next_step_due_run": record.get("next_step_due_run"),
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
    expected_generation: Optional[int] = None,
    expected_status: Optional[Any] = None,
    expected_probe_id: Optional[str] = None,
    evidence_cutoff_run: Optional[int] = None,
    resolution_kind: Optional[str] = None,
    residual_uncertainty: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Compare-and-set one lifecycle transition.

    A delayed probe must not close a newer recurrence of the same question. A
    terminal transition also needs a proposition and current durable evidence;
    process exit alone is execution evidence, not epistemic closure.
    """
    if status not in VALID_STATUSES:
        raise ValueError("invalid question status: %s" % status)
    refs = sorted(set(str(value) for value in (evidence_refs or []) if value))
    current_path, events_path, lock_path = _paths()
    current_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        rows = load_all()
        record = next((value for value in rows if value.get("id") == question_id), None)
        if record is None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            return None
        generation = int(record.get("generation") or 1)
        if expected_generation is not None and generation != int(expected_generation):
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            return None
        if expected_status is not None:
            allowed = set(expected_status) if isinstance(expected_status, (set, list, tuple)) else {expected_status}
            if record.get("status") not in allowed:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                return None
        if expected_probe_id is not None and record.get("active_probe_id") != expected_probe_id:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            return None
        # Autonomous probe transitions are always state-machine transitions even
        # if an older caller omitted explicit expectations.
        if status == "probing" and record.get("status") != "open":
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            return None
        if status == "open" and probe_id and record.get("status") != "probing":
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            return None
        if status in {"answered", "abandoned"} and record.get("status") == "probing":
            if not probe_id or record.get("active_probe_id") != probe_id:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                return None

        if status in {"answered", "abandoned"}:
            if not str(answer or "").strip():
                raise ValueError("terminal question transition requires an answer")
            if not refs:
                raise ValueError("terminal question transition requires durable evidence")
            cutoff = evidence_cutoff_run if evidence_cutoff_run is not None else run
            latest_obligation = max(
                int(record.get("generation_opened_run") or 0),
                int(record.get("last_seen_run") or 0),
            )
            if not isinstance(cutoff, int) or cutoff < latest_obligation:
                raise ValueError(
                    "terminal evidence predates the active question generation"
                )
            evidence_cutoff_run = cutoff
            if not resolution_kind:
                if result_status in {"falsified", "rejected"}:
                    resolution_kind = "falsified"
                elif result_status in {"superseded"}:
                    resolution_kind = "superseded"
                else:
                    resolution_kind = "supported"

        record["status"] = status
        if answer is not None:
            record["answer"] = answer
        if refs:
            record["evidence_refs"] = sorted(set((record.get("evidence_refs") or []) + refs))
            record["generation_evidence_refs"] = refs
        if status == "probing":
            record["active_probe_id"] = probe_id
            record["probe_started_run"] = run
            record["owner"] = "research"
            record["next_step"] = "Complete probe %s and adjudicate its falsifier." % (probe_id or "assigned")
            record["next_step_due_run"] = (
                run + rules.QUESTION_MAX_AGE_RUNS if isinstance(run, int) else record.get("next_step_due_run")
            )
            record["closed_run"] = None
            record["closed_ts"] = None
        elif status == "open":
            record["active_probe_id"] = None
            record["probe_started_run"] = None
            record["owner"] = record.get("owner") or "research"
            record["next_step"] = (
                "Review probe %s and bind a revised probe or explicit human decision."
                % (probe_id or "result")
            )
            record["next_step_due_run"] = (
                run + rules.QUESTION_MAX_AGE_RUNS if isinstance(run, int) else record.get("next_step_due_run")
            )
            record["closed_run"] = None
            record["closed_ts"] = None
        elif status in {"answered", "abandoned"}:
            record["active_probe_id"] = None
            record["probe_started_run"] = None
            record["closed_run"] = run
            record["closed_ts"] = _utcnow()
            record["resolution_kind"] = resolution_kind
            record["residual_uncertainty"] = str(residual_uncertainty or "")
            record["evidence_cutoff_run"] = evidence_cutoff_run
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
                "generation": generation,
                "answer": answer,
                "evidence_refs": refs,
                "evidence_cutoff_run": evidence_cutoff_run,
                "resolution_kind": resolution_kind,
                "residual_uncertainty": residual_uncertainty,
                "probe_id": probe_id,
                "result_status": result_status,
            },
        )
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return record


def open_questions() -> List[Dict[str, Any]]:
    return [row for row in load_all() if row.get("status") in {"open", "probing"}]


def release_stale_probes(current_run: Optional[int]) -> List[Dict[str, Any]]:
    """Return questions stranded by a killed probe to schedulable open state."""
    if not isinstance(current_run, int):
        return []
    released: List[Dict[str, Any]] = []
    for question in open_questions():
        if question.get("status") != "probing":
            continue
        started = question.get("probe_started_run")
        if not isinstance(started, int) or current_run - started < rules.PROBE_STALE_RUNS:
            continue
        changed = set_status(
            str(question.get("id")),
            "open",
            answer="Probe lease expired before a result was admitted; retry remains bounded.",
            run=current_run,
            probe_id=str(question.get("active_probe_id") or "interrupted-probe"),
            result_status="interrupted",
            expected_generation=int(question.get("generation") or 1),
            expected_status="probing",
            expected_probe_id=str(question.get("active_probe_id") or ""),
        )
        if changed:
            released.append(changed)
    return released


def events() -> List[Dict[str, Any]]:
    _, path, _ = _paths()
    return _read_events(path)


def _read_events(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def backfill_metadata(run: Optional[int]) -> Dict[str, Any]:
    """Migrate legacy active rows without inventing a newer opening run."""
    current_path, events_path, lock_path = _paths()
    current_path.parent.mkdir(parents=True, exist_ok=True)
    changed: List[str] = []
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        rows = load_all()
        for record in rows:
            if record.get("status") not in {"open", "probing"}:
                continue
            touched = False
            defaults = template(str(record.get("class") or ""))
            if _PRIORITY.get(str(defaults.get("priority") or "medium"), 1) > _PRIORITY.get(
                str(record.get("priority") or "medium"), 1
            ):
                record["priority"] = defaults["priority"]
                touched = True
            if record.get("class") == "strategy.unbacked_parameter":
                generic = "The alert reflects a durable strategic uncertainty"
                if str(record.get("hypothesis") or "").startswith(generic):
                    record["hypothesis"] = defaults["hypothesis"]
                    record["settle_measurement"] = defaults["settle"]
                    record["next_step"] = defaults["settle"]
                    touched = True
            if record.get("owner") not in VALID_OWNERS:
                record["owner"] = "research"
                touched = True
            if not str(record.get("next_step") or "").strip():
                record["next_step"] = str(record.get("settle_measurement") or defaults["settle"])
                touched = True
            if not isinstance(record.get("generation_opened_run"), int):
                opened = record.get("opened_run")
                record["generation_opened_run"] = opened if isinstance(opened, int) else 0
                if not isinstance(opened, int):
                    record["age_source"] = "unknown_conservative"
                touched = True
            generation_run = record.get("generation_opened_run")
            if not isinstance(record.get("next_step_due_run"), int) and isinstance(generation_run, int):
                record["next_step_due_run"] = generation_run + rules.QUESTION_MAX_AGE_RUNS
                touched = True
            if touched:
                changed.append(str(record.get("id")))
        if changed:
            _write_current(current_path, rows)
            _append_event(events_path, {
                "schema_version": SCHEMA_VERSION,
                "event": "metadata_backfilled",
                "ts": _utcnow(),
                "run": run,
                "question_ids": sorted(changed),
            })
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return {"changed": len(changed), "question_ids": sorted(changed)}


def reconcile(
    current_run: Optional[int],
    rows: Optional[List[Dict[str, Any]]] = None,
    registry: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Close only obligations settled by current class-specific evidence.

    This is not an age-based garbage collector. Every transition names a
    proposition, immutable evidence window, resolution kind, and any remaining
    uncertainty. A recurring detector can reopen the same stable identity.
    """
    if not isinstance(current_run, int):
        return []
    from . import analysis, claims, mechanics, policy

    history = list(rows) if rows is not None else analysis.history_rows(limit=240)
    history = [row for row in history if isinstance(row.get("run"), int) and not row.get("dry")]
    if not history:
        return []
    history.sort(key=lambda row: int(row["run"]))
    latest = history[-1]
    latest_run = int(latest["run"])
    # The evidence cutoff, not a possibly stale caller snapshot, owns closure.
    current_run = latest_run
    window = history[-rules.QUESTION_FLOW_WINDOW_RUNS :]
    first = window[0]
    score_health = rules.score_production_health(history)
    registry = registry if registry is not None else (claims.load() or {})
    claim_map = claims.claim_map(registry) if registry else {}
    resolved: List[Dict[str, Any]] = []

    def evidence_window() -> str:
        return "state/history.ndjson#runs=%s-%s" % (first.get("run"), latest.get("run"))

    def close(
        question: Dict[str, Any],
        answer: str,
        refs: List[str],
        kind: str,
        residual: str = "",
        result_status: str = "supported",
        evidence_cutoff: Optional[int] = None,
    ) -> bool:
        identity = str(question.get("id") or "")
        if not identity:
            return False
        try:
            changed = set_status(
                identity,
                "answered",
                answer=answer,
                evidence_refs=refs,
                run=current_run,
                probe_id="deterministic-reconciliation",
                result_status=result_status,
                expected_generation=int(question.get("generation") or 1),
                expected_status="open",
                evidence_cutoff_run=(
                    evidence_cutoff if isinstance(evidence_cutoff, int) else current_run
                ),
                resolution_kind=kind,
                residual_uncertainty=residual,
            )
        except ValueError:
            return False
        if changed is None:
            return False
        resolved.append({"question_id": identity, "resolution_kind": kind, "answer": answer})
        return True

    current_score = float(latest.get("produce") or 0)
    first_score = float(first.get("produce") or 0)
    our_gain = max(0.0, current_score - first_score)
    current_at_cap = bool(
        int(latest.get("animal_capacity") or 0) > 0
        and int(latest.get("animals") or 0) >= int(latest.get("animal_capacity") or 0)
    )

    for question in open_questions():
        qclass = str(question.get("class") or "")
        subject = str(question.get("subject") or "")
        alert = str(question.get("alert") or "")
        last_seen = question.get("last_seen_run")
        age = current_run - int(last_seen) if isinstance(last_seen, int) else None

        if qclass == "strategy.unbacked_parameter":
            policy_support = policy.parameter_support(subject)
            parameter_values = {
                "feed_cooldown_runs": rules.FEED_COOLDOWN_RUNS,
                "threat_share": rules.THREAT_SHARE,
            }
            if (
                policy_support.get("level") == "conservative_invariant"
                and subject in parameter_values
                and policy_support.get("value") == parameter_values[subject]
                and score_health.get("status") in {"healthy", "watching"}
            ):
                close(
                    question,
                    str(policy_support.get("rationale") or "The value is bounded by a conservative invariant."),
                    list(policy_support.get("evidence_refs") or []) + [evidence_window()],
                    "supported",
                    str(policy_support.get("falsifier") or "new contrary evidence can reopen this value"),
                )
                continue
            if (
                subject in {"growth_min_marginal_gain", "growth_comparison_window"}
                and current_at_cap
                and latest.get("rank") == 1
            ):
                close(
                    question,
                    "The growth threshold is unreachable while the herd is at its hard capacity; directional uncertainty is retained until a below-cap regime makes it decision-active.",
                    [evidence_window(), "state/policy.json#capacity-guard"],
                    "condition_cleared",
                    "the exact value remains unproven and reopens immediately below cap",
                    result_status="inactive_regime",
                )
                continue

        if qclass == "model_drift" and subject == "output_yield":
            claim = claim_map.get("mechanic.output_linear_with_herd") or {}
            if (
                claim.get("status") == "accepted"
                and isinstance(claim.get("last_validated_run"), int)
                and int(claim["last_validated_run"]) >= int(last_seen or 0)
            ):
                close(
                    question,
                    "A newer regime-filtered output claim validates positive herd/output scaling through run %s."
                    % claim.get("last_validated_run"),
                    ["state/claims.json#mechanic.output_linear_with_herd", evidence_window()],
                    "superseded",
                    "future cohort drift remains independently detectable",
                    evidence_cutoff=int(claim["last_validated_run"]),
                )
                continue

        if qclass == "model_drift" and subject == "hunger_wall":
            claim = claim_map.get("safety.bulk_husbandry") or {}
            if (
                claim.get("status") == "accepted"
                and (claim.get("refresh") or {}).get("state") == "current"
                and isinstance(claim.get("last_validated_run"), int)
                and int(claim["last_validated_run"]) >= int(last_seen or 0)
            ):
                close(
                    question,
                    "Constant-time whole-herd feeding and direct hunger/runway guards supersede the synthetic herd-size wall.",
                    ["state/claims.json#safety.bulk_husbandry", evidence_window()],
                    "superseded",
                    "actual hunger and feed-runway incidents remain live safety signals",
                    evidence_cutoff=int(claim["last_validated_run"]),
                )
                continue

        if qclass == "knob_age" and subject == "individual_feeds" and (age or 0) >= rules.QUESTION_MAX_AGE_RUNS:
            if all("individual_feeds" not in (row.get("knobs") or {}) for row in window):
                close(
                    question,
                    "The retired per-animal feed knob is absent throughout the current evidence window.",
                    [evidence_window(), "farm/heal.py#retired-individual-feeds"],
                    "superseded",
                    "bulk-feed performance remains governed by safety.bulk_husbandry",
                )
                continue

        if qclass == "knob_age" and subject == "rate_ceiling" and (age or 0) >= rules.QUESTION_MAX_AGE_RUNS:
            noisy = any(
                "transport retries" in " ".join(row.get("anomalies") or []).lower()
                or "server pushing back" in " ".join(row.get("anomalies") or []).lower()
                for row in window
            )
            if not noisy and all("rate_ceiling" not in (row.get("knobs") or {}) for row in window):
                close(
                    question,
                    "The transient rate override is absent and the current window has no transport/backpressure incident.",
                    [evidence_window(), "state/heal.json#knobs"],
                    "condition_cleared",
                    "future transport pressure can recreate a fresh bounded override",
                )
                continue

        if qclass == "knob_age" and subject in claim_map:
            claim = claim_map.get(subject) or {}
            if (
                claim.get("status") == "accepted"
                and (claim.get("refresh") or {}).get("state") == "current"
                and isinstance(claim.get("last_validated_run"), int)
                and int(claim["last_validated_run"]) >= int(last_seen or 0)
            ):
                close(
                    question,
                    "Claim %s was revalidated through run %s with its declared estimator."
                    % (subject, claim.get("last_validated_run")),
                    ["state/claims.json#%s" % subject] + list(claim.get("evidence_refs") or [])[:3],
                    "supported",
                    "the claim will reopen when its evidence-age contract expires again",
                    evidence_cutoff=int(claim["last_validated_run"]),
                )
                continue

        if qclass == "activity_novelty_risk" and "rustler" in alert.lower():
            verified = None
            for row in reversed(history):
                for action in row.get("mechanic_actions") or []:
                    before = ((action.get("verification") or {}).get("before") or {}) if isinstance(action, dict) else {}
                    if (
                        isinstance(action, dict)
                        and action.get("kind") == "crisis"
                        and action.get("tool") == "resolve_crisis"
                        and action.get("status") in {"verified", "reconciled"}
                        and before.get("crisis_kind") == "rustlers"
                        and int(row.get("run") or 0) >= int(last_seen or 0)
                    ):
                        verified = row
                        break
                if verified:
                    break
            if verified and "resolve_crisis" in mechanics.active_tools():
                close(
                    question,
                    "The rustlers signature is now bound to the protected resolve_crisis policy and was verified to clear the crisis.",
                    ["state/history.ndjson#run=%s" % verified.get("run"), "capability-policy:resolve_crisis"],
                    "supported",
                    "new loss signatures remain blocked until separately classified",
                    evidence_cutoff=int(verified.get("run") or current_run),
                )
                continue

        if qclass == "operational_throughput":
            interval = float(latest.get("interval_min") or 0.0)
            rate = latest.get("units_per_animal_min")
            exposure = int(latest.get("collection_animals") or latest.get("animals") or 0)
            if rate is None and interval > 0 and exposure > 0:
                rate = float(latest.get("units_collected") or 0) / float(exposure) / interval
            if (
                isinstance(rate, (int, float))
                and rules.backlog_drained(int(latest.get("ready_units") or 0), int(latest.get("animals") or 0))
                and int(latest.get("max_hunger") or 0) < rules.HUNGER_ALARM
                and score_health.get("status") == "healthy"
            ):
                close(
                    question,
                    "All-species throughput is %.3f units/animal/min with a drained barn, safe hunger, and healthy score production; the chicken-only denominator was invalid."
                    % float(rate),
                    ["state/history.ndjson#run=%s" % current_run, "farm/cycle.py#units_per_animal_min"],
                    "superseded",
                    "upper-band changes remain periodic model evidence, not operational incidents",
                )
                continue

        if qclass == "operational_collection_backlog" and (age or 0) >= rules.QUESTION_REOPEN_RUNS:
            if rules.backlog_drained(
                int(latest.get("ready_units") or 0), int(latest.get("animals") or 0)
            ):
                close(
                    question,
                    "The barn backlog is drained in the current negative-observation window.",
                    [evidence_window()],
                    "condition_cleared",
                    "a renewed material backlog after empty collection calls reopens immediately",
                )
                continue

        production_episode = (
            qclass in {"operational_production", "operational_zero_collect"}
            or (qclass == "operational_unknown" and alert.startswith("PRODUCTION:"))
        )
        if production_episode and score_health.get("status") == "healthy":
            close(
                question,
                "The burst-spanning score window is healthy; adjacent zero deltas were a scoreboard publication phase, not a production halt.",
                [evidence_window(), "farm/rules.py#score_production_health"],
                "condition_cleared",
                "a future 35-minute flat score still opens a new production incident",
            )
            continue

        if qclass == "operational_trades_in" and (age or 0) >= rules.QUESTION_MAX_AGE_RUNS:
            if all(int(row.get("trades_in") or 0) == 0 for row in window):
                close(
                    question,
                    "No inbound trade remains anywhere in the current negative-observation window.",
                    [evidence_window()],
                    "condition_cleared",
                    "future inbound offers remain subject to the promoted trade policy",
                )
                continue

        if qclass == "operational_animals_fell":
            if current_at_cap and not latest.get("active_crisis") and score_health.get("status") == "healthy":
                close(
                    question,
                    "The herd recovered to its league capacity with no active crisis and healthy score production.",
                    [evidence_window(), "state/history.ndjson#run=%s" % current_run],
                    "condition_cleared",
                    "future classified loss events remain independently actionable",
                )
                continue

        if qclass == "idle_capital":
            if current_at_cap and latest.get("rank") == 1 and score_health.get("status") == "healthy":
                close(
                    question,
                    "The farm is at the hard animal cap, remains rank 1, and advances score; idle coins are not blocking an available growth action.",
                    [evidence_window(), "state/claims.json#strategy.capped_slot_efficiency"],
                    "condition_cleared",
                    "exact capped-slot replacement economics remain claim-governed",
                )
                continue

        if qclass in {"rival_wake", "rival_growing", "threat"} and (age or 0) >= rules.QUESTION_REOPEN_RUNS:
            def named(mapping: Any, name: str) -> Optional[float]:
                if not isinstance(mapping, dict):
                    return None
                for key, value in mapping.items():
                    if str(key).strip().lower() == name.strip().lower() and isinstance(value, (int, float)):
                        return float(value)
                return None

            before_score = named(first.get("rivals"), subject)
            after_score = named(latest.get("rivals"), subject)
            before_herd = named(first.get("rival_herds"), subject)
            after_herd = named(latest.get("rival_herds"), subject)
            if before_score is not None and after_score is not None:
                rival_gain = max(0.0, after_score - before_score)
                herd_gain = max(0.0, (after_herd or 0.0) - (before_herd or 0.0))
                material = bool(
                    (rival_gain > 0 and our_gain <= 0)
                    or (rival_gain > 0 and our_gain > 0
                        and rival_gain >= rules.THREAT_SHARE * our_gain)
                    or herd_gain >= rules.RIVAL_HERD_GROWTH_ALARM
                )
                if (
                    latest.get("rank") == 1
                    and score_health.get("status") == "healthy"
                    and not material
                ):
                    close(
                        question,
                        "The historical %s episode is no longer decision-material: over runs %s-%s our score gained %.0f versus %.0f for %s, while rank 1 was retained."
                        % (qclass, first.get("run"), latest.get("run"), our_gain, rival_gain, subject),
                        [evidence_window()],
                        "condition_cleared",
                        "the original rival mechanism is unresolved; any renewed material gain reopens the episode",
                    )
                    continue

    return resolved


def health(
    current_run: Optional[int],
    rows: Optional[List[Dict[str, Any]]] = None,
    event_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Deterministic age, hygiene, WIP, and arrival/closure assessment."""
    active = [
        row for row in (rows if rows is not None else load_all())
        if row.get("status") in {"open", "probing"}
    ]
    history = list(event_rows if event_rows is not None else events())
    high_missing: List[str] = []
    other_missing: List[str] = []
    overdue_high: List[str] = []
    overdue_other: List[str] = []
    for row in active:
        identity = str(row.get("id") or "unknown")
        missing = (
            row.get("owner") not in VALID_OWNERS
            or not str(row.get("next_step") or "").strip()
            or not isinstance(row.get("next_step_due_run"), int)
            or not isinstance(row.get("generation_opened_run"), int)
        )
        high = row.get("priority") in {"high", "critical"}
        if missing:
            (high_missing if high else other_missing).append(identity)
        generation_run = row.get("generation_opened_run")
        due_run = row.get("next_step_due_run")
        overdue = (
            not isinstance(generation_run, int)
            or not isinstance(current_run, int)
            or current_run - generation_run >= rules.QUESTION_MAX_AGE_RUNS
            or (isinstance(due_run, int) and isinstance(current_run, int) and current_run >= due_run)
        )
        if overdue:
            (overdue_high if high else overdue_other).append(identity)

    probing = [str(row.get("id")) for row in active if row.get("status") == "probing"]
    window = rules.QUESTION_FLOW_WINDOW_RUNS

    def flow(start: int, end: int) -> Dict[str, int]:
        selected = [
            row for row in history
            if isinstance(row.get("run"), int) and start <= int(row["run"]) <= end
        ]
        arrivals = sum(row.get("event") in {"opened", "reopened"} for row in selected)
        answered = sum(row.get("event") == "answered" for row in selected)
        abandoned = sum(row.get("event") == "abandoned" for row in selected)
        return {
            "arrivals": arrivals,
            "answered": answered,
            "abandoned": abandoned,
            # Abandonment is a work disposition, not evidence that uncertainty
            # was resolved; it must never make learning throughput look healthy.
            "closures": answered,
        }

    if isinstance(current_run, int):
        current_flow = flow(max(0, current_run - window + 1), current_run)
        previous_flow = flow(max(0, current_run - 2 * window + 1), max(0, current_run - window))
    else:
        current_flow = {"arrivals": 0, "answered": 0, "abandoned": 0, "closures": 0}
        previous_flow = dict(current_flow)

    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    if high_missing:
        fail_reasons.append("high-priority questions lack owner, next step, due run, or generation age")
    if overdue_high:
        fail_reasons.append("high-priority questions exceeded the 40-run SLO")
    if len(probing) > rules.MAX_PROBING_QUESTIONS:
        fail_reasons.append("probing WIP exceeds the single mutation boundary")
    backlog_stalled = (
        len(active) > rules.QUESTION_BACKLOG_WARN
        and current_flow["closures"] <= current_flow["arrivals"]
    )
    if backlog_stalled:
        fail_reasons.append("question backlog exceeds WIP warning with no net drain")
    if (current_flow["arrivals"] > current_flow["closures"]
            and previous_flow["arrivals"] > previous_flow["closures"]):
        fail_reasons.append("arrivals exceeded closures for two consecutive windows")
    elif current_flow["arrivals"] > current_flow["closures"]:
        warn_reasons.append("question arrivals exceed closures in the current window")
    if other_missing:
        warn_reasons.append("lower-priority questions lack lifecycle metadata")
    if overdue_other:
        warn_reasons.append("lower-priority questions exceeded the 40-run SLO")
    if active and not current_flow["arrivals"] and not current_flow["closures"]:
        warn_reasons.append("nonempty question backlog has zero flow")

    status = "fail" if fail_reasons else "warn" if warn_reasons else "pass"
    return {
        "status": status,
        "reasons": fail_reasons + warn_reasons,
        "open": len(active),
        "probing": len(probing),
        "probing_ids": probing,
        "high_missing_metadata": high_missing,
        "other_missing_metadata": other_missing,
        "overdue_high_priority": overdue_high,
        "overdue_other_priority": overdue_other,
        "backlog_warn_threshold": rules.QUESTION_BACKLOG_WARN,
        "max_age_runs": rules.QUESTION_MAX_AGE_RUNS,
        "current_flow": current_flow,
        "previous_flow": previous_flow,
    }


def summary() -> Dict[str, Any]:
    rows = load_all()
    open_rows = [row for row in rows if row.get("status") in {"open", "probing"}]
    return {
        "schema_version": SCHEMA_VERSION,
        "total": len(rows),
        "open": len(open_rows),
        "critical": sum(1 for row in open_rows if row.get("priority") == "critical"),
        "owned": sum(1 for row in open_rows if row.get("owner") in VALID_OWNERS),
        "questions": rows,
    }
