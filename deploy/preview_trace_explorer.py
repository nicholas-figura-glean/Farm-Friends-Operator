#!/usr/bin/env python3
"""Render the current execution trace headlessly to a standalone HTML preview.

This drives the same trace_explorer.js model and HTML renderer embedded in the
monitor; it does not approximate the DOM and does not open a browser.

Usage:
  python3 deploy/preview_trace_explorer.py --check
  python3 deploy/preview_trace_explorer.py --view matrix --out /tmp/matrix.html
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


def capture(now_ms: int):
    with tempfile.TemporaryDirectory() as tmp:
        input_path = os.path.join(tmp, "input.json")
        result_path = os.path.join(tmp, "result.json")
        payload = {
            "topology": monitor.topology.cached_graph(),
            "pipeline": monitor._pipeline(),
            "trace": monitor._trace(),
            "now": now_ms,
            "resultPath": result_path,
        }
        with open(input_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "dashboard/preview_trace_explorer.js", input_path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode or not os.path.exists(result_path):
            raise SystemExit("headless trace render failed:\n%s\n%s" % (proc.stdout, proc.stderr))
        with open(result_path, encoding="utf-8") as handle:
            result = json.load(handle)
        return payload, result


def standalone(fragment: str, css: str, title: str) -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title><style>
:root{color-scheme:dark;--bg:#101714;--panel:#18231e;--panel2:#1e2d26;--line:#30463a;--text:#edf7ef;--muted:#9cb4a4;--green:#72e09a;--yellow:#f5cc75;--red:#ff8a83}
*{box-sizing:border-box}body{margin:0;padding:24px;background:#101714;color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.preview{max-width:1400px;margin:auto;border:1px solid var(--line);border-radius:14px;overflow:hidden;background:#18231e}.trace-explorer{margin:0;border:0;border-radius:0}
%s
</style></head><body><div class="preview"><div class="trace-explorer">%s</div></div></body></html>""" % (title, css, fragment)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--view", choices=("trace", "matrix"), default="trace")
    parser.add_argument("--out", default="/tmp/trace_explorer_preview.html")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    payload, result = capture(int(time.time() * 1000))
    fragment = result["traceHtml"] if args.view == "trace" else result["matrixHtml"]
    with open(os.path.join(ROOT, "dashboard", "trace_explorer.css"), encoding="utf-8") as handle:
        css = handle.read()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(standalone(fragment, css, "Farm Friends execution %s" % args.view))

    summary, metrics = result["summary"], result["metrics"]
    print("run %s · %s · active %s" % (summary["run"], summary["status"], summary["active"] or "none"))
    print("%d steps · %d calls in %d lanes · %d tools · coverage %s" % (
        summary["steps"], summary["calls"], summary["callGroups"], summary["tools"], summary["coverage"]))
    print("rows: %d step + %d tool · spans: %d · matrix observed cells: %d" % (
        metrics["stepRows"], metrics["callRows"], metrics["callSpans"], metrics["observedCells"]))
    print("wrote %s" % args.out)

    if args.check:
        failures = []
        expected_steps = len(payload["pipeline"].get("steps") or payload["topology"].get("steps") or [])
        expected_calls = len(payload["trace"].get("calls") or payload["trace"].get("activity") or [])
        expected_tools = sum(1 for node in payload["topology"].get("nodes") or [] if node.get("kind") == "tool")
        if metrics["stepRows"] != expected_steps:
            failures.append("step rows %d != %d" % (metrics["stepRows"], expected_steps))
        if metrics["callSpans"] != expected_calls:
            failures.append("call spans %d != %d" % (metrics["callSpans"], expected_calls))
        if expected_calls and not (0 < metrics["callRows"] <= expected_calls):
            failures.append("grouped call rows %d invalid for %d calls" % (metrics["callRows"], expected_calls))
        if metrics["toolColumns"] != expected_tools:
            failures.append("tool columns %d != %d" % (metrics["toolColumns"], expected_tools))
        if metrics["hasCanvas"]:
            failures.append("2D trace unexpectedly depends on canvas")
        if "static reachability" not in result["traceHtml"].lower():
            failures.append("static/runtime distinction missing")
        if failures:
            print("HEADLESS TRACE CHECK FAILED: " + "; ".join(failures))
            return 1
        print("headless trace check: PASS (6 bounds)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
