#!/usr/bin/env python3
"""Regression gate for closed-loop adaptive game mechanics.

No live MCP calls are made. Fixtures exercise the same parsers, policy validator,
decision functions, growth reset, novelty release, and canary path used at runtime.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, List

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "experiments"))

from farm import analysis, canary, contract, cycle, growth, mechanics, novelty, parse, rules, watch, workorders  # noqa: E402
import expand  # noqa: E402
import research_agent  # noqa: E402


class Suite:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: List[str] = []

    def check(self, condition: Any, label: str, detail: Any = "") -> None:
        self.checks += 1
        passed = bool(condition)
        print("  %-4s %s%s" % (
            "ok" if passed else "FAIL",
            label,
            (" [%s]" % detail) if detail and not passed else "",
        ))
        if not passed:
            self.failures.append(label + ((" [%s]" % detail) if detail else ""))


def section(title: str) -> None:
    print("\n== %s" % title)


def animal() -> parse.Animal:
    return parse.Animal(1, "Pecky", "chicken", "delighted", 0, 100)


def farm(**overrides: Any) -> parse.Farm:
    values = {
        "coins": 1_000_000,
        "animals": [animal()],
        "animal_total": 9_500,
        "animal_counts": {"chicken": 9_500},
        "plot_total": 10,
        "league": "Gold II",
        "league_name": "Gold",
        "league_tier": "II",
        "league_level": 7,
        "lifetime_produce": 2_000_000,
        "capacity": 10_000,
        "plot_capacity": 20_000,
        "prestige_available": False,
        "inventory": {"feed": 400_000},
    }
    values.update(overrides)
    return parse.Farm(**values)


def main() -> int:
    suite = Suite()

    section("league and crisis state are runtime facts")
    league_text = (
        "🌾 Nick's Farm  🪙 336000821 coins  💠 Platinum II (level 11)\n"
        "   Lifetime produce 253241476 · animals 16875/16875 · plots 493/33750 "
        "· ✨ prestige available — call prestige\n\n"
        "Animals (16875 total — summarising by kind):\n"
        "  🐝 beehive: 89\n  🐔 chicken: 16786\n"
        "  A few of them up close:\n"
        "    🐔 Pecky the chicken (#7) is delighted. hunger 0/100, happiness 100/100\n"
        "Fields (493 plots — summarising by kind):\n"
        "  🌼 wildflowers: 493 planted, 493 in bloom\n"
        "Barn inventory: 🌱 feed x8270936\n"
    )
    parsed = parse.parse_farm(league_text)
    suite.check(
        parsed.league == "Platinum II" and parsed.league_level == 11,
        "list_farm preserves league identity and numeric level",
        mechanics.farm_snapshot(parsed),
    )
    suite.check(
        parsed.capacity == 16875 and parsed.full and parsed.prestige_available,
        "list_farm preserves capacity and prestige eligibility",
        mechanics.farm_snapshot(parsed),
    )

    crisis_text = league_text.replace(
        "\n\nAnimals",
        "\n🥸🚚 RUSTLERS IN PROGRESS (since 01:21 UTC) — resolve_crisis costs 35% of your gold and ends it instantly.\n\nAnimals",
    )
    crisis_farm = parse.parse_farm(crisis_text)
    suite.check(
        crisis_farm.crisis is not None
        and crisis_farm.crisis.kind == "rustlers"
        and crisis_farm.crisis.resolver == "resolve_crisis"
        and crisis_farm.crisis.cost_fraction == 0.35,
        "active crisis kind, resolver, start, and declared cost are parsed",
        crisis_farm.crisis,
    )

    board = parse.parse_leaderboard(
        "🏆 Farm Friends leaderboard (by league, then lifetime produce)\n"
        " 1. 💠 Platinum II     Nick: 253255174 lifetime produce, 16875/16875 animals, 336028563 coins, 493 🌼 ⚠️ FULL — prestige!\n"
        " 2. 🥇 Gold II         Neill: 41080580 lifetime produce, 11241/11250 animals, 1509012 coins, 1074306 🌼\n"
        " 3. 🥈 Silver III      Deep: 2796048 lifetime produce, 7004/7500 animals, 581621 coins, 332298 🌼 🥸 Rustlers!\n"
        "(updated just now)\n"
    )
    suite.check(
        board[0].league == "Platinum II"
        and board[0].capacity == 16875
        and board[0].prestige_available,
        "protected leaderboard parser retains league/capacity/status instead of normalizing them away",
        board[0],
    )
    suite.check(board[2].crisis == "rustlers", "leaderboard crisis suffix remains visible", board[2])

    section("literal policy surface and hard ceilings")
    loaded = mechanics.load_policies()
    suite.check(not loaded["errors"], "shipped capability policies validate", loaded["errors"])
    suite.check(
        mechanics.active_tools(loaded) == {"prestige", "resolve_crisis", "call_fbi"},
        "all three direct mechanics are executable policies",
        mechanics.active_tools(loaded),
    )
    with tempfile.TemporaryDirectory() as tmp:
        malicious = Path(tmp) / "policies.py"
        marker = Path(tmp) / "executed"
        malicious.write_text(
            "open(%r, 'w').write('bad')\nCAPABILITY_POLICIES = []\n" % str(marker),
            encoding="utf-8",
        )
        rejected = mechanics.load_policies(malicious, {"tools": {}})
        suite.check(rejected["errors"] and not marker.exists(),
                    "policy parser rejects executable Python without running it", rejected)

        wrong = Path(tmp) / "wrong.py"
        wrong.write_text(
            "CAPABILITY_POLICIES = [%r]\n" % dict(
                loaded["policies"][0], contract={"description_sha": "wrong", "required": []}
            ),
            encoding="utf-8",
        )
        wrong_result = mechanics.load_policies(wrong, json.loads((PROJECT / "state" / "contract.json").read_text()))
        suite.check(
            any("fingerprint" in error for error in wrong_result["errors"]),
            "a stale or invented contract fingerprint disables the policy",
            wrong_result["errors"],
        )

    section("objective and crisis decisions")
    prestige_farm = farm(prestige_available=True, animal_total=10_000, animal_counts={"chicken": 10_000})
    prestige = mechanics.next_decision(
        prestige_farm, mechanics.active_tools(loaded), run=10, loaded=loaded
    )["decision"]
    suite.check(prestige and prestige["tool"] == "prestige" and prestige["kind"] == "progression",
                "an earned prestige is the due objective action", prestige)

    rustlers = farm(
        crisis=parse.Crisis("rustlers", "RUSTLERS", "01:21", "resolve_crisis", 0.35),
        animal_total=9_500,
        animal_counts={"chicken": 9_500},
    )
    resolution = mechanics.next_decision(
        rustlers, mechanics.active_tools(loaded), loaded=loaded
    )["decision"]
    suite.check(resolution and resolution["tool"] == "resolve_crisis",
                "a declared-cost animal crisis at a nearly full barn is resolved", resolution)

    blight = farm(
        crisis=parse.Crisis("crop_blight", "CROP BLIGHT", "01:11", "resolve_crisis", 0.30),
        animal_total=5_000,
        animal_counts={"chicken": 5_000},
        plot_total=10,
    )
    blight_view = mechanics.next_decision(blight, mechanics.active_tools(loaded), loaded=loaded)
    suite.check(
        blight_view["decision"] is None and any("field utilization" in item["reason"] for item in blight_view["held"]),
        "a low-value crop crisis cannot spend 30% merely because the endpoint exists",
        blight_view,
    )

    invasion = farm(
        prestige_available=True,
        crisis=parse.Crisis("alien_invasion", "ALIEN INVASION", "02:00", "call_fbi", 0.80),
        animal_total=10_000,
        animal_counts={"chicken": 10_000},
    )
    fbi = mechanics.next_decision(invasion, mechanics.active_tools(loaded), loaded=loaded)["decision"]
    suite.check(fbi and fbi["tool"] == "call_fbi",
                "an active invasion is cleared before an already-earned coin reset", fbi)

    previous_rank = {
        "run": 1, "ts": "2026-08-29T00:00:00Z", "rank": 2,
        "produce": 100, "animals": 500, "max_hunger": 0,
        "feed": 15_000, "reserve_target": 15_000, "coins": 50_000,
        "units_collected": 50, "units_per_chicken_min": 0.2,
        "interval_min": 5.0, "zero_streak": 0, "call_rate": 5.0,
        "transport_errors_core": 0, "calls": 10,
        "rivals": {"Leader": 50}, "rival_herds": {"Leader": 600},
        "rival_coins": {"Leader": 10_000}, "rival_leagues": {"Leader": "Silver III"},
        "league": "Bronze III", "leader": "Leader", "next_level_produce": 200,
        "notes": [], "mechanic_actions": [],
    }
    current_rank = dict(
        previous_rank,
        run=2,
        ts="2026-08-29T00:05:00Z",
        produce=110,
        rivals={"Leader": 55},
    )
    rank_alerts, _ = watch.evaluate(current_rank, previous_rank)
    suite.check(
        not rank_alerts and (current_rank.get("projection") or {}).get("kind") == "league_progression",
        "a higher-league leader creates a progression path, never a fake lifetime-produce crossover",
        {"alerts": rank_alerts, "projection": current_rank.get("projection")},
    )

    section("post-action verification")
    after_prestige = farm(
        league="Gold I", league_tier="I", league_level=8,
        lifetime_produce=2_000_000, capacity=10_000,
        animal_total=1, animal_counts={"chicken": 1}, prestige_available=False,
    )
    verified = mechanics.verify_action(prestige, prestige_farm, after_prestige)
    suite.check(verified["ok"] and all(verified["checks"].values()),
                "prestige requires level growth, lifetime preservation, and nondecreasing capacity", verified)
    bad = mechanics.verify_action(
        prestige,
        prestige_farm,
        farm(league_level=8, lifetime_produce=1_999_999, capacity=11_250),
    )
    suite.check(not bad["ok"] and not bad["checks"]["lifetime_produce_preserved"],
                "a prestige that loses lifetime produce fails closed", bad)
    after_crisis = farm(crisis=None, coins=650_000)
    crisis_verified = mechanics.verify_action(resolution, rustlers, after_crisis)
    suite.check(crisis_verified["ok"], "crisis must clear within its declared fraction", crisis_verified)

    class FakeClient:
        def __init__(self) -> None:
            self.calls: List[Any] = []

        def call(self, tool: str, **kwargs: Any) -> str:
            self.calls.append((tool, kwargs))
            return "ok"

    runner = object.__new__(cycle.Cycle)
    runner.c = FakeClient()
    runner.meta = {}
    runner.actions = {
        "mechanic_actions": [], "mechanic_failures": 0, "prestige_count": 0,
        "crises_resolved": 0, "progression_pending": False,
        "capability_policy_errors": [],
    }
    runner.notes, runner.notes_soft = [], []
    runner.read_state = lambda tag: after_prestige
    saved_intent, saved_raw = cycle._intent, cycle._raw
    saved_reset, saved_lock = growth.reset_after_progression, mechanics.exclusive_expansion_lock
    try:
        cycle._intent = lambda *args, **kwargs: None
        cycle._raw = lambda *args, **kwargs: None
        growth.reset_after_progression = lambda *args, **kwargs: {}
        @contextlib.contextmanager
        def unlocked():
            yield True
        mechanics.exclusive_expansion_lock = unlocked
        observed = runner.handle_mechanics(prestige_farm, list(mechanics.active_tools(loaded)), 10)
    finally:
        cycle._intent, cycle._raw = saved_intent, saved_raw
        growth.reset_after_progression, mechanics.exclusive_expansion_lock = saved_reset, saved_lock
    suite.check(
        observed.league_level == 8
        and runner.actions["prestige_count"] == 1
        and runner.actions["mechanic_actions"][0]["status"] == "verified",
        "cycle executes and records the verified policy outcome",
        runner.actions,
    )
    suite.check(
        runner.c.calls == [("prestige", {"_transport_retries": 1})],
        "irreversible cycle action receives exactly one transport attempt",
        runner.c.calls,
    )
    transition_rows = [
        {"run": 1, "ts": "2026-08-29T00:00:00Z", "produce": 1_900_000,
         "animals": 10_000, "max_hunger": 0, "league_level": 7},
        {"run": 2, "ts": "2026-08-29T00:05:00Z", "produce": 2_000_000,
         "animals": 1, "max_hunger": 0, "league_level": 8, "prestige_count": 1},
        {"run": 3, "ts": "2026-08-29T00:10:00Z", "produce": 2_000_050,
         "animals": 400, "max_hunger": 0, "league_level": 8},
    ]
    rates = analysis.rate_samples(transition_rows)
    suite.check(
        len(rates) == 1 and rates[0]["run"] == 3,
        "the retiring herd's score is never attributed to the reset herd",
        rates,
    )
    suite.check(
        canary._progression_transition_runs(transition_rows) == {2, 3},
        "canary excludes the reset row and first lagging leaderboard interval",
        canary._progression_transition_runs(transition_rows),
    )

    section("growth and expansion cannot re-enter the old deadlock")
    suite.check(expand.bounded_target(prestige_farm, 250_000) == 10_000,
                "expansion target is capped by the parsed league capacity")
    rules_plan = rules.expansion_plan(
        1_000_000, 10_000, 1_000_000, 0, cap=400, animal_capacity=10_000
    )
    suite.check(
        isinstance(rules_plan, dict),
        "capacity-aware plan returns a concrete result",
        rules_plan,
    )
    suite.check(rules_plan["adopt"] == 0, "a full barn plans zero adoption", rules_plan)
    expand_source = (PROJECT / "experiments" / "expand.py").read_text(encoding="utf-8")
    suite.check(
        "qty=want" in expand_source and "_transport_retries=1" in expand_source,
        "expansion uses one non-retried bulk qty mutation",
    )
    suite.check(
        "BULK_IMPLEMENTATION_ERROR" in expand_source
        and "bounded_individual_fallback_after_bulk_error" in expand_source,
        "advertised-but-broken bulk behavior has a bounded circuit-breaker fallback",
    )
    with tempfile.TemporaryDirectory() as tmp:
        breaker = Path(tmp) / "bulk.json"
        now = 10_000.0
        breaker.write_text(json.dumps({
            "disabled": True,
            "failed_at_epoch": now,
            "description_sha": expand._adopt_contract_sha(),
        }), encoding="utf-8")
        suite.check(not expand.bulk_due(now + expand.BULK_REPROBE_SECONDS - 1, str(breaker)),
                    "bulk circuit breaker prevents a five-minute error loop")
        suite.check(expand.bulk_due(now + expand.BULK_REPROBE_SECONDS + 1, str(breaker)),
                    "bulk behavior is periodically re-probed so a server fix is learned")
        fallback_client = FakeClient()
        fallback = expand._individual_fallback(
            fallback_client, "chicken", 3, time.time() + 5, 1
        )
        suite.check(
            fallback["ok"] == 3
            and all(call[0] == "adopt_animal" and "qty" not in call[1]
                    for call in fallback_client.calls),
            "definitive bulk failure falls back to bounded no-qty calls",
            {"result": fallback, "calls": fallback_client.calls},
        )

    with tempfile.TemporaryDirectory() as tmp:
        old_store, old_history = growth.STORE, growth.HISTORY
        try:
            growth.STORE = str(Path(tmp) / "growth.json")
            growth.HISTORY = str(Path(tmp) / "history.ndjson")
            Path(growth.HISTORY).write_text(
                "".join(json.dumps({
                    "run": n, "ts": "2026-08-29T00:%02d:00Z" % n,
                    "animals": 10_000, "produce": 1000, "max_hunger": 0,
                    "verified": True,
                }) + "\n" for n in range(1, 10)),
                encoding="utf-8",
            )
            growth.save({"saturated": True, "production_stalled": True, "production_stall_windows": 5})
            growth.reset_after_progression(10, 8)
            decision = growth.decide(1, {}, run=10, persist=False)
        finally:
            growth.STORE, growth.HISTORY = old_store, old_history
        suite.check(decision["cap"] == 400 and not decision["verdict"]["production_stalled"],
                    "prestige starts a fresh evidence epoch instead of inheriting the full-barn stall", decision)

    section("novelty and research reach implementation")
    old_state = {
        "initialized": True,
        "tools": ["list_farm"],
        "blocks": {
            "activity_novelty_tools": {
                "class": "activity_novelty_tools",
                "first_run": 1,
                "last_run": 1,
                "domains": ["adopt"],
                "evidence": {"before": ["list_farm"], "after": ["list_farm", "prestige"]},
            },
            "activity_novelty_risk": {
                "class": "activity_novelty_risk",
                "first_run": 1,
                "last_run": 1,
                "domains": ["adopt"],
                "evidence": {"new_signatures": ["unknown:prestige is available call prestige"]},
            },
        },
    }
    suite.check(
        novelty.event_signature(
            "💠 PRESTIGE! Nick rose from Platinum II to Platinum I (level 12). 16872 animals retired, coins reset to 50."
        ) == "progression_completed",
        "a verified prestige event cannot reopen the risk hold that progression just settled",
    )
    novelty_result = novelty.assess(
        {"run": 2, "tools": ["list_farm", "prestige"], "trades": [],
         "rival_herds": {}, "rival_coins": {}, "risk_kinds": [], "event_signatures": []},
        None,
        state=old_state,
        handled_tools={"prestige"},
    )
    suite.check(not novelty_result["blocked_domains"],
                "a validated policy releases the discovery hold instead of waiting forever", novelty_result)

    prestige_class = mechanics.classify_capability(
        "prestige", json.loads((PROJECT / "state" / "contract.json").read_text())["tools"]["prestige"]["description"], []
    )
    gift_class = mechanics.classify_capability("gift", "Send produce to another farmer", ["item", "qty", "to"])
    suite.check(prestige_class["direct"] and not gift_class["direct"],
                "direct mandatory mechanics and speculative social tools take different paths")
    direct_proposal = research_agent.capability_proposal({
        "capability": "prestige",
        "description": "League ranks first and prestige is the only way to advance while preserving lifetime produce.",
        "description_sha": "fixture",
        "required": [],
        "args": [],
    })
    suite.check(
        direct_proposal["change"]["kind"] == "capability_policy"
        and direct_proposal["files"] == [mechanics.POLICY_RELATIVE],
        "direct mechanics create implementation orders, not inert probe orders",
        direct_proposal,
    )

    with tempfile.TemporaryDirectory() as tmp:
        old_env = dict(os.environ)
        queue = str(Path(tmp) / "workorders.ndjson")
        try:
            os.environ["FARM_STATE_DIR"] = tmp
            from farm import provenance
            spec = {
                "hypothesis": "A bounded novel boost improves league progress.",
                "null_hypothesis": "The boost has no effect.",
                "falsifier": "League progress is unchanged.",
                "primary_metric": "league progress",
                "expected_improvement": 0.01,
            }
            registered = provenance.register_hypothesis(spec, ["discovery#1"])
            probe_order = workorders.submit(
                {"id": "research-capability-novel_boost", "severity": "opportunity",
                 "kind": "unused_capability", "tool": "novel_boost", "summary": "probe"},
                source="research_agent", intent="probe", files=["experiments/novel_boost_probe.py"],
                path=queue, provenance=dict(spec, hypothesis_id=registered["id"],
                                            discovery_evidence=["discovery#1"]),
            )
            workorders.resolve(probe_order["id"], workorders.PUBLISHED, path=queue)
            provenance.record_result(
                registered["id"], "supported", ["validation#2"], "holdout", {"effect": 0.05}
            )
            promoted_orders = research_agent.file_supported_implementations(queue)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        suite.check(
            len(promoted_orders) == 1
            and promoted_orders[0]["kind"] == "capability_policy"
            and promoted_orders[0]["files"] == [mechanics.POLICY_RELATIVE],
            "a supported probe result automatically creates its implementation order",
            promoted_orders,
        )

    section("contract reliance and progression-aware canary")
    relied = contract.reliance(str(PROJECT))
    suite.check(
        {"prestige", "resolve_crisis", "call_fbi"}.issubset(relied),
        "declarative live tools participate in contract drift severity",
        sorted(relied),
    )
    current_contract = json.loads((PROJECT / "state" / "contract.json").read_text())
    old_contract = {
        "tools": {"prestige": dict(current_contract["tools"]["prestige"])},
        "shapes": {},
        "reliance": {"prestige": {"args": [], "sites": ["experiments/capability_policies.py:league_prestige"]}},
    }
    new_contract = json.loads(json.dumps(old_contract))
    new_contract["tools"]["prestige"]["description_sha"] = "changed"
    new_contract["tools"]["prestige"]["description"] = "Semantics changed."
    drift = contract.diff(old_contract, new_contract)
    suite.check(
        len(drift) == 1 and drift[0]["severity"] == "degraded"
        and (drift[0].get("detail") or {}).get("policy_driven"),
        "a semantic change to an active policy cannot be absorbed as cosmetic",
        drift,
    )
    with tempfile.TemporaryDirectory() as tmp:
        history = Path(tmp) / "history.ndjson"
        store = Path(tmp) / "canary.json"
        audit = Path(tmp) / "canary.ndjson"
        baseline = [
            {"run": n, "animals": 10_000, "produce_per_min": 1_000.0,
             "interval_min": 5.0, "collected": 10}
            for n in range(1, 13)
        ]
        history.write_text("".join(json.dumps(row) + "\n" for row in baseline), encoding="utf-8")
        canary.arm(
            "rev-progress", "rev-old", change_class="strategy",
            store=str(store), history=str(audit), run_history=str(history),
        )
        action = {
            "tool": "prestige", "kind": "progression", "status": "verified",
            "policy_id": "league_prestige",
            "verification": {
                "ok": True,
                "checks": {
                    "league_level_increases": True,
                    "lifetime_produce_preserved": True,
                    "capacity_does_not_decrease": True,
                },
                "before": {"league_level": 7, "capacity": 10_000, "lifetime_produce": 2_000_000},
                "after": {"league_level": 8, "capacity": 11_250, "lifetime_produce": 2_000_000},
            },
        }
        candidate = [
            {"run": 13, "animals": 1, "produce_per_min": 1.0, "interval_min": 5.0,
             "collected": 1, "mechanic_actions": [action], "mechanic_failures": 0},
            {"run": 14, "animals": 400, "produce_per_min": 40.0, "interval_min": 5.0,
             "collected": 10, "mechanic_actions": [], "mechanic_failures": 0},
            {"run": 15, "animals": 800, "produce_per_min": 80.0, "interval_min": 5.0,
             "collected": 10, "mechanic_actions": [], "mechanic_failures": 0},
        ]
        history.write_text(
            "".join(json.dumps(row) + "\n" for row in baseline + candidate),
            encoding="utf-8",
        )
        verdict = canary.evaluate(str(store), str(history))
        suite.check(
            verdict["status"] == canary.HEALTHY
            and (verdict.get("efficacy") or {}).get("metric") == "league_level_then_lifetime_produce",
            "verified league progress beats the intentionally reset herd's secondary rate comparison",
            verdict,
        )
        failed_row = dict(candidate[0], mechanic_failures=1)
        suite.check(canary._breakage(failed_row) == "adaptive_mechanic_verification_failure",
                    "failed mechanic verification is decisive canary breakage")

    print()
    if suite.failures:
        print("MECHANICS TEST FAILED: %d of %d checks" % (len(suite.failures), suite.checks))
        for failure in suite.failures:
            print("  - " + failure)
        return 1
    print("MECHANICS TEST PASSED: %d checks" % suite.checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
