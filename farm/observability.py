"""Pure attribution filters for alerts that cannot apply in the current regime."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple


def animal_cap_saturated(row: Dict[str, Any], fraction: float = 0.99) -> bool:
    capacity = int(row.get("animal_capacity") or 0)
    animals = int(row.get("animals") or 0)
    return bool(capacity and animals / float(capacity) >= fraction)


def filter_strategy_findings(
    findings: Iterable[Dict[str, Any]], latest: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Suppress uncapped-growth findings when the server hard cap is binding.

    A full barn plus accumulating non-score coins is not evidence that a growth
    gate froze the herd; buying another animal is impossible. Capped strategy is
    audited by species-per-slot evidence instead. The suppressed rows remain on
    the immutable history and are projected explicitly for diagnostics.
    """
    kept: List[Dict[str, Any]] = []
    suppressed: List[Dict[str, Any]] = []
    capped = animal_cap_saturated(latest)
    for finding in findings:
        if capped and finding.get("code") in {"strategy_stale", "idle_capital"}:
            suppressed.append(dict(finding, suppression="server animal cap is binding"))
        else:
            kept.append(finding)
    return kept, suppressed
