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

# Serialize release preparation, guard arming, and pointer activation. A stale
# directory is recoverable only when its recorded process no longer exists.
RELEASE_LOCK="$DEPLOY_PROJECT/state/.release.lock"
mkdir -p "$DEPLOY_PROJECT/state"
recover_stale_release() {
  phase="$(cat "$RELEASE_LOCK/phase" 2>/dev/null || true)"
  prior="$(cat "$RELEASE_LOCK/previous" 2>/dev/null || true)"
  candidate="$(cat "$RELEASE_LOCK/revision" 2>/dev/null || true)"
  live=""
  if [[ -L "$LINK" ]]; then
    live="$(basename "$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$LINK")")"
  fi
  if [[ "$phase" == "preparing" || "$phase" == "guarded" ]]; then
    if [[ "$live" == "$prior" ]]; then
      if [[ -f "$RELEASE_LOCK/canary.before" ]]; then
        cp "$RELEASE_LOCK/canary.before" "$DEPLOY_PROJECT/state/canary.json"
      else
        rm -f "$DEPLOY_PROJECT/state/canary.json"
      fi
      if [[ -f "$RELEASE_LOCK/gates.before" ]]; then
        cp "$RELEASE_LOCK/gates.before" "$DEPLOY_PROJECT/state/release_gate_health.json"
      else
        rm -f "$DEPLOY_PROJECT/state/release_gate_health.json"
      fi
    elif [[ "$live" != "$candidate" ]]; then
      echo "release rejected: stale activation state does not match live pointer" >&2
      return 1
    fi
  fi
  return 0
}
if ! mkdir "$RELEASE_LOCK" 2>/dev/null; then
  lock_pid="$(cat "$RELEASE_LOCK/pid" 2>/dev/null || true)"
  if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "release rejected: another release process is active ($lock_pid)" >&2
    exit 6
  fi
  recover_stale_release || exit 6
  rm -rf "$RELEASE_LOCK"
  mkdir "$RELEASE_LOCK" || { echo "release rejected: could not recover release lock" >&2; exit 6; }
fi
echo "$$" > "$RELEASE_LOCK/pid"
GUARDS_PREPARED=0
POINTER_ACTIVATED=0
release_unlock() {
  if [[ "$(cat "$RELEASE_LOCK/pid" 2>/dev/null || true)" == "$$" ]]; then
    rm -rf "$RELEASE_LOCK"
  fi
}
release_cleanup() {
  if [[ "$GUARDS_PREPARED" -eq 1 && "$POINTER_ACTIVATED" -eq 0 ]] \
     && declare -F restore_activation_guards >/dev/null; then
    restore_activation_guards
  fi
  release_unlock
}
trap release_cleanup EXIT INT TERM

PREVIOUS=""
CHANGE_CLASS="${FARM_CANARY_CHANGE_CLASS:-reliability}"
COMPATIBILITY_RELEASE=0
if [[ -L "$LINK" ]]; then
  PREVIOUS="$(basename "$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$LINK")")"
fi

# One unresolved candidate is the unit of attribution. Check before remote gates,
# staging, or pointer mutation so manual and installer-driven releases cannot bypass
# the author agent's advisory guard. --stage-only remains safe because it never flips.
PREFLIGHT_RUNTIME="$SCRIPT_PROJECT"
if [[ -n "$PREVIOUS" && -f "$RELEASES/$PREVIOUS/farm/canary.py" ]]; then
  PREFLIGHT_RUNTIME="$RELEASES/$PREVIOUS"
fi
if [[ "$STAGE_ONLY" -eq 0 && -n "$PREVIOUS" ]]; then
  if ! FARM_PROJECT_ROOT="$DEPLOY_PROJECT" PYTHONPATH="$PREFLIGHT_RUNTIME" \
       /usr/bin/python3 - "$DEPLOY_PROJECT/state/canary.json" <<'PY'
import sys
from farm import canary

watching = canary.active(sys.argv[1])
if watching:
    raise SystemExit(
        "release rejected: canary %s is still watching" % watching.get("revision")
    )
PY
  then
    exit 4
  fi
fi

# A release is built from the working tree, so the exact source that ships must be
# both committed and durable upstream. This is a hard gate rather than a warning:
# publishing a local-only repair makes the live runtime impossible to reproduce from
# GitHub and lets the next checkout silently erase an autonomous change.
#
# FARM_PROJECT_ROOT is deliberately rebound to SOURCE_PROJECT for this one check.
# An autonomous build runs release.sh from a gated worktree while DEPLOY_PROJECT is
# the canonical checkout; the worktree's HEAD is the commit whose bytes will ship.
if ! (
  cd "$SOURCE_PROJECT"
  FARM_PROJECT_ROOT="$DEPLOY_PROJECT" PYTHONPATH="$SCRIPT_PROJECT" \
    /usr/bin/python3 - "$SOURCE_PROJECT" <<'PY'
import sys
from pathlib import Path
from farm import vcs

vcs.PROJECT = Path(sys.argv[1]).resolve()
if not vcs.available():
    raise SystemExit("release rejected: source is not a Git worktree")
try:
    proof = vcs.require_remote_sync(require_clean=True)
except (vcs.GitError, OSError) as exc:
    raise SystemExit("release rejected: remote synchronization failed: %s" % exc)
print("remote gate: %s pushed to %s/%s" % (
    vcs.short(proof.get("sha")), proof.get("remote"), proof.get("branch"),
))
PY
); then
  exit 3
fi
SOURCE_COMMIT="$(git -C "$SOURCE_PROJECT" rev-parse HEAD)"
BASE_COMMIT=""
if [[ -n "$PREVIOUS" && -f "$RELEASES/$PREVIOUS/SOURCE_COMMIT" ]]; then
  BASE_COMMIT="$(cat "$RELEASES/$PREVIOUS/SOURCE_COMMIT")"
fi
REV="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$RELEASES/$REV"

# Every gate imports code from SOURCE_PROJECT but must resolve operational side
# effects (rollback, release pointer, launchd, shared state ownership) against the
# canonical deployment checkout. Without this, a clean detached release worktree
# made canary.PROJECT point at itself and the rollback-root safety check correctly
# refused publication.
export FARM_PROJECT_ROOT="$DEPLOY_PROJECT"

# Compatibility overlays are not a shortcut around strategy evidence. They are
# admitted only when every packaged byte matches the current immutable release
# except the narrow response normalizer. Any strategy, parser-invariant, policy,
# UI, test-fixture, or control-plane difference rejects this lane.
if [[ "$CHANGE_CLASS" == "compatibility" ]]; then
  if [[ -z "$PREVIOUS" ]]; then
    echo "compatibility release rejected: no immutable base release" >&2
    exit 5
  fi
  if ! PYTHONPATH="$SCRIPT_PROJECT" /usr/bin/python3 - "$LINK" "$SOURCE_PROJECT" <<'PY'
import sys
from pathlib import Path
from farm import compatibility

proof = compatibility.overlay_proof(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
print("compatibility proof: %s" % proof.get("reason"))
if not proof.get("ok"):
    raise SystemExit(1)
PY
  then
    echo "compatibility release rejected: immutable strategy proof failed" >&2
    exit 5
  fi
  COMPATIBILITY_RELEASE=1
fi

cd "$SOURCE_PROJECT"
if [[ -e "$TARGET" ]]; then
  echo "release target already exists: $TARGET" >&2
  exit 2
fi

# Gate: never publish code whose runtime, knowledge, evidence, observability, or
# user-facing artifacts disagree. Every candidate-executing gate runs with no
# network, no ambient credentials, read-only source/state, and a bounded TMPDIR.
gate() {
  /usr/bin/python3 "$SCRIPT_PROJECT/deploy/run_sandboxed.py" \
    --timeout 900 --project-root "$DEPLOY_PROJECT" "$SOURCE_PROJECT" -- "$@"
}

gate /usr/bin/python3 run.py --self-test
if [[ "$COMPATIBILITY_RELEASE" -eq 0 ]]; then
  gate /usr/bin/python3 deploy/test_knowledge.py
fi
gate /usr/bin/python3 deploy/test_governance.py
gate /usr/bin/python3 deploy/test_safety.py
gate /usr/bin/python3 deploy/test_probe_guard.py
gate /usr/bin/python3 deploy/test_sandbox.py
gate /usr/bin/python3 deploy/test_mechanics.py
gate /usr/bin/python3 deploy/test_strategy.py
if [[ "$COMPATIBILITY_RELEASE" -eq 0 ]]; then
  gate /usr/bin/python3 deploy/test_evidence.py
else
  echo "strategy evidence gates retained by adapter-only byte identity proof"
fi
gate /usr/bin/python3 deploy/test_tool_trace.py
gate /usr/bin/python3 deploy/test_topology.py
gate /usr/bin/python3 deploy/test_architecture.py
gate /usr/bin/python3 deploy/test_dashboard.py
gate /usr/bin/python3 deploy/test_recovery_watch.py
gate /usr/bin/python3 deploy/test_notifications.py
gate /usr/bin/python3 deploy/test_degraded_cycle.py
gate /usr/bin/python3 deploy/test_runtime_compat.py
# The self-healing loop is part of the runtime, so its deterministic suites gate
# releases too. The paid live gateway smoke test is opt-in and never runs here.
gate /usr/bin/python3 deploy/test_contract.py
gate /usr/bin/python3 deploy/test_contract_watch.py
gate /usr/bin/python3 deploy/test_vcs.py
gate /usr/bin/python3 deploy/test_author.py
gate /usr/bin/python3 deploy/test_dashboard_agent.py
gate /bin/bash deploy/test_mcp_wire.sh
gate /bin/bash deploy/test_architecture_js.sh
gate /bin/bash deploy/test_game.sh
# Promotion is intentionally not automatic. Refuse to package an unversioned or
# claim-incompatible runtime; run.py --promote-policy is the explicit boundary.
gate /usr/bin/python3 - <<'PY'
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
echo "$SOURCE_COMMIT" > "$TARGET/SOURCE_COMMIT"

# Verify the staged runtime and composed dashboard inside the same authority
# boundary before anything points at them. This catches packaging drift without
# giving packaged-but-unaccepted code ambient credentials or live-state writes.
stage_gate() {
  /usr/bin/python3 "$SCRIPT_PROJECT/deploy/run_sandboxed.py" \
    --timeout 900 --project-root "$DEPLOY_PROJECT" "$TARGET" -- "$@"
}
stage_gate /usr/bin/python3 run.py --self-test >/dev/null
stage_gate /usr/bin/python3 -m farm.staged_verify

if [[ "${FARM_CANARY_CHANGE_CLASS:-reliability}" == "observability" ]]; then
  stage_gate /usr/bin/python3 - "$DEPLOY_PROJECT" "$REV" "$PREVIOUS" <<'PY'
import sys
from farm import canary
project, revision, previous = sys.argv[1:]
errors = canary.observability_release_errors(project, revision, previous)
if errors:
    raise SystemExit("observability release touches gameplay/control judge paths: " + ", ".join(errors))
print("observability gate: changed paths are readout-only")
PY
fi

if [[ "$STAGE_ONLY" -eq 1 ]]; then
  echo "staged $REV at $TARGET; live pointer unchanged"
  exit 0
fi

# Make rollback and certification durable before the candidate can be resolved by
# the live pointer. Backups restore the prior guard state if preparation or rename
# fails; the per-revision certification archive may remain as an unused audit row.
CANARY_STORE="$DEPLOY_PROJECT/state/canary.json"
GATE_STORE="$DEPLOY_PROJECT/state/release_gate_health.json"
CANARY_BACKUP="$RELEASE_LOCK/canary.before"
GATE_BACKUP="$RELEASE_LOCK/gates.before"
if [[ -f "$CANARY_STORE" ]]; then cp "$CANARY_STORE" "$CANARY_BACKUP"; fi
if [[ -f "$GATE_STORE" ]]; then cp "$GATE_STORE" "$GATE_BACKUP"; fi
printf '%s\n' "$PREVIOUS" > "$RELEASE_LOCK/previous"
printf '%s\n' "$REV" > "$RELEASE_LOCK/revision"
printf '%s\n' "preparing" > "$RELEASE_LOCK/phase"
GUARDS_PREPARED=1
restore_activation_guards() {
  if [[ -f "$CANARY_BACKUP" ]]; then cp "$CANARY_BACKUP" "$CANARY_STORE"; else rm -f "$CANARY_STORE"; fi
  if [[ -f "$GATE_BACKUP" ]]; then cp "$GATE_BACKUP" "$GATE_STORE"; else rm -f "$GATE_STORE"; fi
  GUARDS_PREPARED=0
}
TRUSTED_RUNTIME="$SCRIPT_PROJECT"
if [[ -n "$PREVIOUS" && -f "$RELEASES/$PREVIOUS/farm/canary.py" ]]; then
  TRUSTED_RUNTIME="$RELEASES/$PREVIOUS"
fi
if ! FARM_CANARY_REASON="${FARM_CANARY_REASON:-release $REV}" \
     FARM_CANARY_ORDER_ID="${FARM_CANARY_ORDER_ID:-manual-release-$REV}" \
     FARM_CANARY_COMMIT="${FARM_CANARY_COMMIT:-$SOURCE_COMMIT}" \
     FARM_CANARY_BASE_COMMIT="${FARM_CANARY_BASE_COMMIT:-$BASE_COMMIT}" \
     FARM_CANARY_CHANGE_CLASS="${FARM_CANARY_CHANGE_CLASS:-reliability}" \
     FARM_CANARY_HYPOTHESIS_ID="${FARM_CANARY_HYPOTHESIS_ID:-}" \
     FARM_CANARY_POLICY_ID="${FARM_CANARY_POLICY_ID:-}" \
     FARM_CANARY_EXPECTED_IMPROVEMENT="${FARM_CANARY_EXPECTED_IMPROVEMENT:-0}" \
     FARM_CANARY_STRATEGY_INTENT="${FARM_CANARY_STRATEGY_INTENT:-}" \
     FARM_WORKORDER_ID="${FARM_WORKORDER_ID:-}" \
     FARM_WORKORDER_CLAIM_TOKEN_SHA256="${FARM_WORKORDER_CLAIM_TOKEN_SHA256:-}" \
     /usr/bin/python3 "$SCRIPT_PROJECT/deploy/prepare_activation.py" \
     "$TARGET" "$DEPLOY_PROJECT" "$REV" "$PREVIOUS" "$TRUSTED_RUNTIME" \
     "$SCRIPT_PROJECT/farm/gates.py" "$COMPATIBILITY_RELEASE"; then
  restore_activation_guards
  echo "release activation failed closed before pointer exposure" >&2
  exit 4
fi
printf '%s\n' "guarded" > "$RELEASE_LOCK/phase"

# A directory left over from the pre-symlink layout has to go exactly once.
if [[ -d "$LINK" && ! -L "$LINK" ]]; then
  rm -rf "$LINK"
fi

# Atomic flip. Note: `mv -f newlink release` is WRONG here - BSD mv follows an
# existing symlink-to-directory and moves the new link INSIDE the old release,
# leaving the pointer untouched. That silently pinned launchd to stale code for
# several runs. os.replace uses rename(2), which replaces the symlink itself.
if ! /usr/bin/python3 - "$TARGET" "$LINK" <<'PY'
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
then
  restore_activation_guards
  echo "release pointer failed; prior guards restored" >&2
  exit 4
fi
POINTER_ACTIVATED=1
GUARDS_PREPARED=0
printf '%s\n' "activated" > "$RELEASE_LOCK/phase"

# Guard state is already durable here; only now may the live pointer expose the
# candidate. The release lock prevents a second coordinator from racing this one.
if [[ -n "$PREVIOUS" ]]; then
  echo "canary armed $REV -> previous $PREVIOUS before pointer activation"
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
