#!/usr/bin/env python3
"""Release gate for farm/vcs.py.

Everything here runs against a throwaway repository in a temp directory. The live
repo is never touched: a test that can move the real `main` is a test that can lose
the farm's history.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import vcs  # noqa: E402

CHECKS = 0
FAILURES = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if ok:
        print("  ok   %s" % label)
    else:
        FAILURES.append(label)
        print("  FAIL %s %s" % (label, detail))
    return ok


def section(name: str) -> None:
    print("\n== %s" % name)


def git(args, cwd, check_rc=True):
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=False)
    if check_rc and proc.returncode != 0:
        raise RuntimeError("git %s: %s" % (args, proc.stderr))
    return proc.stdout.strip()


def build_repo(root: str) -> None:
    """A miniature of the real layout: code, a gitignored state/, a release dir."""
    os.makedirs(os.path.join(root, "farm"))
    os.makedirs(os.path.join(root, "state"))
    os.makedirs(os.path.join(root, "releases", "r1"))
    with open(os.path.join(root, "farm", "cycle.py"), "w") as fh:
        fh.write("VALUE = 1\n")
    with open(os.path.join(root, "state", "history.ndjson"), "w") as fh:
        fh.write('{"run": 1}\n')
    with open(os.path.join(root, ".gitignore"), "w") as fh:
        fh.write("/state\n/releases\n/release\n__pycache__/\n")
    git(["init", "-q", "-b", "main"], root)
    git(["config", "user.name", "test"], root)
    git(["config", "user.email", "test@localhost"], root)
    git(["add", "-A"], root)
    git(["commit", "-q", "-m", "base"], root)


sandbox = tempfile.mkdtemp(prefix="vcs-test-")
repo = os.path.join(sandbox, "repo")
os.makedirs(repo)
build_repo(repo)

# Point the module at the sandbox. Every test below therefore operates on the
# throwaway repo, and a bug here cannot reach the farm's own history.
REAL_PROJECT = vcs.PROJECT
# Snapshot the real repo's branches before touching anything, so the final check can
# assert this suite added none rather than that none exist.
BRANCHES_BEFORE = set(subprocess.run(["git", "branch", "--format=%(refname:short)"],
                                     cwd=str(REAL_PROJECT), capture_output=True,
                                     text=True).stdout.split())
vcs.PROJECT = Path(repo)

try:
    section("repository detection")
    check(vcs.available(), "a real repository is detected")
    base_sha = vcs.head()
    check(bool(base_sha) and len(base_sha) == 40, "HEAD resolves to a full sha", str(base_sha))
    check(vcs.short(base_sha) == base_sha[:12], "short() truncates to 12")
    check(vcs.dirty_paths() == [], "a fresh commit leaves no dirty tracked files",
          str(vcs.dirty_paths()))

    section("worktree isolation")
    wt = vcs.worktree_add("order-1")
    check(os.path.isdir(wt["path"]), "the worktree directory exists")
    check(wt["branch"] == "author/order-1", "the branch is namespaced under author/", wt["branch"])
    check(wt["base_sha"] == base_sha, "the worktree forks from the current main")
    check(os.path.isfile(os.path.join(wt["path"], "farm", "cycle.py")),
          "tracked code is present in the worktree")
    # The gates read the real ledgers, so state must be reachable -- but as a link,
    # never a copy, because a branch of an append-only ledger would be a fiction.
    link = os.path.join(wt["path"], "state")
    check(os.path.islink(link), "state/ is present as a symlink, not a copy")
    check(os.path.isfile(os.path.join(link, "history.ndjson")),
          "the real ledger is readable through the link")
    check(os.path.realpath(link) == os.path.realpath(os.path.join(repo, "state")),
          "the link resolves to the one true state directory")

    section("the state symlink must never be committed")
    # Regression test for a real bug: .gitignore said `state/`, which matches a
    # directory but not a symlink, so `git add -A` committed the link -- writing a
    # machine-specific absolute path into the tree and into every release built from it.
    with open(os.path.join(wt["path"], "farm", "cycle.py"), "w") as fh:
        fh.write("VALUE = 2\n")
    sha = vcs.commit_worktree(wt, "change the value")
    check(bool(sha), "the branch commit succeeds")
    tracked = git(["ls-tree", "-r", "--name-only", sha], wt["path"]).split()
    check("state" not in tracked, "the state symlink is not in the commit", str(tracked))
    check(not any(t.startswith("state/") for t in tracked),
          "no ledger content is in the commit", str(tracked))
    check(not any(t.startswith("releases") for t in tracked),
          "published release trees are not in the commit", str(tracked))

    section("the diff is recorded")
    diff = vcs.diff_stat(wt)
    check(diff["files"] == ["farm/cycle.py"], "the changed file is named", str(diff["files"]))
    check(diff["insertions"] >= 1, "insertions are counted", str(diff))
    check("VALUE = 2" in diff["patch"], "the patch body is captured")
    check(diff["truncated"] is False, "a small patch is not marked truncated")

    section("main does not move until the change is merged")
    check(vcs.head() == base_sha, "committing on a branch leaves main where it was")
    with open(os.path.join(repo, "farm", "cycle.py")) as fh:
        check(fh.read().strip() == "VALUE = 1", "the live tree is untouched by the branch")

    merged = vcs.merge_to_main(wt, "merge order-1")
    check(merged == sha, "main fast-forwards to the gated commit", str(merged))
    check(vcs.head() == sha, "main now points at the change")
    with open(os.path.join(repo, "farm", "cycle.py")) as fh:
        check(fh.read().strip() == "VALUE = 1",
              "the live tree still lags until it is explicitly synced")
    synced = vcs.sync_live_tree(diff["files"])
    check(synced == ["farm/cycle.py"], "only the changed file is synced", str(synced))
    with open(os.path.join(repo, "farm", "cycle.py")) as fh:
        check(fh.read().strip() == "VALUE = 2", "the live tree now matches main")

    section("teardown leaves nothing behind")
    path_was = wt["path"]
    vcs.worktree_remove(wt)
    check(not os.path.isdir(path_was), "the worktree directory is gone")
    branches = git(["branch", "--format=%(refname:short)"], repo).split()
    check("author/order-1" not in branches, "the branch is deleted", str(branches))
    check(os.path.isdir(os.path.join(repo, "state")),
          "tearing down the worktree does not follow the link and delete state/")
    check(os.path.isfile(os.path.join(repo, "state", "history.ndjson")),
          "the real ledger survived teardown")

    section("a stale base is refused")
    # Two passes racing, or a human publishing mid-pass. The gates that just passed
    # were run against a tree that no longer reflects main, so the merge must refuse.
    wt2 = vcs.worktree_add("order-2")
    with open(os.path.join(wt2["path"], "farm", "cycle.py"), "w") as fh:
        fh.write("VALUE = 3\n")
    vcs.commit_worktree(wt2, "branch work")
    git(["commit", "-q", "--allow-empty", "-m", "someone else published"], repo)
    refused = False
    try:
        vcs.merge_to_main(wt2, "should refuse")
    except vcs.GitError as exc:
        refused = "moved" in str(exc)
    check(refused, "merging a branch whose base moved is refused")
    vcs.worktree_remove(wt2)

    section("revert by content")
    # The canary's symlink flip restores health; this is what stops the rejected
    # change being re-published by the next release.
    before = vcs.head()
    inverse = vcs.revert_commit(sha, "Revert: canary rejected this")
    check(bool(inverse), "an inverse commit is created", str(inverse))
    check(vcs.head() == inverse, "main advances to the revert")
    check(inverse != before, "the revert is a new commit, not a rewind")
    content = git(["show", "%s:farm/cycle.py" % inverse], repo)
    check(content.strip() == "VALUE = 1", "the reverted content is restored on main", content)
    check(git(["cat-file", "-t", sha], repo) == "commit",
          "the rejected commit is still in history, not erased")
    log = git(["log", "--oneline"], repo)
    check("Revert" in log, "the revert is visible in the log")

    section("release tags join the two histories")
    tag = vcs.tag_release("20260825T190000Z", sha)
    check(tag == "release/20260825T190000Z", "the tag is namespaced", str(tag))
    check(git(["rev-parse", tag + "^{commit}"], repo) == sha,
          "the tag resolves to the commit the release was built from")

    section("housekeeping")
    check(isinstance(vcs.recent(3), list) and vcs.recent(3), "recent() returns history")
    first = vcs.recent(3)[0]
    check({"sha", "author", "when", "subject"} <= set(first),
          "log rows are fully parsed", str(first))
    check(isinstance(vcs.prune(), int), "prune() reports a count")
    check(vcs.branch_name("weird/id with spaces!").startswith("author/"),
          "branch names are sanitised", vcs.branch_name("weird/id with spaces!"))
    check(" " not in vcs.branch_name("weird/id with spaces!"),
          "branch names contain no spaces")

    section("degrades instead of failing")
    empty = os.path.join(sandbox, "not-a-repo")
    os.makedirs(empty)
    vcs.PROJECT = Path(empty)
    check(vcs.available() is False, "a non-repository is reported unavailable")
    check(vcs.head() is None, "head() returns None rather than raising")
    check(vcs.dirty_paths() == [], "dirty_paths() is empty rather than raising")
    check(vcs.recent() == [], "recent() is empty rather than raising")
    check(vcs.tag_release("r") is None, "tagging fails soft")
    check(vcs.revert_commit("deadbeef") is None, "reverting fails soft")

finally:
    vcs.PROJECT = REAL_PROJECT
    shutil.rmtree(sandbox, ignore_errors=True)

# The whole point of pointing PROJECT at a sandbox: prove the real repo is untouched.
section("the live repository was never touched")
check(vcs.PROJECT == REAL_PROJECT, "module PROJECT is restored")
if vcs.available():
    branches = set(subprocess.run(["git", "branch", "--format=%(refname:short)"],
                                  cwd=str(REAL_PROJECT), capture_output=True,
                                  text=True).stdout.split())
    # Compare against the snapshot taken before any of this ran, rather than
    # asserting no author/ branch exists at all.
    #
    # The absolute form was wrong in a way that would have deadlocked the loop: the
    # author agent runs this very suite from inside its own worktree, so its
    # author/<order-id> branch legitimately exists while the gates execute. The
    # assertion would fail on every real authoring pass, and because the same
    # failure also reproduces on the unpatched tree, the pre-existing-failure
    # attribution in author_agent.py would classify it as not-our-fault and stand
    # down forever. A test that cannot tell "I leaked a branch" from "someone is
    # legitimately using one" silently disables the thing it is protecting.
    leaked = (branches - BRANCHES_BEFORE) - {"main"}
    check(not leaked, "the suite leaked no branches into the real repo", str(leaked))

print()
print("%d checks, %d failures" % (CHECKS, len(FAILURES)))
if FAILURES:
    for item in FAILURES:
        print("  failed: %s" % item)
    raise SystemExit(1)
print("vcs suite passed")
