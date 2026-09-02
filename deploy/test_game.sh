#!/bin/bash
# Run the Coop Rush test suites headlessly in JavaScriptCore.
#
# The dashboard has no build step and this machine has no node, so the engine is
# exercised through osascript -l JavaScript. game/coop_rush.js is kept free of DOM
# and network access precisely so it can be tested this way.
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail=0

echo "=== syntax ==="
for f in game/coop_rush.js game/coop_rush_ui.js game/test_mechanics.js game/test_ui.js; do
  if osascript -l JavaScript -e "ObjC.import('Foundation'); \
     var s = \$.NSString.stringWithContentsOfFileEncodingError('$f', \$.NSUTF8StringEncoding, null).js; \
     try { new Function(s); 'ok' } catch (e) { throw new Error('$f: ' + e.message) }" >/dev/null 2>&1; then
    echo "  ok   $f parses"
  else
    echo "  FAIL $f does not parse"; fail=1
  fi
done

echo "=== mechanics ==="
mech="$(osascript -l JavaScript game/test_mechanics.js 2>&1)" || { echo "  FAIL mechanics suite errored"; fail=1; }
echo "$mech"
printf '%s\n' "$mech" | grep -q "FAIL" && fail=1

echo "=== ui ==="
uiout="$(osascript -l JavaScript game/test_ui.js 2>&1)" || { echo "  FAIL ui suite errored"; fail=1; }
echo "$uiout"
printf '%s\n' "$uiout" | grep -q "FAIL" && fail=1

if [[ "${1:-}" == "--balance" ]]; then
  echo "=== balance (6h simulated) ==="
  osascript -l JavaScript game/test_balance.js || { echo "  FAIL balance sim errored"; fail=1; }
fi

echo
if [[ "$fail" == "0" ]]; then echo "game tests: PASS"; else echo "game tests: FAILURES"; fi
exit "$fail"
