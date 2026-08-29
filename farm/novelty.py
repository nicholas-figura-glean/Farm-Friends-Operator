"""Pre-action novelty sentinel for strategic adaptation.

Operational health asks whether known machinery is working. This module asks a
separate question before strategic mutations: did the game, another player, or
our interaction surface just enter a regime the promoted policy has not
explained?

Signals fail closed only in the affected domains. Husbandry and liquidation
continue; trades, offers, or adoption pause until a durable question is settled
by evidence, or a later promoted classifier proves the captured signal was
routine. Baselines and holds live in ``state/meta.json`` so a restart cannot
forget uncertainty between detection and the supervisor/research pass.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set

from . import rules

SCHEMA_VERSION = 2
TRADE_ID_JUMP = 5
MATERIAL_TRADE_VALUE = 10_000
RIVAL_COIN_INFLOW_ALARM = 10_000
RIVAL_HERD_RELATIVE_ALARM = 0.02
REMINDER_RUNS = 5
SETTLED_RESULTS = {"supported", "accepted", "passed", "falsified", "rejected"}

CLASS_DOMAINS = {
    "activity_novelty_trade": {"trades", "offers"},
    "activity_novelty_rival": {"trades", "offers"},
    "activity_novelty_risk": {"adopt"},
    "activity_novelty_tools": {"adopt", "trades", "offers"},
}


def _default_state() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "initialized": False,
        "max_trade_id": 0,
        "trade_profiles": [],
        "risk_kinds": [],
        "event_signatures": [],
        "players": [],
        "blocks": {},
    }


def event_signature(text: str) -> str:
    """Collapse high-volume routine events while preserving unknown mechanics."""
    value = str(text or "").lower()
    routine = (
        (r"\bjoined the farm\b", "animal_joined"),
        (r"\bbought\b.*\bfeed\b", "feed_bought"),
        (r"\bsold\b", "inventory_sold"),
        (r"\bfed\b.*\banimals?\b", "animals_fed"),
        (r"\bcollected\b", "produce_collected"),
        (r"\b(?:laid|produced|made honey|filled a comb|found a truffle|gave milk|grew wool)\b", "production"),
        (r"\bsays?\b", "animal_chatter"),
        (r"\btrade\b|\boffers?\b", "trade_event"),
        (r"\bprestige\b.*\brose from\b.*\banimals retired\b|\brose from\b.*\bcoins reset\b", "progression_completed"),
        (r"\bprestige (?:is )?available\b|\bcall prestige\b", "progression_available"),
        (r"\b(?:rustlers|crop blight|locust swarm|barn fire|wolf pack|alien invasion)\b.*\bin progress\b", "active_crisis"),
    )
    for pattern, label in routine:
        if re.search(pattern, value):
            return label
    # New prose is not automatically a new mechanic. Flavor events vary by
    # animal and wording; only unclassified text that mentions an economic,
    # scoring, inventory, loss, or capability concept can affect strategy.
    material_terms = re.compile(
        r"\b(?:coin|coins|feed|egg|eggs|honey|truffle|milk|wool|produce|crop|"
        r"animal|animals|chicken|chickens|cow|cows|sheep|pig|pigs|beehive|"
        r"lost|lose|took|take|stole|steal|ate|cost|charge|charged|damage|"
        r"damaged|sick|disease|wolf|wolves|storm|spoil|abduct|alien|bonus|"
        r"multiplier|rate|limit|cap|unlock|market|store|price|tool|ability)\b"
    )
    if not material_terms.search(value):
        return "ambient"
    value = re.sub(r"\d+", "#", value)
    value = re.sub(r"[^a-z#]+", " ", value)
    return "unknown:" + " ".join(value.split())[:120]


def _trade_profile(trade: Dict[str, Any]) -> str:
    qty = max(int(trade.get("offer_qty") or 0), int(trade.get("want_qty") or 0))
    if qty <= 100:
        size = "tiny"
    elif qty <= 10_000:
        size = "small"
    elif qty <= 100_000:
        size = "medium"
    else:
        size = "large"
    direction = "out" if trade.get("outgoing") else "in"
    actor = str(trade.get("recipient") if direction == "out" else trade.get("sender") or "unknown")
    return "%s|%s|%s>%s|%s" % (
        direction,
        actor.strip().lower(),
        trade.get("offer_item"),
        trade.get("want_item"),
        size,
    )


def _trade_value(trade: Dict[str, Any]) -> int:
    return max(
        rules.trade_value(str(trade.get("offer_item") or ""), int(trade.get("offer_qty") or 0)),
        rules.trade_value(str(trade.get("want_item") or ""), int(trade.get("want_qty") or 0)),
    )


def _reclassified_routine(block: Dict[str, Any]) -> bool:
    """Release a stale false-positive hold after a stricter classifier ships."""
    if block.get("class") != "activity_novelty_risk":
        return False
    signatures = list((block.get("evidence") or {}).get("new_signatures") or [])
    if not signatures:
        return False
    reclassified = []
    for signature in signatures:
        text = str(signature)
        if text.startswith("unknown:"):
            text = text.split(":", 1)[1]
        reclassified.append(event_signature(text))
    return bool(reclassified) and all(not value.startswith("unknown:") for value in reclassified)


def _handled_tool_change(block: Dict[str, Any], handled_tools: Set[str]) -> bool:
    if block.get("class") != "activity_novelty_tools":
        return False
    evidence = block.get("evidence") or {}
    before = set(str(value) for value in evidence.get("before") or [])
    after = set(str(value) for value in evidence.get("after") or [])
    added, removed = after - before, before - after
    return bool(added) and not removed and added.issubset(handled_tools)


def _handled_risk(block: Dict[str, Any], handled_risks: Set[str]) -> bool:
    if block.get("class") != "activity_novelty_risk":
        return False
    evidence = block.get("evidence") or {}
    kinds = set(str(value) for value in evidence.get("new") or [])
    return bool(kinds) and kinds.issubset(handled_risks)


def _settled(block: Dict[str, Any], questions: Iterable[Dict[str, Any]]) -> bool:
    first_run = block.get("first_run")
    for question in questions:
        if question.get("class") != block.get("class") or question.get("status") != "answered":
            continue
        closed_run = question.get("closed_run")
        if isinstance(first_run, int) and isinstance(closed_run, int) and closed_run < first_run:
            continue
        if question.get("probe_result_status") not in SETTLED_RESULTS:
            continue
        if not question.get("evidence_refs"):
            continue
        return True
    return False


def _signal(
    alert_class: str,
    subject: str,
    detail: str,
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    domains = sorted(CLASS_DOMAINS[alert_class])
    return {
        "class": alert_class,
        "subject": subject,
        "domains": domains,
        "detail": detail,
        "evidence": evidence,
        "alert": "NOVEL ACTIVITY [%s]: %s; holding %s pending evidence"
        % (alert_class.rsplit("_", 1)[-1], detail, ",".join(domains)),
    }


def assess(
    snapshot: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    state: Optional[Dict[str, Any]] = None,
    question_rows: Optional[List[Dict[str, Any]]] = None,
    handled_tools: Optional[Iterable[str]] = None,
    handled_risks: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Detect rising-edge activity and return persistent domain holds.

    ``state`` is copied and returned as ``state``; callers persist it atomically
    with their normal run metadata. The function performs no I/O, which keeps it
    replayable in self-tests and bounded probes.
    """
    current = _default_state()
    current.update(dict(state or {}))
    current["schema_version"] = SCHEMA_VERSION
    current.setdefault("blocks", {})
    questions = list(question_rows or [])
    tools_with_policy = set(str(value) for value in (handled_tools or []))
    risks_with_policy = set(str(value) for value in (handled_risks or []))
    run = int(snapshot.get("run") or 0)

    # Evidence-linked question closure is the ordinary unblock path. A promoted
    # classifier correction may also clear a hold whose own captured evidence is
    # now deterministically proven to be routine flavor text.
    blocks: Dict[str, Dict[str, Any]] = {}
    resolved_blocks: List[Dict[str, Any]] = []
    for key, value in (current.get("blocks") or {}).items():
        block = dict(value)
        if _settled(block, questions):
            resolved_blocks.append({"class": key, "reason": "evidence-linked question settled"})
        elif _handled_tool_change(block, tools_with_policy):
            resolved_blocks.append({"class": key, "reason": "added tools now have validated capability policies"})
        elif _handled_risk(block, risks_with_policy):
            resolved_blocks.append({"class": key, "reason": "risk kind now has a validated bounded policy"})
        elif _reclassified_routine(block):
            resolved_blocks.append({"class": key, "reason": "captured event reclassified as routine"})
        else:
            blocks[key] = block
    current["blocks"] = blocks
    signals: List[Dict[str, Any]] = []

    tools = sorted(set(str(value) for value in (snapshot.get("tools") or [])))
    prior_tools = sorted(set(str(value) for value in (current.get("tools") or [])))
    if prior_tools and tools != prior_tools:
        added_tools = set(tools) - set(prior_tools)
        removed_tools = set(prior_tools) - set(tools)
        # A validated policy is the completed handling path, not unresolved
        # novelty. Removals still fail closed because losing a capability cannot
        # be repaired by the policy that used it.
        if removed_tools or not added_tools.issubset(tools_with_policy):
            signals.append(_signal(
                "activity_novelty_tools",
                "server capability surface",
                "tool names changed added=%s removed=%s"
                % (sorted(added_tools), sorted(removed_tools)),
                {"before": prior_tools, "after": tools},
            ))
    current["tools"] = tools

    trades = [dict(value) for value in (snapshot.get("trades") or [])]
    incoming = [trade for trade in trades if not trade.get("outgoing")]
    seen_profiles = set(str(value) for value in (current.get("trade_profiles") or []))
    incoming_profiles = {_trade_profile(trade) for trade in incoming}
    new_profiles = sorted(incoming_profiles - seen_profiles)
    old_max = int(current.get("max_trade_id") or 0)
    observed_ids = [int(trade.get("id") or 0) for trade in trades]
    new_max = max([old_max] + observed_ids)
    id_jump = old_max > 0 and new_max - old_max >= TRADE_ID_JUMP
    material = [trade for trade in incoming if _trade_value(trade) >= MATERIAL_TRADE_VALUE]
    if incoming and (not current.get("initialized") or new_profiles or id_jump or material):
        ids = sorted(int(trade.get("id") or 0) for trade in incoming)
        requested_coins = sum(
            int(trade.get("want_qty") or 0)
            for trade in incoming
            if trade.get("want_item") == "coin"
        )
        reasons = []
        if new_profiles:
            reasons.append("new profile %s" % ", ".join(new_profiles[:4]))
        if id_jump:
            reasons.append("trade id high-water jumped %d -> %d" % (old_max, new_max))
        if material:
            reasons.append("%d material offer(s)" % len(material))
        if not reasons:
            reasons.append("first observed inbound activity")
        signals.append(_signal(
            "activity_novelty_trade",
            "trade activity",
            "trade ids %s: %s; requested coin outflow=%d"
            % (ids, "; ".join(reasons), requested_coins),
            {
                "trade_ids": ids,
                "profiles": sorted(incoming_profiles),
                "requested_coin_outflow": requested_coins,
                "material_values": [_trade_value(trade) for trade in material],
                "previous_high_water": old_max,
                "observed_high_water": new_max,
            },
        ))
    current["max_trade_id"] = new_max
    current["trade_profiles"] = sorted((seen_profiles | incoming_profiles))[-200:]

    rival_herds = dict(snapshot.get("rival_herds") or {})
    rival_coins = dict(snapshot.get("rival_coins") or {})
    previous_herds = dict((previous or {}).get("rival_herds") or {})
    previous_coins = dict((previous or {}).get("rival_coins") or {})
    players = set(rival_herds) | set(rival_coins)
    known_players = set(str(value) for value in (current.get("players") or []))
    new_players = sorted(players - known_players) if current.get("initialized") else []
    accelerations = []
    for name in sorted(players):
        herd = int(rival_herds.get(name) or 0)
        old_herd = previous_herds.get(name)
        coins = int(rival_coins.get(name) or 0)
        old_coins = previous_coins.get(name)
        herd_delta = herd - int(old_herd or 0) if old_herd is not None else 0
        coin_delta = coins - int(old_coins or 0) if old_coins is not None else 0
        herd_floor = max(
            rules.RIVAL_HERD_GROWTH_ALARM,
            int(max(int(old_herd or 0), 1) * RIVAL_HERD_RELATIVE_ALARM),
        )
        if herd_delta >= herd_floor or coin_delta >= RIVAL_COIN_INFLOW_ALARM:
            accelerations.append({
                "player": name,
                "herd_before": old_herd,
                "herd_after": herd,
                "herd_delta": herd_delta,
                "coins_before": old_coins,
                "coins_after": coins,
                "coin_delta": coin_delta,
            })
    if new_players or accelerations:
        prior_coin_transfers = {}
        for decision in (previous or {}).get("trade_decisions") or []:
            if decision.get("accept") and decision.get("want_item") == "coin":
                sender = str(decision.get("sender") or "unknown")
                prior_coin_transfers[sender] = prior_coin_transfers.get(sender, 0) + int(
                    decision.get("want_qty") or 0
                )
        signals.append(_signal(
            "activity_novelty_rival",
            "competitive activity",
            "new players=%s; material rival changes=%s"
            % (new_players, [item["player"] for item in accelerations]),
            {
                "new_players": new_players,
                "accelerations": accelerations,
                "prior_accepted_coin_transfers": prior_coin_transfers,
            },
        ))
    current["players"] = sorted(players)

    risk_kinds = set(str(value) for value in (snapshot.get("risk_kinds") or []))
    known_risk = set(str(value) for value in (current.get("risk_kinds") or []))
    new_risk = sorted((risk_kinds - known_risk) - risks_with_policy)
    if new_risk:
        signals.append(_signal(
            "activity_novelty_risk",
            "game risk mechanics",
            "new risk event kind(s) %s" % new_risk,
            {"new": new_risk, "known": sorted(known_risk)},
        ))
    current["risk_kinds"] = sorted(known_risk | risk_kinds)

    signatures = set(str(value) for value in (snapshot.get("event_signatures") or []))
    known_signatures = set(str(value) for value in (current.get("event_signatures") or []))
    new_signatures = sorted(signatures - known_signatures) if current.get("initialized") else []
    unknown_signatures = [value for value in new_signatures if value.startswith("unknown:")]
    if unknown_signatures:
        signals.append(_signal(
            "activity_novelty_risk",
            "game event surface",
            "unclassified event signature(s) %s" % unknown_signatures[:4],
            {"new_signatures": unknown_signatures[:20], "known": sorted(known_signatures)},
        ))
    current["event_signatures"] = sorted(known_signatures | signatures)[-200:]

    # One persistent block per question class keeps repeated events bounded while
    # retaining the newest evidence and all affected domains.
    for signal in signals:
        key = str(signal["class"])
        existing = dict(blocks.get(key) or {})
        blocks[key] = {
            "class": key,
            "subject": signal["subject"],
            "domains": sorted(set(existing.get("domains") or []) | set(signal["domains"])),
            "first_run": existing.get("first_run", run),
            "last_run": run,
            "alert": signal["alert"],
            "evidence": signal["evidence"],
        }

    # If routing broke, periodically re-emit the original alert rather than
    # silently retaining a hold no autonomous worker can see.
    questioned_classes = {str(row.get("class")) for row in questions}
    for key, block in sorted(blocks.items()):
        if key in questioned_classes or any(signal["class"] == key for signal in signals):
            continue
        first_run = block.get("first_run")
        if isinstance(first_run, int) and run - first_run >= REMINDER_RUNS and (run - first_run) % REMINDER_RUNS == 0:
            reminder = dict(block)
            reminder["detail"] = "unrouted novelty hold still awaiting a durable question"
            signals.append({
                "class": key,
                "subject": block.get("subject"),
                "domains": list(block.get("domains") or []),
                "detail": reminder["detail"],
                "evidence": dict(block.get("evidence") or {}),
                "alert": str(block.get("alert") or "NOVEL ACTIVITY: unresolved hold"),
            })

    current["blocks"] = blocks
    current["initialized"] = True
    blocked_domains: Set[str] = set()
    for block in blocks.values():
        blocked_domains.update(str(value) for value in (block.get("domains") or []))
    return {
        "schema_version": SCHEMA_VERSION,
        "signals": signals,
        "active_blocks": [dict(block) for _, block in sorted(blocks.items())],
        "blocked_domains": sorted(blocked_domains),
        "resolved_blocks": resolved_blocks,
        "state": current,
    }
