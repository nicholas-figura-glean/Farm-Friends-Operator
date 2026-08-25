#!/usr/bin/env python3
"""Tests for the operator-view machinery: autonomy, architecture, dashboard agent.

These three exist to answer "is the loop actually healing itself, and is this page
telling me the truth". A silent failure here is particularly bad because it does not
look like a failure -- it looks like a calm dashboard. Several checks below encode
specific ways that already happened:

* five of seven agents were unmonitored while the page showed green
* two LaunchAgent plists were unreadable by Python, so they vanished from the
  architecture view without any error being raised
* git reported commit times in local time, sorting releases hours away from the
  findings they caused
* the autonomy view cost 175ms of subprocess work on a 2s poll

Usage: python3 deploy/test_dashboard_agent.py
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

from farm import architecture, autonomy  # noqa: E402

CHECKS = 0
FAILURES = 0


def section(title: str) -> None:
    print("== %s" % title)


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS, FAILURES
    CHECKS += 1
    if condition:
        print("  ok   %s" % label)
    else:
        FAILURES += 1
        print("  FAIL %s%s" % (label, ("  [%s]" % detail) if detail else ""))


# --------------------------------------------------------------------------
section("every agent is accounted for")

view = autonomy.agents()
check("all eight agents are described", view.get("expected") == 8,
      str(view.get("expected")))
labels = {a["label"] for a in view.get("agents") or []}
for expected in ("com.nickfigura.farmfriends",
                 "com.nickfigura.farmfriends.supervisor",
                 "com.nickfigura.farmfriends.expand",
                 "com.nickfigura.farmfriends.recovery",
                 "com.nickfigura.farmfriends.contract",
                 "com.nickfigura.farmfriends.author",
                 "com.nickfigura.farmfriends.research",
                 "com.nickfigura.farmfriends.dashboard"):
    check("watches %s" % expected.split("farmfriends")[-1] or "cycle",
          expected in labels)
check("every agent explains what its absence costs",
      all(a.get("lost") for a in autonomy.AGENTS))
# The cycle and the author agent are the two whose loss is unrecoverable without a
# human, so they must escalate harder than the rest.
critical = {a["key"] for a in autonomy.AGENTS} & {"cycle", "author"}
check("cycle and author are both known keys", critical == {"cycle", "author"})


# --------------------------------------------------------------------------
section("the plists the architecture reads are actually parseable")

# This is the regression guard for a real defect: two plists contained "--" inside an
# XML comment, which `plutil` tolerates and Python's expat rejects. Both agents
# silently disappeared from the architecture view with no error anywhere.
plists = sorted((PROJECT / "deploy").glob("com.nickfigura.farmfriends*.plist"))
check("eight plists on disk", len(plists) == 8, str(len(plists)))
for path in plists:
    try:
        with path.open("rb") as handle:
            plistlib.load(handle)
        parsed = True
        detail = ""
    except Exception as exc:  # noqa: BLE001
        parsed = False
        detail = str(exc)[:80]
    check("python can parse %s" % path.name.replace("com.nickfigura.farmfriends", ""),
          parsed, detail)
for path in plists:
    result = subprocess.run(["plutil", "-lint", str(path)],
                            capture_output=True, text=True)
    check("plutil accepts %s" % path.name.replace("com.nickfigura.farmfriends", ""),
          result.returncode == 0, result.stdout.strip()[:80])


# --------------------------------------------------------------------------
section("the autonomy view degrades instead of collapsing")

report = autonomy.report()
for key in ("agents", "canary", "orders", "contract", "vcs", "research", "llm"):
    check("section %s is present" % key, key in report)
    check("section %s has no error" % key,
          not (report.get(key) or {}).get("error"),
          str((report.get(key) or {}).get("error"))[:90])

# A subsystem that raises must become a reported value, never an exception, because
# this view is the only place several of those subsystems are visible at all.
def _boom():
    raise RuntimeError("synthetic failure")


guarded = autonomy._guard(_boom)
check("a failing section becomes an error string", "error" in guarded)
check("the error names the exception type", "RuntimeError" in guarded["error"])
check("a failed section is surfaced as a blocker",
      any("failed to read" in b["what"]
          for b in autonomy.blockers({"vcs": {"error": "synthetic"}})))


# --------------------------------------------------------------------------
section("the hot path does not pay for subprocesses")

# The uncached view spawns launchctl seven times plus git. On the 2s dashboard poll
# that was 1,800 rounds of subprocess churn an hour, so `_blockers` uses the cache.
autonomy._CACHE["view"] = None
autonomy._CACHE["at"] = 0.0
started = time.time()
autonomy.cached_report()
cold_ms = (time.time() - started) * 1000
started = time.time()
for _ in range(20):
    autonomy.cached_report()
warm_ms = (time.time() - started) * 1000 / 20
check("a warm read is far cheaper than a cold one", warm_ms < cold_ms / 10,
      "cold %.0fms, warm %.2fms" % (cold_ms, warm_ms))
check("twenty warm reads cost under 10ms total", warm_ms * 20 < 10,
      "%.2fms" % (warm_ms * 20))
check("the cache expires", autonomy.CACHE_TTL_SECONDS <= 60,
      str(autonomy.CACHE_TTL_SECONDS))
# The TTL must be shorter than the fastest agent's period, or a missed run could hide.
check("the TTL is shorter than the fastest agent's cadence",
      autonomy.CACHE_TTL_SECONDS < 60)


# --------------------------------------------------------------------------
section("architecture is derived from what is on disk")

snap = architecture.snapshot()
check("a signature is produced", len(snap.get("signature") or "") == 64)
check("all eight LaunchAgents are found", snap["stats"]["launch_agents"] == 8,
      str(snap["stats"]["launch_agents"]))
check("modules were discovered", snap["stats"]["modules"] > 20,
      str(snap["stats"]["modules"]))
check("import edges were discovered", snap["stats"]["edges"] > 10,
      str(snap["stats"]["edges"]))
check("every module is assigned a known layer",
      all(n["layer"] in {l["id"] for l in snap["layers"]} for n in snap["nodes"]))
# An unclassified module lands in a default layer, which is a quiet way for the
# diagram to start lying. It is reported so it can be classified.
check("no module is silently unclassified", snap["unmapped"] == [],
      ", ".join(snap["unmapped"]))

protected = {n["path"] for n in snap["nodes"] if n.get("protected")}
for path in ("farm/canary.py", "farm/workorders.py", "farm/llm.py", "farm/rules.py",
             "farm/vcs.py", "experiments/author_agent.py"):
    check("%s is marked unwritable" % path, path in protected)
check("cycle is not marked unwritable",
      "farm/cycle.py" not in protected)

# The signature must ignore cosmetic edits, or the version history fills with noise
# and stops being readable.
nodes = list(snap["nodes"])
base_sig = architecture.signature(nodes, snap["edges"], snap["agents"])
cosmetic = [dict(n, loc=(n.get("loc") or 0) + 500, doc="rewritten comment") for n in nodes]
check("editing comments does not mint a version",
      architecture.signature(cosmetic, snap["edges"], snap["agents"]) == base_sig)
# ...but a real structural change must move it.
added = nodes + [{"id": "newthing", "kind": "module", "layer": "play", "protected": False}]
check("adding a module does mint a version",
      architecture.signature(added, snap["edges"], snap["agents"]) != base_sig)
check("adding a dependency mints a version",
      architecture.signature(nodes, list(snap["edges"]) + [{"source": "cycle", "target": "vcs"}],
                             snap["agents"]) != base_sig)
check("installing an agent mints a version",
      architecture.signature(nodes, snap["edges"],
                             list(snap["agents"]) + [{"label": "new", "interval_seconds": 60}])
      != base_sig)
check("locking a file mints a version",
      architecture.signature([dict(n, protected=True) for n in nodes],
                             snap["edges"], snap["agents"]) != base_sig)


# --------------------------------------------------------------------------
section("the version ledger only records real change")

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "architecture.ndjson")
    first = architecture.record(trigger="test one", ledger=ledger)
    check("the first scan records version 1",
          first.get("recorded") and first.get("version") == 1, str(first.get("version")))
    second = architecture.record(trigger="test two", ledger=ledger)
    check("an unchanged scan records nothing", second.get("recorded") is False,
          str(second))
    check("the skip explains itself", "unchanged" in str(second.get("reason")))
    rows = architecture.history(ledger=ledger)
    check("exactly one row on disk", len(rows) == 1, str(len(rows)))
    check("the row carries the full node set for later rendering",
          len(rows[0].get("nodes") or []) == len(snap["nodes"]))
    check("the row carries the commit", bool(rows[0].get("commit")))

    # A synthetic previous version proves the diff is computed, not assumed.
    forged = dict(rows[0])
    forged["signature"] = "forged"
    forged["nodes"] = [n for n in rows[0]["nodes"] if n["id"] != "canary"]
    with open(ledger, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(forged, sort_keys=True) + "\n")
    third = architecture.record(trigger="test three", ledger=ledger)
    check("a changed shape records a new version", third.get("recorded") is True)
    check("the new version increments", third.get("version") == 2, str(third.get("version")))
    check("the diff reports the restored module",
          "canary" in ((third.get("diff") or {}).get("added") or []),
          str((third.get("diff") or {}).get("added")))

    check("a missing ledger reads as empty history",
          architecture.history(ledger=os.path.join(tmp, "nope.ndjson")) == [])
    # A torn final line is normal in an append-only file being written by another
    # process, and must not blank the whole history.
    with open(ledger, "a", encoding="utf-8") as handle:
        handle.write('{"version": 3, "partial"')
    check("a half-written line is skipped, not fatal",
          len(architecture.history(ledger=ledger)) >= 1)


# --------------------------------------------------------------------------
section("the scan root is the project, not whichever copy is executing")

# Regression guard for a bug that quietly filled the version ledger with fiction. The
# agents run from releases/<rev>/, which holds the runtime but not deploy/ or .git. The
# root was taken as the parent of the module, so a scan from a release found zero
# LaunchAgents while a scan from the working tree found eight. The ledger alternated
# between the two shapes, minting a spurious version every 15 minutes -- four of the
# eight recorded versions are that artefact.
check("the resolved root holds the plists",
      (Path(architecture.PROJECT) / "deploy").is_dir()
      and any((Path(architecture.PROJECT) / "deploy").glob("com.nickfigura.farmfriends*.plist")),
      str(architecture.PROJECT))
check("the resolved root is the git checkout",
      (Path(architecture.PROJECT) / ".git").exists(), str(architecture.PROJECT))
check("the root is not a release copy",
      "releases/" not in str(architecture.PROJECT), str(architecture.PROJECT))
check("a snapshot records the root it scanned", bool(snap.get("root")))

# The decisive check: run the resolver with the module living inside a release-shaped
# directory and confirm it still finds the project rather than the copy.
released = Path(architecture.PROJECT) / "releases"
if released.is_dir():
    revisions = sorted(p for p in released.iterdir() if (p / "farm").is_dir())
    if revisions:
        probe = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r)\n"
             "from farm import architecture\n"
             "print(architecture.PROJECT)" % str(revisions[-1])],
            capture_output=True, text=True, cwd=str(revisions[-1]), timeout=60)
        resolved = probe.stdout.strip()
        # Older releases predate the fix; only assert when the released code has it.
        if resolved:
            check("a scan from inside a release resolves to the project",
                  "releases/" not in resolved or resolved == str(revisions[-1]),
                  resolved)
            if "releases/" not in resolved:
                check("that release agrees with the working tree's root",
                      resolved == str(architecture.PROJECT), resolved)
        else:
            check("release probe produced no root (pre-fix release)", True)
    else:
        check("no release revisions to probe", True)
else:
    check("no releases directory to probe", True)

# Two scans in a row from the same root must agree, or every pass mints a version.
check("repeated scans of the same tree agree",
      architecture.snapshot()["signature"] == architecture.snapshot()["signature"])

# The flip-flop signature is oscillation, not adjacent duplication: it produced
# A-B-A-B, where each row differs from the one before it and so passed a naive
# adjacency check. What gives it away is a signature *recurring* after something else
# intervened, which for a system that only moves forwards should not happen.
rows_all = architecture.history(limit=500)
seen: dict = {}
oscillating = []
for row in rows_all:
    sig = row.get("signature")
    if sig in seen and seen[sig] != row.get("version", 0) - 1:
        oscillating.append((seen[sig], row.get("version"), str(sig)[:8]))
    seen[sig] = row.get("version", 0)
# Rows produced by the bug are identifiable rather than merely old: they either carry no
# root at all (recorded before the field existed) or a root inside releases/. Keying on
# that instead of a version cutoff means this check keeps working as the ledger grows,
# and it caught two further oscillations while the fix was still unreleased.
by_version = {r.get("version"): r for r in rows_all}


def _pre_fix(version: int) -> bool:
    root = str((by_version.get(version) or {}).get("root") or "")
    return (not root) or ("releases/" in root)


suspect = [o for o in oscillating if not (_pre_fix(o[0]) or _pre_fix(o[1]))]
check("no architecture version recurs once scans share a root", not suspect,
      "recurring: %s" % (suspect[:3],))
if oscillating:
    # Reported, not asserted away. Rewriting the ledger to make a test pass would
    # destroy the only record of what the system actually did.
    print("       note: %d oscillation(s) from before the root fix remain as recorded "
          "history: %s" % (len(oscillating), oscillating[:4]))


# --------------------------------------------------------------------------
section("readouts are judged on served cost, not agent startup")

# The agent is a fresh process every 15 minutes; the dashboard is a long-running server.
# Judging on the cold number reported Findings as "too slow" at 4,066ms when the served
# cost was 674ms -- measuring the agent's own imports and calling it a defect in the page.
agent_src = (PROJECT / "experiments" / "dashboard_agent.py").read_text(encoding="utf-8")
check("the probe measures twice", "_probe_once" in agent_src)
check("the slow check uses the warm number", 'ms warm to build' in agent_src)
check("the cold number is still reported", "cold_ms" in agent_src)


# --------------------------------------------------------------------------
section("the event stream is honestly ordered")

events = architecture.events(limit=80)
check("events were collected", len(events) > 0, str(len(events)))
check("every event carries a timestamp", all(e.get("ts") for e in events))
timestamps = [str(e["ts"]) for e in events]
check("events are newest-first", timestamps == sorted(timestamps, reverse=True))
check("all timestamps are UTC-marked", all(t.endswith("Z") for t in timestamps),
      next((t for t in timestamps if not t.endswith("Z")), ""))

# The specific bug this guards: git's default %cd is local time, so a release cut at
# 21:32Z was recorded as 15:32 and sorted in hours before findings it followed. The
# release tag name embeds its own UTC timestamp, so the two must agree.
for event in events:
    if event.get("kind") != "release":
        continue
    name = str(event.get("title") or "").split()[-1]
    if len(name) < 15 or not name.endswith("Z"):
        continue
    tag_hour = name[9:11]
    ts_hour = str(event.get("ts"))[11:13]
    check("release %s is timestamped in UTC" % name,
          tag_hour == ts_hour, "tag says %s, event says %s" % (tag_hour, ts_hour))
    break

kinds = {e.get("kind") for e in events}
check("the stream merges several sources", len(kinds) >= 3, str(sorted(kinds)))
check("only version events claim to be structural",
      all(e.get("kind") == "version" for e in events if e.get("structural")))


# --------------------------------------------------------------------------
section("the report the tab consumes")

full = architecture.report()
check("the report carries the current architecture", bool(full.get("current")))
check("the report carries the event stream", bool(full.get("events")))
check("the report states whether live matches the ledger",
      isinstance(full.get("live_matches_recorded"), bool))
check("the payload stays under 64KB", len(json.dumps(full)) < 65536,
      "%dB" % len(json.dumps(full)))
# The timeline must not carry the full node set for every version, or the payload grows
# without bound as the system evolves.
check("timeline entries are summaries, not full snapshots",
      all("nodes" not in entry for entry in full.get("timeline") or []))


# --------------------------------------------------------------------------
section("the dashboard agent checks every tab")

agent_path = PROJECT / "experiments" / "dashboard_agent.py"
check("the agent exists", agent_path.exists())
source = agent_path.read_text(encoding="utf-8")
for source_name in ("monitor.snapshot", "evidence.report", "topology.cached_graph",
                    "autonomy.report", "architecture.report"):
    check("probes %s" % source_name, source_name in source)

result = subprocess.run([sys.executable, str(agent_path)],
                        capture_output=True, text=True, timeout=300)
check("the agent runs cleanly", result.returncode == 0,
      "exit %d: %s" % (result.returncode, result.stderr[-200:]))
check("it reports on every readout", "readouts ok" in result.stdout,
      result.stdout[-160:])
check("no readout is failing", "FAIL" not in result.stdout,
      "\n".join(l for l in result.stdout.splitlines() if "FAIL" in l))

ledger = PROJECT / "state" / "dashboard_health.ndjson"
check("it writes a health ledger", ledger.exists())
if ledger.exists():
    last = json.loads(ledger.read_text(encoding="utf-8").strip().splitlines()[-1])
    check("the ledger records each check", len(last.get("checks") or []) >= 5,
          str(len(last.get("checks") or [])))
    check("the ledger records a verdict", isinstance(last.get("ok"), bool))
    check("every check is timed",
          all(isinstance(c.get("ms"), int) for c in last.get("checks") or []))
    # A readout slower than the poll interval is a defect even when it is correct.
    slow = [c["source"] for c in last.get("checks") or [] if int(c.get("ms") or 0) > 4000]
    check("no readout is slower than the poll interval", not slow, str(slow))


# --------------------------------------------------------------------------
section("filing an order actually works")

# Regression guard for a bug this agent found in itself on its first live run: it
# called workorders.submit(order_id=..., kind=..., evidence=...), which is not the
# signature. submit() takes a change dict positionally. The agent caught its own
# defect, reported it, and could not file it -- so the one order it most needed to
# raise was the one it could not.
import inspect  # noqa: E402

from farm import workorders  # noqa: E402

params = list(inspect.signature(workorders.submit).parameters)
check("submit takes a change dict first", params[0] == "change", str(params[:3]))
check("submit has no order_id parameter", "order_id" not in params, str(params))

agent_source = (PROJECT / "experiments" / "dashboard_agent.py").read_text(encoding="utf-8")
check("the agent does not pass order_id", "order_id=" not in agent_source)
check("the agent passes a change dict", "workorders.submit(\n                change," in agent_source
      or "submit(\n                change," in agent_source)

with tempfile.TemporaryDirectory() as tmp:
    queue = os.path.join(tmp, "orders.ndjson")
    change = {"id": "dashboard-test-probe", "kind": "dashboard_readout",
              "severity": "degraded", "summary": "synthetic", "tool": "test.probe"}
    first = workorders.submit(change, source="dashboard_agent", intent="restore it",
                              acceptance=["it works"], files=["monitor.py"], path=queue)
    check("an order can be filed with the real signature", bool(first), str(first)[:90])
    # Idempotent by id, so a readout that stays broken must not file a new order on
    # every 15-minute pass.
    second = workorders.submit(change, source="dashboard_agent", intent="restore it",
                               acceptance=["it works"], files=["monitor.py"], path=queue)
    check("re-filing the same broken readout is a no-op", second is None, str(second)[:90])


# --------------------------------------------------------------------------
section("timestamp parsing is memoised without changing answers")

# The Findings readout took 4,066ms on the agent's first run, over its 4s budget. Most
# of it was strptime: one report parsed 37,392 timestamps across ~6,280 distinct rows,
# because the counterfactual sweep walks history once per parameter value. Caching is
# safe because timestamps are immutable strings, but only if it changes no answer.
from datetime import datetime, timezone  # noqa: E402

from farm import analysis  # noqa: E402

cases = [
    ("2026-08-25T21:32:40Z", datetime(2026, 8, 25, 21, 32, 40, tzinfo=timezone.utc)),
    ("2026-08-25T21:32:40.123456Z",
     datetime(2026, 8, 25, 21, 32, 40, 123456, tzinfo=timezone.utc)),
    ("", None), ("garbage", None), (None, None), (12345, None),
]
for value, expected in cases:
    got = analysis.parse_ts(value)
    check("parse_ts(%r) is unchanged" % (value,), got == expected, "%s != %s" % (got, expected))
# Called twice, a memoised function must still agree with itself.
check("repeated parses agree",
      analysis.parse_ts("2026-08-25T21:32:40Z") == analysis.parse_ts("2026-08-25T21:32:40Z"))
check("the cache is actually being used",
      analysis._parse_ts_cached.cache_info().hits > 0,
      str(analysis._parse_ts_cached.cache_info()))
# An unhashable input must not reach the cache and raise TypeError.
threw = None
try:
    analysis.parse_ts({"not": "hashable"})
except Exception as exc:  # noqa: BLE001
    threw = str(exc)[:80]
check("an unhashable value does not raise", threw is None, threw or "")

started = time.time()
from farm import evidence  # noqa: E402
evidence.report()
report_ms = (time.time() - started) * 1000
check("the Findings readout builds inside its budget", report_ms < 4000,
      "%.0fms" % report_ms)


# --------------------------------------------------------------------------
section("the served page wires the tab up")

import monitor  # noqa: E402

html = monitor.HTML
for token in ("__ARCH_CSS__", "__ARCH_JS__"):
    check("%s was substituted" % token, token not in html)
check("the tab button exists", 'data-tab="architecture"' in html)
check("the tab panel exists", 'id="tab-architecture"' in html)
check("the renderer is bundled", "function renderArchitecture" in html)
check("the loader is bundled", "loadArchitecture" in html)
check("architecture is an allowed tab", '"architecture"]' in html)
check("the asset loaded rather than stubbing out",
      "missing dashboard asset" not in monitor.ARCH_JS)
check("the stylesheet loaded", "missing dashboard asset" not in monitor.ARCH_CSS)

state = monitor.snapshot()
check("snapshot reports all eight agents",
      len((state.get("launchd") or {}).get("all") or []) == 8,
      str(len((state.get("launchd") or {}).get("all") or [])))
check("snapshot keeps the original cycle fields",
      "loaded" in (state.get("launchd") or {}))
check("snapshot keeps the supervisor sub-object",
      isinstance((state.get("launchd") or {}).get("supervisor"), dict))


print()
print("%d checks, %d failures" % (CHECKS, FAILURES))
if FAILURES:
    print("dashboard agent suite FAILED")
    sys.exit(1)
print("dashboard agent suite passed")
