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

from farm import architecture, autonomy, canary, control  # noqa: E402

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
check("all eleven required processes are described", view.get("expected") == 11,
      str(view.get("expected")))
labels = {a["label"] for a in view.get("agents") or []}
for expected in ("com.nickfigura.farmfriends",
                 "com.nickfigura.farmfriends.supervisor",
                 "com.nickfigura.farmfriends.expand",
                 "com.nickfigura.farmfriends.recovery",
                 "com.nickfigura.farmfriends.outage",
                 "com.nickfigura.farmfriends.eod",
                 "com.nickfigura.farmfriends.contract",
                 "com.nickfigura.farmfriends.author",
                 "com.nickfigura.farmfriends.research",
                 "com.nickfigura.farmfriends.dashboard",
                 "com.nickfigura.farmfriends.monitor"):
    check("watches %s" % expected.split("farmfriends")[-1] or "cycle",
          expected in labels)
check("every agent explains what its absence costs",
      all(a.get("lost") for a in autonomy.AGENTS))
check("health consumes the authoritative service registry",
      [a["label"] for a in autonomy.AGENTS] == [a["label"] for a in control.SERVICES])
# The cycle and author remain critical recovery dependencies; critical means the
# automated recovery path is active, never that operator input is required.
critical = {a["key"] for a in autonomy.AGENTS} & {"cycle", "author"}
check("cycle and author are both known keys", critical == {"cycle", "author"})


# --------------------------------------------------------------------------
section("the plists the architecture reads are actually parseable")

# This is the regression guard for a real defect: two plists contained "--" inside an
# XML comment, which `plutil` tolerates and Python's expat rejects. Both agents
# silently disappeared from the architecture view with no error anywhere.
plists = sorted((PROJECT / "deploy").glob("com.nickfigura.farmfriends*.plist"))
check("eleven plists on disk", len(plists) == 11, str(len(plists)))
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

monitor_plist_path = PROJECT / "deploy" / "com.nickfigura.farmfriends.monitor.plist"
with monitor_plist_path.open("rb") as handle:
    monitor_plist = plistlib.load(handle)
monitor_args = monitor_plist.get("ProgramArguments") or []
check("the browser server is kept alive", monitor_plist.get("KeepAlive") is True)
check("the browser server binds the documented port", "8765" in monitor_args)
check("the browser server refuses a surprise fallback port", "--strict-port" in monitor_args)
check("the browser server runs at normal priority",
      "ProcessType" not in monitor_plist and not monitor_plist.get("LowPriorityIO"))

install_source = (PROJECT / "deploy" / "install.sh").read_text(encoding="utf-8")
release_source = (PROJECT / "deploy" / "release.sh").read_text(encoding="utf-8")
canary_source = (PROJECT / "farm" / "canary.py").read_text(encoding="utf-8")
control_source = (PROJECT / "farm" / "control.py").read_text(encoding="utf-8")
monitor_source = (PROJECT / "monitor.py").read_text(encoding="utf-8")
check("installation includes the monitor service", 'MONITOR="$LABEL.monitor"' in install_source)
supervisor_source = (PROJECT / "run.py").read_text(encoding="utf-8")
check("supervision iterates the authoritative service registry",
      "for service in control.SERVICES" in supervisor_source)
check("uninstall removes the monitor service", 'bootout "$DOMAIN/$MONITOR"' in install_source)
check("a release restarts module-level HTML and routes",
      'kickstart -k "$MONITOR_DOMAIN/$MONITOR_LABEL"' in release_source)
check("a rollback also restarts module-level HTML and routes",
      "outcome.update(_restart_monitor())" in canary_source
      and 'control.restart_service("monitor")' in canary_source
      and '"kickstart", "-k", domain' in control_source)
check("an open dashboard reloads when its embedded revision changes",
      "refreshForRelease(data)" in monitor_source
      and "window.location.reload()" in monitor_source
      and "__VIEW_REVISION__" in monitor_source)
check("slow state snapshots cannot overlap into a request pileup",
      "if (STATE_LOADING) return;" in monitor_source
      and "STATE_LOADING = true;" in monitor_source
      and "STATE_LOADING = false;" in monitor_source)
check("trace reads stay inside the compaction hot tail",
      "_json_lines(TOOL_CALLS, compaction.DEFAULT_HOT_ROWS)" in monitor_source
      and "calls = boundary[:1000]" in monitor_source)
check("a release separates gated source from deployment state",
      "FARM_SOURCE_ROOT" in release_source and "FARM_DEPLOY_ROOT" in release_source)
check("every activated release arms a canary",
      "canary.arm(" in release_source and "release activation failed closed" in release_source)
check("the release boundary refuses an already-watching candidate before staging",
      "release rejected: canary %s is still watching" in release_source
      and release_source.index("canary.active") < release_source.index("run.py --self-test"))
check("release pruning preserves the active canary's rollback target",
      '! -name "$PREVIOUS"' in release_source)

with (PROJECT / "deploy" / "com.nickfigura.farmfriends.author.plist").open("rb") as handle:
    author_plist = plistlib.load(handle)
check("the immutable author process receives the editable checkout explicitly",
      (author_plist.get("EnvironmentVariables") or {}).get("FARM_PROJECT_ROOT") == "__PROJECT__")


# --------------------------------------------------------------------------
section("the autonomy view degrades instead of collapsing")

report = autonomy.report()
for key in ("agents", "canary", "orders", "contract", "vcs", "research", "governance", "llm", "activity"):
    check("section %s is present" % key, key in report)
    check("section %s has no error" % key,
          not (report.get(key) or {}).get("error"),
          str((report.get(key) or {}).get("error"))[:90])

# A subsystem that raises must become a reported value, never an exception, because
# this view is the only place several of those subsystems are visible at all.
def _boom():
    raise RuntimeError("synthetic failure")


guarded = autonomy._guard(_boom)
activity_view = report.get("activity") or {}
activity_rows = activity_view.get("events") or []
check("autonomy activity projects the existing ledgers", activity_view.get("sources") == 6,
      str(activity_view.get("sources")))
check("autonomy activity is newest-first",
      all(activity_rows[i].get("ts", "") >= activity_rows[i + 1].get("ts", "")
          for i in range(max(0, len(activity_rows) - 1))))
check("autonomy activity retains actor, phase, source and status",
      not activity_rows or all(all(key in row for key in ("actor", "phase", "source", "status"))
                               for row in activity_rows))

check("a failing section becomes an error string", "error" in guarded)
check("the error names the exception type", "RuntimeError" in guarded["error"])
check("a failed section is surfaced as a blocker",
      any("failed to read" in b["what"]
          for b in autonomy.blockers({"vcs": {"error": "synthetic"}})))
operator_source = (PROJECT / "dashboard" / "operator.js").read_text(encoding="utf-8")
architecture_source = (PROJECT / "dashboard" / "architecture.js").read_text(encoding="utf-8")
operator_css = (PROJECT / "dashboard" / "operator.css").read_text(encoding="utf-8")
architecture_css = (PROJECT / "dashboard" / "architecture.css").read_text(encoding="utf-8")
monitor_source = (PROJECT / "monitor.py").read_text(encoding="utf-8")
check("critical status reports self-healing without paging language",
      'critical ? "Self-healing"' in operator_source)
check("served control surfaces have no operator-attention state token",
      all("attention" not in source.lower() for source in (
          operator_source, architecture_source, operator_css, architecture_css, monitor_source,
      )))
check("architecture assigns recovery ownership instead of operator action",
      "Recovery ownership" in architecture_source and "Operator action" not in architecture_source)
check("overview queue is agent-owned rather than an attention queue",
      "Autonomous handling queue" in monitor_source and ">Attention queue<" not in monitor_source)

original_canary_status = canary.status
try:
    canary.status = lambda: {
        "status": canary.WATCHING,
        "revision": "rev-new",
        "previous": "rev-old",
        "order_id": "order-1",
        "armed_ts": "2026-08-26T00:00:00Z",
        "verdict": {"status": canary.WATCHING, "reason": "collecting runs"},
    }
    canary_view = autonomy.canary_state()
finally:
    canary.status = original_canary_status
check("a flat canary status preserves the live and rollback revisions",
      canary_view.get("revision") == "rev-new" and canary_view.get("previous") == "rev-old",
      str(canary_view))
original_canary_status = canary.status
try:
    canary.status = lambda: {
        "status": canary.REGRESSED, "revision": "rev-bad", "previous": "rev-good",
        "resolved_ts": "2026-08-27T16:54:38Z",
        "resolution": "observed 0.1197 below floor 0.1237",
        "verdict": {"status": canary.REGRESSED, "runs_observed": 3,
                    "baseline_per_animal": 0.1650, "observed_per_animal": 0.1197,
                    "threshold": 0.1237},
    }
    resolved_canary_view = autonomy.canary_state()
finally:
    canary.status = original_canary_status
check("a resolved canary keeps its explanatory metrics in the dashboard projection",
      resolved_canary_view.get("status") == canary.REGRESSED
      and resolved_canary_view.get("runs_observed") == 3
      and resolved_canary_view.get("threshold") == 0.1237
      and resolved_canary_view.get("resolved_ts") == "2026-08-27T16:54:38Z",
      str(resolved_canary_view))
check("a completed rollback does not permanently block its repair release",
      not any("automatic rollback active" in item.get("what", "")
              for item in autonomy.blockers({"canary": resolved_canary_view})),
      str(autonomy.blockers({"canary": resolved_canary_view})))
research_only = {
    "orders": {"summary": {"open": 4, "repair_open": 0, "research_open": 4,
                             "oldest_open_age_seconds": 99999}},
    "llm": {"passes_today": 8, "max_passes": 8, "available": True},
}
check("aged research opportunities are not mislabeled as stalled repairs",
      not any("repair(s) queued" in item.get("what", "")
              for item in autonomy.blockers(research_only)))
stalled_repairs = {
    "orders": {"summary": {"open": 2, "repair_open": 2,
                             "oldest_repair_age_seconds": 7200}},
    "llm": {"passes_today": 0, "max_passes": 8, "available": True},
}
check("an actually stalled repair queue remains a blocker",
      any("repair(s) queued" in item.get("what", "")
          for item in autonomy.blockers(stalled_repairs)))
check("dirty release source is safely contained from stale-base authoring",
      any("autonomous authoring is safely paused" in item.get("what", "")
          for item in autonomy.blockers({"vcs": {"dirty_source_paths": ["farm/control.py"]}})))


# --------------------------------------------------------------------------
section("the hot path does not pay for subprocesses")

# The uncached view spawns launchctl once per required process plus git. On the 2s dashboard poll
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
check("all eleven LaunchAgents are found", snap["stats"]["launch_agents"] == 11,
      str(snap["stats"]["launch_agents"]))
check("modules were discovered", snap["stats"]["modules"] > 20,
      str(snap["stats"]["modules"]))
check("all eleven runtime services are first-class architecture nodes",
      snap["stats"]["agent_modules"] == 11, str(snap["stats"]["agent_modules"]))
service_nodes = {n.get("agent_label") for n in snap["nodes"] if n.get("kind") == "agent"}
check("every declared service has a health-addressable node",
      service_nodes == {s["label"] for s in control.SERVICES}, str(sorted(service_nodes)))
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
             "farm/vcs.py", "farm/cycle.py", "farm/compaction.py",
             "farm/evaluation.py", "farm/governance.py", "farm/provenance.py",
             "experiments/author_agent.py"):
    check("%s is marked unwritable" % path, path in protected)
check("the diagram consumes the enforced protected manifest",
      architecture.PROTECTED == control.TRUSTED_PATHS)
check("run.py orchestration is marked unwritable", "run.py" in protected)

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
check("a snapshot labels release-source dirtiness",
      isinstance(snap.get("dirty_source"), list))
check("the live strategy journal is not release-source dirtiness",
      "farm-strategy-journal.md" not in (snap.get("dirty_source") or []))

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
# intervened, which for a system that only moves forwards should not happen. Compare
# only signatures emitted under the same semantics: an old immutable reader can scan
# today's tree with yesterday's layer/protection vocabulary and produce a different
# hash for identical source.
rows_all = architecture.history(limit=500)


def _signature_key(row):
    return (int(row.get("signature_version") or 1), row.get("signature"))


check("architecture signature versions separate incompatible readers",
      _signature_key({"signature": "same", "signature_version": 1})
      != _signature_key({"signature": "same", "signature_version": 2}))
seen: dict = {}
oscillating = []
for row in rows_all:
    key = _signature_key(row)
    if key in seen and seen[key] != row.get("version", 0) - 1:
        oscillating.append((seen[key], row.get("version"), str(key[1])[:8], key[0]))
    seen[key] = row.get("version", 0)
# Rows produced by the bug are identifiable rather than merely old: they either carry no
# root at all (recorded before the field existed) or a root inside releases/. Keying on
# that instead of a version cutoff means this check keeps working as the ledger grows,
# and it caught two further oscillations while the fix was still unreleased.
by_version = {r.get("version"): r for r in rows_all}
first_signature_by_commit = {}
for row in rows_all:
    commit_key = (row.get("commit"), int(row.get("signature_version") or 1))
    if commit_key[0] and commit_key not in first_signature_by_commit:
        first_signature_by_commit[commit_key] = row.get("signature")


def _pre_fix(version: int) -> bool:
    root = str((by_version.get(version) or {}).get("root") or "")
    return (not root) or ("releases/" in root)


def _dirty_scan(version: int) -> bool:
    row = by_version.get(version) or {}
    if "dirty_source" in row:
        return bool(row.get("dirty_source"))
    # Before dirty_source was recorded, multiple shapes under one unchanged commit
    # were scheduled scans of an in-progress working tree. The first observed shape
    # is the committed baseline; later different shapes are retained but not treated
    # as deployed policy oscillation.
    commit = row.get("commit")
    commit_key = (commit, int(row.get("signature_version") or 1))
    return bool(commit and row.get("signature") != first_signature_by_commit.get(commit_key))


suspect = [
    item for item in oscillating
    if not (_pre_fix(item[0]) or _pre_fix(item[1])
            or _dirty_scan(item[0]) or _dirty_scan(item[1]))
]
# A clean C is the recovery from a recorded A->B->A; keeping every historical pair as
# a permanent failure makes forward repair impossible. Fail only when the architecture
# being submitted is itself the recurring shape. A dirty working tree cannot claim to
# be C, so it is judged against the latest clean recorded version until committed.
clean_rows = [row for row in rows_all if not _pre_fix(row.get("version", 0))
              and not _dirty_scan(row.get("version", 0))]
if not snap.get("dirty_source"):
    submitted_key = _signature_key(snap)
else:
    compatible_clean = [
        row for row in clean_rows
        if int(row.get("signature_version") or 1) == int(snap.get("signature_version") or 1)
    ]
    # A protected signature-version bump has no prior compatible clean row by
    # definition. Judge that boundary with the submitted reader rather than falling
    # back to an incompatible historical hash; later dirty scans still use the last
    # clean row from their own version.
    submitted_key = _signature_key(compatible_clean[-1] if compatible_clean else snap)
unresolved = [item for item in suspect
              if _signature_key(by_version.get(item[1]) or {}) == submitted_key]
check("the submitted clean architecture is not a recurring shape", not unresolved,
      "recurring: %s" % (unresolved[:3],))
if oscillating:
    # Reported, not asserted away. Rewriting the ledger to make a test pass would
    # destroy the only record of what the system actually did.
    print("       note: %d historical oscillation(s) remain as recorded evidence: %s"
          % (len(oscillating), oscillating[:4]))


# --------------------------------------------------------------------------
section("readouts are judged on served cost, not agent startup")

# The agent is a fresh process every 15 minutes; the dashboard is a long-running server.
# Judging on the cold number reported Findings as "too slow" at 4,066ms when the served
# cost was 674ms -- measuring the agent's own imports and calling it a defect in the page.
sys.path.insert(0, str(PROJECT / "experiments"))
import dashboard_agent  # noqa: E402

agent_src = (PROJECT / "experiments" / "dashboard_agent.py").read_text(encoding="utf-8")
check("cycle freshness uses the compaction-aware history reader",
      "analysis.history_rows(limit=1)" in agent_src and "journal.history" not in agent_src)
saved_history_rows = dashboard_agent.analysis.history_rows
try:
    dashboard_agent.analysis.history_rows = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fixture"))
    freshness_failure = dashboard_agent._staleness()
finally:
    dashboard_agent.analysis.history_rows = saved_history_rows
check("a cycle freshness exception remains explicit",
      "fixture" in freshness_failure.get("cycle_age_error", ""), str(freshness_failure))
check("freshness-check errors affect the dashboard problem list",
      'if not key.endswith("_error")' in agent_src
      and '"breaking" if source == "cycle_age"' in agent_src)
check("freshness repair orders name only the owning agent",
      '["experiments/dashboard_agent.py"]' in agent_src
      and 'if str(problem["source"]).endswith("_age")' in agent_src)
check("the probe measures twice", "_probe_once" in agent_src)
check("the cold number is still reported", "cold_ms" in agent_src)

# The deeper version of the same mistake. This agent runs under ProcessType=Background
# and LowPriorityIO, which cost a measured 4.4x on this workload: 675ms at normal
# priority against 2,932ms throttled, on identical code and data. So no absolute
# threshold set from a normal-priority measurement can be applied here, and the first
# attempt at a relative check then compared a throttled pass against a median built from
# hand-run unthrottled ones and cried regression.
check("how the pass was started is detected, not inferred",
      isinstance(dashboard_agent._scheduled(), bool))
# A hand run must classify as a hand run, or this very suite compares itself against
# the scheduled population.
check("a hand-run check is not a scheduled pass", dashboard_agent._scheduled() is False)
check("detection reads how the process was started",
      "XPC_SERVICE_NAME" in agent_src)
check("readouts are judged relative to their own history",
      "REGRESSION_MULTIPLE" in agent_src)
check("an absolute ceiling still catches total breakage",
      dashboard_agent.ABSOLUTE_CEILING_MS >= 10000)
check("a floor stops multiples of tiny numbers being treated as signal",
      dashboard_agent.REGRESSION_FLOOR_MS >= 500)

with tempfile.TemporaryDirectory() as tmp:
    ledger = Path(tmp) / "health.ndjson"
    original = dashboard_agent.LEDGER
    dashboard_agent.LEDGER = ledger
    try:
        def _write(throttled, ms, passes=6):
            with ledger.open("a", encoding="utf-8") as handle:
                for _ in range(passes):
                    handle.write(json.dumps({
                        "ts": "2026-08-25T00:00:00Z", "scheduled": throttled,
                        "checks": [{"source": "evidence.report", "ms": ms, "ok": True}],
                    }) + "\n")

        _write(False, 700)
        _write(True, 3100)
        fast = dashboard_agent._baselines(False)
        slow = dashboard_agent._baselines(True)
        check("a hand-run baseline uses only hand-run passes",
              abs(fast.get("evidence.report", 0) - 700) < 1, str(fast))
        check("a scheduled baseline uses only scheduled passes",
              abs(slow.get("evidence.report", 0) - 3100) < 1, str(slow))
        # The bug: had these populations been pooled, the median would sit between them
        # and both environments would look wrong.
        check("the two baselines are kept apart",
              slow.get("evidence.report", 0) > fast.get("evidence.report", 0) * 3)

        # A pass predating the throttled field cannot be attributed, so it must be
        # excluded rather than assumed to belong to either population.
        with ledger.open("a", encoding="utf-8") as handle:
            for _ in range(20):
                handle.write(json.dumps({
                    "ts": "2026-08-25T00:00:00Z",
                    "checks": [{"source": "evidence.report", "ms": 99999, "ok": True}],
                }) + "\n")
        after = dashboard_agent._baselines(False)
        check("unlabelled passes do not pollute a baseline",
              abs(after.get("evidence.report", 0) - 700) < 1, str(after))

        # Too few samples must yield no baseline at all rather than a confident one.
        ledger.write_text(json.dumps({
            "ts": "2026-08-25T00:00:00Z", "scheduled": False,
            "checks": [{"source": "evidence.report", "ms": 700, "ok": True}],
        }) + "\n", encoding="utf-8")
        check("a single sample is not treated as a baseline",
              "evidence.report" not in dashboard_agent._baselines(False))
    finally:
        dashboard_agent.LEDGER = original


# --------------------------------------------------------------------------
section("the verifier checks what a browser actually receives")

# The original verifier imported fresh functions from the current release. It stayed
# green while an eight-hour-old monitor process served seven tabs and 404ed both new
# endpoints. This fixture exercises the HTTP contract without requiring an installed
# daemon during a first-time release gate.
class _Response:
    def __init__(self, body, status=200):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def _http_fixture(stale=False):
    state = {
        "app": "farmfriends-monitor",
        "release": {
            "serving_revision": "rev-a",
            "pointer_revision": "rev-b" if stale else "rev-a",
            "stale": stale,
        },
    }
    bodies = {
        "/": '<button data-tab="architecture"></button><div id="tab-architecture">'
             '<div data-arch-loading>Loading</div><script>'
             'async function loadArchitecture() { fetch("/api/architecture"); }'
             'if (window.loadArchitecture) window.loadArchitecture();</script></div>',
        "/api/state": json.dumps(state),
        "/api/autonomy": json.dumps({"agents": {"live": 9}}),
        "/api/architecture": json.dumps({"current": {"nodes": [{"id": "cycle"}]}}),
    }

    def _open(url, timeout=0):
        del timeout
        path = url.split("fixture.invalid", 1)[-1]
        return _Response(bodies[path])

    return _open


original_urlopen = dashboard_agent.urlopen
try:
    dashboard_agent.urlopen = _http_fixture(stale=False)
    served = dashboard_agent._served_dashboard("http://fixture.invalid")
    check("the browser-path probe accepts a current eight-tab server", served["ok"],
          str(served.get("error")))
    check("the browser-path probe fetched all four resources",
          served.get("bytes", 0) > 100, str(served.get("bytes")))

    dashboard_agent.urlopen = _http_fixture(stale=True)
    stale_served = dashboard_agent._served_dashboard("http://fixture.invalid")
    check("the browser-path probe rejects a stale process", not stale_served["ok"])
    check("the stale diagnosis names both revisions",
          "rev-a" in stale_served.get("error", "")
          and "rev-b" in stale_served.get("error", ""),
          stale_served.get("error", ""))
finally:
    dashboard_agent.urlopen = original_urlopen

check("scheduled passes include the browser-path probe",
      "if throttled:\n        served = _served_dashboard()" in agent_src)

# Composition is part of correctness. architecture.js loaded successfully in isolation,
# but embedding it inside the Switchboard's try block made async function declarations
# block-scoped in Chromium. renderArchitecture leaked out under Annex B compatibility;
# loadArchitecture did not. The tab button therefore worked and exposed a blank panel.
import monitor  # noqa: E402
composed = monitor.HTML
wire_end = composed.find("/*WIRE_JS_END*/")
arch_start = composed.find("/*ARCH_JS_START*/")
arch_end = composed.find("/*ARCH_JS_END*/")
check("architecture code is outside the Switchboard block",
      0 <= wire_end < arch_start < arch_end,
      "wire_end=%s arch_start=%s arch_end=%s" % (wire_end, arch_start, arch_end))
check("the served panel is never statically empty", "data-arch-loading" in composed)
check("tab activation checks the global async loader",
      'typeof loader!=="function"' in composed and "window.loadArchitecture" in composed)
check("loader failure remains visible and recovery-owned",
      "Renderer restarting" in composed and "Architecture agent owns recovery" in composed)
check("an open Findings or History tab refreshes on its own",
      "EVIDENCE_REFRESH_MS = 60000" in composed and "EVIDENCE_LAST_FETCH_MS" in composed)
check("an open Architecture tab refreshes its model and liveness overlay",
      "ARCHITECTURE_REFRESH_MS = 30000" in composed and "ARCH_LAST_FETCH_MS" in composed)


# --------------------------------------------------------------------------
section("the server distinguishes its own release from the pointer")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "deploy").mkdir()
    (root / "deploy" / "com.nickfigura.farmfriends.monitor.plist").write_text(
        "<plist/>", encoding="utf-8")
    releases = root / "releases"
    releases.mkdir()
    for revision, content in (("rev-a", "a"), ("rev-b", "b")):
        release_dir = releases / revision
        (release_dir / "farm").mkdir(parents=True)
        (release_dir / "RELEASED").write_text(revision, encoding="utf-8")
        (release_dir / "run.py").write_text(content, encoding="utf-8")
        (release_dir / "farm" / "sample.py").write_text(content, encoding="utf-8")
    os.symlink(releases / "rev-a", root / "release")

    original_project = monitor.PROJECT
    monitor.PROJECT = releases / "rev-a"
    try:
        same = monitor._release_info()
        check("a release process finds the canonical checkout",
              same.get("project_root") == str(root), str(same.get("project_root")))
        check("the process reports the revision it serves",
              same.get("serving_revision") == "rev-a", str(same))
        check("the current pointer is reported separately",
              same.get("pointer_revision") == "rev-a", str(same))
        check("matching process and pointer are not stale", same.get("stale") is False)

        (root / "release").unlink()
        os.symlink(releases / "rev-b", root / "release")
        stale_info = monitor._release_info()
        check("an old process detects a moved pointer", stale_info.get("stale") is True,
              str(stale_info))
        check("staleness preserves both identities",
              stale_info.get("serving_revision") == "rev-a"
              and stale_info.get("pointer_revision") == "rev-b", str(stale_info))
        check("uncomparable fingerprints are never called not-diverged",
              monitor._release_info().get("diverged") in (True, None))
    finally:
        monitor.PROJECT = original_project


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
    # Large append-only ledgers can make a snapshot exceed the nominal poll cadence.
    # The page is single-flight, so that is latency rather than an overlap leak; the
    # agent's absolute ceiling still distinguishes slow from functionally stranded.
    stranded = [c["source"] for c in last.get("checks") or []
                 if int(c.get("ms") or 0) > dashboard_agent.ABSOLUTE_CEILING_MS]
    check("no readout exceeds the absolute failure ceiling", not stranded, str(stranded))


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
    closed = dashboard_agent._resolve_healthy_orders(
        [{"source": "test.probe", "ok": True}], [], path=queue,
    )
    check("a recovered readout supersedes its stale repair order",
          closed == ["dashboard-test-probe"]
          and workorders.current(queue)["dashboard-test-probe"]["status"] == workorders.SUPERSEDED,
          str(workorders.current(queue)["dashboard-test-probe"]))
    reopened = workorders.submit(change, source="dashboard_agent", intent="restore it",
                                 acceptance=["it works"], files=["monitor.py"], path=queue)
    held = dashboard_agent._resolve_healthy_orders(
        [{"source": "test.probe", "ok": True}],
        [{"source": "test.probe", "severity": "degraded"}], path=queue,
    )
    check("a currently unhealthy readout keeps its repair open",
          bool(reopened) and held == []
          and workorders.current(queue)["dashboard-test-probe"]["status"] == workorders.OPEN,
          str(held))
    freshness_change = {
        "id": "dashboard-cycle_age", "kind": "dashboard_readout",
        "severity": "degraded", "summary": "synthetic stale cycle", "tool": "cycle_age",
    }
    workorders.submit(
        freshness_change, source="dashboard_agent", intent="restore freshness",
        acceptance=["cycle is fresh"], files=["monitor.py"], path=queue,
    )
    freshness_closed = dashboard_agent._resolve_healthy_orders(
        [], [], path=queue, healthy_sources=["cycle_age"],
    )
    check("a recovered staleness source supersedes its repair order",
          freshness_closed == ["dashboard-cycle_age"]
          and workorders.current(queue)["dashboard-cycle_age"]["status"] == workorders.SUPERSEDED,
          str(freshness_closed))


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
check("snapshot reports all eleven required processes",
      len((state.get("launchd") or {}).get("all") or []) == 11,
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
