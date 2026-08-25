#!/usr/bin/env python3
"""Export Coop Rush as one standalone HTML file.

The game lives in monitor.py because that is where it is useful, but it needs
nothing from the dashboard: no server, no state files, no network. This slices
the game's markup, the shared view helpers and the game script straight out of
monitor.HTML between sentinels, so there is exactly ONE copy of the game and a
standalone build can never drift from the tab.

    python3 deploy/export_game.py                 # -> coop-rush.html
    python3 deploy/export_game.py --out /tmp/x.html

Missing sentinels are a hard error rather than a silently half-built page.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import monitor  # noqa: E402

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Coop Rush</title>
<style>
__CSS__
body { padding-bottom:20px; }
main { max-width:1400px; }
.tab { display:block !important; }
</style>
</head>
<body><main>
<header>
  <div>
    <h1>&#127844; Coop Rush</h1>
    <div class="subtitle">An idle farm that actually scales &middot; click, upgrade, rebuild</div>
  </div>
  <div class="refresh">Standalone build &middot; no server, no network<br>Progress saves in this browser</div>
</header>
__MARKUP__
</main>
<script>
__GAME__
</script>
</body></html>
"""


def _slice(text: str, start: str, end: str, what: str) -> str:
    found = re.search(re.escape(start) + r"(.*?)" + re.escape(end), text, re.S)
    if not found:
        raise SystemExit("export failed: %s sentinels (%s / %s) not found in monitor.HTML"
                         % (what, start, end))
    return found.group(1)


def build() -> str:
    html = monitor.HTML
    css = _slice(html, "<style>", "</style>", "stylesheet")
    markup = _slice(html, "<!--GAME_MARKUP_START-->", "<!--GAME_MARKUP_END-->", "game markup")
    game = _slice(html, "/*GAME_JS_START*/", "/*GAME_JS_END*/", "game script")
    # The tab panel is hidden inside the dashboard; standalone it is the page.
    markup = markup.replace('id="tab-game" hidden', 'id="tab-game"')
    page = (
        PAGE.replace("__CSS__", css.strip())
        .replace("__MARKUP__", markup.strip())
        .replace("__GAME__", game.strip())
    )
    for leftover in ("__CSS__", "__MARKUP__", "__GAME__"):
        if leftover in page:
            raise SystemExit("export failed: %s was not substituted" % leftover)
    # A standalone build that talks to a server is a bug, not a feature.
    for banned in ("fetch(", "/api/state", "setInterval(load"):
        if banned in page:
            raise SystemExit("export failed: standalone build still references %r" % banned)
    return page


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Coop Rush as one HTML file")
    parser.add_argument("--out", default=str(PROJECT / "coop-rush.html"))
    args = parser.parse_args()
    page = build()
    out = Path(args.out)
    out.write_text(page, encoding="utf-8")
    print("wrote %s (%.1f KB, self-contained)" % (out, len(page.encode("utf-8")) / 1024))
    print("open it with:  open '%s'" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
