#!/bin/bash
# Run every execution-trace and dashboard suite: Python call-graph extraction,
# MCP boundary telemetry, the DOM-free 2D trace model, and the page render.
#
# There is no node or headless browser on this machine, so JavaScript runs in
# JavaScriptCore through osascript. Span arithmetic and HTML generation live in
# dashboard/trace_explorer.js specifically so they can be tested without pixels.
#
# Usage: deploy/test_dashboard.sh
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail=0

echo "=== syntax ==="
for f in dashboard/trace_explorer.js dashboard/test_trace_explorer.js dashboard/preview_trace_explorer.js \
         dashboard/mcp_wire.js dashboard/test_mcp_wire.js dashboard/preview_mcp_wire.js \
         dashboard/architecture.js dashboard/test_architecture.js; do
  if osascript -l JavaScript -e "ObjC.import('Foundation'); \
     var s = \$.NSString.stringWithContentsOfFileEncodingError('$f', \$.NSUTF8StringEncoding, null).js; \
     try { new Function(s); 'ok' } catch (e) { throw new Error('$f: ' + e.message) }" >/dev/null 2>&1; then
    echo "  ok   $f parses"
  else
    echo "  FAIL $f does not parse"; fail=1
  fi
done
for f in deploy/test_tool_trace.py deploy/test_dashboard.py deploy/test_architecture.py deploy/preview_trace_explorer.py \
         deploy/preview_mcp_wire.py farm/mcp.py farm/architecture.py monitor.py; do
  if python3 -m py_compile "$f"; then
    echo "  ok   $f parses"
  else
    echo "  FAIL $f does not parse"; fail=1
  fi
done

echo "=== topology (python) ==="
if ! python3 deploy/test_topology.py; then fail=1; fi

echo "=== MCP boundary telemetry (python) ==="
if ! python3 deploy/test_tool_trace.py; then fail=1; fi

echo "=== architecture payload (python) ==="
if ! python3 deploy/test_architecture.py; then fail=1; fi

echo "=== 2D trace model (JavaScriptCore) ==="
engine="$(osascript -l JavaScript dashboard/test_trace_explorer.js 2>&1)" || { echo "  FAIL trace suite errored"; fail=1; }
echo "$engine"
grep -q "FAIL" <<<"$engine" && fail=1

echo "=== MCP switchboard model (JavaScriptCore) ==="
wire="$(osascript -l JavaScript dashboard/test_mcp_wire.js 2>&1)" || { echo "  FAIL switchboard suite errored"; fail=1; }
echo "$wire"
grep -q "FAIL" <<<"$wire" && fail=1

echo "=== architecture explorer model (JavaScriptCore) ==="
architecture="$(osascript -l JavaScript dashboard/test_architecture.js 2>&1)" || { echo "  FAIL architecture suite errored"; fail=1; }
echo "$architecture"
grep -q "FAIL" <<<"$architecture" && fail=1

echo "=== switchboard against live telemetry ==="
if ! python3 deploy/preview_mcp_wire.py --check --out /tmp/mcp_switchboard_check.html; then fail=1; fi

echo "=== page render (JavaScriptCore + DOM stub) ==="
if ! python3 deploy/test_dashboard.py; then fail=1; fi

echo
if [[ "$fail" == "0" ]]; then echo "dashboard tests: PASS"; else echo "dashboard tests: FAILURES"; fi
exit "$fail"
