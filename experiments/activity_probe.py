#!/usr/bin/env python3
"""Read-only replay for newly observed trade activity.

The sentinel pauses the trade domain first. This probe then answers the economic
question from durable history: what flowed to whom, what neutral alternative was
available, and did a counterparty's herd accelerate after receiving our coins?
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from farm import analysis, novelty, rules


def _state_dir() -> Path:
    value = os.environ.get("FARM_STATE_DIR")
    return Path(value).resolve() if value else Path("state").resolve()


def build(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    decisions = []
    correlations = []
    rival_changes = []
    held_trade_evidence = []
    for index, row in enumerate(rows):
        novelty_state = row.get("novelty") or {}
        for block in novelty_state.get("active_blocks") or []:
            if not isinstance(block, dict) or block.get("class") != "activity_novelty_trade":
                continue
            evidence = block.get("evidence") or {}
            held_trade_evidence.append({
                "run": row.get("run"),
                "trade_ids": [int(value) for value in evidence.get("trade_ids") or [] if isinstance(value, int)],
                "profiles": [str(value) for value in evidence.get("profiles") or []],
                "requested_coin_outflow": int(evidence.get("requested_coin_outflow") or 0),
                "material_values": [int(value) for value in evidence.get("material_values") or [] if isinstance(value, int)],
            })
        for decision in row.get("trade_decisions") or []:
            item = {
                "run": row.get("run"),
                "trade_id": decision.get("trade_id"),
                "sender": decision.get("sender"),
                "accept": bool(decision.get("accept")),
                "offer_item": decision.get("offer_item"),
                "offer_qty": int(decision.get("offer_qty") or 0),
                "want_item": decision.get("want_item"),
                "want_qty": int(decision.get("want_qty") or 0),
                "reason": decision.get("reason"),
                "surplus": decision.get("surplus"),
            }
            decisions.append(item)
            if not item["accept"] or item["want_item"] != "coin" or index + 1 >= len(rows):
                continue
            sender = str(item.get("sender") or "unknown")
            before = (row.get("rival_herds") or {}).get(sender)
            after = (rows[index + 1].get("rival_herds") or {}).get(sender)
            correlations.append({
                "trade_id": item["trade_id"],
                "sender": sender,
                "coins_transferred": item["want_qty"],
                "herd_before": before,
                "herd_after": after,
                "next_run_herd_delta": (
                    int(after) - int(before)
                    if isinstance(before, (int, float)) and isinstance(after, (int, float))
                    else None
                ),
            })
        if index == 0:
            continue
        previous = rows[index - 1]
        names = set(row.get("rival_herds") or {}) | set(row.get("rival_coins") or {})
        for name in sorted(names):
            herd_before = (previous.get("rival_herds") or {}).get(name)
            herd_after = (row.get("rival_herds") or {}).get(name)
            coins_before = (previous.get("rival_coins") or {}).get(name)
            coins_after = (row.get("rival_coins") or {}).get(name)
            produce_before = (previous.get("rivals") or {}).get(name)
            produce_after = (row.get("rivals") or {}).get(name)
            herd_delta = (
                int(herd_after) - int(herd_before)
                if isinstance(herd_before, (int, float)) and isinstance(herd_after, (int, float))
                else 0
            )
            coin_delta = (
                int(coins_after) - int(coins_before)
                if isinstance(coins_before, (int, float)) and isinstance(coins_after, (int, float))
                else 0
            )
            if herd_delta < rules.RIVAL_HERD_GROWTH_ALARM and coin_delta < novelty.RIVAL_COIN_INFLOW_ALARM:
                continue
            rival_changes.append({
                "run": row.get("run"),
                "player": name,
                "herd_before": herd_before,
                "herd_after": herd_after,
                "herd_delta": herd_delta,
                "coins_before": coins_before,
                "coins_after": coins_after,
                "coin_delta": coin_delta,
                "produce_before": produce_before,
                "produce_after": produce_after,
                "produce_delta": (
                    int(produce_after) - int(produce_before)
                    if isinstance(produce_before, (int, float)) and isinstance(produce_after, (int, float))
                    else None
                ),
            })

    accepted_coin_outflow = sum(
        item["want_qty"]
        for item in decisions
        if item["accept"] and item["want_item"] == "coin"
    )
    blocked_coin_outflow = sum(
        item["want_qty"]
        for item in decisions
        if not item["accept"] and item["want_item"] == "coin"
    )
    material_counterparty_growth = [
        item for item in correlations
        if isinstance(item.get("next_run_herd_delta"), int)
        and item["next_run_herd_delta"] >= rules.RIVAL_HERD_GROWTH_ALARM
    ]
    trade_runs = sorted(
        {int(item["run"]) for item in decisions if isinstance(item.get("run"), int)}
        | {int(item["run"]) for item in held_trade_evidence if isinstance(item.get("run"), int)}
    )
    rival_runs = sorted({int(item["run"]) for item in rival_changes if isinstance(item.get("run"), int)})
    return {
        "schema_version": 1,
        "kind": "trade_activity_replay",
        "runs": [rows[0].get("run"), rows[-1].get("run")] if rows else [],
        "decisions_observed": len(decisions),
        "held_trade_evidence": held_trade_evidence[-40:],
        "trade_ids": sorted(
            {int(item["trade_id"]) for item in decisions if isinstance(item.get("trade_id"), int)}
            | {trade_id for item in held_trade_evidence for trade_id in item["trade_ids"]}
        ),
        "accepted_coin_outflow": accepted_coin_outflow,
        "blocked_coin_outflow": blocked_coin_outflow,
        "counterparty_correlations": correlations[-20:],
        "material_counterparty_growth": material_counterparty_growth[-20:],
        "material_rival_changes": rival_changes[-40:],
        "trade_decision_runs": trade_runs,
        "rival_change_runs": rival_runs,
        "settled_classes": [
            name for name, present in (
                ("activity_novelty_trade", bool(trade_runs)),
                ("activity_novelty_rival", bool(rival_runs)),
            ) if present
        ],
        "neutral_feed_price": rules.FEED_COST,
        "finding": (
            "unsafe coin-to-rival transfer observed or held; categorical coin-outflow policy applies"
            if accepted_coin_outflow or material_counterparty_growth
            or any(item["requested_coin_outflow"] for item in held_trade_evidence)
            else "material rival regime change confirmed from leaderboard deltas"
            if rival_changes
            else "observed offers were contained by the promoted trade and reserve gates"
        ),
        "settled": bool(decisions or rival_changes or held_trade_evidence),
    }


def main() -> int:
    state = _state_dir()
    rows = analysis.read_ndjson(state / "history.ndjson", limit=200)
    result = build(rows)
    destination = state / "activity_probe.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    # An empty replay is a valid measurement with an inconclusive adjudication,
    # not an infrastructure failure. The trusted parent decides whether the
    # admitted evidence covers the active question generation.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
