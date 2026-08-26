"""Git plumbing for autonomous changes.

Why this exists
---------------
Before this, an autonomous change was a directory copy: stage a temp tree, patch
it, and publish it into ``releases/<revision>/``. That gave us immutability and a
pointer flip, which is genuinely most of what a release system needs, but it left
three things missing that matter once a model is doing the editing.

* **No diff.** A work order recorded what was *intended*, never what actually
  changed. Reviewing a week of unattended edits meant comparing release trees by
  hand.
* **No content-addressed revert.** ``farm/canary.py`` reverts by re-pointing the
  symlink at the previous directory. That works, and it stays as the fast path,
  but it cannot express "undo this one change" -- only "go back to that whole
  tree". If a later good change has already shipped on top, the directory revert
  takes it down too.
* **No isolation primitive.** Copying a subset of directories (``STAGE_DIRS``) is
  a guess about what matters. Miss a directory and the staged tree silently
  differs from what gets published.

Worktrees, not checkouts
------------------------
The author agent must never run ``git checkout`` in the live tree. Even though
launchd executes from ``release/`` rather than from the working tree, a checkout
rewrites files under whatever else happens to be reading them, and the whole point
of this module is to make autonomous edits *less* surprising.

So every authoring pass gets its own ``git worktree`` on its own branch, in a temp
directory. It is a real checkout of the repo at a known commit, isolated by
construction rather than by a hand-maintained list of directories. The live tree's
HEAD is untouched until a branch has passed the gates.

State is shared, never branched
-------------------------------
``state/`` is gitignored, so a worktree has no ``state/`` at all. The gates need
one: they read the real ledgers to compute the evidence model. Each worktree
therefore gets a symlink to the live ``state/``. That is deliberate -- the ledgers
are append-only and must stay singular, and a branch of the farm's history would be
a fiction.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import control

PROJECT = control.project_root(Path(__file__).resolve().parent.parent)

# Branch namespace for machine-authored work. Kept under a prefix so a human can
# see at a glance which history is autonomous, and so cleanup can be pattern-based.
BRANCH_PREFIX = "author/"
TAG_PREFIX = "release/"
MAIN = "main"


class GitError(RuntimeError):
    """A git command failed in a way the caller should handle, not ignore."""


def _run(args: List[str], cwd: Optional[str] = None, check: bool = True,
         timeout: int = 120) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git"] + args, cwd=cwd or str(PROJECT), capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    if check and proc.returncode != 0:
        raise GitError("git %s failed: %s" % (" ".join(args[:3]),
                                              (proc.stderr or proc.stdout or "").strip()[:400]))
    return proc


def available() -> bool:
    """Is this a usable git repository?

    Everything in this module is optional. The author agent falls back to the
    directory-copy path when git is unavailable, because losing version control
    should degrade review quality, never stop the farm from repairing itself.
    """
    if not (PROJECT / ".git").exists():
        return False
    try:
        return _run(["rev-parse", "--git-dir"], check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def head() -> Optional[str]:
    try:
        return _run(["rev-parse", "HEAD"]).stdout.strip() or None
    except (GitError, OSError, subprocess.TimeoutExpired):
        return None


def short(sha: Optional[str]) -> str:
    return (sha or "")[:12]


def dirty_paths(include_untracked: bool = False) -> List[str]:
    """Files with working-tree changes, optionally including new files."""
    try:
        mode = "all" if include_untracked else "no"
        out = _run(["status", "--porcelain", "--untracked-files=" + mode]).stdout
    except (GitError, OSError, subprocess.TimeoutExpired):
        return []
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def commit_live(message: str, paths: Optional[List[str]] = None) -> Optional[str]:
    """Commit changes already present in the live working tree.

    Used for human-authored or operator-driven edits, so the branch the author
    agent forks from is never a stale commit with real work sitting on top of it.
    """
    try:
        if paths:
            _run(["add", "--"] + paths)
        else:
            _run(["add", "-A"])
        if not _run(["diff", "--cached", "--name-only"]).stdout.strip():
            return None
        _run(["commit", "-q", "-m", message])
        return head()
    except (GitError, OSError, subprocess.TimeoutExpired):
        return None


# -- worktrees ---------------------------------------------------------------


def branch_name(order_id: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in str(order_id))
    return BRANCH_PREFIX + safe.strip("-")[:60]


def worktree_add(order_id: str, base: str = MAIN) -> Dict[str, Any]:
    """An isolated checkout on a fresh branch, with state/ shared by symlink.

    The branch is force-created: a previous pass on the same order may have left
    one behind, and an authoring attempt should always start from the current base
    rather than resuming someone else's half-finished history.
    """
    branch = branch_name(order_id)
    path = tempfile.mkdtemp(prefix="author-wt-")
    # A fresh mkdtemp exists, but `git worktree add` insists on creating it.
    os.rmdir(path)
    _run(["worktree", "add", "--quiet", "-B", branch, path, base])

    # The gates read the real ledgers. state/ is gitignored, so link it in rather
    # than copying 322MB, and never let a branch fork the farm's history.
    link = Path(path) / "state"
    if not link.exists():
        os.symlink(str(PROJECT / "state"), str(link))
    return {"path": path, "branch": branch, "base": base,
            "base_sha": _run(["rev-parse", base]).stdout.strip()}


def worktree_remove(worktree: Dict[str, Any], keep_branch: bool = False) -> None:
    """Tear down a worktree, and by default its branch too.

    Failed attempts should not accumulate branches; the work order already records
    that the attempt happened and why it failed.
    """
    path = worktree.get("path")
    branch = worktree.get("branch")
    if path:
        # Remove the state symlink first so no tooling can follow it into the real
        # ledgers while tearing the directory down.
        link = Path(path) / "state"
        try:
            if link.is_symlink():
                link.unlink()
        except OSError:
            pass
        _run(["worktree", "remove", "--force", path], check=False)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    _run(["worktree", "prune"], check=False)
    if branch and not keep_branch:
        _run(["branch", "-D", branch], check=False)


def commit_worktree(worktree: Dict[str, Any], message: str) -> Optional[str]:
    """Commit every change in the worktree and return the new sha."""
    path = worktree["path"]
    _run(["add", "-A"], cwd=path)
    if not _run(["diff", "--cached", "--name-only"], cwd=path).stdout.strip():
        return None
    _run(["commit", "-q", "-m", message], cwd=path)
    return _run(["rev-parse", "HEAD"], cwd=path).stdout.strip()


def diff_stat(worktree: Dict[str, Any]) -> Dict[str, Any]:
    """What actually changed, for the work order and the journal.

    This is the record that was missing entirely before: the order said what was
    intended, and nothing said what was done.
    """
    path, base = worktree["path"], worktree.get("base_sha") or worktree.get("base") or MAIN
    try:
        stat = _run(["diff", "--stat", base], cwd=path).stdout.strip()
        names = _run(["diff", "--name-only", base], cwd=path).stdout.split()
        patch = _run(["diff", base], cwd=path).stdout
    except (GitError, OSError, subprocess.TimeoutExpired):
        return {"files": [], "stat": "", "patch": "", "insertions": 0, "deletions": 0}
    insertions = sum(line.count("+") for line in patch.splitlines()
                     if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in patch.splitlines()
                    if line.startswith("-") and not line.startswith("---"))
    return {
        "files": names,
        "stat": stat,
        # Bounded: a runaway patch should not be able to bloat the queue file.
        "patch": patch[:20_000],
        "insertions": insertions,
        "deletions": deletions,
        "truncated": len(patch) > 20_000,
    }


def merge_to_main(worktree: Dict[str, Any], message: str) -> Optional[str]:
    """Fast-forward main onto a gated branch without checking anything out.

    ``git merge`` in the live tree would rewrite files there. Instead the branch
    ref is written straight into main, which is safe precisely because main has not
    moved since the worktree was created -- and if it has, this refuses.

    Refusing is the correct behaviour: main moving mid-pass means something else
    published, and the gates that just passed were run against a tree that no longer
    reflects reality.
    """
    branch, base_sha = worktree["branch"], worktree.get("base_sha")
    current_main = _run(["rev-parse", MAIN]).stdout.strip()
    if base_sha and current_main != base_sha:
        raise GitError(
            "%s moved from %s to %s during this pass; refusing to fast-forward"
            % (MAIN, short(base_sha), short(current_main))
        )
    branch_sha = _run(["rev-parse", branch]).stdout.strip()
    if branch_sha == current_main:
        return None
    # Verify the branch really descends from main before moving the ref.
    if _run(["merge-base", "--is-ancestor", current_main, branch_sha],
            check=False).returncode != 0:
        raise GitError("%s is not a descendant of %s; refusing" % (branch, MAIN))
    _run(["update-ref", "refs/heads/" + MAIN, branch_sha, current_main])
    return branch_sha


def sync_live_tree(paths: List[str]) -> List[str]:
    """Bring specific files in the live tree up to date with main.

    Only the files the change touched, and only after main already points at the
    gated commit. A whole-tree checkout could clobber unrelated local edits.
    """
    updated = []
    for path in paths:
        if _run(["checkout", MAIN, "--", path], check=False).returncode == 0:
            updated.append(path)
    return updated


# -- release identity and revert --------------------------------------------


def tag_release(revision: str, sha: Optional[str] = None) -> Optional[str]:
    """Tie a published release directory to the commit it was built from.

    Without this the two histories -- directories and commits -- have no join, and
    "which code is actually live?" needs a directory diff to answer.
    """
    tag = TAG_PREFIX + str(revision)
    try:
        _run(["tag", "-f", tag, sha or "HEAD"])
        return tag
    except (GitError, OSError, subprocess.TimeoutExpired):
        return None


def revert_commit(sha: str, message: Optional[str] = None) -> Optional[str]:
    """Record an inverse commit on main for a change the canary rejected.

    This complements the symlink revert rather than replacing it. The symlink flip
    is what makes the farm healthy again in seconds; this is what stops the rejected
    change from being silently re-published by the next release, and what leaves a
    reviewable record that it was tried and rejected.

    Implemented with a temporary worktree so main's files are never rewritten under
    a running process.
    """
    path = tempfile.mkdtemp(prefix="revert-wt-")
    os.rmdir(path)
    branch = "revert/" + short(sha)
    try:
        _run(["worktree", "add", "--quiet", "-B", branch, path, MAIN])
        proc = _run(["revert", "--no-edit", sha], cwd=path, check=False)
        if proc.returncode != 0:
            _run(["revert", "--abort"], cwd=path, check=False)
            return None
        if message:
            _run(["commit", "-q", "--amend", "-m", message], cwd=path)
        reverted = _run(["rev-parse", "HEAD"], cwd=path).stdout.strip()
        main_sha = _run(["rev-parse", MAIN]).stdout.strip()
        _run(["update-ref", "refs/heads/" + MAIN, reverted, main_sha])
        return reverted
    except (GitError, OSError, subprocess.TimeoutExpired):
        return None
    finally:
        _run(["worktree", "remove", "--force", path], check=False)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        _run(["worktree", "prune"], check=False)
        _run(["branch", "-D", branch], check=False)


def recent(limit: int = 12) -> List[Dict[str, str]]:
    """Recent history, for `run.py --vcs-status`."""
    try:
        out = _run(["log", "--max-count=%d" % limit,
                    "--pretty=format:%h\x1f%an\x1f%ar\x1f%s"]).stdout
    except (GitError, OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            rows.append({"sha": parts[0], "author": parts[1],
                         "when": parts[2], "subject": parts[3]})
    return rows


def stale_worktrees() -> List[str]:
    """Author worktrees left behind by a crashed pass."""
    try:
        out = _run(["worktree", "list", "--porcelain"]).stdout
    except (GitError, OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            name = os.path.basename(path)
            if name.startswith(("author-wt-", "revert-wt-")) and not os.path.isdir(path):
                found.append(path)
    return found


def prune() -> int:
    """Drop administrative records for worktrees whose directories are gone."""
    stale = stale_worktrees()
    _run(["worktree", "prune"], check=False)
    return len(stale)
