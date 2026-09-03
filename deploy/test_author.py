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
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "experiments"))

from farm import canary, compatibility, control, evaluation, gates, llm, rules, tokens, vcs, workorders  # noqa: E402

import author_agent  # noqa: E402
import research_agent  # noqa: E402

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

check("runtime farm modules require independent approval",
      author_agent.editable("farm/parse.py") is not None)
check("scheduled expansion code requires independent approval",
      author_agent.editable("experiments/expand.py") is not None)
check("literal capability policies require independent approval",
      author_agent.editable("experiments/capability_policies.py") is not None)
check("unscheduled probe candidates remain model-editable only inside the sandbox",
      author_agent.editable("experiments/new_bounded_probe.py") is None)
check("the capability executor is protected",
      author_agent.editable("farm/mechanics.py") is not None)
check("literal strategy policy requires independent approval",
      author_agent.editable("experiments/strategy_policy.py") is not None)
check("strategy policy loader is protected",
      author_agent.editable("farm/strategy.py") is not None)
check("run.py is protected orchestration", author_agent.editable("run.py") is not None)
check("the long-running monitor requires independent approval",
      author_agent.editable("monitor.py") is not None)
check("author resolves the canonical deployable checkout",
      (author_agent.PROJECT / "deploy" / "release.sh").is_file()
      and author_agent.PROJECT == control.project_root())

for protected in author_agent.PROTECTED:
    check("%s is protected" % protected, author_agent.editable(protected) is not None)

# Independent expectations catch a path accidentally omitted from the manifest;
# deriving this set from author_agent.PROTECTED would merely prove the list agrees
# with itself.
required_tcb = {
    "farm/__init__.py", "farm/control.py", "farm/format_compat.py", "farm/gates.py",
    "farm/governance.py", "farm/journal.py", "farm/mcp.py", "farm/parse.py",
    "farm/policy.py", "farm/probe_guard.py", "farm/probes.py", "farm/sandbox.py",
    "farm/staged_verify.py", "monitor.py", "experiments/__init__.py",
    "experiments/activity_probe.py", "experiments/author_agent.py",
    "experiments/crop_score_probe.py", "experiments/crop_timer_probe.py",
    "experiments/dual_cap_audit.py", "experiments/endgame.py", "experiments/expand.py",
    "experiments/registry.py", "deploy/prepare_activation.py", "deploy/release.sh",
    "deploy/run_sandboxed.py",
    "deploy/test_probe_guard.py", "deploy/test_sandbox.py",
}
check("independent trusted-boundary manifest is fully protected",
      all(control.is_protected(path) for path in required_tcb),
      str(sorted(path for path in required_tcb if not control.is_protected(path))))
check("independent trusted-boundary manifest names real files",
      all((pathlib.Path(ROOT) / path).is_file() for path in required_tcb),
      str(sorted(path for path in required_tcb if not (pathlib.Path(ROOT) / path).is_file())))

check("the canary cannot be edited", author_agent.editable("farm/canary.py") is not None)
check("its own source cannot be edited", author_agent.editable("experiments/author_agent.py") is not None)
check("the release script cannot be edited", author_agent.editable("deploy/release.sh") is not None)
check("rules.py (all budgets) cannot be edited", author_agent.editable("farm/rules.py") is not None)
check("mechanics regression gate is mandatory for autonomous patches",
      any(name == "mechanics" and command[-1] == "deploy/test_mechanics.py"
          for name, command in author_agent.GATES))
check("dual-cap strategy gate is mandatory for autonomous patches",
      any(name == "strategy" and command[-1] == "deploy/test_strategy.py"
          for name, command in author_agent.GATES))
check("probe authority and sandbox gates are mandatory for autonomous patches",
      {"probe-guard", "sandbox"}.issubset({name for name, _ in author_agent.GATES}))
check("path traversal is refused", author_agent.editable("../../etc/passwd") is not None)
check("absolute paths are refused", author_agent.editable("/etc/hosts") is not None)
check("non-Python files are refused", author_agent.editable("farm/notes.md") is not None)
check("unknown top-level files are refused", author_agent.editable("setup.py") is not None)
check("state files are refused", author_agent.editable("state/history.ndjson") is not None)


# -- edit block parsing ------------------------------------------------------

section("edit block parsing and application")

sandbox = tempfile.mkdtemp()
os.makedirs(os.path.join(sandbox, "farm"))
os.makedirs(os.path.join(sandbox, "experiments"))
TARGET = """\
def parse_animal(line):
    hunger = extract(line, "hunger")
    happiness = extract(line, "happiness")
    return hunger, happiness


def other(line):
    hunger = extract(line, "hunger")
    return hunger
"""
with open(os.path.join(sandbox, "experiments", "parser_probe.py"), "w") as handle:
    handle.write(TARGET)

block = '''--- FILE: experiments/parser_probe.py
<<<<<<< SEARCH
    happiness = extract(line, "happiness")
=======
    happiness = extract(line, "mood")
>>>>>>> REPLACE'''
edits = author_agent.parse_edits(block)
check("a well formed edit block parses", len(edits) == 1, str(edits))
check("the target path is extracted", edits and edits[0]["path"] == "experiments/parser_probe.py")

applied = author_agent.apply_edits(edits, sandbox)
check("a unique edit applies", "experiments/parser_probe.py" in applied["files"], str(applied["problems"]))
check("no problems are reported", applied["problems"] == [], str(applied["problems"]))
check("the replacement is present", 'extract(line, "mood")' in applied["files"]["experiments/parser_probe.py"])

ambiguous = author_agent.parse_edits('''--- FILE: experiments/parser_probe.py
<<<<<<< SEARCH
    hunger = extract(line, "hunger")
=======
    hunger = extract(line, "fullness")
>>>>>>> REPLACE''')
result = author_agent.apply_edits(ambiguous, sandbox)
check("an ambiguous SEARCH is refused, not guessed", result["files"] == {}, str(result))
check("the ambiguity is explained", any("ambiguous" in p for p in result["problems"]), str(result["problems"]))

missing = author_agent.parse_edits('''--- FILE: experiments/parser_probe.py
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

outside = os.path.join(sandbox, "outside.py")
with open(outside, "w", encoding="utf-8") as handle:
    handle.write("VALUE = 'outside'\n")
link = os.path.join(sandbox, "experiments", "linked.py")
os.symlink(outside, link)
symlink_result = author_agent.apply_edits(
    [{"path": "experiments/linked.py", "search": "outside", "replace": "changed"}],
    sandbox,
    allowed=["experiments/linked.py"],
)
check("candidate edits cannot follow a symlink outside their path",
      not symlink_result["files"] and any("symlink" in p for p in symlink_result["problems"]),
      str(symlink_result))
check("refused symlink edit leaves the target unchanged",
      "outside" in pathlib.Path(outside).read_text(encoding="utf-8"))

check("prose without edit blocks yields no edits",
      author_agent.parse_edits("I would change the parser to handle the new field.") == [])

new_block = author_agent.parse_edits(
    "--- FILE: experiments/bounded_probe.py\n"
    + "<<<<<<< SEARCH\n\n"
    + "=======\n"
    + '\"\"\"Bounded generated probe.\"\"\"\n\nVALUE = 1\n'
    + ">>>>>>> REPLACE"
)
new_result = author_agent.apply_edits(
    new_block, sandbox, allowed=["experiments/bounded_probe.py"],
)
check("an explicitly offered Python file can be created",
      "experiments/bounded_probe.py" in new_result["files"], str(new_result))
check("new-file accounting measures the created bytes",
      0 < new_result["changed_bytes"] < 100, str(new_result["changed_bytes"]))
placeholder_new = author_agent.apply_edits(
    [{
        "path": "experiments/placeholder_probe.py",
        "search": "# new file",
        "replace": '"""Bounded generated probe."""\n\nVALUE = 2\n',
    }],
    sandbox,
    allowed=["experiments/placeholder_probe.py"],
)
check("a placeholder SEARCH cannot strand an explicitly offered new file",
      "experiments/placeholder_probe.py" in placeholder_new["files"]
      and placeholder_new["problems"] == [],
      str(placeholder_new))
empty_new = author_agent.apply_edits(
    [{"path": "experiments/empty_probe.py", "search": "# new file", "replace": ""}],
    sandbox,
    allowed=["experiments/empty_probe.py"],
)
check("a new file still requires a nonempty replacement",
      empty_new["files"] == {}
      and any("nonempty REPLACE" in p for p in empty_new["problems"]),
      str(empty_new))
refused_new = author_agent.apply_edits(
    new_block, sandbox, allowed=["experiments/different_probe.py"],
)
check("a model cannot invent an unoffered new path",
      refused_new["files"] == {} and any("not offered" in p for p in refused_new["problems"]),
      str(refused_new))
existing_empty = author_agent.apply_edits(
    [{"path": "experiments/parser_probe.py", "search": "", "replace": "VALUE = 3\n"}],
    sandbox,
    allowed=["experiments/parser_probe.py"],
)
check("empty SEARCH remains invalid for an existing file",
      existing_empty["files"] == {}
      and any("only valid for a new file" in p for p in existing_empty["problems"]),
      str(existing_empty))
large_existing = "PREFIX = 1\n" + ("# padding\n" * 6000)
with open(os.path.join(sandbox, "experiments", "large.py"), "w") as handle:
    handle.write(large_existing)
small_edit = [{"path": "experiments/large.py", "search": "PREFIX = 1", "replace": "PREFIX = 2"}]
small_result = author_agent.apply_edits(small_edit, sandbox, allowed=["experiments/large.py"])
check("patch accounting measures changed text rather than the whole output file",
      small_result["changed_bytes"] < 100
      and len(small_result["files"]["experiments/large.py"]) > 40_000,
      str(small_result.get("changed_bytes")))


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
os.makedirs(os.path.join(mech, "experiments"), exist_ok=True)
with open(os.path.join(mech, "experiments", "editable_call.py"), "w") as handle:
    handle.write('def call(client):\n    return client.call("feed_animals", animal_id="all")\n')
fallback_order = dict(order, files=["experiments/editable_call.py"])
check("an editable deterministic rename is admitted without model cost",
      author_agent.mechanical_patch(fallback_order, mech) is not None
      and author_agent.max_model_pass_cost(fallback_order, mech) == 0.0)
check("the same order has a bounded reservation if mechanical gates force model fallback",
      author_agent.model_pass_reservation(fallback_order, mech)["cost_usd"] > 0.0)


# -- prompt construction -----------------------------------------------------

section("prompt construction")

prompt_order = {
    "id": "p1",
    "severity": "shape",
    "kind": "response_numeric_labels_changed",
    "tool": "list_farm",
    "summary": "probe response numeric fields changed: -hunger +fullness",
    "intent": "Update the bounded probe parser to accept the new field name.",
    "acceptance": ["experiments/parser_probe.py handles both formats"],
    "sites": ["experiments/parser_probe.py:12"],
    "detail": {"removed": ["hunger"], "added": ["fullness"]},
    "files": ["experiments/parser_probe.py"],
}
user, offered = author_agent.build_prompt(prompt_order, sandbox)
check("the prompt offers the target file", offered == ["experiments/parser_probe.py"], str(offered))
check("the prompt states the intent", "Update the bounded probe parser" in user)
check("the prompt states acceptance criteria", "handles both formats" in user)
check("the prompt includes the file body", "def parse_animal" in user)
check("the prompt includes machine detail", "fullness" in user)

no_files = author_agent.build_prompt(dict(prompt_order, files=["farm/canary.py"]), sandbox)
check("a protected target is not offered to the model", no_files[1] == [], str(no_files[1]))
new_prompt, new_offered = author_agent.build_prompt(
    dict(prompt_order, files=["experiments/future_probe.py"]), sandbox,
)
check("a requested new Python path is explicitly offered",
      new_offered == ["experiments/future_probe.py"] and "--- NEW FILE:" in new_prompt,
      str(new_offered))
capability_order = research_agent.capability_proposal({
    "capability": "future_tool", "description": "fixture", "required": [], "args": [],
})
hypothesis_order = research_agent.hypothesis_proposal({
    "question_id": "q-fixture",
    "title": "Future Strategy", "hypothesis": "A future strategy helps.",
    "falsifier": "The outcome is flat.", "probe": "Replay a fixture.",
    "metric": "fixture output", "risk": "none",
})
check("research capability orders declare their new probe file",
      capability_order["files"][0] == "experiments/future_tool_probe.py",
      str(capability_order["files"]))
check("strategy hypothesis orders declare only their non-autonomous probe candidate",
      hypothesis_order["files"] == ["experiments/future_strategy_probe.py"]
      and hypothesis_order["question_ids"] == ["q-fixture"]
      and "experiments/registry.py" not in hypothesis_order["files"],
      str(hypothesis_order))
check("capability probe proposals cannot edit the protected registry",
      "experiments/registry.py" not in capability_order["files"],
      str(capability_order["files"]))


# -- gates -------------------------------------------------------------------

section("candidate gates fail closed")
saved_gates = author_agent.GATES
try:
    author_agent.GATES = (("missing", ["/usr/bin/python3", "deploy/does_not_exist.py"]),)
    missing_gate_result = author_agent.run_gates(sandbox)
finally:
    author_agent.GATES = saved_gates
check("a missing mandatory gate is a failure rather than a skip",
      not missing_gate_result["passed"]
      and missing_gate_result["failed"][0]["gate"] == "missing",
      str(missing_gate_result))

with tempfile.TemporaryDirectory(prefix="activation-guard-test-") as activation_tmp:
    activation_project = pathlib.Path(activation_tmp) / "project"
    activation_target = pathlib.Path(activation_tmp) / "target"
    (activation_project / "state").mkdir(parents=True)
    (activation_project / "deploy").mkdir()
    (activation_project / "deploy" / "release.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    previous_release = activation_project / "releases" / "rev-old"
    (previous_release / "farm").mkdir(parents=True)
    (activation_target / "farm").mkdir(parents=True)
    (previous_release / "farm" / "value.py").write_text("VALUE=1\n", encoding="utf-8")
    (activation_target / "farm" / "value.py").write_text("VALUE=2\n", encoding="utf-8")
    with (activation_project / "state" / "history.ndjson").open("w", encoding="utf-8") as handle:
        for run in range(1, 8):
            handle.write(json.dumps({
                "run": run, "animals": 100, "produce_per_min": 10.0,
                "collected": 1, "verified": True,
            }) + "\n")
    activation = subprocess.run(
        [
            sys.executable, str(pathlib.Path(ROOT) / "deploy" / "prepare_activation.py"),
            str(activation_target), str(activation_project), "rev-new", "rev-old",
            ROOT, str(pathlib.Path(ROOT) / "farm" / "gates.py"), "0",
        ],
        capture_output=True, text=True, timeout=30,
    )
    canary_path = activation_project / "state" / "canary.json"
    check("activation preparation writes the canonical canary before exposure",
          activation.returncode == 0 and canary_path.is_file()
          and not (activation_project / "state" / "state" / "canary.json").exists(),
          "rc=%s stdout=%s stderr=%s" % (activation.returncode, activation.stdout, activation.stderr))
    (activation_project / "state" / "workorders.ndjson").write_text(json.dumps({
        "id": "lease-test", "status": "claimed", "claim_token": "actual-token",
    }) + "\n")
    lease_env = dict(os.environ)
    lease_env.update({
        "FARM_WORKORDER_ID": "lease-test",
        "FARM_WORKORDER_CLAIM_TOKEN_SHA256": "0" * 64,
    })
    lost_lease = subprocess.run(
        [
            sys.executable, str(pathlib.Path(ROOT) / "deploy" / "prepare_activation.py"),
            str(activation_target), str(activation_project), "rev-lost-lease", "rev-old",
            ROOT, str(pathlib.Path(ROOT) / "farm" / "gates.py"), "0",
        ],
        env=lease_env, capture_output=True, text=True, timeout=30,
    )
    check("activation refuses a lost work-order lease before pointer exposure",
          lost_lease.returncode != 0 and "work-order lease" in lost_lease.stderr,
          "rc=%s stderr=%s" % (lost_lease.returncode, lost_lease.stderr))
    (activation_project / "state" / "history.ndjson").unlink()
    missing_history = subprocess.run(
        [
            sys.executable, str(pathlib.Path(ROOT) / "deploy" / "prepare_activation.py"),
            str(activation_target), str(activation_project), "rev-missing", "rev-old",
            ROOT, str(pathlib.Path(ROOT) / "farm" / "gates.py"), "1",
        ],
        capture_output=True, text=True, timeout=30,
    )
    check("activation cannot refresh inherited evidence without a current run identity",
          missing_history.returncode != 0 and "run-history identity" in missing_history.stderr,
          "rc=%s stderr=%s" % (missing_history.returncode, missing_history.stderr))


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
release_targets = {
    part for _, command in author_agent.GATES for part in command
    if part.startswith("deploy/")
}
check("manual and autonomous release matrices contain the same gate targets",
      all(target in release_source for target in release_targets),
      str(sorted(target for target in release_targets if target not in release_source)))
check("policy compatibility executes inside the candidate sandbox",
      "gate /usr/bin/python3 - <<'PY'" in release_source)
activation_source = (pathlib.Path(ROOT) / "deploy" / "prepare_activation.py").read_text(encoding="utf-8")
check("release preflight never imports candidate source with coordinator authority",
      'PYTHONPATH="$PREFLIGHT_RUNTIME"' in release_source
      and 'PYTHONPATH="$SCRIPT_PROJECT"' in release_source
      and 'PYTHONPATH="$SOURCE_PROJECT"' not in release_source)
check("release coordination imports the previously accepted safety kernel",
      'TRUSTED_RUNTIME="$RELEASES/$PREVIOUS"' in release_source
      and "prepare_activation.py" in release_source
      and "sys.path.insert(0, target)" not in release_source
      and "sys.path.insert(0, str(Path(trusted_runtime).resolve()))" in activation_source
      and "sys.path.insert(0, str(target" not in activation_source)
check("release guard state is durable before pointer exposure",
      release_source.index("prepare_activation.py") < release_source.index("os.replace(tmp, link)"))
check("release activation is serialized across coordinators",
      'RELEASE_LOCK="$DEPLOY_PROJECT/state/.release.lock"' in release_source
      and "another release process is active" in release_source)
check("interrupted pre-activation restores or recovers prior guard state",
      "recover_stale_release" in release_source
      and "restore_activation_guards" in release_source
      and "trap release_cleanup EXIT INT TERM" in release_source
      and '"guarded" > "$RELEASE_LOCK/phase"' in release_source)
check("the canonical release path independently requires remote synchronization",
      "vcs.require_remote_sync(require_clean=True)" in release_source)
check("the release remote gate is fail-closed rather than advisory",
      "release rejected: remote synchronization failed" in release_source)
check("detached release gates retain the canonical rollback root",
      'export FARM_PROJECT_ROOT="$DEPLOY_PROJECT"' in release_source)
check("release metadata and canary retain the complete source range",
      'SOURCE_COMMIT' in release_source
      and 'BASE_COMMIT' in release_source
      and 'FARM_CANARY_BASE_COMMIT' in release_source
      and '"$TARGET/SOURCE_COMMIT"' in release_source)
check("release canary can verify a named evidence-backed strategy action",
      "FARM_CANARY_STRATEGY_INTENT" in release_source and "strategy_intent=" in activation_source)


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
saved_renew_claim = author_agent._renew_claim
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
        author_agent._renew_claim = lambda order, queue: dict(order, claim_token="fixture-lease")
        author_agent.log = lambda *a, **k: None
        author_agent.ledger.record = lambda *a, **k: None
        rc = author_agent.author_pass(
            {"id": "push-fail", "severity": "shape", "kind": "repair",
             "files": ["farm/parse.py"]},
            failroot, os.path.join(failroot, "workorders.ndjson"), {},
            {"vcs": {"base_sha": base_sha}},
        )
    finally:
        author_agent.mechanical_patch = saved_mechanical
        author_agent.run_gates = saved_gates
        author_agent.commit_change = saved_commit_change
        author_agent.publish = saved_publish
        workorders.resolve = saved_resolve
        author_agent._renew_claim = saved_renew_claim
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
with open(hist, "w", encoding="utf-8") as handle:
    handle.write(json.dumps({"event": "armed", "revision": "revA", "commit": "a" * 40}) + "\n")

check("no canary is armed initially", canary.active(store) is None)
armed = canary.arm("revB", "revA", reason="test", order_id="o1", commit="b" * 40,
                   store=store, history=hist, run_history=runs)
check("arming records the previous revision", armed["previous"] == "revA")
check("arming derives the complete candidate range base from release history",
      armed.get("base_commit") == "a" * 40, str(armed))
check("arming captures a baseline rate", abs(armed["baseline_rate"] - 100.0) < 0.01, str(armed))
check("arming records the flip point", armed["armed_at_run"] == 6)
check("the canary is now active", canary.active(store) is not None)
overlap_refused = False
try:
    canary.arm("revC", "revB", reason="overlap", order_id="o2",
               store=store, history=hist, run_history=runs)
except canary.CanaryActiveError:
    overlap_refused = True
check("arming a second unresolved candidate is refused", overlap_refused)

verdict = canary.evaluate(store, runs)
check("with no post-flip runs the verdict is watching", verdict["status"] == canary.WATCHING, str(verdict))
with open(store) as handle:
    stalled_record = json.load(handle)
stalled_record["armed_ts"] = "2020-01-01T00:00:00Z"
with open(store, "w") as handle:
    json.dump(stalled_record, handle)
stalled = canary.evaluate(store, runs)
check("a release that prevents completed runs eventually regresses",
      stalled["status"] == canary.REGRESSED and "no completed" in stalled["reason"], str(stalled))
with open(store, "w") as handle:
    json.dump(armed, handle)

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

# A real regression: output halves across a complete burst-spanning floor window.
write_runs(base + [{"run": n, "produce_per_min": 40.0, "collected": 10} for n in range(7, 13)])
verdict = canary.evaluate(store, runs)
check("a halved produce rate is a regression", verdict["status"] == canary.REGRESSED, str(verdict))
check("the regression is quantified", "baseline" in verdict["reason"], str(verdict))

# A burst-phase sample just below the 25% final floor is not decisive before the
# full efficacy window. The threshold is unchanged at ten runs.
write_runs(base + [{"run": n, "animals": 100, "interval_min": 5.0,
                    "produce_per_min": 70.0, "collected": 10} for n in range(7, 14)])
borderline_early = canary.evaluate(store, runs)
check("borderline nonzero rate waits for the full burst window",
      borderline_early["status"] == canary.WATCHING
      and "decisive early" in borderline_early.get("reason", ""),
      str(borderline_early))
write_runs(base + [{"run": n, "animals": 100, "interval_min": 5.0,
                    "produce_per_min": 70.0, "collected": 10} for n in range(7, 17)])
borderline_final = canary.evaluate(store, runs)
check("the unchanged 25% floor still rejects at the full window",
      borderline_final["status"] == canary.REGRESSED,
      str(borderline_final))

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
check("a persisted global streak cannot replace candidate-owned evidence",
      verdict["status"] == canary.WATCHING, str(verdict))
check("a collection-only streak is not a hard release failure",
      not canary._looks_broken({"zero_streak": 3, "collected": 0}))

# Live runs 1204-1207 exposed both proxies at once: four empty collections included
# one 9.9-minute productive score window and one 13-second duplicate launchd run.
# Equal row weighting reported 683/min and reverted; elapsed-time weighting reports
# the actual ~1,337/min represented by those windows.
banked_candidate = [
    {"run": 7, "animals": 16875, "interval_min": 9.90,
     "produce_per_min": 2732.5, "collected": 0, "zero_streak": 4},
    {"run": 8, "animals": 16875, "interval_min": 0.22,
     "produce_per_min": 0.0, "collected": 0, "zero_streak": 5},
    {"run": 9, "animals": 16875, "interval_min": 5.05,
     "produce_per_min": 0.0, "collected": 0, "zero_streak": 6},
    {"run": 10, "animals": 16875, "interval_min": 5.07,
     "produce_per_min": 0.0, "collected": 0, "zero_streak": 7},
]
write_runs(base + banked_candidate)
verdict = canary.evaluate(store, runs)
check("bursty score growth outweighs collection-only and duplicate-row proxies",
      verdict["status"] == canary.WATCHING
      and 1300.0 < verdict.get("observed_rate", 0) < 1400.0,
      str(verdict))

phase_store = os.path.join(can, "phase-canary.json")
phase_hist = os.path.join(can, "phase-canary.ndjson")
phase_base = []
for run in range(1, 21):
    high = 200.0 if run <= 14 else 600.0
    phase_base.append({"run": run, "animals": 100, "interval_min": 5.0,
                       "produce_per_min": high if run % 2 == 0 else 0.0,
                       "collected": 10})
write_runs(phase_base)
phase_arm = canary.arm(
    "revPhase", "revA", reason="burst phase", order_id="phase-order",
    change_class="reliability", store=phase_store, history=phase_hist,
    run_history=runs,
)
phase_candidate = [
    {"run": 21 + index, "animals": 100, "interval_min": 5.0,
     "produce_per_min": 440.0 if index % 2 else 0.0, "collected": 10}
    for index in range(10)
]
write_runs(phase_base + phase_candidate[:3])
phase_early = canary.evaluate(phase_store, runs)
check("three burst-phase rows cannot trigger ordinary rate rollback",
      phase_early.get("status") == canary.WATCHING
      and "score-burst phase" in phase_early.get("reason", ""), str(phase_early))
write_runs(phase_base + phase_candidate)
phase_verdict = canary.evaluate(phase_store, runs)
check("canary baseline spans enough rows to avoid burst-phase false rollback",
      rules.CANARY_BASELINE_RUNS >= 20
      and phase_arm.get("baseline_per_animal") < 2.0
      and phase_verdict.get("status") == canary.HEALTHY,
      str({"armed": phase_arm, "verdict": phase_verdict}))

# A reliability release may be necessary while the game itself is already stalled.
# Continuing that pre-existing condition is not candidate evidence in either direction:
# clear probation after a minimum clean window, keep the pointer, and do not promote it.
stalled_store = os.path.join(can, "stalled-canary.json")
stalled_hist = os.path.join(can, "stalled-canary.ndjson")
stalled_base = [
    {"run": n, "animals": 100, "interval_min": 5.0,
     "produce_per_min": 0.0, "collected": 0, "transport_errors_core": 0}
    for n in range(1, 8)
]
write_runs(stalled_base)
stalled_arm = canary.arm(
    "revStalled", "revA", reason="repair during existing stall",
    order_id="stalled-order", change_class="reliability",
    store=stalled_store, history=stalled_hist, run_history=runs,
)
check("arming records a pre-existing authoritative score stall",
      stalled_arm.get("baseline_stalled") is True
      and stalled_arm.get("baseline_stall_runs") == list(range(1, 8)),
      str(stalled_arm))
stalled_candidate = [
    {"run": n, "animals": 100, "interval_min": 5.0,
     "produce_per_min": 0.0, "collected": 0, "transport_errors_core": 0}
    for n in range(8, 11)
]
write_runs(stalled_base + stalled_candidate)
stalled_verdict = canary.evaluate(stalled_store, runs)
check("an inherited reliability stall resolves inconclusive rather than reverting",
      stalled_verdict["status"] == canary.INCONCLUSIVE,
      str(stalled_verdict))
stalled_outcome = canary.resolve(
    stalled_verdict, project=can, store=stalled_store, history=stalled_hist,
)
check("inconclusive resolution keeps the pointer path untouched and clears probation",
      stalled_outcome.get("acted") is True
      and stalled_outcome.get("reverted") is False
      and canary.active(stalled_store) is None,
      str(stalled_outcome))
check("inconclusive resolution does not advance the champion",
      evaluation.champion(stalled_store).get("revision") == "revA",
      str(evaluation.champion(stalled_store)))
efficacy_events = pathlib.Path(can, "efficacy_events.ndjson").read_text()
check("the efficacy ledger distinguishes inconclusive from rejected",
      "candidate.inconclusive" in efficacy_events, efficacy_events)
run_source = pathlib.Path(ROOT, "run.py").read_text(encoding="utf-8")
check("the supervisor dispatches inconclusive canary verdicts",
      "canary.HEALTHY, canary.REGRESSED, canary.INCONCLUSIVE" in run_source
      and "champion unchanged" in run_source,
      "inconclusive dispatcher missing")

strategy_store = os.path.join(can, "stalled-strategy-canary.json")
strategy_hist = os.path.join(can, "stalled-strategy-canary.ndjson")
write_runs(stalled_base)
canary.arm(
    "revStrategy", "revA", reason="strategy during existing stall",
    order_id="stalled-strategy", change_class="strategy",
    store=strategy_store, history=strategy_hist, run_history=runs,
)
write_runs(stalled_base + stalled_candidate)
stalled_strategy = canary.evaluate(strategy_store, runs)
check("an unmeasurable strategy candidate remains fail-closed",
      stalled_strategy["status"] == canary.REGRESSED
      and "cannot prove" in stalled_strategy.get("reason", ""),
      str(stalled_strategy))

observability_store = os.path.join(can, "observability-canary.json")
observability_hist = os.path.join(can, "observability-canary.ndjson")
write_runs(base)
canary.arm(
    "revView", "revA", reason="readout-only", order_id="view-order",
    change_class="observability", store=observability_store,
    history=observability_hist, run_history=runs,
)
view_rows = [
    {"run": 7 + index, "animals": 100, "produce_per_min": 0.0,
     "interval_min": 5.0, "collected": 0, "anomalies": []}
    for index in range(3)
]
write_runs(base + view_rows)
view_verdict = canary.evaluate(observability_store, runs)
check("path-gated observability release is judged on clean completed cycles",
      view_verdict.get("status") == canary.HEALTHY
      and (view_verdict.get("efficacy") or {}).get("metric")
          == "clean_completed_cycles_and_current_readouts",
      str(view_verdict))

with tempfile.TemporaryDirectory() as release_root:
    previous_tree = pathlib.Path(release_root) / "releases" / "old"
    current_tree = pathlib.Path(release_root) / "releases" / "new"
    for tree in (previous_tree, current_tree):
        (tree / "farm").mkdir(parents=True)
        (tree / "dashboard").mkdir(parents=True)
        (tree / "monitor.py").write_text("old\n", encoding="utf-8")
        (tree / "farm" / "cycle.py").write_text("same\n", encoding="utf-8")
    (current_tree / "monitor.py").write_text("new\n", encoding="utf-8")
    check("observability path gate accepts dashboard/readout-only changes",
          canary.observability_release_errors(release_root, "new", "old") == [])
    (current_tree / "farm" / "cycle.py").write_text("gameplay changed\n", encoding="utf-8")
    check("observability path gate rejects gameplay changes",
          canary.observability_release_errors(release_root, "new", "old")
          == ["farm/cycle.py"])

# A release can be armed in the middle of an existing zero-collection streak. The
# first candidate row still carries the cycle's global streak, but attribution must
# restart at one and grow only with candidate-owned rows.
scope_store = os.path.join(can, "scope-canary.json")
scope_hist = os.path.join(can, "scope-canary.ndjson")
scope_queue = os.path.join(can, "workorders.ndjson")
preexisting = base + [
    {"run": 7, "animals": 100, "produce_per_min": 0.0, "collected": 0, "zero_streak": 1},
    {"run": 8, "animals": 100, "produce_per_min": 100.0, "collected": 0, "zero_streak": 2},
    {"run": 9, "animals": 100, "produce_per_min": 0.0, "collected": 0, "zero_streak": 3},
]
write_runs(preexisting)
scope_armed = canary.arm(
    "revScoped", "revA", reason="candidate scope", order_id="scope-order",
    commit="a" * 40, files=["monitor.py", "farm/canary.py"],
    store=scope_store, history=scope_hist, run_history=runs,
)
check("arming retains complete release-source provenance",
      scope_armed.get("files") == ["monitor.py", "farm/canary.py"], str(scope_armed.get("files")))
first_candidate = {
    "run": 10, "animals": 100, "produce_per_min": 0.0,
    "collected": 0, "zero_streak": 4,
    "anomalies": ["no produce collected in 4 consecutive runs"],
}
write_runs(preexisting + [first_candidate])
scoped = canary.evaluate(scope_store, runs)
check("the first candidate run does not inherit a pre-release zero streak",
      scoped["status"] == canary.WATCHING and scoped["runs_observed"] == 1, str(scoped))
zero_candidates = [
    dict(first_candidate, run=10 + index, zero_streak=4 + index)
    for index in range(rules.CANARY_RATE_MIN_RUNS)
]
write_runs(preexisting + zero_candidates)
scoped_bad = canary.evaluate(scope_store, runs)
check("six genuinely score-zero candidate runs still trigger rate rollback",
      scoped_bad["status"] == canary.REGRESSED
      and scoped_bad.get("failure_kind") is None
      and "produce" in scoped_bad.get("reason", ""),
      str(scoped_bad))
queued = canary._regression_order(scope_armed, scoped_bad, scope_queue)
queued_again = canary._regression_order(scope_armed, scoped_bad, scope_queue)
queued_row = workorders.current(scope_queue).get(queued.get("id")) or {}
check("a genuine canary regression files one idempotent work order",
      queued.get("created") is True and queued_again.get("created") is False
      and queued_row.get("status") == workorders.OPEN,
      "%s %s %s" % (queued, queued_again, queued_row))
check("the regression order carries candidate evidence and complete changed files",
      queued_row.get("source") == "release_canary"
      and queued_row.get("kind") == "canary_regression"
      and queued_row.get("files") == ["monitor.py", "farm/canary.py"]
      and ((queued_row.get("detail") or {}).get("verdict") or {}).get("status")
          == canary.REGRESSED,
      str(queued_row))

with tempfile.TemporaryDirectory() as verified_root:
    verified_path = pathlib.Path(verified_root)
    successor = verified_path / "releases" / "rev-good"
    successor.mkdir(parents=True)
    (verified_path / "release").symlink_to(successor)
    verified_store = str(verified_path / "canary.json")
    verified_queue = str(verified_path / "workorders.ndjson")
    with open(verified_store, "w", encoding="utf-8") as handle:
        json.dump({
            "status": canary.HEALTHY,
            "revision": "rev-good",
            "commit": "b" * 40,
            "resolved_ts": "2099-01-01T00:00:00Z",
            "efficacy": {"accepted": True},
        }, handle)
    workorders.submit(
        {
            "id": "canary-regression-old", "kind": "canary_regression",
            "severity": "breaking", "summary": "old regression",
            "detail": {"revision": "rev-bad", "commit": "a" * 40},
        },
        source="release_canary", intent="repair old regression", path=verified_queue,
        provenance={"rejected_revision": "rev-bad", "rejected_commit": "a" * 40},
    )
    certificate = {
        "revision": "rev-good", "matrix_fingerprint": gates.fingerprint(),
        "observed": gates.names(), "complete": True, "passed": True,
        "failed": [], "waived": [],
    }
    refused_reconcile = canary.reconcile_regression_orders(
        verified_store, verified_root, verified_queue,
        gate_record=dict(certificate, passed=False), ancestry=lambda a, b: True,
    )
    reconciled_repair = canary.reconcile_regression_orders(
        verified_store, verified_root, verified_queue,
        gate_record=certificate, ancestry=lambda a, b: a == "a" * 40 and b == "b" * 40,
    )
    repeated_reconcile = canary.reconcile_regression_orders(
        verified_store, verified_root, verified_queue,
        gate_record=certificate, ancestry=lambda a, b: True,
    )
    verified_order = workorders.current(verified_queue)["canary-regression-old"]
    check("unproven successor cannot clear a canary repair",
          not refused_reconcile, str(refused_reconcile))
    check("a complete healthy descendant supersedes its stale canary repair",
          len(reconciled_repair) == 1
          and verified_order.get("status") == workorders.SUPERSEDED
          and verified_order.get("superseded_by_release") == "rev-good",
          str(verified_order))
    check("verified canary repair reconciliation is idempotent",
          not repeated_reconcile, str(repeated_reconcile))

with tempfile.TemporaryDirectory() as queue_race_root:
    race_queue = str(pathlib.Path(queue_race_root) / "workorders.ndjson")
    race_failures = []
    for index in range(20):
        order_id = "race-%d" % index
        submitted = workorders.submit(
            {"id": order_id, "kind": "repair", "severity": "breaking", "summary": "race"},
            source="fixture", intent="race", path=race_queue,
        )
        barrier = threading.Barrier(3)
        outcomes = {}

        def claim_race():
            barrier.wait()
            outcomes["claim"] = workorders.claim(order_id, "author", path=race_queue)

        def resolve_race():
            barrier.wait()
            outcomes["resolve"] = workorders.resolve(
                order_id, workorders.SUPERSEDED, path=race_queue,
                expected_status=workorders.OPEN, expected_ts=submitted.get("ts"),
            )

        threads = [threading.Thread(target=claim_race), threading.Thread(target=resolve_race)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        if outcomes.get("claim") and outcomes.get("resolve"):
            race_failures.append(order_id)
    check("work-order claim and reconciliation compare-and-set atomically",
          not race_failures, str(race_failures))
    retry_id = "retry-after-failure"
    workorders.submit(
        {"id": retry_id, "kind": "repair", "severity": "breaking", "summary": "retry"},
        source="fixture", intent="retry", path=race_queue,
    )
    first_claim = workorders.claim(retry_id, "author", path=race_queue)
    workorders.resolve(
        retry_id, workorders.FAILED, note="transient", retryable=True,
        path=race_queue, expected_status=workorders.CLAIMED,
        expected_claim_token=first_claim.get("claim_token"),
    )
    retryable = {row["id"] for row in workorders.open_orders(race_queue)}
    second_claim = workorders.claim(retry_id, "author", path=race_queue)
    check("a transient failed order remains retryable through the bounded attempt limit",
          retry_id in retryable and second_claim is not None
          and second_claim.get("attempts") == 2, str(second_claim))
    check("claimed work-order provenance cannot be rewritten without its lease",
          workorders.attach_provenance(retry_id, {"forged": True}, race_queue) is None)
    workorders.resolve(
        retry_id, workorders.PUBLISHED, path=race_queue,
        expected_status=workorders.CLAIMED,
        expected_claim_token=second_claim.get("claim_token"),
    )
    check("terminal work-order provenance is immutable",
          workorders.attach_provenance(retry_id, {"forged": True}, race_queue) is None)

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

# Live reproduction from release 20260902T081108Z: alien-tagged rows happened to
# be the positive half of the alternating score burst. Excluding only those rows
# retained their paired zeros and manufactured a 40% regression in unchanged
# strategy code. The whole event/burst pair must be non-evidence.
phase_dir = pathlib.Path(tempfile.mkdtemp())
phase_store = str(phase_dir / "canary.json")
phase_history = str(phase_dir / "canary.ndjson")
phase_runs = str(phase_dir / "history.ndjson")
phase_baseline = [
    {
        "run": run, "animals": 100,
        "produce_per_min": 200.0 if run % 2 else 0.0,
        "interval_min": 5.0, "verified": True, "collected": 1,
    }
    for run in range(1, 21)
]
with open(phase_runs, "w", encoding="utf-8") as handle:
    for row in phase_baseline:
        handle.write(json.dumps(row) + "\n")
canary.arm(
    "rev-phase", "rev-base", store=phase_store, history=phase_history,
    run_history=phase_runs,
)
phase_candidate = [
    {"run": 21, "animals": 100, "produce_per_min": 200.0, "interval_min": 5.0,
     "verified": True, "collected": 1, "risk_event_counts": {"aliens": 1}},
    {"run": 22, "animals": 100, "produce_per_min": 0.0, "interval_min": 5.0,
     "verified": True, "collected": 0},
    {"run": 23, "animals": 100, "produce_per_min": 200.0, "interval_min": 5.0,
     "verified": True, "collected": 1},
    {"run": 24, "animals": 100, "produce_per_min": 0.0, "interval_min": 5.0,
     "verified": True, "collected": 0},
    {"run": 25, "animals": 100, "produce_per_min": 200.0, "interval_min": 5.0,
     "verified": True, "collected": 1, "risk_event_counts": {"aliens": 1}},
    {"run": 26, "animals": 100, "produce_per_min": 0.0, "interval_min": 5.0,
     "verified": True, "collected": 0},
]
with open(phase_runs, "a", encoding="utf-8") as handle:
    for row in phase_candidate:
        handle.write(json.dumps(row) + "\n")
phase_verdict = canary.evaluate(phase_store, phase_runs)
check("outside-loss exclusion preserves the complete score-burst pair",
      phase_verdict["status"] == canary.WATCHING
      and phase_verdict.get("excluded_runs") == [21, 22, 25, 26]
      and abs(float(phase_verdict.get("observed_per_animal") or 0) - 1.0) < 1e-6,
      str(phase_verdict))

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
os.makedirs(os.path.join(rev, "releases", "revA", "farm"))
os.makedirs(os.path.join(rev, "releases", "revB", "farm"))
with open(os.path.join(rev, "releases", "revA", "monitor.py"), "w") as handle:
    handle.write("old dashboard\n")
with open(os.path.join(rev, "releases", "revB", "monitor.py"), "w") as handle:
    handle.write("new dashboard\n")
with open(os.path.join(rev, "releases", "revA", "farm", "canary.py"), "w") as handle:
    handle.write("old protected gate\n")
with open(os.path.join(rev, "releases", "revB", "farm", "canary.py"), "w") as handle:
    handle.write("new protected gate\n")
check("release provenance records every changed release-source file",
      canary.release_editable_diff(rev, "revB", "revA") == ["monitor.py", "farm/canary.py"],
      str(canary.release_editable_diff(rev, "revB", "revA")))
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
base_sha = "b" * 40
inverse_sha = "d" * 40
with open(inverse_store, "w") as handle:
    json.dump({"commit": rejected_sha, "base_commit": base_sha,
               "revision": "revB", "previous": "revA"}, handle)
saved_available = vcs.available
saved_revert_range = vcs.revert_range
saved_push_main = vcs.push_main
push_args = {}
range_args = {}
try:
    vcs.available = lambda: True

    def _revert_range(base, sha, message=None):
        range_args.update({"base": base, "sha": sha, "message": message})
        return inverse_sha

    vcs.revert_range = _revert_range

    def _push_inverse(sha, **kwargs):
        push_args.update({"sha": sha, **kwargs})
        return {"remote": "origin", "branch": "main", "sha": sha}

    vcs.push_main = _push_inverse
    inverse_result = canary.record_inverse_commit(inverse_store)
finally:
    vcs.available = saved_available
    vcs.revert_range = saved_revert_range
    vcs.push_main = saved_push_main

check("the inverse commit is retained in rollback bookkeeping",
      inverse_result.get("commit") == inverse_sha, str(inverse_result))
check("source rollback covers the complete release candidate range",
      range_args.get("base") == base_sha and range_args.get("sha") == rejected_sha,
      str(range_args))
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
    vcs.revert_range = lambda *a, **k: inverse_sha

    def _fail_inverse_push(*args, **kwargs):
        raise vcs.GitError("SSH unavailable")

    vcs.push_main = _fail_inverse_push
    failed_inverse = canary.record_inverse_commit(inverse_store)
finally:
    vcs.available = saved_available
    vcs.revert_range = saved_revert_range
    vcs.push_main = saved_push_main
check("a rollback push failure remains explicit without erasing the local inverse",
      failed_inverse.get("commit") == inverse_sha
      and failed_inverse.get("pushed") is False
      and "not pushed" in failed_inverse.get("error", ""), str(failed_inverse))

section("a resolved canary does not act twice")

# Clear the earlier fixture canary before arming a new candidate. Production now
# rejects overlap at the trusted boundary rather than silently replacing evidence.
canary.resolve({"status": canary.REGRESSED, "reason": "fixture reset"}, rev, store, hist)
# Arm against a healthy baseline FIRST, then let the regression appear, so there
# are genuine post-flip runs to judge.
write_runs(base)
canary.arm("revB", "revA", store=store, history=hist, run_history=runs)
write_runs(base + [{"run": n, "produce_per_min": 40.0, "collected": 10} for n in range(7, 13)])
first = canary.resolve(canary.evaluate(store, runs), rev, store, hist)
check("a regressed canary acts", first.get("acted") is True, str(first))
with open(store, encoding="utf-8") as handle:
    resolved_record = json.load(handle)
check("resolution preserves the structured verdict for operator explanation",
      (resolved_record.get("verdict") or {}).get("status") == canary.REGRESSED
      and (resolved_record.get("verdict") or {}).get("runs_observed") == 6,
      str(resolved_record.get("verdict")))
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
        reservation = tokens.record(
            "author_reservation", 1, tokens_in=1000, tokens_out=1000,
            note="worst case", reservation_id="pass-r1",
        )
        tokens.record(
            "author", 1, tokens_in=100, tokens_out=10,
            note="settled completion", reservation_id="pass-r1",
        )
        _, reserved_cost = author_agent.spend_today()
        expected = round(author_cost + reservation["cost_usd"], 4)
        check("reserved and actual cost settle to the conservative maximum, not their sum",
              reserved_cost == expected, str((reserved_cost, expected)))
        for run in range(1201):
            tokens.record("cycle", run, note="shared-ledger churn")
        churn_passes, churn_cost = author_agent.spend_today()
        check("shared-ledger churn cannot evict live 24-hour author accounting",
              churn_passes == 1 and churn_cost == expected,
              str((churn_passes, churn_cost, expected)))
    finally:
        tokens.LEDGER = saved_ledger

# Directly exercise the interval rule, which is the one that stops a tight loop.
# spend_today() reads the real append-only log, so it must be stubbed too: without
# this the assertions below silently test the daily-budget branch instead of the
# interval branch, and begin failing as soon as the agent has genuinely run today.
# That is a test-isolation bug, not a budget bug.
model_reserve = author_agent.max_model_pass_cost(prompt_order, sandbox)
check("a model pass reserves both bounded attempts before claiming",
      model_reserve > tokens.cost(0, author_agent.MAX_MODEL_OUTPUT_TOKENS) * 2,
      str(model_reserve))
runtime_compat_order = {
    "id": "runtime-parse-leaderboard-test",
    "source": "runtime_parse_drift",
    "kind": "runtime_parse_drift",
    "severity": "breaking",
    "files": [compatibility.ADAPTER_FILE],
    "provenance": {"change_class": "compatibility"},
}
compat_reserve = author_agent.max_model_pass_cost(runtime_compat_order, sandbox)
check("protected runtime compatibility repair reserves no autonomous model spend",
      author_agent.model_output_token_limit(runtime_compat_order)
      == author_agent.COMPAT_MODEL_OUTPUT_TOKENS
      and compat_reserve == 0,
      str((author_agent.model_output_token_limit(runtime_compat_order), compat_reserve)))

saved_overlay = author_agent.compatibility_overlay_proof
author_agent.compatibility_overlay_proof = lambda root: {"ok": False, "changed": ["farm/cycle.py"]}
check("rolled-back candidate repair falls back to the full reliability gate lane",
      author_agent.effective_change_class(runtime_compat_order, sandbox) == "reliability")
author_agent.compatibility_overlay_proof = lambda root: {"ok": True, "changed": [compatibility.ADAPTER_FILE]}
check("byte-identical adapter repair retains the narrow compatibility lane",
      author_agent.effective_change_class(runtime_compat_order, sandbox) == "compatibility")
author_agent.compatibility_overlay_proof = saved_overlay

saved = canary.latest_run
saved_spend = author_agent.spend_today
saved_reserve = author_agent.max_model_pass_cost
saved_active = canary.active
saved_dirty = vcs.dirty_paths
saved_question_health = author_agent.questions.health
canary.latest_run = lambda *a, **k: 100
author_agent.spend_today = lambda *a, **k: (0, 0.0)
author_agent.max_model_pass_cost = lambda *a, **k: 0.0
canary.active = lambda *a, **k: False
vcs.dirty_paths = lambda *a, **k: []
author_agent.questions.health = lambda *a, **k: {"status": "pass", "reasons": []}
try:
    canary.active = lambda *a, **k: {"revision": "rev-watching"}
    live_canary_reason = author_agent.budget_check({})
    check("a live canary blocks a new authoring pass",
          live_canary_reason is not None and "canary" in live_canary_reason.lower(),
          str(live_canary_reason))
    canary.active = lambda *a, **k: False
    check("with budget, source and canary clear, the interval rule is what decides",
          author_agent.budget_check({}) is None, str(author_agent.budget_check({})))

    fresh_opportunity = {
        "id": "fresh", "severity": "opportunity", "ts": "2099-01-01T00:00:00Z",
    }
    aged_opportunity = {
        "id": "aged", "severity": "opportunity", "ts": "2020-01-01T00:00:00Z",
    }
    breaking_repair = {
        "id": "break", "severity": "breaking", "ts": "2099-01-01T00:00:00Z",
    }
    backlog = [aged_opportunity, dict(aged_opportunity, id="aged-2"),
               dict(aged_opportunity, id="aged-3")]

    author_agent.spend_today = lambda *a, **k: (rules.AUTHOR_MAX_ORDERS_PER_DAY, 0.0)
    reason = author_agent.budget_check({}, fresh_opportunity, [fresh_opportunity])
    check("fresh speculative work stops at the normal quota",
          reason is not None and "normal quota" in reason, str(reason))
    check("a breaking repair autonomously draws from surge capacity",
          author_agent.budget_check({}, breaking_repair, [breaking_repair]) is None,
          str(author_agent.budget_check({}, breaking_repair, [breaking_repair])))
    aged_limit, aged_reason = author_agent.adaptive_pass_limit(aged_opportunity, backlog)
    check("aged backlog earns capacity proportional to queue pressure",
          aged_limit == rules.AUTHOR_MAX_ORDERS_PER_DAY + 6
          and aged_reason == "aged backlog", str((aged_limit, aged_reason)))
    check("aged work proceeds after the normal quota is spent",
          author_agent.budget_check({}, aged_opportunity, backlog) is None,
          str(author_agent.budget_check({}, aged_opportunity, backlog)))

    author_agent.spend_today = lambda *a, **k: (rules.AUTHOR_MAX_SURGE_ORDERS_PER_DAY, 0.0)
    reason = author_agent.budget_check({}, breaking_repair, [breaking_repair])
    check("the absolute surge ceiling still contains priority repairs",
          reason is not None and "priority repair" in reason, str(reason))
    author_agent.spend_today = lambda *a, **k: (0, rules.AUTHOR_MAX_COST_USD_PER_DAY)
    reason = author_agent.budget_check({}, breaking_repair, [breaking_repair])
    check("priority never bypasses the hard dollar ceiling",
          reason is not None and "cost ceiling" in reason, str(reason))
    author_agent.spend_today = lambda *a, **k: (0, 2.0)
    author_agent.max_model_pass_cost = lambda *a, **k: 3.01
    reason = author_agent.budget_check({}, breaking_repair, [breaking_repair])
    check("a pass is refused when worst-case in-flight cost would cross the ceiling",
          reason is not None and "headroom" in reason, str(reason))

    speculative = dict(
        fresh_opportunity, source="research_agent", kind="strategy_hypothesis",
    )
    trusted_repair = dict(
        breaking_repair, source="governance", kind="policy_claim_drift",
    )
    author_agent.spend_today = lambda *a, **k: (0, 2.8)
    author_agent.max_model_pass_cost = lambda *a, **k: 0.3
    exploration_reason = author_agent.budget_check({}, speculative, [speculative])
    check("speculative work cannot consume the protected repair reserve",
          exploration_reason is not None and "held for repair" in exploration_reason,
          str(exploration_reason))
    check("a trusted repair may draw from the reserve",
          author_agent.budget_check({}, trusted_repair, [trusted_repair]) is None,
          str(author_agent.budget_check({}, trusted_repair, [trusted_repair])))
    check("research severity cannot impersonate a trusted repair",
          not author_agent.is_priority_repair(dict(speculative, severity="breaking")))

    author_agent.spend_today = lambda *a, **k: (0, 0.0)
    author_agent.max_model_pass_cost = lambda *a, **k: 0.0
    author_agent.questions.health = lambda *a, **k: {
        "status": "fail", "reasons": ["aged high-priority questions"],
    }
    interlock = author_agent.budget_check({}, speculative, [speculative])
    check("red learning governance blocks unrelated autonomous evolution",
          interlock is not None and "learning governance interlock" in interlock,
          str(interlock))
    check("trusted reliability repair can proceed through a red learning backlog",
          author_agent.budget_check({}, trusted_repair, [trusted_repair]) is None,
          str(author_agent.budget_check({}, trusted_repair, [trusted_repair])))
    learning_repair = {
        "source": "research_agent", "kind": "strategy_hypothesis",
        "files": ["experiments/bounded_probe.py"],
        "provenance": {
            "change_class": "research_probe", "hypothesis_id": "hyp-fixture",
            "question_ids": ["q-aged"],
        },
    }
    check("bounded question-linked probe work can drain the red backlog",
          author_agent.budget_check({}, learning_repair, [learning_repair]) is None,
          str(author_agent.budget_check({}, learning_repair, [learning_repair])))
    mixed_repair = dict(
        learning_repair,
        files=["experiments/bounded_probe.py", "experiments/registry.py"],
    )
    mixed_reason = author_agent.budget_check({}, mixed_repair, [mixed_repair])
    check("one editable file cannot hide a protected file in a mixed order",
          mixed_reason is not None and "independent human approval" in mixed_reason,
          str(mixed_reason))
    author_agent.questions.health = lambda *a, **k: {"status": "pass", "reasons": []}
    author_agent.max_model_pass_cost = lambda *a, **k: 0.0
    author_agent.spend_today = lambda *a, **k: (0, 0.0)

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
    reason = author_agent.budget_check({"last_authored_run": 50, "last_attempted_run": 98})
    check("a rejected or non-publishing pass also enforces the interval",
          reason is not None and "run" in reason, str(reason))
    reason = author_agent.budget_check(
        {"last_authored_run": 50, "last_attempted_run": 100},
        runtime_compat_order, [runtime_compat_order],
    )
    check("runtime compatibility repair waits for independent approval",
          reason is not None and "independent human approval" in reason, str(reason))
    reason = author_agent.budget_check({"last_authored_run": 50})
    check("authoring is allowed once enough runs have passed",
          reason is None, str(reason))
finally:
    canary.latest_run = saved
    author_agent.spend_today = saved_spend
    author_agent.max_model_pass_cost = saved_reserve
    canary.active = saved_active
    vcs.dirty_paths = saved_dirty
    author_agent.questions.health = saved_question_health


# -- model transport idempotency --------------------------------------------

section("model generation is never ambiguously retried")

saved_read_auth = llm._read_auth
saved_pick_model = llm.pick_model
saved_post = llm._post
saved_ledger = tokens.LEDGER
transport = {}
with tempfile.TemporaryDirectory() as transport_tmp:
    tokens.LEDGER = os.path.join(transport_tmp, "tokens.ndjson")
    llm._read_auth = lambda: {"access": "secret", "base": "https://gateway.invalid"}
    llm.pick_model = lambda preferred=None: "test-model"

    def fake_post(auth, path, payload, retries=None):
        transport.update(path=path, retries=retries, payload=payload)
        return {
            "status": "completed", "output_text": "OK",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }

    llm._post = fake_post
    try:
        llm.complete("system", "user", reservation_id="reservation-test")
        rows = tokens.tail()
        check("a paid generation POST allows exactly one transport attempt",
              transport.get("retries") == 1, str(transport))
        check("completion usage is linked to its preflight reservation",
              rows and rows[-1].get("reservation_id") == "reservation-test", str(rows))
    finally:
        llm._read_auth = saved_read_auth
        llm.pick_model = saved_pick_model
        llm._post = saved_post
        tokens.LEDGER = saved_ledger


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
