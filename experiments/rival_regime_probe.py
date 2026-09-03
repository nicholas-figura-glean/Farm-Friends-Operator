#!/usr/bin/env python3
"""Pure subject-specific replay for rival wake/growth/threat questions."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from farm import analysis, rules  # noqa: E402


def _value(row: Dict[str, Any], field: str, subject: str) -> Optional[float]:
    values = row.get(field) or {}
    if not isinstance(values, dict):
        return None
    for name, value in values.items():
        if str(name).strip().lower() == subject and isinstance(value, (int, float)):
            return float(value)
    return None


def build(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    history = [row for row in rows if isinstance(row.get("run"), int) and not row.get("dry")]
    history.sort(key=lambda row: int(row["run"]))
    window = history[-rules.QUESTION_FLOW_WINDOW_RUNS :]
    if len(window) < 2:
        return {
            "schema_version": 1,
            "kind": "rival_regime_replay",
            "run_from": None,
            "run_to": None,
            "score_health": {"status": "unknown"},
            "rivals": {},
        }
    first, latest = window[0], window[-1]
    our_gain = max(0.0, float(latest.get("produce") or 0) - float(first.get("produce") or 0))
    names = {
        str(name).strip().lower()
        for row in (first, latest)
        for field in ("rivals", "rival_herds", "rival_coins")
        for name in (row.get(field) or {})
    }
    rivals: Dict[str, Dict[str, Any]] = {}
    score_health = rules.score_production_health(history)
    for subject in sorted(name for name in names if name):
        before_score = _value(first, "rivals", subject)
        after_score = _value(latest, "rivals", subject)
        before_herd = _value(first, "rival_herds", subject)
        after_herd = _value(latest, "rival_herds", subject)
        before_coins = _value(first, "rival_coins", subject)
        after_coins = _value(latest, "rival_coins", subject)
        if before_score is None or after_score is None:
            continue
        rival_gain = max(0.0, after_score - before_score)
        herd_gain = max(0.0, (after_herd or 0.0) - (before_herd or 0.0))
        material = bool(
            (rival_gain > 0 and our_gain <= 0)
            or (rival_gain > 0 and our_gain > 0
                and rival_gain >= rules.THREAT_SHARE * our_gain)
            or herd_gain >= rules.RIVAL_HERD_GROWTH_ALARM
        )
        rivals[subject] = {
            "subject": subject,
            "our_score_gain": our_gain,
            "rival_score_before": before_score,
            "rival_score_after": after_score,
            "rival_score_gain": rival_gain,
            "rival_herd_before": before_herd,
            "rival_herd_after": after_herd,
            "rival_herd_gain": herd_gain,
            "rival_coins_before": before_coins,
            "rival_coins_after": after_coins,
            "material": material,
            "settled_non_material": bool(
                latest.get("rank") == 1
                and score_health.get("status") == "healthy"
                and not material
            ),
        }
    return {
        "schema_version": 1,
        "kind": "rival_regime_replay",
        "run_from": first.get("run"),
        "run_to": latest.get("run"),
        "rank": latest.get("rank"),
        "our_score_gain": our_gain,
        "score_health": score_health,
        "rivals": rivals,
    }


def main() -> int:
    state = Path(os.environ.get("FARM_STATE_DIR", str(ROOT / "state"))).resolve()
    result = build(analysis.read_ndjson(state / "history.ndjson", limit=240))
    destination = state / "rival_regime_probe.json"
    destination.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
