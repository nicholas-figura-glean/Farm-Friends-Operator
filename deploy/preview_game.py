#!/usr/bin/env python3
"""Render Coop Rush's panels as a *static* page, for looking at spacing.

Why this exists: the game builds its DOM in JavaScript, and the only browser
available here cannot run JavaScript. So there was no way to actually look at the
layout - which is how a milestone label ended up sitting on top of the flavour text
and a Buy/Manager stack ended up drifting a few pixels against every card title.

This lifts the real HTML templates out of game/coop_rush_ui.js rather than retyping
them, fills them with fixtures (including deliberately awful ones: huge numbers,
long text, 0 owned, all-milestones-done), and inlines the real stylesheet. What you
look at is what the game emits.

    python3 deploy/preview_game.py            # write preview to game-preview.html
    python3 deploy/preview_game.py --serve    # ...and serve it on :8790
    python3 deploy/preview_game.py --desktop  # strip the mobile breakpoint

Not part of the game or the dashboard: nothing imports this, and it writes a file
that is ignored by git.
"""

from __future__ import annotations

import http.server
import re
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "game-preview.html"
PORT = 8790


def lift(source: str, start: str, end: str, what: str) -> str:
    """Pull a template literal out of the UI source.

    Returns only the text *after* `start`, so callers prepend whatever opening
    fragment they matched on. Returning it here as well is how the first version of
    this script produced `class="cr-prod<div class="cr-prod`, which silently killed
    the grid and made every card look stacked.
    """
    if start not in source:
        raise SystemExit(f"preview: cannot find the {what} template (looked for {start!r})")
    body = source.split(start, 1)[1]
    if end not in body:
        raise SystemExit(f"preview: {what} template is not terminated by {end!r}")
    return body.split(end, 1)[0]


def fill(tpl: str, pairs: dict[str, str]) -> str:
    out = tpl
    for needle, value in pairs.items():
        out = out.replace(needle, value)
    # Anything left over is an interpolation the fixture forgot; make it obvious
    # rather than shipping a preview with ${...} in the middle of the layout.
    leftovers = re.findall(r"\$\{[^}]*\}", out)
    if leftovers:
        raise SystemExit("preview: unfilled interpolations: " + ", ".join(sorted(set(leftovers))))
    return out


def producer_cards(ui: str) -> str:
    tpl = lift(ui, 'return `<div class="cr-prod', "`;\n    }).join(\"\")", "producer card")
    tpl = '<div class="cr-prod' + tpl

    def card(name, icon, owned, cycle, bar, rate, note, ms, mspct, buyn, cost, mgr, locked):
        return fill(tpl, {
            '${locked ? " locked" : ""}': " locked" if locked else "",
            "${def.id}": "x",
            "${def.icon}": icon,
            "${esc(def.name)}": name,
            "${esc(def.note)}": note,
            '<span class="cr-owned" data-f="owned">0</span>':
                f'<span class="cr-owned" data-f="owned">{owned}</span>',
            '<i data-f="bar" style="width:0%"></i><span data-f="cycle"></span>':
                f'<i data-f="bar" style="width:{bar}%"></i><span data-f="cycle">{cycle}</span>',
            '<span data-f="rate"></span>': f'<span data-f="rate">{rate}</span>',
            '<i data-f="msbar" style="width:0%"></i>': f'<i data-f="msbar" style="width:{mspct}%"></i>',
            '<span class="cr-mstext" data-f="ms"></span>':
                f'<span class="cr-mstext" data-f="ms">{ms}</span>',
            '<b data-f="buyn">1</b>': f'<b data-f="buyn">{buyn}</b>',
            '<span class="cr-btn-cost" data-f="cost">-</span>':
                f'<span class="cr-btn-cost" data-f="cost">{cost}</span>',
            '<span class="cr-btn-cost" data-f="mgr">-</span>':
                f'<span class="cr-btn-cost" data-f="mgr">{mgr}</span>',
        })

    return "".join([
        card("Chicken Coop", "&#128020;", 1, "0.80s", 62,
             "1 produce / cycle &middot; 1.3/s (needs clicks)",
             "the only thing that ever produced", "24 more &#8594; x2 produce", 4, 1, "4.3c", "80c", False).replace('class="cr-btn buy', 'class="cr-btn buy ready'),
        card("Truffle Pigs", "&#128055;", 0, "&ndash;", 0, "6 produce / 3.0s",
             "100 of them produced 2 truffles a run", "25 more &#8594; x2 produce", 0, 1, "60c", "1.20Kc", True),
        card("Farmers Market", "&#127981;", 412, "0.31s", 88,
             "1.44M produce / cycle &middot; 4.65M/s",
             "the market that never opened because nothing grew",
             "all milestones earned", 100, "Max", "8.42Qic", "owned", False),
        card("Dairy Barn", "&#128004;", 199, "1.10s", 35,
             "128.4K produce / cycle &middot; 116.7K/s",
             "milk was never once collected", "1 more &#8594; cycle halved", 96, 100, "45.10Bc", "owned", False),
    ])


def upgrade_cards(ui: str) -> str:
    tpl = lift(ui, 'return `<button class="cr-up', "</button>`", "heirloom upgrade")
    tpl = '<button class="cr-up' + tpl + "</button>"

    def up(name, icon, desc, cost, state):
        owned = state == "owned"
        html = fill(tpl, {
            '${owned ? " owned" : ""}': " owned" if owned else "",
            '${owned ? "disabled" : ""}': "disabled" if owned else "",
            "${u.id}": "x",
            "${u.icon}": icon,
            "${esc(u.name)}": name,
            "${esc(u.desc)}": desc,
            '${owned ? "owned" : fmt(u.cost) + " \\u{1F423}"}': "owned" if owned else cost,
            '${owned ? "owned" : fmt(u.cost) + "c"}': "owned" if owned else cost,
        })
        # paint() adds .ready at runtime rather than in the template, so the fixture
        # has to add it here or the preview would only ever show unaffordable cards.
        if state == "ready":
            html = html.replace('class="cr-up', 'class="cr-up ready', 1)
        return html

    return "".join([
        up("Farmhand", "&#129489;", "every rebuild starts with the Chicken Coop manager - the farm restarts itself", "25 &#128035;", "ready"),
        up("Heirloom Husbandry", "&#128035;", "each heirloom gives 3% instead of 2%", "60 &#128035;", "owned"),
        up("The Run-50 Lesson", "&#129514;", "x3 all produce - the experiment that closed the question", "150 &#128035;", "ready"),
        up("Keeper of the Ledger", "&#128211;", "x5 all produce", "5.00K &#128035;", "locked"),
    ])


def build(desktop: bool, fit: bool = False) -> str:
    ui = (ROOT / "game" / "coop_rush_ui.js").read_text()
    css = (ROOT / "game" / "coop_rush.css").read_text()
    if desktop:
        css = re.sub(r"@media \(max-width:900px\) \{.*?\n\}\n", "", css, flags=re.S)
        if "@media" in css:
            raise SystemExit("preview: --desktop did not strip the breakpoint")
    root_vars = re.search(r":root\s*\{(.*?)\}", (ROOT / "monitor.py").read_text(), re.S).group(1)

    cards = producer_cards(ui)
    ups = upgrade_cards(ui)
    # Cheap guard against the class-mangling bug above coming back in another form.
    for broken in ('class="cr-prod<', 'class="cr-up<', "<div<", "<button<"):
        if broken in cards or broken in ups:
            raise SystemExit(f"preview: malformed markup around {broken!r}")

    # --fit exists because the only viewport available for looking at this is a ~700px
    # panel, and the desktop layout is 1200px wide. Zoom scales the render without
    # tripping the breakpoint, which a narrow window would.
    fit_css = "body > * { width:1240px; }\nbody { zoom:.55; }" if fit else ""

    return f"""<!doctype html><meta charset="utf-8"><title>Coop Rush layout preview</title>
<style>
:root{{{root_vars}}}
body {{ margin:0; padding:20px; background:var(--bg); color:var(--text);
  font:14px/1.5 ui-sans-serif,-apple-system,"SF Pro Text",system-ui,sans-serif; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:14px;
  padding:16px; margin-bottom:16px; }}
.card h2 {{ font-size:12px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted);
  margin:0 0 12px; font-weight:700; }}
{css}
{fit_css}
</style>
<div class="card"><h2>Producers</h2>
  <div class="cr-prods">{cards}</div>
  <div class="cr-legend"><b>Click a producer</b> to run one cycle by hand.
    A <b>manager</b> runs it for you - that is what makes the farm idle.</div>
</div>
<div class="card"><h2>Heirloom upgrades <small>permanent, kept through every rebuild</small></h2>
  <div class="cr-ups-wide">{ups}</div>
</div>
<div class="card cr-prestige"><h2>Rebuild the farm</h2>
  <div class="cr-gain"><strong>24</strong><span>heirloom hens on offer</span></div>
  <button class="cr-big-btn">Rebuild the farm for 24 heirlooms</button>
  <p class="cr-fine">Rebuilding wipes coins, producers and one-off upgrades.
    You keep heirlooms, heirloom upgrades and every record.</p>
</div>
"""


def main() -> int:
    desktop = "--desktop" in sys.argv
    fit = "--fit" in sys.argv
    OUT.write_text(build(desktop, fit))
    flags = ", ".join(f for f, on in (("mobile breakpoint stripped", desktop), ("scaled to fit", fit)) if on)
    print(f"wrote {OUT}" + (f"  ({flags})" if flags else ""))
    if "--serve" in sys.argv:
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(ROOT), **kw)

            def log_message(self, *a):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as srv:
            print(f"serving http://127.0.0.1:{PORT}/{OUT.name}  (ctrl-c to stop)")
            srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
