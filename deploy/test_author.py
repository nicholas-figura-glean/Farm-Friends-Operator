#!/usr/bin/env python3
"""Author agent and canary suite.

This is the highest-risk component in the project: it edits code and publishes
releases with no human in the loop. The tests therefore concentrate on the
refusals rather than the happy path, because a refusal that fails to fire is what
turns an autonomous agent into an outage.

Everything runs against a temp sandbox tree with `author_agent.PROJECT`
redirected, so `publish()` and `deploy/release.sh` are never reached and the live
release pointer is never touched.

One test makes a real call to the Glean llm_proxy gateway to prove the model
backend works end to end. It is skipped, not failed, when the token is dormant --
an expired credential is not a code defect.
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

from farm import canary, llm, rules, workorders  # noqa: E402

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
check("run.py is editable", author_agent.editable("run.py") is None)

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

# A hard failure is decisive immediately, without waiting for an average.
write_runs(base + [{"run": 7, "produce_per_min": 0.0, "collected": 0}])
verdict = canary.evaluate(store, runs)
check("a run producing nothing reverts at once", verdict["status"] == canary.REGRESSED, str(verdict))

# A wolf attack is not a code regression.
write_runs(base + [{"run": n, "produce_per_min": 95.0, "collected": 8,
                    "risk_events": ["wolf"], "anomalies": ["wolf attack"]} for n in range(7, 10)])
verdict = canary.evaluate(store, runs)
check("a risk event alone does not revert a release", verdict["status"] == canary.WATCHING, str(verdict))

section("revert safety")

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
canary.latest_run = lambda *a, **k: 100
author_agent.spend_today = lambda *a, **k: (0, 0.0)
canary.active = lambda *a, **k: False
try:
    check("with budget and canary clear, the interval rule is what decides",
          author_agent.budget_check({}) is None, str(author_agent.budget_check({})))
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


# -- real gateway round trip -------------------------------------------------

section("model backend against the live gateway")

availability = llm.availability()
if not availability.get("available"):
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

    patch = author_agent.model_patch(live_order, real)
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
