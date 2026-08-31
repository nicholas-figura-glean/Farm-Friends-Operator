"""Narrow, author-editable response-format compatibility layer.

The MCP server can change prose without changing game semantics. Autonomous repair
may normalize that prose here, but the protected parsers in ``farm.parse`` remain
the authority for types, totals, ranges, and fail-closed validation. Normalizers
must be pure, bounded, and backward compatible: unchanged text passes through.
"""

from __future__ import annotations

import re
from typing import Callable, Dict


def _identity(text: str) -> str:
    return text


# League boards add a badge/tier column, rename produce to lifetime produce, and
# report current/capacity animals. Translate only rows with that complete shape;
# legacy rows and any unrecognized future format pass through to fail closed in
# the protected parser.
_LEAGUE_LEADER_ROW = re.compile(
    r"^\s*(?P<rank>\d+)\.\s+\S+\s+[A-Za-z][A-Za-z ]*?\s+[IVXLCDM]+\s{2,}"
    r"(?P<name>.+?):\s+(?P<produce>\d+)\s+lifetime produce,\s+"
    r"(?P<animals>\d+)/(?P<capacity>\d+)\s+animals?,\s+"
    r"(?P<coins>\d+)\s+coins?(?P<rest>.*)$"
)
_LEAGUE_UPDATED_FOOTER = re.compile(
    r"^\(updated (?:just now|\d+ (?:sec|secs|second|seconds|min|mins|minute|minutes|hour|hours) ago)\)$",
    re.IGNORECASE,
)


def _leaderboard(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if _LEAGUE_UPDATED_FOOTER.match(line.strip()):
            continue
        match = _LEAGUE_LEADER_ROW.match(line)
        if match is None:
            lines.append(line)
            continue
        lines.append(
            "%s. %s: %s produce, %s animals, %s coins%s"
            % (
                match.group("rank"),
                match.group("name"),
                match.group("produce"),
                match.group("animals"),
                match.group("coins"),
                match.group("rest"),
            )
        )
    return "\n".join(lines)


_NORMALIZERS: Dict[str, Callable[[str], str]] = {
    "list_farm": _identity,
    "leaderboard": _leaderboard,
    "collect_produce": _identity,
    "sell": _identity,
    "buy_feed": _identity,
    "adopt_animal": _identity,
    "farm_events": _identity,
}


def normalize(tool: str, text: str) -> str:
    """Return canonical text for a protected parser without performing I/O."""
    return _NORMALIZERS.get(str(tool), _identity)(str(text or ""))
