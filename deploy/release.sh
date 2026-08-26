#!/bin/bash
# Publish the working tree as an immutable, versioned release and flip the
# `release` symlink to it atomically.
#
# Two incidents drove this design:
#  1. launchd fired mid-edit and ran a half-applied change (adopt_chickens gained
#     a required argument before its caller did), crashing that cycle.
#  2. A naive "mv old aside, mv new in" swap deleted the tree out from under a
#     run that was already executing inside it: its cwd vanished and every
#     relative path (state/, history) failed.
#
# So: releases are never mutated, never deleted while possibly in use, and the
# pointer moves with a single rename(2).
#
# Source and deployment roots may differ for an autonomous worktree build:
#   FARM_SOURCE_ROOT=/tmp/gated-worktree FARM_DEPLOY_ROOT=/canonical/project deploy/release.sh
# Usage: deploy/release.sh [--stage-only]
set -euo pipefail

STAGE_ONLY=0
if [[ "${1:-}" == "--stage-only" ]]; then
  STAGE_ONLY=1
  shift
fi
if [[ "$#" -ne 0 ]]; then
  echo "usage: $0 [--stage-only]" >&2
  exit 2
fi

SCRIPT_PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_PROJECT="${FARM_SOURCE_ROOT:-$SCRIPT_PROJECT}"
DEPLOY_PROJECT="${FARM_DEPLOY_ROOT:-$SCRIPT_PROJECT}"
SOURCE_PROJECT="$(cd "$SOURCE_PROJECT" && pwd)"
DEPLOY_PROJECT="$(cd "$DEPLOY_PROJECT" && pwd)"
RELEASES="$DEPLOY_PROJECT/releases"
LINK="$DEPLOY_PROJECT/release"
PREVIOUS=""
if [[ -L "$LINK" ]]; then
  PREVIOUS="$(basename "$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$LINK")")"
fi

# A release is built from the working tree, so the working tree is what ships. If it
# disagrees with main, then main is not a record of what is running, and the next
# release built after any checkout will silently ship something different.
#
# This guard exists because that divergence actually happened and hid a real bug: a
# test suite rewrote main via update-ref, which does not touch the working tree, so
# the deployment kept running correct code while main had quietly lost it. Nothing
# noticed, because everything that mattered still worked.
#
# A warning rather than a hard failure: an operator mid-edit should still be able to
# cut a release, and refusing would make this script fail in exactly the situation
# where someone is trying to fix something urgently.
if command -v git >/dev/null 2>&1 && git -C "$SOURCE_PROJECT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  DIVERGED="$(git -C "$SOURCE_PROJECT" diff --name-only main -- . 2>/dev/null | head -20)"
  if [[ -n "$DIVERGED" ]]; then
    echo "WARNING: the working tree differs from main; this release will ship the" >&2
    echo "         working tree, and main will not describe what is running:" >&2
    while IFS= read -r line; do echo "           $line" >&2; done <<< "$DIVERGED"
    echo "         commit or check out before releasing to keep main authoritative." >&2
  fi
fi
REV="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$RELEASES/$REV"

cd "$SOURCE_PROJECT"
if [[ -e "$TARGET" ]]; then
  echo "release target already exists: $TARGET" >&2
  exit 2
fi

# Gate: never publish code whose runtime, knowledge, evidence, observability, or
# user-facing artifacts disagree. These run before staging and make no live MCP
# calls; test_knowledge redirects every mutable epistemic store to a temp dir.
/usr/bin/python3 run.py --self-test
/usr/bin/python3 deploy/test_knowledge.py
/usr/bin/python3 deploy/test_evidence.py
/usr/bin/python3 deploy/test_tool_trace.py
/usr/bin/python3 deploy/test_topology.py
/usr/bin/python3 deploy/test_architecture.py
/usr/bin/python3 deploy/test_dashboard.py
/usr/bin/python3 deploy/test_recovery_watch.py
# The self-healing loop is part of the runtime, so its deterministic suites gate
# releases too. The paid live gateway smoke test is opt-in and never runs here.
/usr/bin/python3 deploy/test_contract.py
/usr/bin/python3 deploy/test_contract_watch.py
/usr/bin/python3 deploy/test_vcs.py
/usr/bin/python3 deploy/test_author.py
/usr/bin/python3 deploy/test_dashboard_agent.py
# The switchboard's honesty rules (measured flight time, no invented spans, no
# packet without a recorded call) live in JavaScript, so its JavaScriptCore suite
# is a release gate too rather than something only test_dashboard.sh runs.
wire_suite="$(/usr/bin/osascript -l JavaScript dashboard/test_mcp_wire.js 2>&1)"
echo "$wire_suite" | /usr/bin/tail -n 2
if ! echo "$wire_suite" | /usr/bin/grep -q '^PASS$'; then
  echo "release rejected: MCP switchboard suite failed" >&2
  exit 3
fi
arch_suite="$(/usr/bin/osascript -l JavaScript dashboard/test_architecture.js 2>&1)"
echo "$arch_suite" | /usr/bin/tail -n 2
if ! echo "$arch_suite" | /usr/bin/grep -q '^PASS$'; then
  echo "release rejected: architecture tab suite failed" >&2
  exit 3
fi
/bin/bash deploy/test_game.sh
# Promotion is intentionally not automatic. Refuse to package an unversioned or
# claim-incompatible runtime; run.py --promote-policy is the explicit boundary.
/usr/bin/python3 - <<'PY'
from farm import claims, policy
context = policy.runtime_context(claims.load())
if not context.get("compatible"):
    raise SystemExit("release rejected: " + "; ".join(context.get("errors") or []))
print("policy gate: %s" % context.get("policy_id"))
PY

mkdir -p "$TARGET"
cp run.py monitor.py "$TARGET/"
cp -R farm "$TARGET/farm"
cp -R fixtures "$TARGET/fixtures"
# Package the exact read-only UI bundle that passed the release gates. The
# monitor normally runs from the project root so it can compare working-tree and
# live runtime fingerprints, but the immutable release must still identify the
# dashboard, trace, switchboard, and game assets that were published with it.
cp -R dashboard "$TARGET/dashboard"
cp -R game "$TARGET/game"
# The expansion agent runs experiments/expand.py from the release, so it has to
# be published like any other executed code. Everything else in experiments/ is
# one-off probes that are never on the cycle path.
mkdir -p "$TARGET/experiments"
cp experiments/*.py "$TARGET/experiments/" 2>/dev/null || true
find "$TARGET" -name '__pycache__' -type d -prune -exec rm -rf {} +

# State and the journal live with the project so they survive every release.
ln -sfn "$DEPLOY_PROJECT/state" "$TARGET/state"
ln -sfn "$DEPLOY_PROJECT/farm-strategy-journal.md" "$TARGET/farm-strategy-journal.md"
echo "$REV" > "$TARGET/RELEASED"

# Verify the staged runtime and composed dashboard in isolation before anything
# points at them. This catches an incomplete UI manifest as well as Python drift.
( cd "$TARGET" && /usr/bin/python3 run.py --self-test >/dev/null )
( cd "$TARGET" && /usr/bin/python3 - <<'PY'
import re

import monitor

# Derived from the document rather than hardcoded. The previous version of this gate
# asserted a hardcoded set of seven tabs and printed "7 tabs packaged", so adding an
# eighth tab left the gate reporting success for a set that no longer described the
# page. A gate that cannot notice new work is not much of a gate.
buttons = set(re.findall(r'role="tab" data-tab="([a-z_]+)"', monitor.HTML))
panels = set(re.findall(r'class="tab" id="tab-([a-z_]+)"', monitor.HTML))
required = {'overview', 'pipeline', 'cost', 'history', 'findings', 'game', 'wire',
            'architecture'}

missing = sorted(required - buttons)
if missing:
    raise SystemExit("staged dashboard missing tabs: " + ", ".join(missing))

# A button with no panel, or a panel with no button, is a tab that renders as a blank
# page or as dead markup. Both have happened while wiring a tab up by hand.
orphan_buttons = sorted(buttons - panels)
orphan_panels = sorted(panels - buttons)
if orphan_buttons:
    raise SystemExit("tab buttons with no panel: " + ", ".join(orphan_buttons))
if orphan_panels:
    raise SystemExit("tab panels with no button: " + ", ".join(orphan_panels))

# Every asset placeholder must have been substituted. An unsubstituted token ships a
# page whose scripts are the literal string "__ARCH_JS__", which fails silently.
leftover = sorted(set(re.findall(r'__[A-Z_]+__', monitor.HTML)))
if leftover:
    raise SystemExit("unsubstituted asset placeholders: " + ", ".join(leftover))
if "missing dashboard asset" in monitor.HTML:
    raise SystemExit("a dashboard asset failed to load into the staged page")

print("dashboard gate: %d tabs packaged, all wired" % len(buttons))
PY
)

if [[ "$STAGE_ONLY" -eq 1 ]]; then
  echo "staged $REV at $TARGET; live pointer unchanged"
  exit 0
fi

# A directory left over from the pre-symlink layout has to go exactly once.
if [[ -d "$LINK" && ! -L "$LINK" ]]; then
  rm -rf "$LINK"
fi

# Atomic flip. Note: `mv -f newlink release` is WRONG here - BSD mv follows an
# existing symlink-to-directory and moves the new link INSIDE the old release,
# leaving the pointer untouched. That silently pinned launchd to stale code for
# several runs. os.replace uses rename(2), which replaces the symlink itself.
/usr/bin/python3 - "$TARGET" "$LINK" <<'PY'
import os, sys
target, link = sys.argv[1], sys.argv[2]
tmp = link + ".new.%d" % os.getpid()
if os.path.islink(tmp) or os.path.exists(tmp):
    os.remove(tmp)
os.symlink(target, tmp)
os.replace(tmp, link)
resolved = os.path.realpath(link)
assert resolved == os.path.realpath(target), "flip failed: %s" % resolved
print("pointer -> %s" % os.path.basename(resolved))
PY

# Every activated release is provisional. The release builder owns this boundary so
# manual, autonomous, and installer-driven flips cannot accidentally bypass canary
# coverage. The author supplies the originating work-order metadata through bounded
# environment variables; ordinary builds receive an explicit release identity.
if [[ -n "$PREVIOUS" ]]; then
  CANARY_STORE="$DEPLOY_PROJECT/state/canary.json"
  CANARY_BACKUP="$DEPLOY_PROJECT/state/.canary.pre-release.$$"
  HAD_CANARY=0
  if [[ -f "$CANARY_STORE" ]]; then
    cp "$CANARY_STORE" "$CANARY_BACKUP"
    HAD_CANARY=1
  fi
  if ! FARM_CANARY_REASON="${FARM_CANARY_REASON:-release $REV}" \
       FARM_CANARY_ORDER_ID="${FARM_CANARY_ORDER_ID:-manual-release-$REV}" \
       FARM_CANARY_COMMIT="${FARM_CANARY_COMMIT:-}" \
       /usr/bin/python3 - "$TARGET" "$DEPLOY_PROJECT" "$REV" "$PREVIOUS" <<'PY'
import os
import sys
from pathlib import Path

target, project, revision, previous = sys.argv[1:]
sys.path.insert(0, target)
from farm import canary

root = Path(project)
armed = canary.arm(
    revision,
    previous,
    reason=os.environ.get("FARM_CANARY_REASON", "release " + revision)[:500],
    order_id=os.environ.get("FARM_CANARY_ORDER_ID", "manual-release-" + revision)[:160],
    commit=os.environ.get("FARM_CANARY_COMMIT", "")[:80],
    store=str(root / canary.STORE),
    history=str(root / canary.HISTORY),
    run_history=str(root / canary.RUN_HISTORY),
)
if armed.get("status") != canary.WATCHING or armed.get("revision") != revision:
    raise SystemExit("release activated but canary did not arm")
print("canary armed %s -> previous %s" % (revision, previous))
PY
  then
    echo "release activation failed closed: canary did not arm; restoring $PREVIOUS" >&2
    /usr/bin/python3 - "$RELEASES/$PREVIOUS" "$LINK" <<'PY'
import os, sys
target, link = sys.argv[1], sys.argv[2]
tmp = link + ".rollback.%d" % os.getpid()
os.symlink(target, tmp)
os.replace(tmp, link)
PY
    if [[ "$HAD_CANARY" -eq 1 ]]; then
      mv "$CANARY_BACKUP" "$CANARY_STORE"
    else
      rm -f "$CANARY_STORE" "$CANARY_BACKUP"
    fi
    exit 4
  fi
  rm -f "$CANARY_BACKUP"
else
  echo "WARNING: first release has no prior revision to canary against" >&2
fi

# monitor.py composes HTML and registers routes at import time. Moving the pointer does
# not update an already-running process: before supervision existed, one hand-started
# server survived eight releases and served a seven-tab page for 8.5 hours. If the
# monitor LaunchAgent is installed, restart it after the atomic flip so it imports this
# release. Failure is loud but does not roll back a release whose runtime has already
# been published; the KeepAlive job and dashboard verifier will continue recovery.
MONITOR_LABEL="com.nickfigura.farmfriends.monitor"
MONITOR_DOMAIN="gui/$(id -u)"
if launchctl print "$MONITOR_DOMAIN/$MONITOR_LABEL" >/dev/null 2>&1; then
  if launchctl kickstart -k "$MONITOR_DOMAIN/$MONITOR_LABEL"; then
    echo "restarted $MONITOR_LABEL on $REV"
  else
    echo "WARNING: released code is live but $MONITOR_LABEL did not restart" >&2
  fi
fi

# Clean up any stray link that the old buggy swap left inside a release.
find "$RELEASES" -maxdepth 2 -name '.release.newlink.*' -delete 2>/dev/null || true
find "$RELEASES" -maxdepth 2 -name 'release.new.*' -delete 2>/dev/null || true

# Prune old releases, but only ones old enough that no run can still be inside
# them (the hard timeout is 240s, so 30 minutes is generous). Never prune the
# live release or the previous revision held by the active canary: deleting the
# latter turns an armed rollback into a promise that cannot be kept.
LIVE="$(/usr/bin/python3 -c 'import os,sys; print(os.path.basename(os.path.realpath(sys.argv[1])))' "$LINK")"
find "$RELEASES" -maxdepth 1 -mindepth 1 -type d -mmin +30 \
  ! -name "$REV" ! -name "$LIVE" ! -name "$PREVIOUS" -exec rm -rf {} + 2>/dev/null || true

echo "released $REV -> $LINK ($(ls -1 "$RELEASES" | wc -l | tr -d ' ') kept)"
