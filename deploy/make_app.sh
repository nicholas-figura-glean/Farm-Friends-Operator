#!/bin/bash
# Build double-clickable .app bundles for the game and the dashboard.
#
# These are real bundles (Info.plist + an executable), but not native code and not
# Electron. Nothing here is required: the game is one HTML file you can open, and
# the dashboard is `python3 monitor.py`. The bundles exist only so both show up in
# Spotlight/Launchpad and survive a double-click.
#
#   deploy/make_app.sh              # -> apps/Coop Rush.app, apps/Farm Monitor.app
#   deploy/make_app.sh --uninstall  # remove them
#
# "Coop Rush.app"    re-exports the standalone HTML (so it is never stale) and
#                    opens it in the default browser.
# "Farm Monitor.app" starts monitor.py read-only and opens the dashboard, reusing
#                    an already-running instance instead of starting a second one.
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPS="$PROJECT/apps"
PORT=8765

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -rf "$APPS/Coop Rush.app" "$APPS/Farm Monitor.app"
  rmdir "$APPS" 2>/dev/null || true
  echo "removed app bundles"
  exit 0
fi

plist() {  # bundle-dir, display name, identifier suffix
  cat > "$1/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$2</string>
  <key>CFBundleDisplayName</key><string>$2</string>
  <key>CFBundleIdentifier</key><string>com.nickfigura.farmfriends.$3</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST
}

new_bundle() {  # display name, identifier suffix -> echoes the MacOS dir
  local app="$APPS/$1.app"
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
  plist "$app" "$1" "$2"
  echo "$app/Contents/MacOS"
}

mkdir -p "$APPS"

# --- the game -----------------------------------------------------------------
GAME_DIR="$(new_bundle "Coop Rush" cooprush)"
cat > "$GAME_DIR/launch" <<'EOS'
#!/bin/bash
# Opens Coop Rush. Regenerates the standalone file first so the bundle can never
# serve a stale copy of the game.
set -uo pipefail
PROJECT="__PROJECT__"
GAME="$PROJECT/coop-rush.html"
if command -v python3 >/dev/null 2>&1; then
  python3 "$PROJECT/deploy/export_game.py" --out "$GAME" >/dev/null 2>&1 || true
fi
if [[ ! -f "$GAME" ]]; then
  osascript -e 'display alert "Coop Rush" message "coop-rush.html is missing. Run deploy/export_game.py to build it."'
  exit 1
fi
exec open "$GAME"
EOS

# --- the dashboard ------------------------------------------------------------
MON_DIR="$(new_bundle "Farm Monitor" monitor)"
cat > "$MON_DIR/launch" <<'EOS'
#!/bin/bash
# Opens the read-only dashboard. The port check lives in open_monitor.py because
# "does something answer on 8765" is the wrong question: an unrelated local app
# was already answering there, so identity is checked against /api/state instead.
set -uo pipefail
PROJECT="__PROJECT__"
cd "$PROJECT" || exit 1
exec /usr/bin/env python3 deploy/open_monitor.py --port __PORT__
EOS

for script in "$GAME_DIR/launch" "$MON_DIR/launch"; do
  # Paths are baked in rather than derived at runtime: a .app can be launched from
  # anywhere, so $0-relative discovery would be the fragile choice here.
  /usr/bin/sed -i '' -e "s|__PROJECT__|$PROJECT|g" -e "s|__PORT__|$PORT|g" "$script"
  chmod +x "$script"
  /bin/bash -n "$script" || { echo "generated launcher has a syntax error: $script" >&2; exit 1; }
done

# Refresh Finder/Spotlight so the bundles are recognised as apps immediately.
/usr/bin/touch "$APPS/Coop Rush.app" "$APPS/Farm Monitor.app"

cat <<EOF

Built:
  $APPS/Coop Rush.app
  $APPS/Farm Monitor.app

Double-click either in Finder, or:
  open "$APPS/Coop Rush.app"
  open "$APPS/Farm Monitor.app"

Neither is required. The game is also just $PROJECT/coop-rush.html,
and the dashboard is 'python3 monitor.py'. Remove both with --uninstall.
EOF
