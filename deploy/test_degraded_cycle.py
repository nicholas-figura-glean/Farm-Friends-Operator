#!/usr/bin/env python3
"""Regression tests for domain-scoped survival of leaderboard failures."""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import cycle, growth, mcp, parse, progress, questions  # noqa: E402

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


# A per-call budget must stay client-side and cap only this request.
with tempfile.TemporaryDirectory() as tmp:
    old_log = os.environ.get("FARM_TOOL_CALL_LOG")
    os.environ["FARM_TOOL_CALL_LOG"] = str(Path(tmp) / "calls.ndjson")
    old_urlopen = mcp.urllib.request.urlopen
    attempts = []

    def fail_504(request, timeout, context):
        attempts.append({"payload": json.loads(request.data), "timeout": timeout})
        raise urllib.error.HTTPError(request.full_url, 504, "Gateway Timeout", {}, None)

    mcp.urllib.request.urlopen = fail_504
    try:
        client = mcp.Client(endpoint="https://example.invalid/mcp/token", timeout=120, retries=3)
        try:
            client.call(
                "leaderboard", _transport_timeout=7, _transport_retries=1
            )
            bounded_error = ""
        except mcp.McpError as exc:
            bounded_error = str(exc)
        check(len(attempts) == 1 and attempts[0]["timeout"] == 7,
              "leaderboard can use one bounded HTTP attempt")
        arguments = attempts[0]["payload"]["params"]["arguments"]
        check(arguments == {}, "transport-budget controls never leak into MCP tool arguments", arguments)
        check("after 1 tries" in bounded_error,
              "bounded failure reports its actual one-attempt budget", bounded_error)
    finally:
        mcp.urllib.request.urlopen = old_urlopen
        if old_log is None:
            os.environ.pop("FARM_TOOL_CALL_LOG", None)
        else:
            os.environ["FARM_TOOL_CALL_LOG"] = old_log


class FailedBoardClient:
    def __init__(self):
        self.call_count = 0
        self.transport_errors = 0
        self.transport_errors_by_tool = {}
        self.last_service_seconds = 0.0
        self.board_calls = []

    def tool_names(self):
        return ["collect_produce", "farm_events", "feed_animals", "leaderboard", "list_farm", "sell"]

    def call(self, tool, **arguments):
        self.call_count += 1
        if tool != "leaderboard":
            raise AssertionError("unexpected direct call: %s" % tool)
        self.board_calls.append(dict(arguments))
        self.transport_errors += 1
        self.transport_errors_by_tool[tool] = self.transport_errors_by_tool.get(tool, 0) + 1
        raise mcp.McpError("transport failure after 1 tries: HTTP 504")


farm = parse.Farm(
    coins=0,
    animals=[parse.Animal(id=1, name="Hen", kind="chicken", mood="content", hunger=0, happiness=100, ready=0)],
    plots=[],
    inventory={"feed": 100},
    trades=[],
)

# Availability holds must preserve the last rival baseline and block only strategy.
client = FailedBoardClient()
runner = cycle.Cycle(client)
runner.prev = {
    "run": 8,
    "ts": "2026-08-28T00:00:00Z",
    "rival_herds": {"John": 123},
    "rival_coins": {"John": 456},
}
runner.meta["novelty"] = {
    "initialized": True,
    "players": ["John"],
    "tools": ["leaderboard"],
    "blocks": {},
}
runner.board_error = "HTTP 504"
old_questions = questions.load_all
questions.load_all = lambda: []
try:
    assessed = runner.assess_novelty(
        9, ["leaderboard"], farm, [], board_available=False
    )
finally:
    questions.load_all = old_questions
check(set(cycle.LEADERBOARD_HOLD_DOMAINS).issubset(assessed["blocked_domains"]),
      "leaderboard loss holds adoption, offers, and trades")
check(any(block.get("class") == "leaderboard_unavailable" for block in assessed["active_blocks"]),
      "availability hold is visible in adaptive activity")
check(runner.meta["novelty"].get("players") == ["John"],
      "missing standings preserve rather than erase the rival baseline")


# Exercise the orchestration twice: once in a blind window (the original bug),
# and once on an ordinary cycle. Routine care must finish in both cases.
def run_degraded_case(blind_window):
    order = []
    client = FailedBoardClient()
    runner = cycle.Cycle(client)
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now - datetime.timedelta(hours=2) if blind_window else now
    runner.prev = {
        "run": 8,
        "ts": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "collect_ts": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    runner.meta = {"run": 8, "tools": client.tool_names(), "declines": {}, "offers": {}}

    runner.collect = lambda: order.append("collect")
    runner.read_state = lambda tag: (order.append("read:" + tag) or farm)
    runner.ensure_feed_on_hand = lambda value: value
    runner.feed_if_needed = lambda value, run_no: (order.append("feed") or value)
    runner.read_risk_events = lambda: order.append("events")

    def assess(*args, **kwargs):
        order.append("novelty")
        runner.novelty = {
            "signals": [],
            "active_blocks": [{"class": "leaderboard_unavailable"}],
            "blocked_domains": list(cycle.LEADERBOARD_HOLD_DOMAINS),
        }
        return runner.novelty

    runner.assess_novelty = assess
    runner.sell_all = lambda inventory: order.append("sell")
    runner._finish = lambda started, tools, state, board, final, plan: {
        "run": 9,
        "rank": None,
        "produce": None,
        "board": list(board),
        "plan": dict(plan),
        "order": list(order),
    }

    old_growth = growth.decide
    old_progress = {name: getattr(progress, name) for name in ("begin", "start", "fail", "done", "skip")}
    growth.decide = lambda *args, **kwargs: {
        "cap": 0,
        "verdict": {"saturated": False},
        "changed": False,
        "reason": "test",
    }
    for name in old_progress:
        setattr(progress, name, lambda *args, **kwargs: None)
    try:
        row = runner._run_impl()
    finally:
        growth.decide = old_growth
        for name, value in old_progress.items():
            setattr(progress, name, value)
    return runner, client, row, order


for blind in (True, False):
    runner, client, row, order = run_degraded_case(blind)
    label = "blind-window" if blind else "ordinary"
    check(client.call_count == 1 and len(client.board_calls) == 1,
          "%s cycle attempts leaderboard exactly once" % label, client.board_calls)
    check(client.board_calls[0].get("_transport_retries") == 1,
          "%s leaderboard call disables retry fan-out" % label, client.board_calls)
    check(all(name in order for name in ("collect", "feed", "events", "novelty", "sell")),
          "%s leaderboard failure does not block routine care" % label, order)
    check(row["rank"] is None and row["produce"] is None and row["board"] == [],
          "%s degraded row reports unknown standings honestly" % label, row)
    check(set(cycle.LEADERBOARD_HOLD_DOMAINS).issubset(runner.novelty["blocked_domains"]),
          "%s degraded cycle retains strategy holds" % label)

if failures:
    print("\nDEGRADED CYCLE TEST FAILED: %d of %d" % (len(failures), checks))
    raise SystemExit(1)
print("\nDEGRADED CYCLE TEST PASSED: %d checks" % checks)
