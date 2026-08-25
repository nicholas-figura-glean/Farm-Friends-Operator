#!/bin/bash
# Install (or reinstall) the Farm Friends agents as launchd user agents:
#   com.nickfigura.farmfriends            the 180s cycle
#   com.nickfigura.farmfriends.supervisor the 60s self-healing pass
#   com.nickfigura.farmfriends.expand     the 300s bounded expansion sprint
#   com.nickfigura.farmfriends.recovery   the 1800s one-shot outage recovery watch
#   com.nickfigura.farmfriends.contract   the 900s endpoint contract scanner
#   com.nickfigura.farmfriends.author     the 600s work-order author (edits code)
#   com.nickfigura.farmfriends.research   the 3600s strategy research agent
# The cycle/supervisor repair each other, and the supervisor also keeps the
# contract and author agents alive; the recovery watch is independently inert
# after it proves a positive lifetime-score delta.
# Usage: deploy/install.sh [--uninstall]
set -euo pipefail

LABEL="com.nickfigura.farmfriends"
SUPERVISOR="$LABEL.supervisor"
EXPAND="$LABEL.expand"
RECOVERY="$LABEL.recovery"
CONTRACT="$LABEL.contract"
AUTHOR="$LABEL.author"
RESEARCH="$LABEL.research"
DASHBOARD="$LABEL.dashboard"
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
SUPERVISOR_PLIST="$AGENT_DIR/$SUPERVISOR.plist"
EXPAND_PLIST="$AGENT_DIR/$EXPAND.plist"
RECOVERY_PLIST="$AGENT_DIR/$RECOVERY.plist"
CONTRACT_PLIST="$AGENT_DIR/$CONTRACT.plist"
AUTHOR_PLIST="$AGENT_DIR/$AUTHOR.plist"
RESEARCH_PLIST="$AGENT_DIR/$RESEARCH.plist"
DASHBOARD_PLIST="$AGENT_DIR/$DASHBOARD.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  # Bootout the supervisor FIRST: otherwise it would notice the cycle agent
  # disappearing and dutifully reinstall it -- and it now revives the contract
  # and author agents too.
  launchctl bootout "$DOMAIN/$SUPERVISOR" 2>/dev/null || true
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootout "$DOMAIN/$EXPAND" 2>/dev/null || true
  launchctl bootout "$DOMAIN/$RECOVERY" 2>/dev/null || true
  launchctl bootout "$DOMAIN/$CONTRACT" 2>/dev/null || true
  launchctl bootout "$DOMAIN/$AUTHOR" 2>/dev/null || true
  launchctl bootout "$DOMAIN/$RESEARCH" 2>/dev/null || true
  rm -f "$PLIST" "$SUPERVISOR_PLIST" "$EXPAND_PLIST" "$RECOVERY_PLIST" \
        "$CONTRACT_PLIST" "$AUTHOR_PLIST" "$RESEARCH_PLIST"
  echo "uninstalled $LABEL, $SUPERVISOR, $EXPAND, $RECOVERY, $CONTRACT, $AUTHOR and $RESEARCH"
  exit 0
fi

# Fail fast if the operator itself is broken, then publish a release for
# launchd to execute (never the working tree).
/usr/bin/python3 "$PROJECT/run.py" --self-test
"$PROJECT/deploy/release.sh"

mkdir -p "$AGENT_DIR" "$PROJECT/state"
for label in "$LABEL" "$SUPERVISOR" "$EXPAND" "$RECOVERY" "$CONTRACT" "$AUTHOR" "$RESEARCH" "$DASHBOARD"; do
  sed "s|__PROJECT__|$PROJECT|g" "$PROJECT/deploy/$label.plist" > "$AGENT_DIR/$label.plist"
  plutil -lint "$AGENT_DIR/$label.plist"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$AGENT_DIR/$label.plist"
  launchctl enable "$DOMAIN/$label"
done

echo "installed $LABEL -> every 180s from $PROJECT/release"
echo "installed $SUPERVISOR -> every 60s (self-healing + canary adjudication)"
echo "installed $EXPAND -> every 300s (bounded adoption sprint)"
echo "installed $RECOVERY -> every 1800s (one-shot production recovery watch)"
echo "installed $CONTRACT -> every 900s (endpoint contract scan)"
echo "installed $AUTHOR -> every 600s (work-order author, publishes under canary)"
echo "installed $RESEARCH -> every 3600s (strategy research, proposes probes)"
launchctl print "$DOMAIN/$LABEL" | sed -n '1,6p;/state =/p;/runs =/p'
