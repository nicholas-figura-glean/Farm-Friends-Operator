#!/usr/bin/env python3
"""Headless tests for outage confirmation, Slack delivery, and EOD reporting."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from experiments import eod_report, outage_notifier  # noqa: E402
from farm import control, mcp, notify  # noqa: E402

checks = 0
failures = []


def check(value, label, detail=""):
    global checks
    checks += 1
    if value:
        print("  ok  ", label)
    else:
        print("  FAIL", label, detail)
        failures.append(label)


# Secret handling and delivery -------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "slack_webhook"
    path.write_text("https://hooks.slack.com/services/T123/B456/secret\n", encoding="utf-8")
    path.chmod(0o600)
    old_file = os.environ.get("FARM_SLACK_WEBHOOK_FILE")
    old_url = os.environ.pop("FARM_SLACK_WEBHOOK_URL", None)
    os.environ["FARM_SLACK_WEBHOOK_FILE"] = str(path)
    try:
        check(notify.load_webhook().startswith("https://hooks.slack.com/services/"),
              "webhook loads from a mode-0600 file")
        path.chmod(0o644)
        try:
            notify.load_webhook()
            unsafe = False
        except notify.NotificationConfigError:
            unsafe = True
        check(unsafe, "group-readable webhook files fail closed")
        path.chmod(0o600)

        captured = {}

        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b"ok"

        def opener(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))["text"]
            captured["timeout"] = timeout
            return Response()

        result = notify.send("Howdy 🌾", opener=opener)
        check(result["ok"] and notify.ATTRIBUTION in captured["body"],
              "every delivered message carries Glean Desktop attribution")
        check("secret" not in json.dumps(result), "delivery result never exposes the webhook")
    finally:
        if old_file is None:
            os.environ.pop("FARM_SLACK_WEBHOOK_FILE", None)
        else:
            os.environ["FARM_SLACK_WEBHOOK_FILE"] = old_file
        if old_url is not None:
            os.environ["FARM_SLACK_WEBHOOK_URL"] = old_url

for bad in ("http://hooks.slack.com/services/T/B/X", "https://example.com/services/T/B/X"):
    try:
        notify._validate_url(bad)
        rejected = False
    except notify.NotificationConfigError:
        rejected = True
    check(rejected, "non-Slack or non-HTTPS webhook is rejected", bad)

bounded = mcp.Client(endpoint="https://example.invalid/mcp", timeout=12, retries=1)
check(bounded._timeout == 12 and bounded._retries == 1,
      "outage probes can use a short single-attempt transport budget")

# Pure outage state machine ----------------------------------------------------
local_ok = {"ok": True, "checks": {}}
local_bad = {"ok": False, "checks": {"cycle": {"ok": False}}}
latest = {"run": 9, "animals": 100, "max_hunger": 0, "produce": 1000}
state, event = outage_notifier.decide({}, local_bad, {"ok": False}, latest, "2026-08-27T00:00:00Z")
check(state["status"] == "local_issue" and event is None,
      "a local setup fault can never be announced as an external outage")
open_incident = {"status": "outage", "announced": True, "outage_notification_claimed": True, "incident_id": "farm-open"}
held_open, event = outage_notifier.decide(open_incident, local_bad, {"ok": False}, latest, "2026-08-27T00:01:00Z")
check(held_open["status"] == "outage" and held_open["announced"] and event is None,
      "a local verifier fault cannot silently close an announced incident")
pending_incident = {"status": "outage", "announced": False, "outage_notification_claimed": True, "incident_id": "farm-pending"}
held_pending, event = outage_notifier.decide(pending_incident, local_bad, {"ok": False}, latest, "2026-08-27T00:02:00Z")
check(held_pending["status"] == "outage" and held_pending["outage_notification_claimed"] and event is None,
      "an ambiguous in-flight John mention survives a local verifier fault without retry")
retryable_config = dict(
    pending_incident,
    delivery_error="no Slack webhook configured; write a mode-0600 secret",
)
released = outage_notifier.release_retryable_configuration_claim(retryable_config)
check(not released["outage_notification_claimed"] and released["delivery_error_kind"] == "configuration",
      "a proven pre-delivery configuration failure releases the incident for retry")
ambiguous = dict(pending_incident, delivery_error="Slack delivery failed: TimeoutError")
check(outage_notifier.release_retryable_configuration_claim(ambiguous)["outage_notification_claimed"],
      "an ambiguous network outcome stays claimed to prevent duplicate alerts")

state, event = outage_notifier.decide({}, local_ok, {"ok": False, "error": "down"}, latest, "2026-08-27T00:00:00Z")
check(state["status"] == "suspect" and event is None,
      "one failed remote check is only suspect")
state, event = outage_notifier.decide(state, local_ok, {"ok": False, "error": "down"}, latest, "2026-08-27T00:05:00Z")
check(state["status"] == "outage" and event == "outage" and state.get("incident_id"),
      "two failed remote checks confirm one incident")
claimed = outage_notifier.claim_outage_notification(state, "2026-08-27T00:05:01Z")
check(claimed["outage_notification_claimed"] and not claimed["announced"],
      "the sole John mention is claimed only at the configured delivery boundary")
claimed, event = outage_notifier.decide(claimed, local_ok, {"ok": False, "error": "still down"}, latest, "2026-08-27T00:10:00Z")
check(event is None and claimed["status"] == "outage",
      "an ambiguous claimed incident is never automatically posted again")
announced = dict(claimed, announced=True)
state, event = outage_notifier.decide(announced, local_ok, {"ok": True, "score": 1001, "rank": 1}, latest, "2026-08-27T00:15:00Z")
check(state["status"] == "healthy" and event == "recovered" and state.get("recovered_incident_id")
      and state["recovery_notification_pending"],
      "a successful probe keeps recovery delivery pending until acknowledged")
state, event = outage_notifier.decide(state, local_ok, {"ok": True, "score": 1002, "rank": 1}, latest, "2026-08-27T00:20:00Z")
check(state["status"] == "healthy" and event == "recovered" and state["recovery_notification_pending"],
      "an undelivered recovery is retried on the bounded notifier cadence")
state["recovery_notification_pending"] = False
state, event = outage_notifier.decide(state, local_ok, {"ok": True, "score": 1003, "rank": 1}, latest, "2026-08-27T00:25:00Z")
check(event is None, "an acknowledged recovery is not posted again")

flat = {"status": "healthy", "last_score": 1000, "flat_score_checks": 0}
flat, event = outage_notifier.decide(flat, local_ok, {"ok": True, "score": 1000, "rank": 1}, latest, "2026-08-27T00:00:00Z")
check(flat["status"] == "healthy" and event is None, "one flat score check does not page")
flat, event = outage_notifier.decide(flat, local_ok, {"ok": True, "score": 1000, "rank": 1}, latest, "2026-08-27T00:05:00Z")
check(flat["status"] == "outage" and event == "outage" and flat["outage_kind"] == "production_stall",
      "two healthy-herd flat score checks confirm a production outage")
starved = dict(latest, max_hunger=70)
held, event = outage_notifier.decide({"last_score": 1000}, local_ok, {"ok": True, "score": 1000, "rank": 1}, starved, "2026-08-27T00:00:00Z")
check(held["flat_score_checks"] == 0 and event is None,
      "local starvation is not blamed on an external production outage")

intel = {"outage_alert": {
    "status": "open", "channel_id": "C0BRMBGN7QA", "message_ts": "1787843044.139209",
    "john_mentioned": True, "kind": "transport", "observed_ts": "2026-08-27T15:04:04Z",
}}
adopted = outage_notifier.adopt_slack_intel({}, intel)
check(adopted["status"] == "outage" and adopted["announced"] and adopted["outage_notification_claimed"],
      "an existing Slack alert is adopted as already announced")
check(outage_notifier.adopt_slack_intel(dict(adopted, status="healthy"), intel)["status"] == "healthy",
      "a consumed Slack alert cannot be adopted twice")
outage_text = outage_notifier.outage_message(dict(adopted, local={"checks": {}}), latest)
recovery_text = outage_notifier.recovery_message({"last_score": 1001}, latest)
check(outage_text.count("<@%s>" % outage_notifier.JOHN_USER_ID) == 1,
      "the outage alert mentions John exactly once")
check("<@%s>" % outage_notifier.JOHN_USER_ID not in recovery_text,
      "recovery updates do not notify John again")

# EOD report ------------------------------------------------------------------
history = [
    {"ts": "2026-08-27T05:59:00Z", "run": 1, "rank": 2, "produce": 1000,
     "animals": 100, "feed": 3000, "max_hunger": 0,
     "rivals": {"John": 1400, "Neill": 800}},
    {"ts": "2026-08-27T18:00:00Z", "run": 2, "rank": 1, "produce": 1800,
     "animals": 120, "feed": 3600, "max_hunger": 20, "revenue": 500,
     "adopted": 20, "calls": 30, "rivals": {"John": 1600, "Neill": 1300}},
    {"ts": "2026-08-27T22:55:00Z", "run": 3, "rank": 1, "produce": 2600,
     "animals": 130, "feed": 3900, "max_hunger": 10, "revenue": 700,
     "adopted": 10, "calls": 25, "rivals": {"John": 1800, "Neill": 1700}},
]
heals = [
    {"class": "transport", "action": "call-rate ceiling 5 -> 4"},
    {"class": "relax", "action": "rate ceiling restored"},
    {"class": "strategy_stale", "action": "routed to autonomous question q-1"},
]
report = eod_report.build_report(history, heals, [{"event": "outage"}, {"event": "recovered"}], "2026-08-27")
for phrase in ("🌾", "🐓", "John", "Neill", "How the machinery tended itself", "rank #1", "Outage fence"):
    check(phrase in report, "EOD report includes %s" % phrase, report[:300])
check(notify.ATTRIBUTION not in report,
      "report builder stays channel-agnostic; delivery appends attribution once")

# Scheduling and supervision --------------------------------------------------
outage_plist = plistlib.loads((PROJECT / "deploy" / "com.nickfigura.farmfriends.outage.plist").read_bytes())
eod_plist = plistlib.loads((PROJECT / "deploy" / "com.nickfigura.farmfriends.eod.plist").read_bytes())
check(outage_plist["StartInterval"] == 300, "outage guard runs every five minutes")
check(eod_plist["StartCalendarInterval"] == {"Hour": 17, "Minute": 0},
      "sundown report runs at 5 PM local Mountain Time")
labels = {item["label"] for item in control.SERVICES}
check("com.nickfigura.farmfriends.outage" in labels and "com.nickfigura.farmfriends.eod" in labels,
      "both jobs are in the authoritative supervised service registry")
check(len(list((PROJECT / "deploy").glob("com.nickfigura.farmfriends*.plist"))) == 11,
      "all eleven LaunchAgent plists are present")
check(outage_notifier.CHANNEL_ID == eod_report.CHANNEL_ID == "C0BRMBGN7QA",
      "both jobs target the discovered Farm Friends channel")

if failures:
    print("\nNOTIFICATION TEST FAILED: %d of %d" % (len(failures), checks))
    raise SystemExit(1)
print("\nNOTIFICATION TEST PASSED: %d checks" % checks)
