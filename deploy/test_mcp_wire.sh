#!/bin/bash
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$(/usr/bin/osascript -l JavaScript dashboard/test_mcp_wire.js 2>&1)"
echo "$out" | /usr/bin/tail -n 2
if ! echo "$out" | /usr/bin/grep -q '^PASS$'; then
  echo "MCP switchboard suite failed" >&2
  exit 1
fi
