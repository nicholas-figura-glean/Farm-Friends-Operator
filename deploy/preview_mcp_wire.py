#!/usr/bin/env python3
"""Render the live MCP switchboard headlessly to a standalone HTML preview.

Drives the same mcp_wire.js model and HTML generator embedded in the monitor
against the current run's real boundary telemetry. No browser, no approximation
of the DOM: the animation is pure CSS, so the exported file animates identically
to the tab.

Usage:
  python3 deploy/preview_mcp_wire.py --check
  python3 deploy/preview_mcp_wire.py --speed 12 --out /tmp/switchboard.html
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import monitor  # noqa: E402


def capture(now_ms: int, speed: int, focus):
    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, "input.json")
        result_path = os.path.join(tmp, "result.json")
        payload = {
            "topology": monitor.topology.cached_graph(),
            "pipeline": monitor._pipeline(),
            "trace": monitor._trace(),
            "speed": speed,
            "focus": focus,
            "now": now_ms,
            "resultPath": result_path,
        }
        with open(input_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "dashboard/preview_mcp_wire.js", input_path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode or not os.path.exists(result_path):
            raise SystemExit("headless switchboard render failed:\n%s\n%s" % (proc.stdout, proc.stderr))
        with open(result_path, encoding="utf-8") as handle:
            return payload, json.load(handle)


def standalone(fragment: str, css: str) -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Farm Friends MCP switchboard</title><style>
:root{color-scheme:dark;--bg:#101714;--panel:#18231e;--panel2:#1e2d26;--line:#30463a;--text:#edf7ef;--muted:#9cb4a4;--green:#72e09a;--yellow:#f5cc75;--red:#ff8a83;--blue:#8fc8ff}
*{box-sizing:border-box}body{margin:0;padding:24px;background:#101714;color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.preview{max-width:1400px;margin:auto;border:1px solid var(--line);border-radius:14px;overflow:hidden;background:#18231e}
.mcp-wire{margin:0;border:0;border-radius:0}
%s
</style></head><body><div class="preview"><div class="mcp-wire">%s</div></div></body></html>""" % (css, fragment)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speed", type=int, choices=(1, 4, 12), default=4)
    parser.add_argument("--focus", default=None, help='e.g. tool:adopt_animal or step:adopt')
    parser.add_argument("--out", default="/tmp/mcp_switchboard_preview.html")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    payload, result = capture(int(time.time() * 1000), args.speed, args.focus)
    with open(os.path.join(ROOT, "dashboard", "mcp_wire.css"), encoding="utf-8") as handle:
        css = handle.read()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(standalone(result["html"], css))

    summary, metrics = result["summary"], result["metrics"]
    print("run %s · %s · coverage %s" % (summary["run"], summary["status"], summary["coverage"]))
    print("%d calls (%d drawn%s) across %d lanes · %d silent · %d errors · %d in flight" % (
        summary["calls"], summary["drawn"], ", thinned" if summary["thinned"] else "",
        summary["lanes"], summary["silent"], summary["errors"], summary["inFlight"]))
    print("span %.1fs · boundary %.1fs (x%.1f overlap) · peak %d concurrent · %.0f calls/min" % (
        summary["span"], summary["boundary"], summary["parallelism"], summary["peak"], summary["perMinute"]))
    print("replay loop %.1fs at effective x%.1f · busiest %s" % (
        summary["loop"], summary["effectiveSpeed"], summary["busiest"]))
    print("wrote %s" % args.out)

    if args.check:
        failures = []
        calls = payload["trace"].get("calls") or payload["trace"].get("activity") or []
        tools = sum(1 for node in payload["topology"].get("nodes") or [] if node.get("kind") == "tool")
        if summary["calls"] != len(calls):
            failures.append("call count %d != %d recorded" % (summary["calls"], len(calls)))
        if metrics["packets"] != summary["drawn"]:
            failures.append("packets %d != drawn %d" % (metrics["packets"], summary["drawn"]))
        if summary["drawn"] > summary["calls"]:
            failures.append("more packets drawn than calls observed")
        if tools and metrics["laneRows"] != tools and not payload.get("focus"):
            failures.append("lane rows %d != %d reachable tools" % (metrics["laneRows"], tools))
        if metrics["hasCanvas"]:
            failures.append("switchboard unexpectedly depends on canvas")
        if calls and "measured duration" not in result["html"]:
            failures.append("legend does not disclose that flight time is measured")
        if not calls and "mw-packet" in result["html"]:
            failures.append("packets drawn with no observed calls")
        if failures:
            print("HEADLESS SWITCHBOARD CHECK FAILED: " + "; ".join(failures))
            return 1
        print("headless switchboard check: PASS (7 bounds)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
