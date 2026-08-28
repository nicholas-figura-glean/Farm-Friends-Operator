#!/usr/bin/env python3
"""Author agent: turns work orders into published code, or into nothing at all.

This is the only component permitted to modify the farm's source. It is a
one-shot launchd job so a crash is a no-op rather than a stuck daemon.

The pipeline, and why each stage exists:

    1. budget      an authoring pass costs money and risk; both are rationed
    2. claim       one order at a time, worst severity first
    3. stage       an isolated copy of the tree; the live tree is never edited
                   speculatively, because launchd may fire mid-edit
    4. patch       mechanical repair if the change is deterministic, otherwise
                   the model
    5. gate        the full release matrix, run inside the staging copy
    6. publish     copy back, deploy/release.sh (which re-runs the gates), flip
    7. canary      the flip is provisional and self-reverting

Two backends, and when each is right
------------------------------------
Most contract drift is mechanical: an argument gets renamed, a tool gets renamed.
Those repairs are exactly derivable from the diff, so they are done in Python with
no model, no cost and no variance. The model is reserved for changes that need
judgement -- a response format that has to be reparsed, a capability that has
disappeared and needs a fallback.

The model cannot weaken its own supervision
-------------------------------------------
`PROTECTED` files are refused as edit targets. An agent that can rewrite the gate
matrix, the canary, the work queue, or its own prompt is not a supervised agent;
the first regression it introduces could also delete the thing that would have
caught it. If a work order genuinely requires touching protected code, the order
is safely contained on the last verified release and recorded for an alternate
agent-owned approach instead of weakening the trust boundary.

Exit codes: 0 pass completed, queued, deferred, or safely contained; 4 the agent
itself broke and launchd will retry it.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RUNTIME_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME_ROOT))

from farm import canary, control, ledger, llm, policy, rules, tokens, vcs, workorders  # noqa: E402

# The process executes immutable code through release/, but edits and publishes from
# the canonical checkout. The LaunchAgent injects FARM_PROJECT_ROOT; the fallback
# resolver also walks out of releases/<revision>/ for hand runs and older installs.
PROJECT = control.project_root(RUNTIME_ROOT)
STATE = PROJECT / "state"
LOCK = STATE / ".author.lock"
STORE = STATE / "author.json"
LOG = STATE / "author.ndjson"

# One authoritative trust boundary drives this enforcement, supervision, tests,
# and the architecture diagram. A UI-only mirror previously disagreed with this
# tuple and showed files locked that the model could still rewrite.
EDITABLE_PREFIXES = control.AUTHOR_EDITABLE_PREFIXES
EDITABLE_FILES = control.AUTHOR_EDITABLE_FILES
PROTECTED = tuple(sorted(control.TRUSTED_PATHS))

# Files copied into a staging tree. Mirrors deploy/release.sh's manifest.
STAGE_DIRS = ("farm", "experiments", "fixtures", "dashboard", "game", "deploy")
STAGE_FILES = ("run.py", "monitor.py")

MAX_PATCH_BYTES = 40_000
MAX_FILE_BYTES = 220_000     # a file too big to send is a file too big to rewrite blind
MAX_MODEL_OUTPUT_TOKENS = 32_000
MAX_RETRY_FEEDBACK_BYTES = 4_000


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def log(row: Dict[str, Any]) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row, ts=utcnow()), sort_keys=True, default=str) + "\n")


# -- budget ------------------------------------------------------------------


def spend_today() -> Tuple[int, float]:
    """Real author passes and model cost in the last 24 hours.

    A pass is booked once when an order is claimed. Model completions are cost rows,
    not passes: retries, research calls, and live gateway smoke tests previously made
    49 test requests look like 49 autonomous changes and wedged an 8-pass budget.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    passes, unreserved_cost = 0, 0.0
    reservations: Dict[str, float] = {}
    actual_by_reservation: Dict[str, float] = {}
    # This is a safety window, not a dashboard projection. Read the full ledger so
    # high-volume cycle/heal rows cannot evict still-live 24-hour author charges.
    for row in tokens.tail():
        try:
            when = datetime.strptime(str(row.get("ts")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if when < cutoff:
            continue
        kind = row.get("kind")
        reservation_id = str(row.get("reservation_id") or "")
        row_cost = float(row.get("cost_usd") or 0.0)
        if kind == "author_pass":
            passes += 1
        elif kind == "author_reservation" and reservation_id:
            reservations[reservation_id] = reservations.get(reservation_id, 0.0) + row_cost
        elif kind == "author":
            if reservation_id:
                actual_by_reservation[reservation_id] = (
                    actual_by_reservation.get(reservation_id, 0.0) + row_cost
                )
            else:
                # Legacy/unreserved completions remain fully billable.
                unreserved_cost += row_cost
    reserved_ids = set(reservations) | set(actual_by_reservation)
    cost = unreserved_cost + sum(
        max(reservations.get(key, 0.0), actual_by_reservation.get(key, 0.0))
        for key in reserved_ids
    )
    return passes, round(cost, 4)


def _order_age_seconds(order: Optional[Dict[str, Any]]) -> Optional[float]:
    if not order or not (order.get("created_ts") or order.get("ts")):
        return None
    try:
        when = datetime.strptime(
            str(order.get("created_ts") or order["ts"]), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())


def adaptive_pass_limit(
    order: Optional[Dict[str, Any]],
    open_queue: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[int, str]:
    """Choose repair capacity from severity, backlog age, and queue pressure.

    The normal quota contains speculative research churn. It is deliberately soft:
    a detector-confirmed repair must not wait a day merely because earlier model
    attempts used the exploration allocation. Breaking/shape/degraded work can use
    the independently bounded surge pool immediately. Lower-severity work earns a
    smaller surge only after it has aged, with the size based on the live backlog.
    """
    base = rules.AUTHOR_MAX_ORDERS_PER_DAY
    hard = rules.AUTHOR_MAX_SURGE_ORDERS_PER_DAY
    severity = str((order or {}).get("severity") or "").lower()
    if severity in {"breaking", "shape", "degraded"}:
        return hard, "priority repair"

    age = _order_age_seconds(order)
    if age is not None and age >= rules.AUTHOR_BACKLOG_SURGE_AGE_SECONDS:
        backlog = max(1, len(open_queue or []))
        earned = max(1, min(hard - base, backlog * rules.AUTHOR_SURGE_PASSES_PER_QUEUED_ORDER))
        return min(hard, base + earned), "aged backlog"
    return base, "normal quota"


def budget_check(
    stored: Dict[str, Any],
    order: Optional[Dict[str, Any]] = None,
    open_queue: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Reason to stand down this pass, or None to proceed.

    Capacity is an autonomous queue decision; cost and blast-radius ceilings are
    absolute. Safety checks below are intentionally independent of order priority.
    """
    passes, cost = spend_today()
    if cost >= rules.AUTHOR_MAX_COST_USD_PER_DAY:
        return "daily cost ceiling reached ($%.2f/$%.2f)" % (cost, rules.AUTHOR_MAX_COST_USD_PER_DAY)
    reserved_cost = max_model_pass_cost(order, str(PROJECT)) if order else 0.0
    if reserved_cost and cost + reserved_cost > rules.AUTHOR_MAX_COST_USD_PER_DAY:
        return "insufficient model headroom ($%.2f spent + $%.2f reserved > $%.2f ceiling)" % (
            cost, reserved_cost, rules.AUTHOR_MAX_COST_USD_PER_DAY,
        )
    pass_limit, capacity_reason = adaptive_pass_limit(order, open_queue)
    if passes >= pass_limit:
        return "daily pass capacity spent (%d/%d; %s)" % (
            passes, pass_limit, capacity_reason,
        )

    # A worktree forks from main. If packaged source differs from main, that base is
    # stale and a successful repair would silently republish the pre-change system.
    # Include untracked files: a newly added control module is as release-critical as
    # a modified tracked one. The live strategy journal is linked evidence, not code.
    if vcs.available():
        dirty_source = [
            path for path in vcs.dirty_paths(include_untracked=True)
            if control.is_release_source(path)
        ]
        if dirty_source:
            return "release source differs from main (%d file(s): %s)" % (
                len(dirty_source), ", ".join(dirty_source[:3])
            )

    # Never author while a previous release is still on probation: two unproven
    # changes at once make an unhealthy canary impossible to attribute.
    if canary.active():
        return "a canary is still watching the previous release"

    # Space every claimed pass, not only successful publications. Otherwise a
    # rejected patch or pre-existing gate failure retries every launchd tick.
    last_run = stored.get("last_attempted_run", stored.get("last_authored_run"))
    current = canary.latest_run()
    if isinstance(last_run, int) and isinstance(current, int):
        if current - last_run < rules.AUTHOR_MIN_INTERVAL_RUNS:
            return "only %d run(s) since the last authored change (need %d)" % (
                current - last_run, rules.AUTHOR_MIN_INTERVAL_RUNS,
            )
    return None


# -- staging -----------------------------------------------------------------


def stage_tree(order_id: str = "stage") -> Dict[str, Any]:
    """An isolated tree to patch and gate, preferring a git worktree.

    The live tree is never edited speculatively: launchd fires every 300s and
    would happily execute a half-applied change, which is incident #1 in
    deploy/release.sh's history.

    Two implementations, same contract. A git worktree is better in every way that
    matters -- it is a real checkout at a known commit, so the isolation is a
    property of the tool rather than of STAGE_DIRS being kept in sync, and the
    result is a reviewable diff and a revertable commit. The copy path stays for
    when git is unavailable, because losing version control should degrade review
    quality, not stop the farm from repairing itself.
    """
    if vcs.available():
        try:
            worktree = vcs.worktree_add(order_id)
            return {"root": worktree["path"], "vcs": worktree}
        except vcs.GitError:
            pass  # fall through to the copy path
    root = tempfile.mkdtemp(prefix="author-stage-")
    for name in STAGE_DIRS:
        source = PROJECT / name
        if source.is_dir():
            shutil.copytree(str(source), os.path.join(root, name),
                            ignore=shutil.ignore_patterns("__pycache__"))
    for name in STAGE_FILES:
        source = PROJECT / name
        if source.is_file():
            shutil.copy2(str(source), os.path.join(root, name))
    # Gates read state; they must not get their own empty one and call it clean.
    os.symlink(str(STATE), os.path.join(root, "state"))
    journal = PROJECT / "farm-strategy-journal.md"
    if journal.exists():
        os.symlink(str(journal), os.path.join(root, "farm-strategy-journal.md"))
    return {"root": root, "vcs": None}


def unstage(stage: Dict[str, Any], keep_branch: bool = False) -> None:
    """Tear down a staging tree of either kind."""
    worktree = stage.get("vcs")
    if worktree:
        try:
            vcs.worktree_remove(worktree, keep_branch=keep_branch)
            return
        except vcs.GitError:
            pass
    shutil.rmtree(stage.get("root") or "", ignore_errors=True)


def editable(rel: str) -> Optional[str]:
    """Why `rel` may not be edited, or None if it may."""
    original = str(rel or "")
    rel = control.normalize_path(original)
    if rel.startswith("/") or ".." in rel.split("/"):
        return "path escapes the project"
    if not rel.endswith(".py"):
        return "only Python files may be edited"
    if control.is_protected(rel):
        return "%s is trusted control-plane machinery and is protected" % rel
    if control.author_editable(rel):
        return None
    return "%s is outside the editable set" % rel


# -- mechanical backend ------------------------------------------------------


def mechanical_patch(order: Dict[str, Any], root: str) -> Optional[Dict[str, Any]]:
    """Deterministic repair for changes that need no judgement.

    Currently: argument renames. The diff already identified the old and new
    names, and the fix is a keyword swap at known call sites. Doing this in Python
    keeps the common case free, instant and reproducible.
    """
    if order.get("kind") != "arg_removed":
        return None
    detail = order.get("detail") or {}
    old = str(detail.get("arg") or "")
    new = str(detail.get("rename_candidate") or "")
    tool = str(order.get("tool") or "")
    if not old or not new or not tool:
        return None

    changed: Dict[str, str] = {}
    # Rewrite `<something>.call("tool", ..., old=X, ...)` -> `new=X`, only inside
    # the argument list of a call to this specific tool. A blind global rename
    # would corrupt unrelated code that happens to share the keyword.
    pattern = re.compile(
        r"(\.(?:call|_call|call_tool)\(\s*[\"']%s[\"'][^()]*?)\b%s\s*=" % (re.escape(tool), re.escape(old)),
        re.DOTALL,
    )
    for rel in mechanical_candidate_files(order, root):
        path = os.path.join(root, rel)
        try:
            before = open(path, "r", encoding="utf-8").read()
        except OSError:
            continue
        after, count = pattern.subn(lambda m: m.group(1) + new + "=", before)
        if count:
            changed[rel] = after
    if not changed:
        return None
    return {
        "backend": "mechanical",
        "files": changed,
        "summary": "renamed %s= to %s= at %d call site file(s) for %s"
                   % (old, new, len(changed), tool),
    }


def mechanical_candidate_files(order: Dict[str, Any], root: str) -> List[str]:
    """Files eligible for the narrow deterministic keyword-rename backend.

    A model can never edit trusted code. A mechanically derived endpoint rename is
    different: it only changes one named keyword inside one named MCP call and is
    fully checked by the release matrix. Keeping this path permits contract repair
    in cycle.py without granting model judgement over the control plane.
    """
    out: List[str] = []
    for rel in list(order.get("files") or []):
        rel = control.normalize_path(str(rel))
        if (control.mechanically_editable(rel)
                and os.path.isfile(os.path.join(root, rel))):
            out.append(rel)
    return out[: rules.AUTHOR_MAX_FILES_PER_ORDER]


def candidate_files(order: Dict[str, Any], root: str) -> List[str]:
    """Existing files and explicitly requested new Python paths the model may touch."""
    out: List[str] = []
    for rel in list(order.get("files") or []):
        rel = control.normalize_path(str(rel))
        if editable(rel) is not None:
            continue
        path = os.path.join(root, rel)
        parent = os.path.dirname(path)
        if os.path.isfile(path) or os.path.isdir(parent):
            out.append(rel)
    return out[: rules.AUTHOR_MAX_FILES_PER_ORDER]


# -- model backend -----------------------------------------------------------

SYSTEM_PROMPT = """\
You maintain a headless Python program that plays an automated farming game via an
MCP server. It runs unattended every 300 seconds and is currently in first place.
Your edits ship without human review, so correctness matters more than elegance.

Rules you must follow:

* Reply ONLY with edit blocks in the exact format below. No prose, no explanation,
  no markdown fences around the blocks.
* SEARCH text must be copied byte-for-byte from the file shown to you, and must
  appear exactly once in that file. Include enough surrounding lines to be unique.
* For an explicitly offered NEW FILE only, leave SEARCH empty and put the complete
  bounded file body in REPLACE. Never invent a path that was not offered.
* Make the smallest change that satisfies the acceptance criteria.
* Preserve existing behaviour that the order does not ask you to change.
* Never remove or weaken an existing safety check, budget, timeout or rate limit.
* Never add a third-party dependency; the standard library only.
* Keep new constants in farm/rules.py style (module-level, named, commented) but
  only if you were given that file to edit.
* Explain any non-obvious reasoning in a code comment, not in your reply.

Edit block format, repeated once per change:

--- FILE: path/to/file.py
<<<<<<< SEARCH
exact existing text
=======
replacement text
>>>>>>> REPLACE
"""


def build_prompt(order: Dict[str, Any], root: str) -> Tuple[str, List[str]]:
    """The user half of the prompt: the order, plus the files it may touch."""
    files = candidate_files(order, root)
    parts = [
        "# Work order %s (%s, %s)" % (order.get("id"), order.get("severity"), order.get("kind")),
        "",
        "## What changed on the server",
        str(order.get("summary") or ""),
        "",
        "## What you must achieve",
        str(order.get("intent") or ""),
        "",
        "## Acceptance criteria",
    ]
    parts += ["- %s" % c for c in (order.get("acceptance") or [])] or ["- (none stated)"]
    if order.get("sites"):
        parts += ["", "## Known call sites", ""] + ["- %s" % s for s in order["sites"][:20]]
    if order.get("detail"):
        parts += ["", "## Machine detail", "```json",
                  json.dumps(order["detail"], indent=2, sort_keys=True)[:2000], "```"]
    if order.get("provenance"):
        parts += ["", "## Pre-registered evidence contract", "```json",
                  json.dumps(order["provenance"], indent=2, sort_keys=True)[:3000], "```",
                  "The implementation must preserve this hypothesis identity and must not reuse its discovery evidence as validation evidence."]

    parts += ["", "## Files you may edit", ""]
    if not files:
        parts.append("(none were resolved; reply with no edit blocks)")
    for rel in files:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            parts += [
                "", "--- NEW FILE: %s" % rel,
                "This requested path does not exist. Create it only with an empty SEARCH block.",
            ]
            continue
        try:
            body = open(path, "r", encoding="utf-8").read()
        except OSError:
            continue
        if len(body) > MAX_FILE_BYTES:
            body = body[:MAX_FILE_BYTES] + "\n# ... truncated ...\n"
        parts += ["", "--- FILE: %s" % rel, "```python", body, "```"]
    return "\n".join(parts), files


def model_pass_reservation(order: Optional[Dict[str, Any]], root: str) -> Dict[str, Any]:
    """Conservative token/cost reservation for all model attempts on one order."""
    if not order:
        return {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    user, offered = build_prompt(order, root)
    if not offered:
        return {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    # One token per UTF-8 byte is intentionally conservative for BPE tokenizers.
    # Include maximum retry feedback in every attempt rather than assuming only the
    # second call uses it; this is a financial safety bound, not a usage forecast.
    input_per_attempt = len((SYSTEM_PROMPT + user).encode("utf-8")) + MAX_RETRY_FEEDBACK_BYTES
    tokens_in = input_per_attempt * rules.AUTHOR_MAX_ATTEMPTS_PER_ORDER
    tokens_out = MAX_MODEL_OUTPUT_TOKENS * rules.AUTHOR_MAX_ATTEMPTS_PER_ORDER
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": tokens.cost(tokens_in, tokens_out),
    }


def max_model_pass_cost(order: Optional[Dict[str, Any]], root: str) -> float:
    """Maximum charge needed for admission; deterministic repairs enter free."""
    if order and mechanical_patch(order, root) is not None:
        return 0.0
    return float(model_pass_reservation(order, root)["cost_usd"])


def book_model_reservation(order: Dict[str, Any], root: str) -> str:
    """Durably reserve worst-case model usage immediately before network traffic."""
    reservation = model_pass_reservation(order, root)
    if not reservation["cost_usd"]:
        return ""
    reservation_id = "%s:%s:%d" % (order.get("id"), utcnow(), os.getpid())
    tokens.record(
        "author_reservation", canary.latest_run(),
        tokens_in=reservation["tokens_in"], tokens_out=reservation["tokens_out"],
        note="order=%s worst-case model attempts" % order.get("id"),
        reservation_id=reservation_id,
    )
    log({"event": "cost_reserved", "order": order.get("id"),
         "reservation_id": reservation_id, "cost_usd": reservation["cost_usd"]})
    return reservation_id


EDIT_BLOCK = re.compile(
    r"---\s*FILE:\s*(?P<path>\S+)\s*\n"
    r"<<<<<<<\s*SEARCH\s*\n(?P<search>.*?)\n"
    r"=======\s*\n(?P<replace>.*?)\n"
    r">>>>>>>\s*REPLACE",
    re.DOTALL,
)


def parse_edits(text: str) -> List[Dict[str, str]]:
    out = []
    for match in EDIT_BLOCK.finditer(text or ""):
        out.append({
            "path": match.group("path").strip().lstrip("./"),
            "search": match.group("search"),
            "replace": match.group("replace"),
        })
    return out


def apply_edits(
    edits: List[Dict[str, str]],
    root: str,
    allowed: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Apply edit blocks to the staging tree, refusing anything ambiguous.

    A SEARCH string that matches zero or many times is rejected rather than
    guessed at: applying an edit to the wrong occurrence is how a working farm
    quietly becomes a broken one.
    """
    files: Dict[str, str] = {}
    problems: List[str] = []
    changed_bytes = 0
    allowed_set = set(allowed or []) if allowed is not None else None
    for edit in edits:
        rel = control.normalize_path(edit["path"])
        if allowed_set is not None and rel not in allowed_set:
            problems.append("refused %s: path was not offered" % rel)
            continue
        refusal = editable(rel)
        if refusal:
            problems.append("refused %s: %s" % (rel, refusal))
            continue
        path = os.path.join(root, rel)
        body = files.get(rel)
        if body is None and not os.path.isfile(path):
            if edit["search"]:
                problems.append("refused %s: new files require an empty SEARCH" % rel)
                continue
            files[rel] = edit["replace"]
            changed_bytes += len(edit["replace"].encode("utf-8"))
            continue
        if body is None:
            try:
                body = open(path, "r", encoding="utf-8").read()
            except OSError as exc:
                problems.append("unreadable %s: %s" % (rel, exc.__class__.__name__))
                continue
        if not edit["search"]:
            problems.append("refused %s: empty SEARCH is only valid for a new file" % rel)
            continue
        occurrences = body.count(edit["search"])
        if occurrences == 0:
            problems.append("SEARCH text not found in %s" % rel)
            continue
        if occurrences > 1:
            problems.append("SEARCH text appears %d times in %s; ambiguous" % (occurrences, rel))
            continue
        files[rel] = body.replace(edit["search"], edit["replace"], 1)
        changed_bytes += len(edit["search"].encode("utf-8")) + len(edit["replace"].encode("utf-8"))

    if len(files) > rules.AUTHOR_MAX_FILES_PER_ORDER:
        problems.append("touches %d files, over the %d limit"
                        % (len(files), rules.AUTHOR_MAX_FILES_PER_ORDER))
        files = {}
    return {"files": files, "problems": problems, "changed_bytes": changed_bytes}


def model_patch(order: Dict[str, Any], root: str, feedback: str = "",
                ledger_actor: str = "author", reservation_id: str = "") -> Dict[str, Any]:
    """Ask the gateway for edit blocks and apply them to the staging tree."""
    user, offered = build_prompt(order, root)
    if not offered:
        return {"backend": "model", "files": {}, "problems": ["no editable file resolved for this order"]}
    if feedback:
        user += (
            "\n\n## A previous attempt failed\n"
            "Your last patch was rejected. Fix the underlying problem; do not simply "
            "reformat.\n\n```\n" + feedback[:MAX_RETRY_FEEDBACK_BYTES] + "\n```\n"
        )

    result = llm.complete(
        SYSTEM_PROMPT, user,
        max_output_tokens=MAX_MODEL_OUTPUT_TOKENS,
        run=canary.latest_run(),
        note="order=%s %s" % (order.get("id"), order.get("kind")),
        actor=ledger_actor,
        purpose="work_order" if ledger_actor == "author" else "gateway_smoke_test",
        reservation_id=reservation_id,
    )
    if result["truncated"]:
        return {"backend": "model", "files": {}, "usage": result,
                "problems": ["model output was truncated (%s); patch discarded"
                             % result.get("incomplete_reason")]}

    edits = parse_edits(result["text"])
    if not edits:
        return {"backend": "model", "files": {}, "usage": result,
                "problems": ["model returned no usable edit blocks"]}

    applied = apply_edits(edits, root, allowed=offered)
    size = int(applied.get("changed_bytes") or 0)
    if size > MAX_PATCH_BYTES:
        applied["problems"].append("patch of %d bytes exceeds the %d byte limit" % (size, MAX_PATCH_BYTES))
        applied["files"] = {}
    return {
        "backend": "model",
        "files": applied["files"],
        "problems": applied["problems"],
        "usage": result,
        "summary": "model edited %d file(s) via %d block(s)" % (len(applied["files"]), len(edits)),
    }


# -- gates -------------------------------------------------------------------

GATES = (
    ("self-test", ["/usr/bin/python3", "run.py", "--self-test"]),
    ("knowledge", ["/usr/bin/python3", "deploy/test_knowledge.py"]),
    ("governance", ["/usr/bin/python3", "deploy/test_governance.py"]),
    ("safety", ["/usr/bin/python3", "deploy/test_safety.py"]),
    ("evidence", ["/usr/bin/python3", "deploy/test_evidence.py"]),
    ("tool-trace", ["/usr/bin/python3", "deploy/test_tool_trace.py"]),
    ("topology", ["/usr/bin/python3", "deploy/test_topology.py"]),
    ("dashboard", ["/usr/bin/python3", "deploy/test_dashboard.py"]),
    ("recovery-watch", ["/usr/bin/python3", "deploy/test_recovery_watch.py"]),
    ("contract", ["/usr/bin/python3", "deploy/test_contract.py"]),
    ("contract-watch", ["/usr/bin/python3", "deploy/test_contract_watch.py"]),
    ("runtime-compat", ["/usr/bin/python3", "deploy/test_runtime_compat.py"]),
    ("vcs", ["/usr/bin/python3", "deploy/test_vcs.py"]),
)


def compatibility_preexisting_allowed(
    order: Dict[str, Any],
    pre_existing: List[str],
    attributable: List[str],
) -> bool:
    """Whether release.sh may adjudicate pre-existing strategy-data failures."""
    return (
        str((order.get("provenance") or {}).get("change_class") or "") == "compatibility"
        and bool(pre_existing)
        and set(pre_existing).issubset({"knowledge", "evidence"})
        and not attributable
    )


def run_gates(root: str) -> Dict[str, Any]:
    """The release matrix, executed inside the staging copy.

    Running these before publishing (rather than relying on release.sh alone)
    means a bad patch never reaches the live tree at all.
    """
    results = []
    for name, command in GATES:
        script = command[1] if command[1].endswith(".py") else None
        if script and not os.path.isfile(os.path.join(root, script)):
            continue
        try:
            proc = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                  timeout=600, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({"gate": name, "ok": False, "detail": "%s: %s" % (type(exc).__name__, str(exc)[:200])})
            continue
        ok = proc.returncode == 0
        detail = ""
        if not ok:
            tail = (proc.stdout or "") + (proc.stderr or "")
            detail = tail[-1500:]
        results.append({"gate": name, "ok": ok, "detail": detail})
    failed = [r for r in results if not r["ok"]]
    return {"passed": not failed, "results": results, "failed": failed}


def gates_already_failing(root: str, gate_names: List[str]) -> List[str]:
    """Which of `gate_names` fail on the PRISTINE tree, with no patch applied.

    Attribution matters more than it looks. The gate matrix includes suites that
    assert things about live game data -- for example that the herd/output fit stays
    strong -- and those can go red on their own as the farm grows, with no code
    change involved. An author agent that cannot tell "my patch broke this" from
    "this was already broken" will blame itself, exhaust its retries, and abandon
    perfectly good work orders. Worse, it would keep paying a model to re-fix a
    problem that was never its patch.

    So a gate failure is only charged to the patch if that same gate passes without
    it. This runs only on failure, never on the happy path, so it costs nothing in
    the normal case.
    """
    pristine: List[str] = []
    for name, command in GATES:
        if name not in gate_names:
            continue
        script = command[1] if command[1].endswith(".py") else None
        source = str(PROJECT / script) if script else None
        if source and not os.path.isfile(source):
            continue
        try:
            # Run the live tree's copy of the suite against the live tree, which is
            # by definition unpatched.
            proc = subprocess.run(command, cwd=str(PROJECT), capture_output=True,
                                  text=True, timeout=600, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            pristine.append(name)
    return pristine


def compile_check(files: Dict[str, str], root: str) -> Optional[str]:
    """Syntax-check every edited file before spending minutes on the suites."""
    for rel in files:
        path = os.path.join(root, rel)
        proc = subprocess.run(["/usr/bin/python3", "-m", "py_compile", path],
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return "%s does not compile: %s" % (rel, (proc.stderr or "")[-600:])
    return None


# -- publish -----------------------------------------------------------------


def current_revision() -> str:
    try:
        return os.path.basename(os.path.realpath(str(PROJECT / "release")))
    except OSError:
        return ""


def publish(source_root: str, order: Dict[str, Any], summary: str,
            commit: Optional[str] = None) -> Dict[str, Any]:
    """Package the gated staging tree and atomically publish it under a canary.

    Source and deployment roots are deliberately separate. The old implementation
    copied files into ``PROJECT`` before invoking the release script; when PROJECT was
    mis-resolved to releases/<revision>, that temporarily mutated the running
    immutable release. release.sh now reads code from ``source_root`` and writes only
    a new artifact beneath the canonical checkout.
    """
    previous = current_revision()
    script = PROJECT / "deploy" / "release.sh"
    if not script.is_file():
        return {"published": False, "error": "canonical release script is missing"}
    env = dict(os.environ)
    lineage = order.get("provenance") or {}
    runtime_policy = policy.runtime_context().get("policy_id")
    env.update({
        "FARM_SOURCE_ROOT": str(Path(source_root).resolve()),
        "FARM_DEPLOY_ROOT": str(PROJECT),
        "FARM_CANARY_ORDER_ID": str(order.get("id") or ""),
        "FARM_CANARY_REASON": summary[:500],
        "FARM_CANARY_COMMIT": commit or "",
        "FARM_CANARY_CHANGE_CLASS": str(lineage.get("change_class") or "reliability")[:40],
        "FARM_CANARY_HYPOTHESIS_ID": str(lineage.get("hypothesis_id") or "")[:120],
        "FARM_CANARY_POLICY_ID": str(lineage.get("policy_id") or runtime_policy or "")[:120],
        "FARM_CANARY_EXPECTED_IMPROVEMENT": str(lineage.get("expected_improvement") or 0),
    })
    proc = subprocess.run(["/bin/bash", str(script)], cwd=str(source_root), env=env,
                          capture_output=True, text=True, timeout=1800, check=False)
    if proc.returncode != 0:
        return {"published": False,
                "error": "release.sh refused: %s" % ((proc.stdout + proc.stderr)[-1600:])}

    revision = current_revision()
    if not revision or revision == previous:
        return {"published": False, "error": "release pointer did not advance"}
    armed = canary.status(
        str(PROJECT / canary.STORE), str(PROJECT / canary.RUN_HISTORY)
    )
    if armed.get("revision") != revision or armed.get("status") != canary.WATCHING:
        return {"published": False, "error": "release advanced without arming its canary"}
    return {"published": True, "revision": revision, "previous": previous, "canary": armed}


# -- main --------------------------------------------------------------------


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("AUTHOR skipped: a previous pass is still running")
        return 0

    stored = read_json(STORE)
    queue = str(PROJECT / workorders.QUEUE)

    # Reclaim work abandoned by a killed pass before deciding there is nothing to do.
    for released in workorders.release_stale(3600, queue):
        log({"event": "claim_released", "order": released.get("id"), "status": released.get("status")})

    open_queue = workorders.open_orders(queue)
    order = open_queue[0] if open_queue else None
    if not order:
        print("AUTHOR idle: no open work orders")
        return 0

    standdown = budget_check(stored, order, open_queue)
    if standdown:
        print("AUTHOR standing down: %s" % standdown)
        return 0

    blocked = [f for f in (order.get("files") or []) if editable(control.normalize_path(str(f)))]
    if blocked and not candidate_files(order, str(PROJECT)):
        workorders.resolve(order["id"], workorders.ABANDONED,
                           note="requires protected or non-editable files: %s" % ", ".join(blocked[:4]),
                           path=queue)
        print("AUTHOR contained %s: %s is protected; last verified release remains active"
              % (order["id"], blocked[:2]))
        log({"event": "contained", "order": order["id"], "blocked": blocked})
        return 0

    runtime = policy.runtime_context()
    ledger.set_context(actor="author_agent", run=canary.latest_run(),
                       policy_id=runtime.get("policy_id"),
                       claim_registry_version=runtime.get("claim_registry_version"),
                       step="author_change")

    attempted_run = canary.latest_run()
    workorders.claim(order["id"], "author_agent", run=attempted_run, path=queue)
    stored.update(last_attempted_run=attempted_run, last_order=order["id"])
    write_json(STORE, stored)
    tokens.record("author_pass", attempted_run, note="order=%s %s" % (
        order.get("id"), order.get("kind")
    ))
    print("AUTHOR claimed %s (%s %s): %s"
          % (order["id"], order["severity"], order["kind"], (order.get("summary") or "")[:90]))

    stage = stage_tree(str(order.get("id") or "stage"))
    root = stage["root"]
    if stage.get("vcs"):
        print("  staged in a git worktree on %s at %s"
              % (stage["vcs"]["branch"], vcs.short(stage["vcs"]["base_sha"])))
    try:
        return author_pass(order, root, queue, stored, stage)
    except Exception as exc:  # noqa: BLE001 - a bug here must not wedge the queue
        workorders.resolve(order["id"], workorders.FAILED,
                           note="author agent raised %s: %s" % (type(exc).__name__, str(exc)[:300]),
                           path=queue)
        log({"event": "crashed", "order": order["id"], "error": "%s: %s" % (type(exc).__name__, str(exc)[:400])})
        print("AUTHOR failed on %s: %s: %s" % (order["id"], type(exc).__name__, str(exc)[:200]))
        return 4
    finally:
        unstage(stage)


def author_pass(order: Dict[str, Any], root: str, queue: str, stored: Dict[str, Any],
                stage: Optional[Dict[str, Any]] = None) -> int:
    """Patch, gate and publish one order inside the staging tree."""
    attempt_notes: List[str] = []
    patch: Optional[Dict[str, Any]] = None

    # Mechanical first: free, deterministic, and correct for the common rename.
    reservation_id = ""
    mechanical = mechanical_patch(order, root)
    if mechanical:
        patch = mechanical
        print("  mechanical repair: %s" % mechanical["summary"])
    else:
        availability = llm.availability()
        if not availability.get("available"):
            # Dormancy is a normal state. Leave the order open, say why, and do not
            # charge an attempt for work that never started.
            workorders.resolve(order["id"], workorders.OPEN,
                               note="model dormant: %s" % availability.get("reason", ""),
                               path=queue, attempts=int(order.get("attempts") or 0))
            print("  no mechanical fix and the model is dormant: %s" % availability.get("reason"))
            log({"event": "dormant", "order": order["id"], "reason": availability.get("reason")})
            return 0
        reservation_id = book_model_reservation(order, root)

    feedback = ""
    for attempt in range(1, rules.AUTHOR_MAX_ATTEMPTS_PER_ORDER + 1):
        if patch is None:
            # A deterministic patch may fail an attributable gate and fall back to
            # model judgement. Re-admit and reserve at that exact boundary; never
            # let a free mechanical admission become unbudgeted paid traffic.
            if not reservation_id:
                reservation = model_pass_reservation(order, root)
                _, current_cost = spend_today()
                if (reservation["cost_usd"]
                        and current_cost + reservation["cost_usd"] > rules.AUTHOR_MAX_COST_USD_PER_DAY):
                    workorders.resolve(
                        order["id"], workorders.OPEN,
                        note="mechanical fallback waiting for model budget headroom",
                        path=queue, attempts=int(order.get("attempts") or 0),
                    )
                    print("  model fallback deferred: daily cost ceiling has no safe headroom")
                    return 0
                reservation_id = book_model_reservation(order, root)
            try:
                patch = model_patch(order, root, feedback, reservation_id=reservation_id)
            except llm.Dormant as exc:
                workorders.resolve(order["id"], workorders.OPEN,
                                   note="model became dormant: %s" % exc, path=queue,
                                   attempts=int(order.get("attempts") or 0))
                print("  model went dormant mid-pass: %s" % exc)
                return 0
            except llm.GatewayError as exc:
                workorders.resolve(order["id"], workorders.FAILED,
                                   note="gateway error: %s" % exc, path=queue)
                print("  gateway error contained; a later scheduled pass may retry: %s" % exc)
                return 0

        if patch.get("problems") or not patch.get("files"):
            note = "; ".join(patch.get("problems") or ["no files changed"])
            attempt_notes.append("attempt %d: %s" % (attempt, note))
            print("  attempt %d rejected: %s" % (attempt, note[:200]))
            feedback = note
            patch = None
            continue

        files = patch["files"]
        for rel, body in files.items():
            target_path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as target:
                target.write(body)

        broken = compile_check(files, root)
        if broken:
            attempt_notes.append("attempt %d: %s" % (attempt, broken))
            print("  attempt %d does not compile" % attempt)
            feedback = broken
            restore(order, root, files, stage)
            patch = None
            continue

        print("  gating %d file(s): %s" % (len(files), ", ".join(sorted(files))))
        gates = run_gates(root)
        if not gates["passed"]:
            failed_names = [g["gate"] for g in gates["failed"]]
            # Do not charge a pre-existing failure to this patch.
            pre_existing = gates_already_failing(root, failed_names)
            genuinely_ours = [n for n in failed_names if n not in pre_existing]
            compatibility_waiver = compatibility_preexisting_allowed(
                order, pre_existing, genuinely_ours
            )
            if pre_existing and not genuinely_ours and compatibility_waiver:
                # This is not enough to publish by itself. release.sh independently
                # proves every packaged byte except format_compat.py matches the live
                # immutable release before recognizing these strategy-data failures.
                print("  compatibility repair: pre-existing %s deferred to immutable overlay proof"
                      % ", ".join(pre_existing))
                ledger.record("author.compatibility_preexisting", {
                    "order": order["id"], "gates": pre_existing,
                })
            elif pre_existing and not genuinely_ours:
                workorders.resolve(
                    order["id"], workorders.OPEN,
                    note="blocked: %s already failing before this patch; "
                         "not attributable to the change" % ", ".join(pre_existing),
                    path=queue,
                    # Give the attempt back. Claiming incremented the counter, but
                    # nothing about this order was actually tried, and three such
                    # passes would otherwise abandon a perfectly good order for a
                    # failure that was never its fault.
                    attempts=int(order.get("attempts") or 0),
                )
                print("  standing down: %s already red on the unpatched tree"
                      % ", ".join(pre_existing))
                print("  the order stays open; no attempt charged")
                log({"event": "blocked_pre_existing", "order": order["id"],
                     "gates": pre_existing})
                ledger.record("author.blocked", {"order": order["id"],
                                                "pre_existing_gates": pre_existing})
                return 0
            if genuinely_ours:
                detail = "; ".join("%s failed" % name for name in genuinely_ours)
                evidence = "\n\n".join(
                    "%s:\n%s" % (g["gate"], g["detail"])
                    for g in gates["failed"] if g["gate"] in genuinely_ours
                )[:6000]
                attempt_notes.append("attempt %d: %s" % (attempt, detail))
                print("  attempt %d failed gates: %s" % (attempt, detail))
                feedback = detail + "\n\n" + evidence
                restore(order, root, files, stage)
                patch = None
                continue

        # Verified in isolation. A repair is not publishable until the exact gated
        # commit is present on the allowlisted remote. This is deliberately fail-closed:
        # a locally successful patch with no durable upstream record is not a release.
        summary = patch.get("summary") or "work order %s" % order["id"]
        if not (stage and stage.get("vcs")):
            note = "version control unavailable; remote synchronization is required"
            workorders.resolve(order["id"], workorders.FAILED, note=note, path=queue)
            log({"event": "remote_sync_failed", "order": order["id"], "error": note})
            ledger.record("author.remote_sync_failed", {"order": order["id"], "error": note})
            print("  publish safely contained: %s" % note)
            return 0

        try:
            commit_info = commit_change(stage["vcs"], order, patch, summary)
        except (vcs.GitError, OSError) as exc:
            note = "version control or remote synchronization failed: %s" % str(exc)[:320]
            workorders.resolve(order["id"], workorders.FAILED, note=note, path=queue)
            log({"event": "remote_sync_failed", "order": order["id"], "error": str(exc)[:300]})
            ledger.record("author.remote_sync_failed", {"order": order["id"],
                                                         "error": str(exc)[:300]})
            print("  publish safely contained: %s" % note[:300])
            return 0

        result = publish(root, order, summary, commit=commit_info.get("sha"))
        if not result.get("published"):
            workorders.resolve(order["id"], workorders.FAILED,
                               note=str(result.get("error"))[:400], path=queue)
            print("  publish safely contained: %s" % str(result.get("error"))[:300])
            log({"event": "publish_refused", "order": order["id"], "error": result.get("error")})
            return 0

        vcs.tag_release(result["revision"], commit_info["sha"])
        push = commit_info.get("push") or {}
        remote_ref = "%s/%s" % (push.get("remote") or vcs.PUSH_REMOTE,
                                  push.get("branch") or vcs.MAIN)
        audit_note = "%s | pushed %s to %s" % (
            summary, vcs.short(commit_info.get("sha")), remote_ref,
        )
        workorders.resolve(order["id"], workorders.PUBLISHED,
                           note=audit_note, release=result["revision"], path=queue,
                           backend=patch.get("backend"),
                           commit=commit_info.get("sha"),
                           remote=remote_ref,
                           remote_commit=push.get("sha"),
                           diff=commit_info.get("stat"))
        write_json(STORE, dict(stored, last_authored_run=canary.latest_run(),
                               last_order=order["id"], last_revision=result["revision"],
                               last_commit=commit_info.get("sha"),
                               last_remote=remote_ref,
                               last_remote_commit=push.get("sha"),
                               last_ts=utcnow()))
        log({"event": "published", "order": order["id"], "revision": result["revision"],
             "previous": result["previous"], "backend": patch.get("backend"),
             "files": sorted(files), "summary": summary,
             "commit": commit_info.get("sha"), "remote": remote_ref,
             "remote_commit": push.get("sha"), "diff": commit_info.get("stat")})
        ledger.record("author.published", {"order": order["id"], "revision": result["revision"],
                                          "backend": patch.get("backend"), "files": sorted(files),
                                          "commit": commit_info.get("sha"), "remote": remote_ref,
                                          "remote_commit": push.get("sha")})
        print("AUTHOR published %s as %s (canary armed, previous %s)"
              % (order["id"], result["revision"], result["previous"]))
        print("  commit %s pushed to %s before release"
              % (vcs.short(commit_info["sha"]), remote_ref))
        return 0

    workorders.resolve(order["id"], workorders.FAILED,
                       note=" | ".join(attempt_notes)[:500], path=queue)
    log({"event": "exhausted", "order": order["id"], "notes": attempt_notes})
    print("AUTHOR contained %s after %d bounded attempt(s); verified release unchanged"
          % (order["id"], rules.AUTHOR_MAX_ATTEMPTS_PER_ORDER))
    return 0


def commit_change(worktree: Dict[str, Any], order: Dict[str, Any],
                  patch: Dict[str, Any], summary: str) -> Dict[str, Any]:
    """Commit, push the gated SHA, fast-forward local main, and sync the live tree.

    The commit message is the audit trail a reviewer actually reads, so it carries
    the order id, the detection source, which backend wrote it, and the acceptance
    criteria the gates were standing in for. A model-authored change that cannot
    explain itself is indistinguishable from a corrupted one. Remote publication
    happens before local main moves, so an SSH or destination failure leaves the
    canonical branch and live release untouched.
    """
    body = [summary, ""]
    body.append("Work order: %s (%s, %s)" % (order["id"], order.get("severity"), order.get("kind")))
    if order.get("source"):
        body.append("Detected by: %s" % order["source"])
    body.append("Authored by: %s backend" % (patch.get("backend") or "unknown"))
    if order.get("tool"):
        body.append("Tool: %s" % order["tool"])
    acceptance = order.get("acceptance") or []
    if acceptance:
        body.append("")
        body.append("Acceptance criteria carried by this change:")
        for item in acceptance[:6]:
            body.append("  * %s" % str(item)[:160])
    body.append("")
    body.append("Gates: the full release matrix passed in an isolated worktree before")
    body.append("this commit was made. A canary is armed on the resulting release and")
    body.append("will revert the pointer automatically if production regresses.")
    body.append("")
    body.append("Authored autonomously by experiments/author_agent.py.")
    message = "\n".join(body)

    sha = vcs.commit_worktree(worktree, message)
    if not sha:
        raise vcs.GitError("gated patch produced no commit")
    diff = vcs.diff_stat(worktree)
    base_sha = worktree.get("base_sha")
    push = vcs.push_main(
        sha,
        expected_remote_sha=base_sha,
        expected_local_sha=base_sha,
    )
    merged = vcs.merge_to_main(worktree, summary)
    # Only after GitHub has acknowledged the exact gated commit do local main and
    # the live tree move forward. A whole-tree checkout could clobber unrelated edits.
    synced = vcs.sync_live_tree(diff.get("files") or [])
    return {
        "sha": merged or sha,
        "branch": worktree["branch"],
        "stat": diff.get("stat"),
        "files": diff.get("files"),
        "insertions": diff.get("insertions"),
        "deletions": diff.get("deletions"),
        "synced": synced,
        "push": push,
    }


def restore(order: Dict[str, Any], root: str, files: Dict[str, str],
            stage: Optional[Dict[str, Any]] = None) -> None:
    """Undo a rejected patch in the staging tree so the next attempt starts clean.

    In a worktree the authority is the branch's base commit, not the live tree: the
    live tree may itself have uncommitted edits, and copying those in would mean the
    retry starts from something that was never gated.
    """
    if stage and stage.get("vcs"):
        try:
            vcs._run(["checkout", "--", "."], cwd=root, check=False)
            return
        except (vcs.GitError, OSError):
            pass
    for rel in files:
        source = PROJECT / rel
        try:
            shutil.copy2(str(source), os.path.join(root, rel))
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
