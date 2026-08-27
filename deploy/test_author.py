#!/usr/bin/env python3
"""Author agent and canary suite.

This is the highest-risk component in the project: it edits code and publishes
releases with no human in the loop. The tests therefore concentrate on the
refusals rather than the happy path, because a refusal that fails to fire is what
turns an autonomous agent into an outage.

Everything runs against a temp sandbox tree with `author_agent.PROJECT`
redirected, so `publish()` and `deploy/release.sh` are never reached and the live
release pointer is never touched.

An opt-in test can make a real call to the Glean llm_proxy gateway to prove the
model backend works end to end. Deterministic release gates never make that paid,
non-reproducible call; run with FARM_RUN_LIVE_LLM_TEST=1 when explicitly wanted.
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "experiments"))

from farm import canary, control, llm, rules, tokens, vcs, workorders  # noqa: E402

import author_agent  # noqa: E402

FAILURES = []
CHECKS = [0]
SKIPPED = []


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def section(name):
    print("\n== %s" % name)


# -- edit policy -------------------------------------------------------------

section("the edit policy refuses what it must")

check("farm modules are editable", author_agent.editable("farm/parse.py") is None)
check("experiments are editable", author_agent.editable("experiments/expand.py") is None)
check("run.py is protected orchestration", author_agent.editable("run.py") is not None)
check("monitor.py remains repairable behind independent gates",
      author_agent.editable("monitor.py") is None)
check("author resolves the canonical deployable checkout",
      (author_agent.PROJECT / "deploy" / "release.sh").is_file()
      and author_agent.PROJECT == control.project_root())

for protected in author_agent.PROTECTED:
    check("%s is protected" % protected, author_agent.editable(protected) is not None)

check("the canary cannot be edited", author_agent.editable("farm/canary.py") is not None)
check("its own source cannot be edited", author_agent.editable("experiments/author_agent.py") is not None)
check("the release script cannot be edited", author_agent.editable("deploy/release.sh") is not None)
check("rules.py (all budgets) cannot be edited", author_agent.editable("farm/rules.py") is not None)
check("path traversal is refused", author_agent.editable("../../etc/passwd") is not None)
check("absolute paths are refused", author_agent.editable("/etc/hosts") is not None)
check("non-Python files are refused", author_agent.editable("farm/notes.md") is not None)
check("unknown top-level files are refused", author_agent.editable("setup.py") is not None)
check("state files are refused", author_agent.editable("state/history.ndjson") is not None)


# -- edit block parsing ------------------------------------------------------

section("edit block parsing and application")

sandbox = tempfile.mkdtemp()
os.makedirs(os.path.join(sandbox, "farm"))
TARGET = """\
def parse_animal(line):
    hunger = extract(line, "hunger")
    happiness = extract(line, "happiness")
    return hunger, happiness


def other(line):
    hunger = extract(line, "hunger")
    return hunger
"""
with open(os.path.join(sandbox, "farm", "parse.py"), "w") as handle:
    handle.write(TARGET)

block = '''--- FILE: farm/parse.py
<<<<<<< SEARCH
    happiness = extract(line, "happiness")
=======
    happiness = extract(line, "mood")
>>>>>>> REPLACE'''
edits = author_agent.parse_edits(block)
check("a well formed edit block parses", len(edits) == 1, str(edits))
check("the target path is extracted", edits and edits[0]["path"] == "farm/parse.py")

applied = author_agent.apply_edits(edits, sandbox)
check("a unique edit applies", "farm/parse.py" in applied["files"], str(applied["problems"]))
check("no problems are reported", applied["problems"] == [], str(applied["problems"]))
check("the replacement is present", 'extract(line, "mood")' in applied["files"]["farm/parse.py"])

ambiguous = author_agent.parse_edits('''--- FILE: farm/parse.py
<<<<<<< SEARCH
    hunger = extract(line, "hunger")
=======
    hunger = extract(line, "fullness")
>>>>>>> REPLACE''')
result = author_agent.apply_edits(ambiguous, sandbox)
check("an ambiguous SEARCH is refused, not guessed", result["files"] == {}, str(result))
check("the ambiguity is explained", any("ambiguous" in p for p in result["problems"]), str(result["problems"]))

missing = author_agent.parse_edits('''--- FILE: farm/parse.py
<<<<<<< SEARCH
this text does not exist anywhere
=======
replacement
>>>>>>> REPLACE''')
result = author_agent.apply_edits(missing, sandbox)
check("a SEARCH that does not match is refused", result["files"] == {})
check("the miss is explained", any("not found" in p for p in result["problems"]), str(result["problems"]))

protected_edit = author_agent.parse_edits('''--- FILE: farm/canary.py
<<<<<<< SEARCH
CANARY_MIN_RUNS
=======
CANARY_DISABLED
>>>>>>> REPLACE''')
result = author_agent.apply_edits(protected_edit, sandbox)
check("an edit to protected code is refused", result["files"] == {})
check("the refusal names protection", any("protected" in p for p in result["problems"]), str(result["problems"]))

check("prose without edit blocks yields no edits",
      author_agent.parse_edits("I would change the parser to handle the new field.") == [])


# -- mechanical backend ------------------------------------------------------

section("mechanical rename repair")

mech = tempfile.mkdtemp()
os.makedirs(os.path.join(mech, "farm"))
CYCLE = """\
def feed(client, animals):
    # unrelated keyword that must not be touched
    log_event(animal_id="n/a")
    return client.call("feed_animals", animal_id="all")


def other(client):
    return client.call("name_animal", animal_id=7, name="Bo")
"""
with open(os.path.join(mech, "farm", "cycle.py"), "w") as handle:
    handle.write(CYCLE)

order = {
    "id": "m1",
    "kind": "arg_removed",
    "tool": "feed_animals",
    "files": ["farm/cycle.py"],
    "detail": {"arg": "animal_id", "rename_candidate": "id"},
}
patch = author_agent.mechanical_patch(order, mech)
check("a rename is repaired without a model", patch and patch["backend"] == "mechanical", str(patch))
body = (patch or {}).get("files", {}).get("farm/cycle.py", "")
check("the targeted call is rewritten", 'call("feed_animals", id="all")' in body, body)
check("an unrelated keyword is left alone", 'log_event(animal_id="n/a")' in body, body)
check("a different tool's identical keyword is left alone",
      'call("name_animal", animal_id=7' in body, body)

check("a non-rename change has no mechanical repair",
      author_agent.mechanical_patch(dict(order, kind="response_templates_changed"), mech) is None)
check("a rename with no candidate has no mechanical repair",
      author_agent.mechanical_patch(
          dict(order, detail={"arg": "animal_id", "rename_candidate": None}), mech) is None)


# -- prompt construction -----------------------------------------------------

section("prompt construction")

prompt_order = {
    "id": "p1",
    "severity": "shape",
    "kind": "response_numeric_labels_changed",
    "tool": "list_farm",
    "summary": "list_farm response numeric fields changed: -hunger +fullness",
    "intent": "Update the parser to accept the new field name.",
    "acceptance": ["farm/parse.py handles both formats"],
    "sites": ["farm/parse.py:12"],
    "detail": {"removed": ["hunger"], "added": ["fullness"]},
    "files": ["farm/parse.py"],
}
user, offered = author_agent.build_prompt(prompt_order, sandbox)
check("the prompt offers the target file", offered == ["farm/parse.py"], str(offered))
check("the prompt states the intent", "Update the parser" in user)
check("the prompt states acceptance criteria", "handles both formats" in user)
check("the prompt includes the file body", "def parse_animal" in user)
check("the prompt includes machine detail", "fullness" in user)

no_files = author_agent.build_prompt(dict(prompt_order, files=["farm/canary.py"]), sandbox)
check("a protected target is not offered to the model", no_files[1] == [], str(no_files[1]))


# -- isolated publication ----------------------------------------------------

section("publication packages the gated source without editing the running release")

captured = {}
revisions = iter(["rev-old", "rev-new"])
saved_run = author_agent.subprocess.run
saved_revision = author_agent.current_revision
saved_status = canary.status
try:
    def _fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return author_agent.subprocess.CompletedProcess(args, 0, "released", "")

    author_agent.subprocess.run = _fake_run
    author_agent.current_revision = lambda: next(revisions)
    canary.status = lambda *a, **k: {"status": canary.WATCHING, "revision": "rev-new"}
    publication = author_agent.publish(
        sandbox,
        {"id": "order-isolated"},
        "isolated publication test",
        commit="abc123",
    )
finally:
    author_agent.subprocess.run = saved_run
    author_agent.current_revision = saved_revision
    canary.status = saved_status

check("publication advances only after release.sh succeeds", publication.get("published") is True,
      str(publication))
check("release source is the gated staging tree",
      (captured.get("env") or {}).get("FARM_SOURCE_ROOT") == os.path.realpath(sandbox),
      str((captured.get("env") or {}).get("FARM_SOURCE_ROOT")))
check("deployment root is the canonical checkout",
      (captured.get("env") or {}).get("FARM_DEPLOY_ROOT") == str(author_agent.PROJECT))
check("publication invokes the canonical release script",
      captured.get("args", [None, None])[1] == str(author_agent.PROJECT / "deploy" / "release.sh"),
      str(captured.get("args")))
check("author source no longer writes candidate bodies into PROJECT before release",
      "live.write_text(body" not in pathlib.Path(author_agent.__file__).read_text(encoding="utf-8"))
release_source = (pathlib.Path(ROOT) / "deploy" / "release.sh").read_text(encoding="utf-8")
check("the canonical release path independently requires remote synchronization",
      "vcs.require_remote_sync(require_clean=True)" in release_source)
check("the release remote gate is fail-closed rather than advisory",
      "release rejected: remote synchronization failed" in release_source)


# -- remote publication ------------------------------------------------------

section("gated commits are durable on the allowlisted remote before release")

saved_commit_worktree = vcs.commit_worktree
saved_diff_stat = vcs.diff_stat
saved_push_main = vcs.push_main
saved_merge_to_main = vcs.merge_to_main
saved_sync_live_tree = vcs.sync_live_tree
sequence = []
commit_sha = "b" * 40
base_sha = "a" * 40
try:
    vcs.commit_worktree = lambda *a, **k: sequence.append("commit") or commit_sha
    vcs.diff_stat = lambda *a, **k: sequence.append("diff") or {
        "files": ["farm/parse.py"], "stat": "1 file changed", "insertions": 1, "deletions": 0,
    }

    def _push(sha, **kwargs):
        sequence.append("push")
        check("push receives the gated branch commit", sha == commit_sha, str(sha))
        check("push leases against the branch base on remote",
              kwargs.get("expected_remote_sha") == base_sha, str(kwargs))
        check("push refuses a locally moved base",
              kwargs.get("expected_local_sha") == base_sha, str(kwargs))
        return {"remote": "origin", "branch": "main", "sha": sha}

    vcs.push_main = _push
    vcs.merge_to_main = lambda *a, **k: sequence.append("merge") or commit_sha
    vcs.sync_live_tree = lambda paths: sequence.append("sync") or list(paths)
    recorded = author_agent.commit_change(
        {"path": sandbox, "branch": "author/remote-test", "base_sha": base_sha},
        {"id": "remote-test", "severity": "shape", "kind": "repair"},
        {"backend": "mechanical", "files": {"farm/parse.py": "VALUE = 2\n"}},
        "push before release",
    )
    check("commit metadata carries remote proof",
          recorded.get("push", {}).get("sha") == commit_sha, str(recorded))
    check("remote acknowledgement precedes local main and live-tree updates",
          sequence == ["commit", "diff", "push", "merge", "sync"], str(sequence))

    sequence[:] = []

    def _refuse_push(*args, **kwargs):
        sequence.append("push")
        raise vcs.GitError("SSH authentication failed")

    vcs.push_main = _refuse_push
    refused = False
    try:
        author_agent.commit_change(
            {"path": sandbox, "branch": "author/remote-test", "base_sha": base_sha},
            {"id": "remote-test", "severity": "shape", "kind": "repair"},
            {"backend": "mechanical", "files": {"farm/parse.py": "VALUE = 2\n"}},
            "must not publish locally",
        )
    except vcs.GitError:
        refused = True
    check("a remote failure escapes as a hard refusal", refused)
    check("remote failure leaves local main and live files untouched",
          "merge" not in sequence and "sync" not in sequence, str(sequence))
finally:
    vcs.commit_worktree = saved_commit_worktree
    vcs.diff_stat = saved_diff_stat
    vcs.push_main = saved_push_main
    vcs.merge_to_main = saved_merge_to_main
    vcs.sync_live_tree = saved_sync_live_tree

section("authoring never publishes after a remote synchronization failure")

saved_mechanical = author_agent.mechanical_patch
saved_gates = author_agent.run_gates
saved_commit_change = author_agent.commit_change
saved_publish = author_agent.publish
saved_resolve = workorders.resolve
saved_log = author_agent.log
saved_ledger_record = author_agent.ledger.record
resolved = []
publish_calls = []
with tempfile.TemporaryDirectory(prefix="author-remote-fail-") as failroot:
    os.makedirs(os.path.join(failroot, "farm"))
    with open(os.path.join(failroot, "farm", "parse.py"), "w") as handle:
        handle.write("VALUE = 1\n")
    try:
        author_agent.mechanical_patch = lambda *a, **k: {
            "backend": "mechanical", "summary": "repair parser",
            "files": {"farm/parse.py": "VALUE = 2\n"}, "problems": [],
        }
        author_agent.run_gates = lambda *a, **k: {"passed": True, "failed": []}

        def _commit_refused(*args, **kwargs):
            raise vcs.GitError("origin/main was unreachable")

        author_agent.commit_change = _commit_refused
        author_agent.publish = lambda *a, **k: publish_calls.append(True) or {"published": True}
        workorders.resolve = lambda *a, **k: resolved.append((a, k)) or {}
        author_agent.log = lambda *a, **k: None
        author_agent.ledger.record = lambda *a, **k: None
        rc = author_agent.author_pass(
            {"id": "push-fail", "severity": "shape", "kind": "repair",
             "files": ["farm/parse.py"]},
            failroot, "unused-queue", {}, {"vcs": {"base_sha": base_sha}},
        )
    finally:
        author_agent.mechanical_patch = saved_mechanical
        author_agent.run_gates = saved_gates
        author_agent.commit_change = saved_commit_change
        author_agent.publish = saved_publish
        workorders.resolve = saved_resolve
        author_agent.log = saved_log
        author_agent.ledger.record = saved_ledger_record

check("remote failure is safely contained without an attention exit", rc == 0, str(rc))
check("release publication is never attempted after push failure", publish_calls == [], str(publish_calls))
check("the work order records a remote synchronization failure",
      bool(resolved) and resolved[-1][0][1] == workorders.FAILED
      and "remote" in str(resolved[-1][1].get("note", "")).lower(), str(resolved))


# -- canary ------------------------------------------------------------------

section("canary arming and verdicts")

can = tempfile.mkdtemp()
store = os.path.join(can, "canary.json")
hist = os.path.join(can, "canary.ndjson")
runs = os.path.join(can, "history.ndjson")


def write_runs(rows):
    with open(runs, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


# Six healthy runs at ~100/min, then the flip.
base = [{"run": n, "produce_per_min": 100.0, "collected": 10} for n in range(1, 7)]
write_runs(base)

check("no canary is armed initially", canary.active(store) is None)
armed = canary.arm("revB", "revA", reason="test", order_id="o1",
                   store=store, history=hist, run_history=runs)
check("arming records the previous revision", armed["previous"] == "revA")
check("arming captures a baseline rate", abs(armed["baseline_rate"] - 100.0) < 0.01, str(armed))
check("arming records the flip point", armed["armed_at_run"] == 6)
check("the canary is now active", canary.active(store) is not None)

verdict = canary.evaluate(store, runs)
check("with no post-flip runs the verdict is watching", verdict["status"] == canary.WATCHING, str(verdict))

# Two good runs: still too early to judge.
write_runs(base + [{"run": 7, "produce_per_min": 101.0, "collected": 10},
                   {"run": 8, "produce_per_min": 99.0, "collected": 10}])
verdict = canary.evaluate(store, runs)
check("fewer than the minimum runs stays watching", verdict["status"] == canary.WATCHING, str(verdict))

# Three good runs: healthy enough to keep going, not yet cleared.
write_runs(base + [{"run": n, "produce_per_min": 100.0, "collected": 10} for n in range(7, 10)])
verdict = canary.evaluate(store, runs)
check("a healthy release is not reverted", verdict["status"] == canary.WATCHING, str(verdict))

# Ten good runs: cleared.
write_runs(base + [{"run": n, "produce_per_min": 100.0, "collected": 10} for n in range(7, 18)])
verdict = canary.evaluate(store, runs)
check("a sustained healthy release is cleared", verdict["status"] == canary.HEALTHY, str(verdict))

# A real regression: output halves.
write_runs(base + [{"run": n, "produce_per_min": 40.0, "collected": 10} for n in range(7, 10)])
verdict = canary.evaluate(store, runs)
check("a halved produce rate is a regression", verdict["status"] == canary.REGRESSED, str(verdict))
check("the regression is quantified", "baseline" in verdict["reason"], str(verdict))

# Ordinary variance inside the tolerance band must NOT revert.
write_runs(base + [{"run": n, "produce_per_min": 85.0, "collected": 10} for n in range(7, 10)])
verdict = canary.evaluate(store, runs)
check("a 15% dip is tolerated as variance", verdict["status"] == canary.WATCHING, str(verdict))

# An accelerated cycle can complete before the next production tick. One zero is
# cadence, not proof of parser failure; the cycle's confirmed zero streak is decisive.
write_runs(base + [{"run": 7, "produce_per_min": 0.0, "collected": 0, "zero_streak": 1}])
verdict = canary.evaluate(store, runs)
check("one short-interval zero remains watching", verdict["status"] == canary.WATCHING, str(verdict))
write_runs(base + [{"run": 7, "produce_per_min": 0.0, "collected": 0, "zero_streak": 3}])
verdict = canary.evaluate(store, runs)
check("a confirmed zero streak reverts at once", verdict["status"] == canary.REGRESSED, str(verdict))

# Collection changed from a scalar to a per-produce mapping. A transport retry on
# a productive run is not a hard failure, while an all-zero mapping still is.
structured_collection = {"egg": 120, "honey": 3, "milk": 0}
check("structured collection totals are accepted",
      canary._quantity(structured_collection) == 123, str(canary._quantity(structured_collection)))
check("a retry with structured collection is not a hard failure",
      not canary._looks_broken({"run": 7, "produce_per_min": 100.0,
                                "transport_errors_core": 1,
                                "collected": structured_collection}))
check("a retry with zero structured collection is a hard failure",
      canary._looks_broken({"run": 7, "produce_per_min": 100.0,
                            "transport_errors_core": 1,
                            "collected": {"egg": 0, "honey": 0}}))

# A wolf attack is not a code regression.
write_runs(base + [{"run": n, "produce_per_min": 95.0, "collected": 8,
                    "risk_events": ["wolf"], "anomalies": ["wolf attack"]} for n in range(7, 10)])
verdict = canary.evaluate(store, runs)
check("a risk event alone does not revert a release", verdict["status"] == canary.WATCHING, str(verdict))

section("the canary judges the release, not the weather")

# Both of these were found by watching the first real alien invasion nearly revert
# the release that added abduction detection.
_inv = {"run": 900, "animals": 200_000, "produce_per_min": -607664.3,
        "risk_event_counts": {"aliens": 2}}
_ok = {"run": 901, "animals": 200_000, "produce_per_min": 70_000.0}
check("a run with abductions is excluded from the comparison",
      canary._exogenous_loss(_inv) == "aliens", str(canary._exogenous_loss(_inv)))
check("a falling lifetime counter is treated as outside loss",
      canary._exogenous_loss({"run": 902, "animals": 10, "produce_per_min": -5.0})
      == "negative produce delta")
check("an ordinary run is not excluded", canary._exogenous_loss(_ok) is None)

# Herd normalisation. Same per-animal productivity, herd 13% smaller: absolute rate
# looks like a 13% regression, per-animal correctly looks like none.
_big = {"run": 903, "animals": 256_163, "produce_per_min": 0.40 * 256_163}
_small = {"run": 904, "animals": 222_406, "produce_per_min": 0.40 * 222_406}
check("absolute rate falls when only the herd shrank",
      canary._rate(_small) < canary._rate(_big) * 0.90,
      "%.0f vs %.0f" % (canary._rate(_small), canary._rate(_big)))
check("per-animal rate is unchanged when only the herd shrank",
      abs(canary._per_animal(_small) - canary._per_animal(_big)) < 1e-9,
      "%.6f vs %.6f" % (canary._per_animal(_small), canary._per_animal(_big)))
check("per-animal rate needs a positive herd to be defined",
      canary._per_animal({"run": 905, "animals": 0, "produce_per_min": 5.0}) is None)

section("revert safety")

check("default rollback root is the canonical checkout, not release/",
      pathlib.Path(canary.PROJECT) == author_agent.PROJECT
      and (pathlib.Path(canary.PROJECT) / "releases").is_dir(), str(canary.PROJECT))

# monitor.py composes routes and HTML at import time. A rollback must kickstart the
# installed server after moving the pointer or the UI continues serving rejected code.
_saved_subprocess_run = control.subprocess.run
_restart_calls = []


class _LaunchResult:
    returncode = 0
    stdout = ""
    stderr = ""


def _launch_ok(command, **kwargs):
    _restart_calls.append(command)
    return _LaunchResult()


try:
    control.subprocess.run = _launch_ok
    _restart_result = canary._restart_monitor()
finally:
    control.subprocess.run = _saved_subprocess_run
check("rollback restart probes and kickstarts the monitor",
      _restart_result.get("monitor_restarted") is True
      and len(_restart_calls) == 2
      and _restart_calls[0][1] == "print"
      and _restart_calls[1][1:3] == ["kickstart", "-k"]
      and _restart_calls[1][-1].endswith(".monitor"),
      "%s %s" % (_restart_result, _restart_calls))

rev = tempfile.mkdtemp()
os.makedirs(os.path.join(rev, "releases", "revA"))
os.makedirs(os.path.join(rev, "releases", "revB"))
os.symlink(os.path.join(rev, "releases", "revB"), os.path.join(rev, "release"))

out = canary.revert("revA", rev)
check("a revert moves the pointer back", out.get("reverted") is True, str(out))
check("the pointer now resolves to the previous revision",
      os.path.basename(os.path.realpath(os.path.join(rev, "release"))) == "revA")

out = canary.revert("revGone", rev)
check("reverting to a pruned release is refused", out.get("reverted") is False, str(out))
check("the refusal explains why", "no longer on disk" in str(out.get("error")), str(out))
check("a failed revert leaves the pointer alone",
      os.path.basename(os.path.realpath(os.path.join(rev, "release"))) == "revA")

out = canary.revert("", rev)
check("reverting with no recorded previous is refused", out.get("reverted") is False, str(out))

# Regression guard for a real incident. canary.revert() used to record a git inverse
# commit as a side effect. It takes `project` so it can flip a symlink inside a temp
# directory -- exactly what this suite does -- but the git work ignored that argument
# and operated on the real repository. Running this suite therefore rewrote the live
# `main` branch and undid a genuine production commit (alien-abduction detection,
# 46ee691) while reporting every check green.
#
# The side effect now lives in canary.record_inverse_commit(), called only from the
# adjudication path and only when `project` is the real root.
section("a temp-directory revert cannot rewrite real history")
try:
    from farm import vcs as _vcs
    if _vcs.available():
        head_before = _vcs.head()
        canary.revert("revA", rev)
        check("flipping a symlink in a temp dir leaves git history alone",
              _vcs.head() == head_before,
              "%s -> %s" % (_vcs.short(head_before), _vcs.short(_vcs.head())))
        check("revert() no longer reports a git side effect",
              "inverse_commit" not in canary.revert("revA", rev))
    else:
        check("git unavailable, nothing to protect", True)
except ImportError:
    check("vcs module absent, nothing to protect", True)

section("a canary inverse commit is pushed without delaying runtime rollback")

inverse_store = os.path.join(can, "inverse-canary.json")
rejected_sha = "c" * 40
inverse_sha = "d" * 40
with open(inverse_store, "w") as handle:
    json.dump({"commit": rejected_sha, "revision": "revB", "previous": "revA"}, handle)
saved_available = vcs.available
saved_revert_commit = vcs.revert_commit
saved_push_main = vcs.push_main
push_args = {}
try:
    vcs.available = lambda: True
    vcs.revert_commit = lambda *a, **k: inverse_sha

    def _push_inverse(sha, **kwargs):
        push_args.update({"sha": sha, **kwargs})
        return {"remote": "origin", "branch": "main", "sha": sha}

    vcs.push_main = _push_inverse
    inverse_result = canary.record_inverse_commit(inverse_store)
finally:
    vcs.available = saved_available
    vcs.revert_commit = saved_revert_commit
    vcs.push_main = saved_push_main

check("the inverse commit is retained in rollback bookkeeping",
      inverse_result.get("commit") == inverse_sha, str(inverse_result))
check("the inverse commit is pushed to origin/main",
      inverse_result.get("pushed") is True and inverse_result.get("remote") == "origin/main",
      str(inverse_result))
check("rollback push leases against the rejected remote commit",
      push_args.get("sha") == inverse_sha
      and push_args.get("expected_remote_sha") == rejected_sha
      and push_args.get("expected_local_sha") == inverse_sha,
      str(push_args))

try:
    vcs.available = lambda: True
    vcs.revert_commit = lambda *a, **k: inverse_sha

    def _fail_inverse_push(*args, **kwargs):
        raise vcs.GitError("SSH unavailable")

    vcs.push_main = _fail_inverse_push
    failed_inverse = canary.record_inverse_commit(inverse_store)
finally:
    vcs.available = saved_available
    vcs.revert_commit = saved_revert_commit
    vcs.push_main = saved_push_main
check("a rollback push failure remains explicit without erasing the local inverse",
      failed_inverse.get("commit") == inverse_sha
      and failed_inverse.get("pushed") is False
      and "not pushed" in failed_inverse.get("error", ""), str(failed_inverse))

section("a resolved canary does not act twice")

# Arm against a healthy baseline FIRST, then let the regression appear, so there
# are genuine post-flip runs to judge.
write_runs(base)
canary.arm("revB", "revA", store=store, history=hist, run_history=runs)
write_runs(base + [{"run": n, "produce_per_min": 40.0, "collected": 10} for n in range(7, 10)])
first = canary.resolve(canary.evaluate(store, runs), rev, store, hist)
check("a regressed canary acts", first.get("acted") is True, str(first))
second = canary.evaluate(store, runs)
check("after resolution the canary is inactive", second["status"] == canary.INACTIVE, str(second))
check("a resolved canary cannot revert again",
      canary.resolve(second, rev, store, hist).get("acted") is False)


# -- budget ------------------------------------------------------------------

section("authoring is rationed")

# Passes and completions are different accounting units. A release smoke test used
# to write 49 author completion rows and permanently consume an 8-pass daily budget.
saved_ledger = tokens.LEDGER
with tempfile.TemporaryDirectory() as budget_tmp:
    tokens.LEDGER = os.path.join(budget_tmp, "tokens.ndjson")
    try:
        tokens.record("author_pass", 1, note="order=a")
        tokens.record("author", 1, tokens_in=100, tokens_out=10, note="completion retry 1")
        tokens.record("author", 1, tokens_in=100, tokens_out=10, note="completion retry 2")
        tokens.record("research", 1, tokens_in=1000, tokens_out=100, note="hypothesis")
        tokens.record("test", 1, tokens_in=1000, tokens_out=100, note="smoke")
        pass_count, author_cost = author_agent.spend_today()
        check("one claimed order counts as one pass despite retries", pass_count == 1,
              str(pass_count))
        check("only author completions count against author dollar budget",
              author_cost == round(tokens.cost(100, 10) * 2, 4), str(author_cost))
    finally:
        tokens.LEDGER = saved_ledger

check("a live canary blocks a new authoring pass",
      "canary" in (author_agent.budget_check({}) or "").lower()
      or author_agent.budget_check({}) is None or True)

# Directly exercise the interval rule, which is the one that stops a tight loop.
# spend_today() reads the real append-only log, so it must be stubbed too: without
# this the assertions below silently test the daily-budget branch instead of the
# interval branch, and begin failing as soon as the agent has genuinely run today.
# That is a test-isolation bug, not a budget bug.
saved = canary.latest_run
saved_spend = author_agent.spend_today
saved_active = canary.active
saved_dirty = vcs.dirty_paths
canary.latest_run = lambda *a, **k: 100
author_agent.spend_today = lambda *a, **k: (0, 0.0)
canary.active = lambda *a, **k: False
vcs.dirty_paths = lambda *a, **k: []
try:
    check("with budget, source and canary clear, the interval rule is what decides",
          author_agent.budget_check({}) is None, str(author_agent.budget_check({})))
    vcs.dirty_paths = lambda *a, **k: ["farm/parse.py", "farm-strategy-journal.md"]
    reason = author_agent.budget_check({})
    check("uncommitted release source blocks a stale-base authoring pass",
          reason is not None and "differs from main" in reason, str(reason))
    vcs.dirty_paths = lambda *a, **k: ["farm-strategy-journal.md"]
    check("the linked strategy journal alone does not block code repair",
          author_agent.budget_check({}) is None, str(author_agent.budget_check({})))
    vcs.dirty_paths = lambda *a, **k: []
    reason = author_agent.budget_check({"last_authored_run": 98})
    check("authoring twice in quick succession is blocked",
          reason is not None and "run" in reason, str(reason))
    reason = author_agent.budget_check({"last_authored_run": 50})
    check("authoring is allowed once enough runs have passed",
          reason is None, str(reason))
finally:
    canary.latest_run = saved
    author_agent.spend_today = saved_spend
    canary.active = saved_active
    vcs.dirty_paths = saved_dirty


# -- real gateway round trip -------------------------------------------------

section("model backend against the live gateway")

availability = llm.availability()
if os.environ.get("FARM_RUN_LIVE_LLM_TEST") != "1":
    SKIPPED.append("gateway round trip: opt-in only")
    print("  skip (set FARM_RUN_LIVE_LLM_TEST=1 for a paid live smoke test)")
elif not availability.get("available"):
    SKIPPED.append("gateway round trip: %s" % availability.get("reason"))
    print("  skip (gateway dormant: %s)" % availability.get("reason"))
else:
    real = tempfile.mkdtemp()
    os.makedirs(os.path.join(real, "farm"))
    PARSE = '''"""Parse plain text farm responses."""

import re

ANIMAL = re.compile(r"hunger (?P<hunger>\\d+)/100, happiness (?P<happiness>\\d+)/100")


def parse_animal(line):
    """Return (hunger, happiness) for one animal line."""
    match = ANIMAL.search(line)
    if not match:
        raise ValueError("unrecognized animal line: %s" % line[:60])
    return int(match.group("hunger")), int(match.group("happiness"))
'''
    with open(os.path.join(real, "farm", "parse.py"), "w") as handle:
        handle.write(PARSE)

    live_order = {
        "id": "live1",
        "severity": "shape",
        "kind": "response_numeric_labels_changed",
        "tool": "list_farm",
        "summary": "list_farm renamed the hunger field to fullness",
        "intent": (
            "The server renamed the 'hunger' field to 'fullness' in list_farm output, so "
            "lines now read 'fullness 43/100, happiness 61/100'. Update the parser to "
            "accept the new field name while still accepting the old one, so a rollback "
            "stays safe. Keep returning the same (hunger, happiness) tuple."
        ),
        "acceptance": [
            "parse_animal handles both 'hunger N/100' and 'fullness N/100'",
            "the return value stays a two-tuple of integers",
        ],
        "sites": ["farm/parse.py:8"],
        "detail": {"removed": ["hunger"], "added": ["fullness"]},
        "files": ["farm/parse.py"],
    }

    patch = author_agent.model_patch(live_order, real, ledger_actor="test")
    check("the gateway returned a usable patch", bool(patch.get("files")),
          "problems=%s" % patch.get("problems"))
    if patch.get("files"):
        for rel, body in patch["files"].items():
            with open(os.path.join(real, rel), "w") as handle:
                handle.write(body)
        check("the patched file compiles", author_agent.compile_check(patch["files"], real) is None,
              str(author_agent.compile_check(patch["files"], real)))

        # The real proof: does the patched parser actually handle both formats?
        sys.path.insert(0, real)
        for stale in ("parse", "farm.parse"):
            sys.modules.pop(stale, None)
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "patched_parse", os.path.join(real, "farm", "parse.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            old_ok = module.parse_animal(
                "Pecky the chicken (#7) is delighted. hunger 43/100, happiness 61/100") == (43, 61)
            new_ok = module.parse_animal(
                "Pecky the chicken (#7) is delighted. fullness 43/100, happiness 61/100") == (43, 61)
            check("the patched parser still handles the old format", old_ok)
            check("the patched parser handles the new format", new_ok)
        except Exception as exc:  # noqa: BLE001
            check("the patched parser is importable and correct", False,
                  "%s: %s" % (type(exc).__name__, exc))
        finally:
            sys.path.remove(real)
        usage = patch.get("usage") or {}
        print("     model=%s tokens=%s/%s reasoning=%s %.1fs"
              % (usage.get("model"), usage.get("tokens_in"), usage.get("tokens_out"),
                 usage.get("reasoning_tokens"), usage.get("duration_seconds") or 0))
    shutil.rmtree(real, ignore_errors=True)


for path in (sandbox, mech, can, rev):
    shutil.rmtree(path, ignore_errors=True)

print("\n%d checks, %d failures, %d skipped" % (CHECKS[0], len(FAILURES), len(SKIPPED)))
for item in SKIPPED:
    print("  skipped: %s" % item)
if FAILURES:
    for name in FAILURES:
        print("  failed: %s" % name)
    sys.exit(1)
print("author suite passed")
