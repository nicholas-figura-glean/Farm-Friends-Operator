"""Narrow, author-editable response-format compatibility layer.

The MCP server can change prose without changing game semantics. Autonomous repair
may normalize that prose here, but the protected parsers in ``farm.parse`` remain
the authority for types, totals, ranges, and fail-closed validation. Normalizers
must be pure, bounded, and backward compatible: unchanged text passes through.
"""

from __future__ import annotations

from typing import Callable, Dict


def _identity(text: str) -> str:
    return text


_NORMALIZERS: Dict[str, Callable[[str], str]] = {
    "list_farm": _identity,
    "leaderboard": _identity,
    "collect_produce": _identity,
    "sell": _identity,
    "buy_feed": _identity,
    "adopt_animal": _identity,
    "farm_events": _identity,
}


def normalize(tool: str, text: str) -> str:
    """Return canonical text for a protected parser without performing I/O."""
    return _NORMALIZERS.get(str(tool), _identity)(str(text or ""))
