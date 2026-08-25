#!/usr/bin/env python3
"""Open the farm dashboard, starting it only if it is not already up.

Why this is not two lines of bash: port 8765 was already taken on this machine by
an unrelated local app that happily answers HTTP 200 on `/`. A naive "curl the URL
and assume it's ours" check therefore opened someone else's page and never started
the monitor. So identity is checked against `/api/state`, not the status code.

    python3 deploy/open_monitor.py            # reuse or start, then open a browser
    python3 deploy/open_monitor.py --no-open  # same, but just print the URL
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

APP_ID = "farmfriends-monitor"
DEFAULT_PORT = 8765
SEARCH = 10


def _is_ours(port: int, timeout: float = 1.5) -> bool:
    """True only if the farm monitor itself answers on this port."""
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/api/state" % port, timeout=timeout
        ) as fh:
            payload = json.loads(fh.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("app") == APP_ID


def find_running(start_port: int = DEFAULT_PORT, search: int = SEARCH) -> Optional[int]:
    for port in range(start_port, start_port + search + 1):
        if _is_ours(port):
            return port
    return None


def start(start_port: int = DEFAULT_PORT, wait: float = 12.0) -> Optional[int]:
    """Spawn a detached monitor and wait for it to answer as ours."""
    log = PROJECT / "state" / "monitor.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as fh:
        subprocess.Popen(
            [sys.executable, "monitor.py", "--no-open", "--port", str(start_port)],
            cwd=str(PROJECT),
            stdout=fh,
            stderr=fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,   # survives this process exiting
        )
    deadline = time.time() + wait
    while time.time() < deadline:
        port = find_running(start_port)
        if port:
            return port
        time.sleep(0.4)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Open the farm dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    port = find_running(args.port)
    reused = port is not None
    if port is None:
        port = start(args.port)
    if port is None:
        print("could not start the monitor; see state/monitor.log", file=sys.stderr)
        return 1

    url = "http://127.0.0.1:%d/" % port
    print("%s farm monitor at %s" % ("reusing" if reused else "started", url))
    if not args.no_open:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
