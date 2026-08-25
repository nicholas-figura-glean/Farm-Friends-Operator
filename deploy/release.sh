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

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASES="$PROJECT/releases"
LINK="$PROJECT/release"
REV="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$RELEASES/$REV"

cd "$PROJECT"
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
/usr/bin/python3 deploy/test_dashboard.py
/usr/bin/python3 deploy/test_recovery_watch.py
# The self-healing loop is now part of the runtime, so its suites gate releases
# too. test_author includes a live gateway round trip that skips (not fails) when
# the Desktop-managed token is dormant.
/usr/bin/python3 deploy/test_contract.py
/usr/bin/python3 deploy/test_contract_watch.py
/usr/bin/python3 deploy/test_author.py
# The switchboard's honesty rules (measured flight time, no invented spans, no
# packet without a recorded call) live in JavaScript, so its JavaScriptCore suite
# is a release gate too rather than something only test_dashboard.sh runs.
wire_suite="$(/usr/bin/osascript -l JavaScript dashboard/test_mcp_wire.js 2>&1)"
echo "$wire_suite" | /usr/bin/tail -n 2
if ! echo "$wire_suite" | /usr/bin/grep -q '^PASS$'; then
  echo "release rejected: MCP switchboard suite failed" >&2
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
ln -sfn "$PROJECT/state" "$TARGET/state"
ln -sfn "$PROJECT/farm-strategy-journal.md" "$TARGET/farm-strategy-journal.md"
echo "$REV" > "$TARGET/RELEASED"

# Verify the staged runtime and composed dashboard in isolation before anything
# points at them. This catches an incomplete UI manifest as well as Python drift.
( cd "$TARGET" && /usr/bin/python3 run.py --self-test >/dev/null )
( cd "$TARGET" && /usr/bin/python3 - <<'PY'
import monitor
expected = {'overview', 'pipeline', 'cost', 'history', 'findings', 'game', 'wire'}
missing = [tab for tab in sorted(expected) if ('data-tab="%s"' % tab) not in monitor.HTML]
if missing:
    raise SystemExit("staged dashboard missing tabs: " + ", ".join(missing))
print("dashboard gate: 7 tabs packaged")
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

# Clean up any stray link that the old buggy swap left inside a release.
find "$RELEASES" -maxdepth 2 -name '.release.newlink.*' -delete 2>/dev/null || true
find "$RELEASES" -maxdepth 2 -name 'release.new.*' -delete 2>/dev/null || true

# Prune old releases, but only ones old enough that no run can still be inside
# them (the hard timeout is 240s, so 30 minutes is generous). Never prune the
# release the pointer currently resolves to.
LIVE="$(/usr/bin/python3 -c 'import os,sys; print(os.path.basename(os.path.realpath(sys.argv[1])))' "$LINK")"
find "$RELEASES" -maxdepth 1 -mindepth 1 -type d -mmin +30 \
  ! -name "$REV" ! -name "$LIVE" -exec rm -rf {} + 2>/dev/null || true

echo "released $REV -> $LINK ($(ls -1 "$RELEASES" | wc -l | tr -d ' ') kept)"
