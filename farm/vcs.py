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
from urllib.parse import unquote, urlparse

from . import control

PROJECT = control.project_root(Path(__file__).resolve().parent.parent)

# Branch namespace for machine-authored work. Kept under a prefix so a human can
# see at a glance which history is autonomous, and so cleanup can be pattern-based.
BRANCH_PREFIX = "author/"
TAG_PREFIX = "release/"
MAIN = "main"
GENERATED_PATHS = {"farm-strategy-journal.md"}
PUSH_REMOTE = "origin"
# An unattended author must never trust whichever destination happens to be named
# "origin". The repository identity is allowlisted so a local configuration mistake
# cannot redirect machine-authored code to another project.
EXPECTED_REMOTE_REPOSITORY = "https://github.com/nicholas-figura-glean/Farm-Friends-Operator"


class GitError(RuntimeError):
    """A git command failed in a way the caller should handle, not ignore."""


def _run(args: List[str], cwd: Optional[str] = None, check: bool = True,
         timeout: int = 120,
         env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git"] + args, cwd=cwd or str(PROJECT), capture_output=True, text=True,
        timeout=timeout, check=False, env=env,
    )
    if check and proc.returncode != 0:
        raise GitError("git %s failed: %s" % (" ".join(args[:3]),
                                              (proc.stderr or proc.stdout or "").strip()[:400]))
    return proc


def available() -> bool:
    """Is this a usable git repository?

    Runtime healing remains independent of Git, but source authoring and release now
    fail closed when this is false: an unattended code change without a reviewable,
    remotely durable commit is not publishable.
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
            selected = [path for path in paths if path not in GENERATED_PATHS]
            if not selected:
                return None
            _run(["add", "--"] + selected)
        else:
            exclusions = [":(exclude)%s" % path for path in sorted(GENERATED_PATHS)]
            _run(["add", "-A", "--", "."] + exclusions)
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
        exists = _run(["cat-file", "-e", "%s:%s" % (MAIN, path)], check=False).returncode == 0
        command = ["checkout", MAIN, "--", path] if exists else ["rm", "-f", "--", path]
        if _run(command, check=False).returncode == 0:
            updated.append(path)
    return updated


# -- remote synchronization --------------------------------------------------


def _network_env() -> Dict[str, str]:
    """Git environment suitable for an unattended launchd process.

    A missing key, passphrase, or host-key decision must fail quickly rather than
    wedging the single author lock on an invisible prompt. Existing SSH options are
    respected so an operator can still select a specific key in ~/.ssh/config.
    """
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=15")
    return env


def _repository_identity(url: str) -> str:
    """Normalize SSH, HTTPS, file URLs and local paths for destination checks."""
    value = str(url or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value and "@" in value.split(":", 1)[0] and ":" in value:
        host, path = value.split(":", 1)
        host = host.rsplit("@", 1)[-1]
        path = path.rstrip("/")
        if path.lower().endswith(".git"):
            path = path[:-4]
        return (host + "/" + path.lstrip("/")).lower()
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file" and parsed.hostname:
        path = unquote(parsed.path).strip("/")
        if path.lower().endswith(".git"):
            path = path[:-4]
        return (parsed.hostname + "/" + path).lower()
    local = unquote(parsed.path) if parsed.scheme == "file" else value
    return "file:" + os.path.realpath(os.path.expanduser(local))


def remote_url(remote: str = PUSH_REMOTE) -> str:
    """Configured push URL for the autonomous destination, or raise loudly."""
    url = _run(["remote", "get-url", "--push", remote]).stdout.strip()
    if not url:
        raise GitError("remote %s has no push URL" % remote)
    return url


def _checked_remote_url(remote: str, expected_repository: Optional[str]) -> str:
    url = remote_url(remote)
    if expected_repository:
        actual = _repository_identity(url)
        expected = _repository_identity(expected_repository)
        if actual != expected:
            raise GitError(
                "refusing to push %s: configured destination %s is not allowlisted %s"
                % (remote, actual or "unknown", expected or "unknown")
            )
    return url


def remote_head(remote: str = PUSH_REMOTE) -> str:
    """Read the remote main SHA over the configured non-interactive transport."""
    out = _run(
        ["ls-remote", "--exit-code", remote, "refs/heads/" + MAIN],
        timeout=30, env=_network_env(),
    ).stdout.strip()
    rows = [line.split()[0] for line in out.splitlines() if line.split()]
    if len(rows) != 1 or len(rows[0]) != 40:
        raise GitError("remote %s did not return one %s commit" % (remote, MAIN))
    return rows[0]


def push_main(sha: Optional[str] = None, remote: str = PUSH_REMOTE,
              expected_remote_sha: Optional[str] = None,
              expected_local_sha: Optional[str] = None,
              expected_repository: Optional[str] = EXPECTED_REMOTE_REPOSITORY) -> Dict[str, Any]:
    """Push one verified commit to remote main and read it back.

    ``expected_remote_sha`` is the gated branch's base during autonomous authoring.
    Requiring that exact value makes a concurrent remote update a refusal, never a
    force-push. ``expected_local_sha`` closes the equivalent race in the local repo.
    The push itself is an ordinary fast-forward update; history is never rewritten.
    """
    local_main = _run(["rev-parse", MAIN]).stdout.strip()
    target = _run(["rev-parse", sha or MAIN]).stdout.strip()
    if expected_local_sha and local_main != expected_local_sha:
        raise GitError(
            "local %s moved from %s to %s before push; refusing"
            % (MAIN, short(expected_local_sha), short(local_main))
        )
    if _run(["cat-file", "-e", target + "^{commit}"], check=False).returncode != 0:
        raise GitError("push target %s is not a commit" % short(target))

    url = _checked_remote_url(remote, expected_repository)
    before = remote_head(remote)
    # A previous attempt may have completed remotely and lost its response. Reading
    # the exact target back makes that ambiguous transport outcome safely idempotent.
    if before == target:
        return {"remote": remote, "url": url, "branch": MAIN, "sha": target,
                "previous": before, "already_current": True}
    if expected_remote_sha and before != expected_remote_sha:
        raise GitError(
            "remote %s/%s moved from %s to %s; refusing"
            % (remote, MAIN, short(expected_remote_sha), short(before))
        )
    if _run(["merge-base", "--is-ancestor", before, target], check=False).returncode != 0:
        raise GitError(
            "%s is not a descendant of remote %s/%s at %s; refusing"
            % (short(target), remote, MAIN, short(before))
        )

    spec = "%s:refs/heads/%s" % (target, MAIN)
    proc = _run(["push", "--porcelain", remote, spec], check=False,
                timeout=120, env=_network_env())
    if proc.returncode != 0:
        # The server may have accepted the update immediately before the connection
        # dropped. Verify before calling it a failure; repeating the same SHA is safe.
        try:
            observed = remote_head(remote)
        except (GitError, OSError, subprocess.TimeoutExpired):
            observed = ""
        if observed != target:
            detail = (proc.stderr or proc.stdout or "push failed").strip()[-500:]
            raise GitError("push to %s/%s failed: %s" % (remote, MAIN, detail))

    observed = remote_head(remote)
    if observed != target:
        raise GitError(
            "push returned success but %s/%s is %s, expected %s"
            % (remote, MAIN, short(observed), short(target))
        )
    # Keep the local tracking ref useful to read-only status views without making
    # those views perform network calls of their own.
    _run(["update-ref", "refs/remotes/%s/%s" % (remote, MAIN), target], check=False)
    return {"remote": remote, "url": url, "branch": MAIN, "sha": target,
            "previous": before, "already_current": False}


def require_remote_sync(remote: str = PUSH_REMOTE,
                        expected_repository: Optional[str] = EXPECTED_REMOTE_REPOSITORY,
                        require_clean: bool = True) -> Dict[str, Any]:
    """Fail unless the release source is clean and exactly present on remote main."""
    url = _checked_remote_url(remote, expected_repository)
    local = head()
    if not local:
        raise GitError("local %s does not resolve to a commit" % MAIN)
    all_dirty = dirty_paths(include_untracked=True)
    dirty = [path for path in all_dirty if control.is_release_source(path)]
    if require_clean and dirty:
        raise GitError(
            "release source has %d uncommitted path(s): %s"
            % (len(dirty), ", ".join(dirty[:6]))
        )
    observed = remote_head(remote)
    if observed != local:
        raise GitError(
            "local %s %s is not synchronized with %s/%s %s"
            % (MAIN, short(local), remote, MAIN, short(observed))
        )
    return {"remote": remote, "url": url, "branch": MAIN, "sha": local,
            "clean": not dirty, "non_release_dirty": sorted(set(all_dirty) - set(dirty)),
            "synchronized": True}


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


def revert_range(
    base_exclusive: str,
    sha: str,
    message: Optional[str] = None,
) -> Optional[str]:
    """Record one inverse commit for every candidate commit after the live base.

    A release may contain several pushed commits. Reverting only its final SHA left
    earlier candidate commits on ``main`` even though the runtime pointer rolled all
    the way back. The next release could then silently republish rejected code. This
    applies the entire ``base..sha`` range, newest first, in an isolated worktree and
    advances main once.
    """
    dirty_before = set(dirty_paths(include_untracked=True))
    path = tempfile.mkdtemp(prefix="revert-wt-")
    os.rmdir(path)
    branch = "revert/%s-range" % short(sha)
    try:
        if base_exclusive:
            ancestor = _run(
                ["merge-base", "--is-ancestor", base_exclusive, sha], check=False
            )
            if ancestor.returncode != 0:
                return None
            commits = [
                value for value in _run(
                    ["rev-list", "%s..%s" % (base_exclusive, sha)]
                ).stdout.splitlines() if value
            ]
        else:
            commits = [sha]
        if not commits:
            return None
        _run(["worktree", "add", "--quiet", "-B", branch, path, MAIN])
        proc = _run(["revert", "--no-commit"] + commits, cwd=path, check=False)
        if proc.returncode != 0:
            _run(["revert", "--abort"], cwd=path, check=False)
            return None
        if not _run(["diff", "--cached", "--name-only"], cwd=path).stdout.strip():
            return None
        _run([
            "commit", "-q", "-m",
            message or "Revert rejected release candidate %s" % short(sha),
        ], cwd=path)
        reverted = _run(["rev-parse", "HEAD"], cwd=path).stdout.strip()
        main_sha = _run(["rev-parse", MAIN]).stdout.strip()
        changed = _run(["diff", "--name-only", main_sha, reverted], cwd=path).stdout.split()
        _run(["update-ref", "refs/heads/" + MAIN, reverted, main_sha])
        safe_to_sync = [item for item in changed if item not in dirty_before]
        sync_live_tree(safe_to_sync)
        return reverted
    except (GitError, OSError, subprocess.TimeoutExpired):
        return None
    finally:
        _run(["worktree", "remove", "--force", path], check=False)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        _run(["worktree", "prune"], check=False)
        _run(["branch", "-D", branch], check=False)


def revert_commit(sha: str, message: Optional[str] = None) -> Optional[str]:
    """Backward-compatible one-commit inverse."""
    return revert_range("", sha, message)


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
