#!/usr/bin/env python3
"""Confirm a Farm Friends outage, then notify Slack exactly once per incident.

A failed farm call is not enough. Before calling anything an external outage this
job proves that the local endpoint secret is valid, the immutable release exists,
and both the cycle and supervisor LaunchAgents are loaded (repairing them first when
needed). It then requires repeated read-only remote failures or repeated flat lifetime
score checks. Recovery is announced only after the same remote probe succeeds again.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import analysis, control, ledger, mcp, notify, parse, rules, scheduler  # noqa: E402

STATE = PROJECT / "state"
STORE = STATE / "outage_notifier.json"
SLACK_INTEL = STATE / "slack_intel.json"
EVENTS = STATE / "notification_events.ndjson"
LOCK = STATE / ".outage-notifier.lock"
TRANSPORT_FAILURES_TO_CONFIRM = 2
FLAT_SCORE_CHECKS_TO_CONFIRM = 2
PROBE_TIMEOUT_SECONDS = 12
CHANNEL_NAME = "#farm-friends"
CHANNEL_ID = "C0BRMBGN7QA"
JOHN_USER_ID = "U0APP5TP32S"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def append_event(event: str, detail: Dict[str, Any]) -> None:
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": utcnow(), "event": event, "detail": detail}
    with EVENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _latest() -> Dict[str, Any]:
    rows = analysis.history_rows(limit=1)
    return rows[-1] if rows else {}


def _endpoint_check() -> Dict[str, Any]:
    try:
        endpoint = mcp._load_endpoint()
    except Exception as exc:  # noqa: BLE001 - local configuration must be explicit
        return {"ok": False, "detail": str(exc)[:160]}
    return {"ok": endpoint.startswith("https://"), "detail": "farm endpoint configured"}


def _release_check() -> Dict[str, Any]:
    try:
        root = control.project_root(PROJECT)
        release = (root / "release").resolve(strict=True)
        ok = (release / "run.py").is_file() and (release / "farm" / "mcp.py").is_file()
    except (OSError, RuntimeError) as exc:
        return {"ok": False, "detail": "live release unavailable: %s" % str(exc)[:100]}
    return {"ok": ok, "detail": "immutable live release available" if ok else "live release incomplete"}


def _service_check(label: str) -> Dict[str, Any]:
    actions: List[str] = []
    try:
        info = scheduler.status(label)
        if not info.get("loaded"):
            actions = scheduler.repair(label, kick=True)
            info = scheduler.status(label)
    except Exception as exc:  # noqa: BLE001 - a local check must block attribution, not crash
        return {"ok": False, "detail": "%s check failed: %s" % (label, str(exc)[:100]), "actions": actions}
    return {
        "ok": bool(info.get("loaded")),
        "detail": "%s %s" % (label, info.get("state") or "unknown"),
        "actions": actions,
    }


def local_setup_checks() -> Dict[str, Any]:
    """Prove local prerequisites before attributing the failure externally."""
    checks = {
        "endpoint": _endpoint_check(),
        "release": _release_check(),
        "cycle": _service_check(scheduler.CYCLE_LABEL),
        "supervisor": _service_check(scheduler.SUPERVISOR_LABEL),
    }
    return {"ok": all(item.get("ok") for item in checks.values()), "checks": checks}


def remote_probe(farmer: str = "Nick") -> Dict[str, Any]:
    """One bounded, read-only score check with a short outage-specific timeout."""
    client = mcp.Client(timeout=PROBE_TIMEOUT_SECONDS, retries=1)
    try:
        board = parse.parse_leaderboard(client.call("leaderboard"))
    except Exception as exc:  # noqa: BLE001 - every remote failure has the same disposition
        return {"ok": False, "error": client.scrub(str(exc))[:180]}
    ours = next((row for row in board if row.name.lower() == farmer.lower()), None)
    if ours is None:
        return {"ok": False, "error": "configured farmer missing from leaderboard"}
    return {"ok": True, "score": int(ours.produce), "rank": int(ours.rank)}


def _incident_id(first_failure_ts: str) -> str:
    return "farm-%s" % hashlib.sha256(first_failure_ts.encode("utf-8")).hexdigest()[:10]


def adopt_slack_intel(previous: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    """Adopt a human/Glean-posted alert so the local guard cannot duplicate it."""
    alert = intel.get("outage_alert") if isinstance(intel, dict) else None
    if not isinstance(alert, dict) or alert.get("status") != "open":
        return previous
    message_ts = str(alert.get("message_ts") or "")
    consumed = list(previous.get("consumed_slack_alerts") or [])
    if (
        not message_ts
        or message_ts in consumed
        or alert.get("channel_id") != CHANNEL_ID
        or not alert.get("john_mentioned")
        or previous.get("status") == "outage"
    ):
        return previous
    try:
        first = datetime.fromtimestamp(float(message_ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        first = str(alert.get("observed_ts") or utcnow())
    state = dict(previous)
    state.update({
        "status": "outage",
        "announced": True,
        "outage_notification_claimed": True,
        "outage_notification_claimed_ts": str(alert.get("observed_ts") or first),
        "incident_id": "slack-%s" % hashlib.sha256(message_ts.encode("utf-8")).hexdigest()[:10],
        "first_failure_ts": first,
        "outage_kind": str(alert.get("kind") or "transport"),
        "slack_thread_ts": message_ts,
        "consumed_slack_alerts": (consumed + [message_ts])[-20:],
    })
    return state


def claim_outage_notification(state: Dict[str, Any], now: str) -> Dict[str, Any]:
    """Reserve the sole John mention immediately before configured delivery."""
    claimed = dict(state)
    claimed.update({
        "outage_notification_claimed": True,
        "outage_notification_claimed_ts": now,
        "announced": False,
    })
    return claimed


def release_retryable_configuration_claim(state: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate a pre-delivery configuration failure back to retryable state.

    A missing or invalid webhook proves that no request reached Slack, so preserving
    the non-idempotent delivery claim would suppress a notification that definitely
    was not sent. Network delivery failures remain claimed because their outcome is
    ambiguous and an automatic retry could duplicate the John mention.
    """
    error = str(state.get("delivery_error") or "")
    retryable = (
        error.startswith("no Slack webhook configured")
        or error.startswith("Slack webhook file")
        or error.startswith("Slack webhook must")
        or error.startswith("Slack webhook URL")
    )
    if not retryable or state.get("announced") or not state.get("outage_notification_claimed"):
        return state
    released = dict(state)
    released["outage_notification_claimed"] = False
    released.pop("outage_notification_claimed_ts", None)
    released["delivery_error_kind"] = "configuration"
    return released


def decide(
    previous: Dict[str, Any],
    local: Dict[str, Any],
    probe: Dict[str, Any],
    latest: Dict[str, Any],
    now: str,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Pure incident state transition. Returns (new state, event to announce)."""
    state = dict(previous)
    state.update({"schema_version": 1, "last_checked_ts": now, "local": local, "probe": probe})

    previous_status = str(previous.get("status") or "healthy")
    previous_announced = bool(previous.get("announced"))
    previous_claimed = bool(previous.get("outage_notification_claimed"))
    recovery_pending = bool(previous.get("recovery_notification_pending"))
    if not local.get("ok"):
        state["local_issue_ts"] = now
        state["local_blocked"] = True
        if previous_status == "outage" and (previous_announced or previous_claimed):
            # Do not lose an open or claim-before-send incident merely because its
            # independent local verifier is temporarily unhealthy. Recovery still
            # needs remote proof, and John must not be mentioned a second time.
            state.update({
                "status": "outage",
                "announced": previous_announced,
                "outage_notification_claimed": previous_claimed,
            })
        else:
            # Old remote suspicion cannot survive a local setup fault and become
            # the first half of a later external-outage claim.
            state.update({
                "status": "local_issue", "announced": False,
                "transport_failures": 0, "flat_score_checks": 0,
                "first_failure_ts": None, "incident_id": None,
            })
        return state, None

    state["local_blocked"] = False
    if not probe.get("ok"):
        failures = int(previous.get("transport_failures") or 0) + 1
        first = str(previous.get("first_failure_ts") or now)
        confirmed = (previous_status == "outage" and (previous_announced or previous_claimed)) or failures >= TRANSPORT_FAILURES_TO_CONFIRM
        state.update({
            "status": "outage" if confirmed else "suspect",
            "transport_failures": failures,
            "flat_score_checks": 0,
            "first_failure_ts": first,
            "incident_id": previous.get("incident_id") or _incident_id(first),
            "announced": previous_announced,
            "outage_notification_claimed": previous_claimed,
            "recovery_notification_pending": False,
            "recovered_incident_id": None,
        })
        if confirmed and not previous_claimed:
            return state, "outage"
        return state, None

    score = int(probe.get("score") or 0)
    old_score = previous.get("last_score")
    production_eligible = (
        isinstance(old_score, int)
        and (latest.get("max_hunger") or 0) < rules.HUNGER_STOP
        and (latest.get("animals") or 0) > 0
    )
    flat = int(previous.get("flat_score_checks") or 0) + 1 if production_eligible and score <= old_score else 0
    state.update({
        "last_score": score,
        "last_rank": probe.get("rank"),
        "transport_failures": 0,
        "flat_score_checks": flat,
    })
    if flat >= FLAT_SCORE_CHECKS_TO_CONFIRM:
        first = str(previous.get("first_failure_ts") or now)
        state.update({
            "status": "outage",
            "first_failure_ts": first,
            "incident_id": previous.get("incident_id") or _incident_id(first),
            "announced": previous_announced,
            "outage_notification_claimed": previous_claimed,
            "outage_kind": "production_stall",
            "recovery_notification_pending": False,
            "recovered_incident_id": None,
        })
        if not previous_claimed:
            return state, "outage"
        return state, None

    recovered = previous_status == "outage" and previous_announced
    recovery_pending = recovered or recovery_pending
    state.update({
        "status": "healthy",
        "announced": False,
        "outage_notification_claimed": False,
        "recovery_notification_pending": recovery_pending,
        "recovered_incident_id": (
            previous.get("incident_id") if recovered else previous.get("recovered_incident_id")
        ) if recovery_pending else None,
        "first_failure_ts": None,
        "incident_id": None,
        "outage_kind": None,
    })
    return state, "recovered" if recovery_pending else None


def _local_proof(local: Dict[str, Any]) -> str:
    checks = local.get("checks") or {}
    return "; ".join(str((checks.get(key) or {}).get("detail") or key) for key in ("endpoint", "release", "cycle", "supervisor"))


def outage_message(state: Dict[str, Any], latest: Dict[str, Any]) -> str:
    trouble = "production has stopped moving" if state.get("outage_kind") == "production_stall" else "the leaderboard gate has stopped answering"
    runway = rules.feed_buffer_minutes(int(latest.get("feed") or 0), int(latest.get("animals") or 0))
    return (
        "<@%s> 🚨🏆 *The scoreboard fence is stuck* — %s.\n"
        "Last safe count: rank #%s, %s produce, %s animals, and about %.0f minutes of feed. 🐓\n"
        "Routine farm care may still be healthy while the ranch hands test the scoreboard. I’ll holler when standings move again. 🛠️🌾"
        % (
            JOHN_USER_ID, trouble, latest.get("rank"), latest.get("produce"),
            latest.get("animals"), runway,
        )
    )


def recovery_message(state: Dict[str, Any], latest: Dict[str, Any]) -> str:
    return (
        "🌤️🏆 *The scoreboard is answering again!* Farm Friends is back at rank #%s with %s lifetime produce.\n"
        "The ranch hands confirmed fresh standings; routine farm care kept tending the herd. 🐓🌾"
        % (latest.get("rank"), state.get("last_score") or latest.get("produce"))
    )


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("OUTAGE NOTIFIER skipped: previous check still active")
        return 0

    previous = adopt_slack_intel(read_json(STORE), read_json(SLACK_INTEL))
    previous = release_retryable_configuration_claim(previous)
    local = local_setup_checks()
    probe = remote_probe(str(previous.get("farmer") or "Nick")) if local.get("ok") else {"ok": False, "error": "local setup not cleared"}
    latest = _latest()
    state, event = decide(previous, local, probe, latest, utcnow())
    state["farmer"] = str(previous.get("farmer") or "Nick")
    state["channel"] = {"name": CHANNEL_NAME, "id": CHANNEL_ID}

    if event:
        message = outage_message(state, latest) if event == "outage" else recovery_message(state, latest)
        config = notify.configured()
        state["delivery_attempts"] = int(state.get("delivery_attempts") or 0) + 1
        state["last_delivery_attempt_ts"] = utcnow()
        if not config.get("ok"):
            # No request can have reached Slack, so leave the incident unclaimed.
            # The five-minute job cadence provides a bounded retry once the approved
            # app's webhook is installed without risking duplicate channel posts.
            state["delivery_error"] = str(config.get("detail") or "Slack notification is not configured")
            state["delivery_error_kind"] = "configuration"
            if event == "outage":
                state["outage_notification_claimed"] = False
                state.pop("outage_notification_claimed_ts", None)
            append_event("delivery_failed", {
                "kind": event, "error": state["delivery_error"], "retryable": True,
            })
        else:
            if event == "outage":
                # Persist immediately before the non-idempotent webhook call. A
                # network failure can be ambiguous, so only that case keeps the
                # claim and requires adoption/operator review before another post.
                state = claim_outage_notification(state, utcnow())
                write_json(STORE, state)
            try:
                notify.send(message)
                state["announced"] = event == "outage"
                state["recovery_notification_pending"] = False
                state["last_notification_ts"] = utcnow()
                state["last_notification_event"] = event
                incident_id = state.get("incident_id") or state.get("recovered_incident_id")
                state.pop("delivery_error", None)
                state.pop("delivery_error_kind", None)
                append_event(event, {"incident_id": incident_id, "run": latest.get("run")})
                ledger.record("notification.%s" % event, {"channel": CHANNEL_NAME, "incident_id": incident_id}, actor="outage_notifier", run=latest.get("run"))
            except notify.NotificationConfigError as exc:
                # A configuration race still proves no valid Slack request was made.
                state["delivery_error"] = str(exc)
                state["delivery_error_kind"] = "configuration"
                if event == "outage":
                    state["outage_notification_claimed"] = False
                    state.pop("outage_notification_claimed_ts", None)
                append_event("delivery_failed", {
                    "kind": event, "error": str(exc), "retryable": True,
                })
            except notify.NotificationDeliveryError as exc:
                state["delivery_error"] = str(exc)
                state["delivery_error_kind"] = "ambiguous_delivery"
                if event == "recovered":
                    state["recovery_notification_pending"] = False
                append_event("delivery_failed", {
                    "kind": event, "error": str(exc), "retryable": False,
                })
    write_json(STORE, state)
    print("OUTAGE NOTIFIER status=%s event=%s local=%s delivery=%s" % (
        state.get("status"), event or "none", "ok" if local.get("ok") else "blocked",
        "ok" if event and not state.get("delivery_error") else "idle" if not event else "failed",
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
