#!/usr/bin/env python3
"""Contract watcher: one-shot endpoint scanner, scheduled every 15 minutes.

This is the eyes of the self-healing loop. It answers one question -- *has the
server changed underneath us?* -- and turns any answer into durable work orders
for the author agent. It never edits code and never mutates the farm.

What one pass costs
-------------------
Exactly one MCP call (`tools/list`). Response formats are read from the raw dumps
the cycle already writes, so watching the whole 15-tool surface adds essentially
nothing to server load. POSTMORTEM-run377 is the reason for that constraint:
mis-aimed throttles and added load nearly lost the game once already.

The baseline is what our code was written against
------------------------------------------------
The stored baseline is deliberately *not* "the last thing we saw". It is the
contract the current code is known to work against, so it only advances when
either:

  * the change needs no code (cosmetic rewording, an unused tool gaining an
    optional argument) and is absorbed immediately, or
  * the author agent successfully publishes a fix and advances it.

Pinning it this way is what makes the loop self-healing rather than
self-forgetting. If the baseline advanced on sight, a breaking change would be
reported once, and if that single repair attempt failed the farm would be left
broken with nothing left to re-detect it. Pinned, the drift is re-reported every
15 minutes until the code actually matches the server again, while `submit()`
keeps the queue free of duplicates.

Exit codes: 0 nothing actionable, 3 actionable drift queued, 4 the scan failed.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import contract, journal, ledger, policy, workorders  # noqa: E402
from farm.mcp import Client, McpError  # noqa: E402

STATE = PROJECT / "state"
STORE = STATE / "contract_watch.json"
LOCK = STATE / ".contract-watch.lock"

# Severities that justify spending a model on a code change. `additive` and
# `cosmetic` are recorded and absorbed without ever waking the author.
ACTIONABLE = ("breaking", "shape", "opportunity")


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


# -- turning a change into an instruction ------------------------------------


def order_for(change: Dict[str, Any]) -> Optional[Tuple[str, List[str], List[str]]]:
    """Intent, acceptance criteria and candidate files for one change.

    The author agent is given an objective and a definition of done, never a
    diff to copy. Acceptance criteria are deliberately checkable by machine so a
    published change can be verified without a human reading it.
    """
    kind = str(change.get("kind") or "")
    tool = str(change.get("tool") or "")
    detail = change.get("detail") or {}
    sites = [s.split(":")[0] for s in (change.get("sites") or [])]
    files = sorted({s for s in sites if s.endswith(".py")})

    if kind == "required_arg_added":
        args = ", ".join(detail.get("args") or [])
        return (
            "The server now requires %s on `%s`, and every existing call site omits it, "
            "so the next call will fail. Pass a correct value for %s at each call site. "
            "Derive the value from state the caller already has; do not invent a constant "
            "and do not add a new configuration knob unless there is genuinely no local "
            "source for it." % (args, tool, args),
            [
                "every call to %s supplies %s" % (tool, args),
                "no new required human configuration is introduced",
                "farm/rules.py holds any new constant, per project convention",
            ],
            files,
        )

    if kind == "arg_removed":
        candidate = detail.get("rename_candidate")
        if candidate:
            return (
                "`%s` no longer accepts `%s`; the server now exposes `%s` in its place, "
                "which is almost certainly a rename. Update every call site to pass `%s` "
                "instead, keeping the value semantics identical."
                % (tool, detail.get("arg"), candidate, candidate),
                [
                    "no call to %s passes %s" % (tool, detail.get("arg")),
                    "calls that previously passed %s now pass %s" % (detail.get("arg"), candidate),
                ],
                files,
            )
        return (
            "`%s` no longer accepts `%s`, which our code still passes. Remove it and "
            "preserve the existing behaviour by other means if the argument was doing "
            "real work." % (tool, detail.get("arg")),
            ["no call to %s passes %s" % (tool, detail.get("arg"))],
            files,
        )

    if kind == "arg_type_changed":
        return (
            "`%s.%s` changed type from %s to %s. Update the values we pass so they "
            "serialize as %s, and adjust any local arithmetic that assumed the old type."
            % (tool, detail.get("arg"), detail.get("from"), detail.get("to"), detail.get("to")),
            ["values passed as %s.%s are %s" % (tool, detail.get("arg"), detail.get("to"))],
            files,
        )

    if kind == "enum_values_removed":
        removed = ", ".join(detail.get("removed") or [])
        return (
            "`%s.%s` no longer accepts: %s. Stop selecting those values and make sure "
            "any table, ranking or strategy that references them still works with the "
            "remaining set." % (tool, detail.get("arg"), removed),
            ["the removed values (%s) are no longer passed to %s" % (removed, tool)],
            files,
        )

    if kind == "tool_removed":
        return (
            "`%s` has disappeared from the server's tool list but our code still calls "
            "it. Make the code degrade safely: the cycle must keep running and keep the "
            "herd fed even with this capability gone. Do not simply delete the feature if "
            "the farm depends on it -- find the supported equivalent, and if there is "
            "none, fail soft and raise an alert." % tool,
            [
                "no unguarded call to %s remains" % tool,
                "a cycle completes with %s unavailable" % tool,
            ],
            files,
        )

    if kind == "tool_added":
        return (
            "The server exposes a new tool `%s`: %s. Do not wire it into the cycle yet. "
            "Add a bounded, read-only probe under experiments/ that measures whether it "
            "helps lifetime produce, and register it so the research agent can schedule "
            "it. Strategy changes are earned with evidence, never assumed."
            % (tool, change.get("summary", "")),
            [
                "a new probe exists for %s" % tool,
                "the probe is read-only or explicitly budgeted",
                "farm/cycle.py is unchanged by this order",
            ],
            ["experiments/registry.py"],
        )

    if kind == "enum_values_added":
        added = ", ".join(detail.get("added") or [])
        return (
            "`%s.%s` accepts new values: %s. These may be better than what we currently "
            "choose. Add a bounded probe that compares them against the incumbent on "
            "produce per coin; do not switch the live selection without evidence."
            % (tool, detail.get("arg"), added),
            ["a probe compares the new values against the current choice",
             "no live selection changes in this order"],
            ["experiments/registry.py"],
        )

    if kind == "arg_added":
        return (
            "`%s` accepts a new optional argument `%s`. It may enable a better play, but an "
            "untested argument in the live cycle is risk with no measured upside. Add a "
            "bounded probe that measures its effect on produce per coin and register it for "
            "the research agent; leave the cycle's current calls alone."
            % (tool, detail.get("arg")),
            [
                "a probe exercises %s with %s" % (tool, detail.get("arg")),
                "farm/cycle.py is unchanged by this order",
            ],
            ["experiments/registry.py"],
        )

    if kind.startswith("response_"):
        removed = ", ".join(str(x) for x in (detail.get("removed") or [])[:8])
        added = ", ".join(str(x) for x in (detail.get("added") or [])[:8])
        return (
            "The text `%s` returns has changed shape (removed: %s | added: %s), which is "
            "how the parser silently starts producing wrong numbers. Update farm/parse.py "
            "to handle the new format, keeping backward compatibility with the old one so "
            "a rollback stays safe." % (tool, removed or "none", added or "none"),
            [
                "farm/parse.py handles both the old and the new format",
                "parsing the captured sample in state/raw/latest yields no ParseDrift",
            ],
            ["farm/parse.py"],
        )

    return None


# -- main --------------------------------------------------------------------


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("CONTRACT WATCH skipped: previous scan still active")
        return 0

    stored = read_json(STORE)
    runtime = policy.runtime_context()
    ledger.set_context(
        actor="contract_watch",
        policy_id=runtime.get("policy_id"),
        claim_registry_version=runtime.get("claim_registry_version"),
        step="scan_contract",
    )

    baseline_path = str(PROJECT / contract.BASELINE)
    history_path = str(PROJECT / contract.HISTORY)
    queue_path = str(PROJECT / workorders.QUEUE)

    try:
        snapshot = contract.capture(
            Client(), raw_dir=str(PROJECT / contract.RAW_DIR), root=str(PROJECT)
        )
    except (McpError, OSError) as exc:
        # A scan failure is not a farm failure. Say so and exit non-zero for the
        # supervisor, but never touch the baseline on a bad read.
        print("CONTRACT WATCH failed: %s" % str(exc)[:200])
        ledger.record("contract.scan_failed", {"error": str(exc)[:200]})
        return 4

    baseline = contract.load_baseline(baseline_path)

    # First ever scan: adopt what we see. There is nothing to compare against,
    # and treating an unknown contract as "all new" would file 15 spurious orders.
    if not baseline:
        contract.save_baseline(snapshot, baseline_path)
        write_json(STORE, {
            "schema_version": 1,
            "scans": 1,
            "last_scan_ts": utcnow(),
            "baseline_fingerprint": snapshot["fingerprint"],
            "streaks": {},
        })
        contract.record_scan({
            "ts": utcnow(), "fingerprint": snapshot["fingerprint"],
            "event": "baseline_established", "tools": len(snapshot["tools"]),
        }, history_path)
        print("CONTRACT WATCH baseline established: %d tools, %d shapes, fingerprint %s"
              % (len(snapshot["tools"]), len(snapshot["shapes"]), snapshot["fingerprint"][:12]))
        ledger.record("contract.baseline", {"fingerprint": snapshot["fingerprint"],
                                            "tools": len(snapshot["tools"])})
        return 0

    changes = contract.diff(baseline, snapshot)
    prior_streaks = stored.get("streaks") if isinstance(stored.get("streaks"), dict) else {}
    confirmed, streaks = contract.confirm(changes, prior_streaks)

    actionable = [c for c in confirmed if c.get("severity") in ACTIONABLE]
    absorbable = [c for c in changes if c.get("severity") not in ACTIONABLE]

    # File orders for confirmed, actionable drift.
    filed: List[Dict[str, Any]] = []
    for change in actionable:
        built = order_for(change)
        if not built:
            continue
        intent, acceptance, files = built
        order = workorders.submit(
            change, source="contract_watch", intent=intent,
            acceptance=acceptance, files=files, path=queue_path,
        )
        if order:
            filed.append(order)

    # Absorb changes that need no code so they stop appearing in every diff. The
    # baseline advances only for the untouched parts of the contract: anything
    # under an actionable change stays pinned until the fix ships.
    if not actionable and absorbable:
        contract.save_baseline(snapshot, baseline_path)

    scan_row = {
        "ts": utcnow(),
        "fingerprint": snapshot["fingerprint"],
        "baseline_fingerprint": baseline.get("fingerprint"),
        "changes": len(changes),
        "actionable": len(actionable),
        "orders_filed": len(filed),
        "absorbed": len(absorbable) if not actionable else 0,
        "tools": len(snapshot["tools"]),
        "detail": [
            {"severity": c["severity"], "kind": c["kind"], "tool": c["tool"],
             "summary": c["summary"][:160], "seen": c.get("seen_consecutive")}
            for c in changes[:20]
        ],
    }
    contract.record_scan(scan_row, history_path)

    write_json(STORE, {
        "schema_version": 1,
        "scans": int(stored.get("scans") or 0) + 1,
        "last_scan_ts": utcnow(),
        "baseline_fingerprint": baseline.get("fingerprint"),
        "last_fingerprint": snapshot["fingerprint"],
        "streaks": streaks,
        "open_orders": workorders.summary(queue_path),
    })

    # Surface breaking drift on the existing alert path so `run.py --alerts`
    # reports it without needing to know this agent exists.
    breaking = [c for c in actionable if c["severity"] == "breaking"]
    if breaking:
        journal.record_alerts(
            {"ts": utcnow(), "run": None},
            ["contract drift: %s" % c["summary"][:160] for c in breaking],
        )

    if not changes:
        print("CONTRACT WATCH clean: %d tools, fingerprint %s"
              % (len(snapshot["tools"]), snapshot["fingerprint"][:12]))
        ledger.record("contract.clean", {"fingerprint": snapshot["fingerprint"]})
        return 0

    print("CONTRACT WATCH drift: %d change(s), %d actionable, %d order(s) filed"
          % (len(changes), len(actionable), len(filed)))
    for change in changes[:10]:
        mark = "!" if change["severity"] == "breaking" else " "
        print("  %s %-11s %-26s %s" % (mark, change["severity"], change["kind"], change["summary"][:100]))
    if filed:
        print("  queued: %s" % ", ".join(o["id"] for o in filed))
    ledger.record("contract.drift", {
        "changes": len(changes), "actionable": len(actionable), "filed": len(filed),
        "breaking": len(breaking),
    })
    return 3 if actionable else 0


if __name__ == "__main__":
    raise SystemExit(main())
