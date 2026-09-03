#!/usr/bin/env python3
"""Contract watcher and work-order queue suite.

Covers the seam where detection turns into permission-to-edit-code. Two things
must hold: an actionable change reliably produces exactly one work order, and a
non-actionable change never produces any. Both directions are load-bearing --
the first is the self-healing property, the second is what keeps the author agent
from being woken (and billed) by ordinary game noise.

The watcher's `main()` is exercised against a temp state directory with a stubbed
MCP client, so these tests make no network calls and never touch real state.
"""

import copy
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)
sys.path.insert(0, os.path.join(PROJECT, "experiments"))

from farm import contract, journal, workorders  # noqa: E402

FAILURES = []
CHECKS = [0]


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def section(name):
    print("\n== %s" % name)


# -- work order queue -------------------------------------------------------

section("work order queue lifecycle")

tmp = tempfile.mkdtemp()
queue = os.path.join(tmp, "workorders.ndjson")

change = {
    "id": "abc123",
    "severity": "breaking",
    "kind": "required_arg_added",
    "tool": "feed_animals",
    "summary": "feed_animals now requires batch_id",
    "we_use_it": True,
    "sites": ["farm/cycle.py:357"],
    "detail": {"args": ["batch_id"]},
}

first = workorders.submit(change, "contract_watch", "fix it", ["works"], ["farm/cycle.py"], path=queue)
check("an order is filed", first and first["status"] == workorders.OPEN)
check("it retains the detected severity", first["severity"] == "breaking")

again = workorders.submit(change, "contract_watch", "fix it", ["works"], ["farm/cycle.py"], path=queue)
check("re-detecting the same drift does not duplicate the order", again is None)
check("the queue still holds one order", len(workorders.open_orders(queue)) == 1)

claimed = workorders.claim("abc123", "author_agent", run=42, path=queue)
check("an order can be claimed", claimed and claimed["status"] == workorders.CLAIMED)
check("claiming counts an attempt", claimed["attempts"] == 1)
check("claiming retains immutable submission age",
      claimed.get("created_ts") == first.get("created_ts"), str(claimed))
check("a claimed order leaves the open queue", workorders.open_orders(queue) == [])

workorders.resolve(
    "abc123", workorders.PUBLISHED, note="shipped", release="20260825T1", path=queue,
    expected_status=workorders.CLAIMED, expected_claim_token=claimed.get("claim_token"),
)
cur = workorders.current(queue)["abc123"]
check("an order can be published", cur["status"] == workorders.PUBLISHED)
check("the publishing release is recorded", cur["release"] == "20260825T1")

section("queue ordering and exhaustion")

queue2 = os.path.join(tmp, "q2.ndjson")
for cid, sev in (("c1", "opportunity"), ("c2", "breaking"),
                 ("c3", "shape"), ("c4", "degraded")):
    workorders.submit(dict(change, id=cid, severity=sev), "contract_watch", "i", [], [], path=queue2)
order = workorders.next_order(queue2)
check("breaking work is served first", order and order["id"] == "c2", str(order and order["id"]))
check(
    "degraded repair precedes shape and opportunity work",
    [o["severity"] for o in workorders.open_orders(queue2)]
    == ["breaking", "degraded", "shape", "opportunity"],
    str([o["severity"] for o in workorders.open_orders(queue2)]),
)

queue_age = os.path.join(tmp, "q-age.ndjson")
with open(queue_age, "w", encoding="utf-8") as handle:
    for row in (
        {"id": "old", "status": "open", "severity": "opportunity",
         "ts": "2020-01-01T00:00:00Z"},
        {"id": "new", "status": "open", "severity": "opportunity",
         "ts": "2021-01-01T00:00:00Z"},
        {"id": "old", "status": "open", "severity": "opportunity",
         "ts": "2022-01-01T00:00:00Z", "note": "returned after a claim"},
    ):
        handle.write(json.dumps(row) + "\n")
aged = workorders.open_orders(queue_age)
check("queue events do not move older same-severity work behind newer work",
      [o["id"] for o in aged] == ["old", "new"], str(aged))
check("legacy orders derive immutable age from their first event",
      aged[0].get("created_ts") == "2020-01-01T00:00:00Z", str(aged[0]))

queue3 = os.path.join(tmp, "q3.ndjson")
workorders.submit(change, "contract_watch", "i", [], [], path=queue3)
for _ in range(workorders.MAX_ATTEMPTS):
    failed_claim = workorders.claim("abc123", "author_agent", path=queue3)
    workorders.resolve(
        "abc123", workorders.FAILED, note="gate failed", path=queue3,
        retryable=True,
        expected_status=workorders.CLAIMED,
        expected_claim_token=(failed_claim or {}).get("claim_token"),
    )
check(
    "an order that keeps failing stops being refiled",
    workorders.submit(change, "contract_watch", "i", [], [], path=queue3) is None,
)
legacy_failed_queue = os.path.join(tmp, "legacy-failed.ndjson")
with open(legacy_failed_queue, "w", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "id": "abc123", "status": workorders.FAILED, "attempts": 1,
        "severity": "breaking", "ts": "2020-01-01T00:00:00Z",
    }) + "\n")
check("legacy failures are historical until a current detector reopens them",
      workorders.open_orders(legacy_failed_queue) == [])
reopened_failure = workorders.submit(
    change, "contract_watch", "still observed", [], [], path=legacy_failed_queue,
)
check("current evidence reopens a bounded historical failure",
      reopened_failure and reopened_failure["status"] == workorders.OPEN
      and reopened_failure["attempts"] == 1, str(reopened_failure))

section("abandoned claims return to the queue")

queue4 = os.path.join(tmp, "q4.ndjson")
workorders.submit(change, "contract_watch", "i", [], [], path=queue4)
workorders.claim("abc123", "author_agent", path=queue4)
check("a fresh claim is not stale", workorders.stale_claims(3600, queue4) == [])
check("an old claim is detected as stale", len(workorders.stale_claims(-1, queue4)) == 1)
workorders.release_stale(-1, queue4)
check("a stale claim is returned to open", len(workorders.open_orders(queue4)) == 1)

legacy_queue = os.path.join(tmp, "legacy-claim.ndjson")
with open(legacy_queue, "w", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "id": "legacy-claimed", "status": workorders.CLAIMED,
        "severity": "breaking", "attempts": 1,
        "ts": "2020-01-01T00:00:00Z", "created_ts": "2020-01-01T00:00:00Z",
    }) + "\n")
legacy_released = workorders.release_stale(0, legacy_queue)
check("pre-token stale claims migrate back to the actionable queue",
      len(legacy_released) == 1
      and legacy_released[0].get("legacy_tokenless_claim") is True
      and workorders.open_orders(legacy_queue)[0]["id"] == "legacy-claimed",
      str(legacy_released))


# -- intent generation ------------------------------------------------------

section("every actionable change yields an instruction")

import contract_watch  # noqa: E402

section("contract orders reconcile with current drift")
queue5 = os.path.join(tmp, "q5.ndjson")
for stale in (
    dict(change, id="shape-old-a", kind="response_templates_changed", tool="list_farm", severity="shape"),
    dict(change, id="shape-old-b", kind="response_templates_changed", tool="list_farm", severity="shape"),
    dict(change, id="gone-tool", kind="tool_removed", tool="old_tool", severity="breaking"),
):
    workorders.submit(stale, "contract_watch", "i", [], [], path=queue5)
current_change = dict(change, id="shape-current", kind="response_templates_changed",
                      tool="list_farm", severity="shape")
workorders.submit(current_change, "contract_watch", "i", [], [], path=queue5)
retired = contract_watch.reconcile_orders([current_change], [current_change], queue5)
check("superseded variants and disappeared drift are retired", len(retired) == 3, str(retired))
check("the exact current contract order stays open",
      [o["id"] for o in workorders.open_orders(queue5)] == ["shape-current"],
      str([o["id"] for o in workorders.open_orders(queue5)]))
queue6 = os.path.join(tmp, "q6.ndjson")
old_variant = dict(change, id="symbols-old", kind="response_symbols_changed",
                   tool="list_farm", severity="shape")
current_variant = dict(old_variant, id="symbols-current")
for variant in (old_variant, current_variant):
    workorders.submit(variant, "contract_watch", "i", [], [], path=queue6)
retired = contract_watch.reconcile_orders([current_variant], [], queue6)
check("an exact observed order compacts unconfirmed historical variants",
      len(retired) == 1 and [o["id"] for o in workorders.open_orders(queue6)] == ["symbols-current"],
      str(workorders.current(queue6)))
queue7 = os.path.join(tmp, "q7.ndjson")
workorders.submit(current_variant, "contract_watch", "i", [], [], path=queue7)
settled = contract_watch.reconcile_orders([current_variant], [], queue7, settled=[current_variant])
check("a parser-accepted current shape retires its exact open order",
      len(settled) == 1 and workorders.open_orders(queue7) == [], str(workorders.current(queue7)))

with tempfile.TemporaryDirectory() as raw_tmp:
    raw = pathlib.Path(raw_tmp)
    leaderboard_sample = raw / "leaderboard.txt"
    leaderboard_sample.write_text(
        "Leaderboard\n1. Nick: 100 produce, 5 animals, 20 coins\n",
        encoding="utf-8",
    )
    leaderboard_change = {"tool": "leaderboard", "kind": "response_templates_changed"}
    accepted = contract_watch.shape_parser_verdict(leaderboard_change, raw)
    check("a confirmed shape that the live parser accepts needs no repair",
          accepted.get("accepted") is True and accepted.get("consumed") is True, str(accepted))
    leaderboard_sample.write_text("Leaderboard\nthis is not a leaderboard row\n", encoding="utf-8")
    rejected = contract_watch.shape_parser_verdict(leaderboard_change, raw)
    check("a confirmed shape that breaks the live parser remains actionable",
          rejected.get("accepted") is False and "ParseDrift" in rejected.get("reason", ""), str(rejected))
    ignored = contract_watch.shape_parser_verdict(
        {"tool": "feed_animals", "kind": "response_templates_changed"}, raw)
    check("response text with no runtime consumer cannot wake the author",
          ignored.get("accepted") is True and ignored.get("consumed") is False, str(ignored))

# The real alert ledger, captured before any redirection, so the suite can prove
# it never wrote its fabricated breaking change into live operations.
REAL_ALERTS = journal.ALERTS
REAL_ALERTS_SIZE = os.path.getsize(REAL_ALERTS) if os.path.exists(REAL_ALERTS) else 0

cases = [
    ("required_arg_added", {"args": ["batch_id"], "we_pass": ["animal_id"]}),
    ("arg_removed", {"arg": "animal_id", "rename_candidate": "id"}),
    ("arg_removed", {"arg": "animal_id", "rename_candidate": None}),
    ("arg_type_changed", {"arg": "qty", "from": "integer", "to": "string"}),
    ("enum_values_removed", {"arg": "kind", "removed": ["cow"]}),
    ("tool_removed", {}),
    ("tool_added", {"required": ["item"]}),
    ("enum_values_added", {"arg": "kind", "added": ["alpaca"]}),
    ("response_templates_changed", {"removed": ["a"], "added": ["b"]}),
    ("response_numeric_labels_changed", {"removed": ["hunger"], "added": ["fullness"]}),
]
for kind, detail in cases:
    built = contract_watch.order_for({
        "kind": kind, "tool": "feed_animals", "severity": "breaking",
        "summary": "s", "sites": ["farm/cycle.py:357"], "detail": detail,
    })
    check("%s produces an intent" % kind, built is not None and bool(built[0]))
    if built:
        check("%s defines acceptance criteria" % kind, bool(built[1]), str(built))

check(
    "an unrecognized change kind yields no order rather than a vague one",
    contract_watch.order_for({"kind": "something_new", "tool": "x", "detail": {}}) is None,
)


# -- end to end -------------------------------------------------------------

section("watcher end to end against a stubbed server")


class StubClient(object):
    """Returns whatever tools/list payload the test sets."""

    payload = []

    def rpc(self, method, params=None):
        return {"tools": copy.deepcopy(StubClient.payload)}


BASE_TOOLS = [
    {
        "name": "feed_animals",
        "description": "Feed animals.",
        "inputSchema": {"type": "object", "properties": {"animal_id": {"type": "string"}}, "required": []},
    },
    {
        "name": "leaderboard",
        "description": "Show the leaderboard.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def fresh_env():
    """A temp project dir with its own state/, wired into the watcher module."""
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "state", "raw", "latest"), exist_ok=True)
    # A minimal source tree so reliance() sees a real call site for feed_animals.
    os.makedirs(os.path.join(root, "farm"), exist_ok=True)
    with open(os.path.join(root, "farm", "cycle.py"), "w") as handle:
        handle.write("def go(c):\n    return c.call('feed_animals', animal_id='all')\n")
    with open(os.path.join(root, "state", "raw", "latest", "leaderboard.txt"), "w") as handle:
        handle.write("Nick: 100 produce, 5 animals, 20 coins\nJohn: 90 produce, 4 animals, 10 coins\n")

    contract_watch.PROJECT = pathlib.Path(root)
    contract_watch.STATE = contract_watch.PROJECT / "state"
    contract_watch.STORE = contract_watch.STATE / "contract_watch.json"
    contract_watch.LOCK = contract_watch.STATE / ".contract-watch.lock"
    contract_watch.Client = StubClient

    # Redirect the shared alert ledger too.
    #
    # This suite feeds the watcher a fabricated breaking change ("feed_animals now
    # requires batch_id"). On detecting breaking drift the watcher calls
    # journal.record_alerts, which appends to a module-level ALERTS constant --
    # the real state/alerts.ndjson, regardless of where the watcher's own state was
    # pointed. Every run of this suite therefore wrote a fictional breaking change
    # into live operations, and the 60s supervisor dutifully escalated it with
    # needs_llm: true. 34 such entries accumulated before this was caught, each one
    # inviting a paid escalation for a problem that never existed and making a
    # genuine breaking change harder to see.
    #
    # Redirecting the module constant is the fix; the assertion below is what stops
    # it regressing.
    journal.ALERTS = os.path.join(root, "state", "alerts.ndjson")
    return root


root = fresh_env()
StubClient.payload = BASE_TOOLS

code = contract_watch.main()
check("first scan establishes a baseline and reports nothing", code == 0, "exit=%s" % code)
check("a baseline file is written", os.path.exists(os.path.join(root, "state", "contract.json")))

code = contract_watch.main()
check("an unchanged server produces no work", code == 0, "exit=%s" % code)
check("no orders are queued for a quiet server",
      workorders.open_orders(os.path.join(root, "state", "workorders.ndjson")) == [])

# A breaking change: feed_animals gains a required argument.
breaking = copy.deepcopy(BASE_TOOLS)
breaking[0]["inputSchema"]["required"] = ["batch_id"]
breaking[0]["inputSchema"]["properties"]["batch_id"] = {"type": "string"}
StubClient.payload = breaking

code = contract_watch.main()
queue_path = os.path.join(root, "state", "workorders.ndjson")
orders = workorders.open_orders(queue_path)
check("breaking drift exits 0 after autonomous routing", code == 0, "exit=%s" % code)
check("breaking drift files exactly one order", len(orders) == 1, str(orders))
if orders:
    check("the order is marked breaking", orders[0]["severity"] == "breaking")
    check("the order names the file to change", "farm/cycle.py" in orders[0]["files"], str(orders[0]["files"]))
    check("the order carries acceptance criteria", bool(orders[0]["acceptance"]))

code = contract_watch.main()
check("re-scanning does not pile up duplicate orders",
      len(workorders.open_orders(queue_path)) == 1, str(workorders.open_orders(queue_path)))
check("unfixed breaking drift remains headless on repeat scans", code == 0, "exit=%s" % code)

baseline_now = contract.load_baseline(os.path.join(root, "state", "contract.json"))
check(
    "the baseline stays pinned while the fix is outstanding",
    "batch_id" not in (baseline_now["tools"]["feed_animals"]["args"] or {}),
    "baseline absorbed the break, which would hide it",
)
if orders:
    workorders.resolve(orders[0]["id"], workorders.PUBLISHED,
                       note="full release matrix passed", release="test-rev", path=queue_path)
    code = contract_watch.main()
    advanced = contract.load_baseline(os.path.join(root, "state", "contract.json"))
    check("a published exact repair advances the pinned baseline", code == 0 and
          "batch_id" in (advanced["tools"]["feed_animals"]["args"] or {}), str(advanced))
    check("the repaired contract no longer leaves an open order",
          workorders.open_orders(queue_path) == [], str(workorders.open_orders(queue_path)))

# A cosmetic change on a quiet server should be absorbed, not queued.
root = fresh_env()
StubClient.payload = BASE_TOOLS
contract_watch.main()
reworded = copy.deepcopy(BASE_TOOLS)
reworded[0]["description"] = "Feed all the animals on your farm."
StubClient.payload = reworded
code = contract_watch.main()
queue_path = os.path.join(root, "state", "workorders.ndjson")
check("a reworded description exits 0", code == 0, "exit=%s" % code)
check("a reworded description queues no work", workorders.open_orders(queue_path) == [])
absorbed = contract.load_baseline(os.path.join(root, "state", "contract.json"))
check(
    "a cosmetic change is absorbed into the baseline",
    absorbed["tools"]["feed_animals"]["description_sha"]
    == contract.normalize_tools(reworded)["feed_animals"]["description_sha"],
)
code = contract_watch.main()
check("an absorbed change stops being reported", code == 0, "exit=%s" % code)

# A parser-rejected response is direct breakage evidence and cannot wait for a
# second 15-minute shape scan.
root = fresh_env()
valid_farm = (
    "🌾 Nick's Farm  🪙 10 coins\n\nAnimals:\n"
    "  🐔 Hen the chicken (#1) is content. hunger 0/100, happiness 100/100\n\n"
    "Fields:\n  🌼 wildflowers plot (#1): in full bloom\n\n"
    "Barn inventory: 🌱 feed x30\n\nOpen trades:\n  none\n"
)
farm_sample = pathlib.Path(root) / "state" / "raw" / "latest" / "list_farm_state.txt"
farm_sample.write_text(valid_farm, encoding="utf-8")
StubClient.payload = BASE_TOOLS
contract_watch.main()
farm_sample.write_text(
    "🌾 Nick's Farm  🪙 10 coins\n\nCreatures changed:\n  unparseable\n",
    encoding="utf-8",
)
code = contract_watch.main()
queue_path = os.path.join(root, "state", "workorders.ndjson")
orders = workorders.open_orders(queue_path)
check("a parser-rejected first shape scan routes immediately",
      code == 0 and len(orders) == 1, str(orders))
if orders:
    check("shape repair targets only the format adapter",
          orders[0].get("files") == ["farm/format_compat.py"], str(orders[0].get("files")))
    check("shape repair carries compatibility provenance",
          (orders[0].get("provenance") or {}).get("change_class") == "compatibility",
          str(orders[0].get("provenance")))
    captured = (orders[0].get("detail") or {}).get("captured_sample") or {}
    check("shape repair carries bounded structural evidence",
          "Creatures changed" in str(captured.get("structural_excerpt") or ""),
          str(captured))

# An opportunity: a brand new tool should be probed, not wired in.
root = fresh_env()
StubClient.payload = BASE_TOOLS
contract_watch.main()
added = BASE_TOOLS + [{
    "name": "auction",
    "description": "Bid on livestock.",
    "inputSchema": {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]},
}]
StubClient.payload = added
contract_watch.main()
queue_path = os.path.join(root, "state", "workorders.ndjson")
orders = workorders.open_orders(queue_path)
check("a new tool becomes an opportunity order",
      len(orders) == 1 and orders[0]["severity"] == "opportunity", str(orders))
if orders:
    check("the new-tool order forbids touching the live cycle",
          any("cycle.py is unchanged" in a for a in orders[0]["acceptance"]), str(orders[0]["acceptance"]))
code = contract_watch.main()
check("the safe additive contract is absorbed after filing its research task", code == 0,
      "exit=%s" % code)
check("absorbing a safe capability does not erase its durable research order",
      len(workorders.open_orders(queue_path)) == 1
      and workorders.open_orders(queue_path)[0]["severity"] == "opportunity",
      str(workorders.open_orders(queue_path)))

# A directly documented mandatory mechanic is implementation work, not an inert
# generic probe. It still cannot edit the protected cycle or executor.
root = fresh_env()
StubClient.payload = BASE_TOOLS
contract_watch.main()
prestige_tool = {
    "name": "prestige",
    "description": (
        "Retire your herd to bank the league. The board ranks by league FIRST, "
        "so prestiging is the only way to outrank a higher league. Lifetime produce is kept."
    ),
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}
StubClient.payload = BASE_TOOLS + [prestige_tool]
contract_watch.main()
queue_path = os.path.join(root, "state", "workorders.ndjson")
orders = workorders.open_orders(queue_path)
check("direct progression drift creates one implementation order",
      len(orders) == 1 and orders[0]["kind"] == "capability_policy", str(orders))
if orders:
    check("direct progression jumps speculative probe work",
          orders[0]["severity"] == "degraded", str(orders[0]))
    check("direct progression targets only the literal policy surface",
          orders[0]["files"] == ["experiments/capability_policies.py"], str(orders[0]["files"]))
    check("direct progression is not described as a probe",
          any("enabled literal capability policy" in item for item in orders[0]["acceptance"]),
          str(orders[0]["acceptance"]))

shutil.rmtree(tmp, ignore_errors=True)

section("the suite does not touch live operations")
# Regression guard for a real incident: this suite leaked 34 fictional
# "feed_animals now requires batch_id" alerts into state/alerts.ndjson, and the
# supervisor escalated every one of them with needs_llm: true.
now_size = os.path.getsize(REAL_ALERTS) if os.path.exists(REAL_ALERTS) else 0
check("the real alert ledger was not appended to", now_size == REAL_ALERTS_SIZE,
      "%d -> %d bytes" % (REAL_ALERTS_SIZE, now_size))
check("journal.ALERTS was redirected away from the real ledger",
      str(journal.ALERTS) != str(REAL_ALERTS), str(journal.ALERTS))
if os.path.exists(REAL_ALERTS):
    with open(REAL_ALERTS) as _fh:
        check("no fabricated batch_id alert reached the real ledger",
              "batch_id" not in _fh.read())

print("\n%d checks, %d failures" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for name in FAILURES:
        print("  failed: %s" % name)
    sys.exit(1)
print("contract watch suite passed")
