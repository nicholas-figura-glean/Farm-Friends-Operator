"""Verify one packaged release tree before its pointer can become live."""

from __future__ import annotations

import re

import monitor


def main() -> int:
    buttons = set(re.findall(r'role="tab" data-tab="([a-z_]+)"', monitor.HTML))
    panels = set()
    for tag in re.findall(r"<div\b[^>]*>", monitor.HTML):
        panel_id = re.search(r'\bid="tab-([a-z_]+)"', tag)
        classes = re.search(r'\bclass="([^"]*)"', tag)
        if panel_id and classes and "tab" in classes.group(1).split():
            panels.add(panel_id.group(1))
    required = {
        "overview", "pipeline", "cost", "history", "findings", "game", "wire",
        "architecture",
    }
    missing = sorted(required - buttons)
    if missing:
        raise SystemExit("staged dashboard missing tabs: " + ", ".join(missing))
    orphan_buttons = sorted(buttons - panels)
    orphan_panels = sorted(panels - buttons)
    if orphan_buttons:
        raise SystemExit("tab buttons with no panel: " + ", ".join(orphan_buttons))
    if orphan_panels:
        raise SystemExit("tab panels with no button: " + ", ".join(orphan_panels))
    leftover = sorted(set(re.findall(r"__[A-Z_]+__", monitor.HTML)))
    if leftover:
        raise SystemExit("unsubstituted asset placeholders: " + ", ".join(leftover))
    if "missing dashboard asset" in monitor.HTML:
        raise SystemExit("a dashboard asset failed to load into the staged page")
    print("dashboard gate: %d tabs packaged, all wired" % len(buttons))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
