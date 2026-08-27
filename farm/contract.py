"""The observable contract of the MCP surface, and detection of its drift.

The farm is played against a live server that is expected to keep changing. Today
the loop notices exactly one kind of change: `cycle.py` compares the sorted list
of tool *names* against the previous run. That is a thin tripwire. It cannot see:

  * a new **required** argument on a tool we already call (breaks on next call)
  * an argument **renamed** (`animal_id` -> `id`) or its type changed
  * an enum value removed from a schema we depend on
  * a **response format** change, until `parse.py` happens to raise ParseDrift
  * a brand new tool that opens a strategy we are not playing

This module captures the whole observable contract, fingerprints it, diffs it
against a stored baseline, and classifies each change by whether it actually
breaks *us* -- which is knowable, because we can read our own call sites.

Cost discipline (why this does not hammer the server)
----------------------------------------------------
POSTMORTEM-run377 is unambiguous that added load and mis-aimed throttles nearly
lost the game, so a monitoring agent that polls 15 endpoints on a timer is not
acceptable. A scan therefore makes **exactly one** MCP call: `tools/list`.

Response shapes come free from `state/raw/latest/`, which the cycle already
writes on every run. Those files are at most 300s stale, cover 10 of the 15
tools, and cost nothing to read. `list_farm` alone is ~20MB on the wire; re-fetching
it every 15 minutes to check its format would add real pressure for no new
information. The remaining tools are mutating (`adopt_animal`, `plant`, `gift`)
and are never called for monitoring -- their schemas are still covered by
`tools/list`, and their response shapes are learned the next time the farm
legitimately uses them.

Noise discipline
----------------
Game text is full of moods, names and species that come and go. A single scan is
not evidence of a format change. Structural diffs are always recorded to
`state/contract.ndjson` for audit, but a **work order** is only emitted once the
same change has been observed in `CONFIRM_SCANS` consecutive scans. Schema
changes from `tools/list` are authoritative and need no such confirmation.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASELINE = os.path.join("state", "contract.json")
HISTORY = os.path.join("state", "contract.ndjson")
RAW_DIR = os.path.join("state", "raw", "latest")

# How many consecutive scans must agree before a response-shape change becomes a
# work order. Schema changes bypass this.
CONFIRM_SCANS = 2

# Bounds on what a shape retains. A shape is a *structure*, not a sample: it must
# not grow with herd size, and must never carry game data into the baseline.
MAX_TEMPLATES = 40
MAX_VOCAB = 120
MIN_VOCAB_COUNT = 3
MAX_LINES_SCANNED = 4000

# Raw-file prefix -> tool. The cycle names raw dumps by call site, not by tool,
# so this mapping is how free samples get attributed. Longest prefix wins.
RAW_PREFIX_TO_TOOL = (
    ("list_farm", "list_farm"),
    ("leaderboard", "leaderboard"),
    ("collect", "collect_produce"),
    ("events", "farm_events"),
    ("feed_retry", "feed_animals"),
    ("backstop_feed", "feed_animals"),
    ("feed", "feed_animals"),
    ("harvest", "harvest"),
    ("buy_feed", "buy_feed"),
    ("sell", "sell"),
    ("propose", "propose_trade"),
    ("respond", "respond_to_trade"),
)

# Tools that must never be called by a monitoring agent because they change game
# state. Kept as an explicit allowlist's complement: anything not provably
# read-only is treated as mutating.
READ_ONLY_TOOLS = ("list_farm", "leaderboard", "farm_events")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- response shape ---------------------------------------------------------

_DIGIT_TOKEN = re.compile(r"^[^A-Za-z]*\d")
_LEADING_DIGIT = re.compile(r"^\d")
_ALPHA = re.compile(r"^[a-z][a-z_-]*$")
_WORD = re.compile(r"[A-Za-z_]+")


def _token_class(token: str) -> str:
    """Collapse one whitespace-delimited token to its structural class.

    Every word becomes `<w>` and every number `#`, keeping only the punctuation
    and symbol frame. This is deliberately more aggressive than it first appears
    necessary, and the reason is a measured false-positive:

    An earlier version kept lowercase words verbatim, on the theory that they
    carry the schema (`hunger`, `happiness`, species, moods). But moods are data,
    not structure. Real captured samples of the *same* server version disagreed
    purely because the herd's mood had changed -- `is delighted.` in one dump,
    `is starving.` in another -- so an unchanged server produced a "response
    format changed" diff on almost every scan. That is the exact false positive
    that would wake the author agent to rewrite working code.

    Word identity is not lost: it moves to `vocabulary` and `numeric_labels`,
    where a set difference is the honest representation and severity is lower.
    """
    if not token:
        return ""
    if _DIGIT_TOKEN.match(token):
        # Keep the punctuation that frames a number so "(#7)" and "0/100," stay
        # distinguishable from a bare count. These templates end up in work
        # orders that a model has to read, so legibility is worth the few bytes.
        lead = "".join(ch for ch in token[:2] if ch in "([{<")
        trail = "".join(ch for ch in token[-2:] if ch in ",.:;)]}>")
        return lead + "#" + trail
    if _WORD.search(token):
        # Preserve trailing punctuation, which is real structure ('delighted.'
        # ends a clause; 'delighted' does not).
        lead = "".join(ch for ch in token[:1] if ch in "([{<\"'")
        trail = "".join(ch for ch in token[-2:] if ch in ",.:;!?)]}>\"'")
        return lead + "<w>" + trail
    return token


def _template(line: str) -> str:
    """Structural skeleton of one line, with word runs collapsed.

    Consecutive words collapse to a single `<w+>` because proper nouns have a
    variable word count: 'Gold Rush the beehive' and 'Buzzy the beehive' are the
    same structure but tokenize to different lengths. Without collapsing, simply
    adopting an animal whose name happens to be two words would register as a
    response format change.

    A run is broken by any non-word token (a number, a bare symbol) or by closing
    punctuation on a word, since 'delighted.' ends a clause and 'delighted' does
    not.
    """
    out: List[str] = []
    run_open = False
    for token in line.split():
        klass = _token_class(token)
        if not klass:
            continue
        marker = klass.find("<w>")
        if marker < 0:
            out.append(klass)
            run_open = False
            continue
        leading = klass[:marker]
        trailing = klass[marker + 3 :]
        if run_open and not leading:
            out[-1] = "<w+>" + trailing
        else:
            out.append(leading + "<w+>" + trailing)
        run_open = not trailing
    return " ".join(out)


def shape_of(text: str) -> Dict[str, Any]:
    """Structural signature of a tool response.

    Deliberately lossy and bounded: two responses from the same server version
    must produce the same shape regardless of how many animals exist or what mood
    they are in.
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    scanned = lines[:MAX_LINES_SCANNED]

    template_counts = Counter(_template(ln) for ln in scanned)
    templates = [tpl for tpl, _ in template_counts.most_common(MAX_TEMPLATES)]

    words = Counter()
    for line in scanned:
        for word in _WORD.findall(line):
            # Only words that are already lowercase in the source. Animal names
            # are capitalized ('Pecky the chicken'), and folding them in made the
            # vocabulary churn every time the herd changed -- which would have
            # produced a steady drip of false 'response changed' work orders.
            # Schema words (hunger, happiness), species and moods are lowercase.
            if _ALPHA.match(word) and len(word) > 2:
                words[word] += 1
    vocabulary = sorted(w for w, c in words.most_common(MAX_VOCAB) if c >= MIN_VOCAB_COUNT)

    # A word immediately followed by a number is a field label: 'hunger 0/100',
    # 'happiness 100/100', '# coins'. This is the signal that survives the
    # template abstraction and catches a field being renamed, which is the
    # response-side change most likely to break parse.py.
    labels = Counter()
    for line in scanned:
        tokens = line.split()
        for index, token in enumerate(tokens[:-1]):
            word = token.strip("()[]{}.,:;!?'\"").lower()
            # The next token must begin with an actual digit, not '(' or '#'. An
            # identifier like 'chicken (#88215)' is not a measurement, and
            # counting it made the species look like a numeric field whenever a
            # given species happened to appear often enough to pass the floor.
            if _ALPHA.match(word) and len(word) > 2 and _LEADING_DIGIT.match(tokens[index + 1]):
                labels[word] += 1
    numeric_labels = sorted(w for w, c in labels.items() if c >= MIN_VOCAB_COUNT)

    # Emoji and symbols are load-bearing structure in this game's output
    # (🐔 species, 🪙 coins, 🏆 leaderboard), so they are tracked explicitly.
    symbols = sorted({ch for ln in scanned for ch in ln if ord(ch) > 0x2100})

    # "key: value" and "key=value" field names, the strongest schema signal.
    fields = sorted(
        {
            m.group(1).lower()
            for ln in scanned
            for m in [re.match(r"^\s*([A-Za-z][A-Za-z _-]{0,30})[:=]", ln)]
            if m
        }
    )

    return {
        "templates": sorted(templates),
        "vocabulary": vocabulary,
        "numeric_labels": numeric_labels,
        "symbols": symbols,
        "fields": fields,
        "empty": not scanned,
    }


# -- schema normalization ---------------------------------------------------


def normalize_tools(tools: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Full tool descriptors reduced to a stable, comparable form."""
    out: Dict[str, Any] = {}
    for tool in tools or []:
        name = str(tool.get("name") or "")
        if not name:
            continue
        schema = tool.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        args: Dict[str, Any] = {}
        for arg, spec in sorted(properties.items()):
            spec = spec if isinstance(spec, dict) else {}
            args[arg] = {
                "type": spec.get("type"),
                "enum": sorted(str(v) for v in spec.get("enum")) if isinstance(spec.get("enum"), list) else None,
                "default": spec.get("default"),
                # Description wording is cosmetic drift; keep a digest so a
                # reworded doc string does not masquerade as a schema change.
                "description_sha": _sha(str(spec.get("description") or ""))[:12] if spec.get("description") else None,
            }
        out[name] = {
            "required": sorted(str(r) for r in (schema.get("required") or [])),
            "args": args,
            "description_sha": _sha(str(tool.get("description") or ""))[:12],
            "description": str(tool.get("description") or "")[:400],
        }
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def fingerprint(snapshot: Dict[str, Any]) -> str:
    """Content address of a contract, ignoring timestamps.

    Two scans of an unchanged server must produce the same fingerprint, so the
    watcher can decide "nothing happened" without diffing.
    """
    material = {
        "tools": snapshot.get("tools") or {},
        "shapes": snapshot.get("shapes") or {},
    }
    return _sha(json.dumps(material, sort_keys=True, default=str))


# -- what the code actually relies on ---------------------------------------


def reliance(root: str = ".") -> Dict[str, Any]:
    """Which tools our code calls, with which argument names, and from where.

    This is what makes severity decidable. A new required argument on a tool we
    never call is trivia; the same change on `feed_animals` will break feeding on
    the next cycle and must be fixed before it does.

    Static analysis over our own source, mirroring topology.py's reasoning: it
    works without executing a cycle, and it is the code rather than a memory of
    the code. topology.py is not reused here because it resolves tools but drops
    argument names, which are precisely what breakage turns on.
    """
    used: Dict[str, Dict[str, Any]] = {}
    for path in _source_files(root):
        try:
            tree = ast.parse(open(path, "r", encoding="utf-8").read(), filename=path)
        except (OSError, SyntaxError):
            continue
        rel = os.path.relpath(path, root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_tool(node)
            if not name:
                continue
            entry = used.setdefault(name, {"args": set(), "sites": set()})
            for keyword in node.keywords:
                if keyword.arg:
                    entry["args"].add(keyword.arg)
            entry["sites"].add("%s:%d" % (rel, getattr(node, "lineno", 0)))
    return {
        tool: {"args": sorted(v["args"]), "sites": sorted(v["sites"])}
        for tool, v in sorted(used.items())
    }


def _called_tool(node: ast.Call) -> Optional[str]:
    """Extract the tool name from `<client>.call("tool", ...)` style calls."""
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in ("call", "_call", "call_tool"):
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _source_files(root: str) -> List[str]:
    out: List[str] = []
    for base in ("farm", "experiments"):
        directory = os.path.join(root, base)
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".py"):
                out.append(os.path.join(directory, entry))
    top = os.path.join(root, "run.py")
    if os.path.isfile(top):
        out.append(top)
    return out


# -- capture ----------------------------------------------------------------


def sampled_shapes(raw_dir: str = RAW_DIR, max_age_seconds: Optional[float] = None) -> Dict[str, Any]:
    """Response shapes derived from the cycle's own raw dumps. Zero MCP calls.

    Multiple raw files can map to one tool (`list_farm_start`, `list_farm_final`,
    ...). The freshest file wins, because an older dump may predate a server
    change and would make a real drift look like a flap.
    """
    shapes: Dict[str, Any] = {}
    freshest: Dict[str, float] = {}
    if not os.path.isdir(raw_dir):
        return shapes
    now = datetime.now(timezone.utc).timestamp()
    for entry in sorted(os.listdir(raw_dir)):
        if not entry.endswith(".txt"):
            continue
        tool = _tool_for_raw(entry[:-4])
        if not tool:
            continue
        path = os.path.join(raw_dir, entry)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if max_age_seconds is not None and (now - mtime) > max_age_seconds:
            continue
        if tool in freshest and freshest[tool] >= mtime:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = _head(handle, MAX_LINES_SCANNED)
        except OSError:
            continue
        freshest[tool] = mtime
        shape = shape_of(text)
        shape["source"] = entry
        shape["sampled_at"] = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        shapes[tool] = shape
    return shapes


def _head(handle, limit: int) -> str:
    """Read at most `limit` lines. list_farm is ~20MB; never load it whole."""
    lines: List[str] = []
    for index, line in enumerate(handle):
        if index >= limit:
            break
        lines.append(line)
    return "".join(lines)


def _tool_for_raw(stem: str) -> Optional[str]:
    for prefix, tool in sorted(RAW_PREFIX_TO_TOOL, key=lambda p: -len(p[0])):
        if stem == prefix or stem.startswith(prefix + "_") or stem.startswith(prefix):
            return tool
    return None


def capture(client: Any, raw_dir: str = RAW_DIR, root: str = ".") -> Dict[str, Any]:
    """One full contract snapshot. Exactly one MCP call: tools/list."""
    result = client.rpc("tools/list")
    tools = normalize_tools(result.get("tools") or [])
    snapshot = {
        "ts": _utcnow(),
        "tools": tools,
        "shapes": sampled_shapes(raw_dir),
        "reliance": reliance(root),
    }
    snapshot["fingerprint"] = fingerprint(snapshot)
    return snapshot


# -- diff -------------------------------------------------------------------

# Severity ranking, worst first. `breaking` means the farm will fail or already
# is failing; `opportunity` means new capability we are not using.
SEVERITIES = ("breaking", "shape", "opportunity", "additive", "cosmetic")


def diff(old: Optional[Dict[str, Any]], new: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Classified changes between two snapshots.

    Severity is judged against `new["reliance"]`: the same schema change is
    `breaking` on a tool we call and `additive` on one we do not.
    """
    changes: List[Dict[str, Any]] = []
    if not old:
        return changes

    old_tools = old.get("tools") or {}
    new_tools = new.get("tools") or {}
    rely = new.get("reliance") or {}

    for name in sorted(set(old_tools) | set(new_tools)):
        before = old_tools.get(name)
        after = new_tools.get(name)
        used = name in rely
        used_args = set((rely.get(name) or {}).get("args") or [])

        if before and not after:
            changes.append(_change(
                "tool_removed", "breaking" if used else "additive", name,
                "tool %s disappeared from tools/list" % name,
                used=used, sites=(rely.get(name) or {}).get("sites") or [],
            ))
            continue
        if after and not before:
            changes.append(_change(
                "tool_added", "opportunity", name,
                "new tool %s: %s" % (name, (after.get("description") or "")[:160]),
                used=False, detail={"required": after.get("required"), "args": sorted(after.get("args") or {})},
            ))
            continue
        if not before or not after:
            continue

        # Required arguments are the sharpest edge: adding one silently breaks
        # every existing call site.
        added_required = sorted(set(after.get("required") or []) - set(before.get("required") or []))
        dropped_required = sorted(set(before.get("required") or []) - set(after.get("required") or []))
        if added_required:
            changes.append(_change(
                "required_arg_added", "breaking" if used else "additive", name,
                "%s now requires %s" % (name, ", ".join(added_required)),
                used=used, sites=(rely.get(name) or {}).get("sites") or [],
                detail={"args": added_required, "we_pass": sorted(used_args)},
            ))
        if dropped_required:
            changes.append(_change(
                "required_arg_dropped", "additive", name,
                "%s no longer requires %s" % (name, ", ".join(dropped_required)), used=used,
            ))

        before_args = before.get("args") or {}
        after_args = after.get("args") or {}
        removed_args = sorted(set(before_args) - set(after_args))
        added_args = sorted(set(after_args) - set(before_args))

        for arg in removed_args:
            # An argument we actually pass vanishing is breaking; and if exactly
            # one new argument appeared alongside, name it as a rename candidate
            # so the author agent can make a mechanical fix instead of guessing.
            candidate = added_args[0] if len(added_args) == 1 and len(removed_args) == 1 else None
            changes.append(_change(
                "arg_removed", "breaking" if arg in used_args else "additive", name,
                "%s lost argument %s%s" % (name, arg, " (possibly renamed to %s)" % candidate if candidate else ""),
                used=used, sites=(rely.get(name) or {}).get("sites") or [],
                detail={"arg": arg, "rename_candidate": candidate, "we_pass": sorted(used_args)},
            ))
        for arg in added_args:
            if len(added_args) == 1 and len(removed_args) == 1:
                continue  # already reported as a rename candidate
            if arg in added_required:
                continue  # already reported, with sharper severity, as required
            changes.append(_change(
                "arg_added", "opportunity" if used else "additive", name,
                "%s gained optional argument %s" % (name, arg), used=used,
                detail={"arg": arg, "spec": after_args.get(arg)},
            ))

        for arg in sorted(set(before_args) & set(after_args)):
            b, a = before_args[arg] or {}, after_args[arg] or {}
            if b.get("type") != a.get("type"):
                changes.append(_change(
                    "arg_type_changed", "breaking" if arg in used_args else "additive", name,
                    "%s.%s type %s -> %s" % (name, arg, b.get("type"), a.get("type")),
                    used=used, sites=(rely.get(name) or {}).get("sites") or [],
                    detail={"arg": arg, "from": b.get("type"), "to": a.get("type")},
                ))
            lost = sorted(set(b.get("enum") or []) - set(a.get("enum") or []))
            gained = sorted(set(a.get("enum") or []) - set(b.get("enum") or []))
            if lost:
                changes.append(_change(
                    "enum_values_removed", "breaking" if used else "additive", name,
                    "%s.%s dropped values: %s" % (name, arg, ", ".join(lost[:8])),
                    used=used, detail={"arg": arg, "removed": lost},
                ))
            if gained:
                changes.append(_change(
                    "enum_values_added", "opportunity", name,
                    "%s.%s offers new values: %s" % (name, arg, ", ".join(gained[:8])),
                    used=used, detail={"arg": arg, "added": gained},
                ))

        if before.get("description_sha") != after.get("description_sha"):
            changes.append(_change(
                "description_changed", "cosmetic", name,
                "%s description was reworded" % name, used=used,
            ))

    changes.extend(_shape_diff(old.get("shapes") or {}, new.get("shapes") or {}, rely))
    changes.sort(key=lambda c: (SEVERITIES.index(c["severity"]), c["tool"], c["kind"]))
    return changes


def _shape_diff(old: Dict[str, Any], new: Dict[str, Any], rely: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Response-format drift, which schemas cannot reveal."""
    out: List[Dict[str, Any]] = []
    for tool in sorted(set(old) & set(new)):
        before, after = old[tool] or {}, new[tool] or {}
        if after.get("empty") and not before.get("empty"):
            # An empty response may just be a quiet farm, so this is not treated
            # as breakage on its own.
            out.append(_change("response_empty", "shape", tool,
                               "%s returned no content this scan" % tool, used=tool in rely))
            continue

        for key, label, severity in (
            ("fields", "field", "shape"),
            ("numeric_labels", "numeric field", "shape"),
            ("symbols", "symbol", "shape"),
            ("templates", "line format", "shape"),
            # Vocabulary is the noisiest signal (moods and species come and go
            # with the herd), so it is recorded for context but never on its own
            # grounds for a rewrite.
            ("vocabulary", "word", "cosmetic"),
        ):
            lost = sorted(set(before.get(key) or []) - set(after.get(key) or []))
            gained = sorted(set(after.get(key) or []) - set(before.get(key) or []))
            if not lost and not gained:
                continue
            # Vocabulary and templates flap with moods and names, so they are
            # reported but lean on scan-to-scan confirmation before acting.
            out.append(_change(
                "response_%s_changed" % key, severity, tool,
                "%s response %ss changed: %s%s%s" % (
                    tool, label,
                    ("-" + ", ".join(lost[:6])) if lost else "",
                    " " if lost and gained else "",
                    ("+" + ", ".join(gained[:6])) if gained else "",
                ),
                used=tool in rely,
                detail={"removed": lost[:20], "added": gained[:20]},
            ))
    return out


def _change(
    kind: str,
    severity: str,
    tool: str,
    summary: str,
    used: bool = False,
    sites: Optional[List[str]] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    record = {
        "kind": kind,
        "severity": severity,
        "tool": tool,
        "summary": summary,
        "we_use_it": bool(used),
        "sites": list(sites or []),
        "detail": detail or {},
    }
    record["id"] = _sha(json.dumps(
        {"kind": kind, "tool": tool, "detail": record["detail"]}, sort_keys=True, default=str
    ))[:16]
    return record


# -- persistence ------------------------------------------------------------


def load_baseline(path: str = BASELINE) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def save_baseline(snapshot: Dict[str, Any], path: str = BASELINE) -> None:
    """Atomic replace: a truncated baseline would look like a total contract wipe."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, sort_keys=True, default=str)
    os.replace(tmp, path)


def record_scan(row: Dict[str, Any], path: str = HISTORY) -> Dict[str, Any]:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def history(limit: int = 50, path: str = HISTORY) -> List[Dict[str, Any]]:
    try:
        lines = open(path, "r", encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def confirm(changes: List[Dict[str, Any]], prior: Dict[str, int]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Count consecutive sightings and mark which changes are actionable.

    Schema facts from tools/list are authoritative immediately. Response-shape
    drift must be seen `CONFIRM_SCANS` times in a row, so one odd game message
    cannot trigger a code rewrite.
    """
    streaks: Dict[str, int] = {}
    actionable: List[Dict[str, Any]] = []
    for change in changes:
        key = change["id"]
        streaks[key] = int(prior.get(key, 0)) + 1
        change["seen_consecutive"] = streaks[key]
        is_shape = change["kind"].startswith("response_")
        change["confirmed"] = (not is_shape) or streaks[key] >= CONFIRM_SCANS
        if change["confirmed"]:
            actionable.append(change)
    return actionable, streaks
