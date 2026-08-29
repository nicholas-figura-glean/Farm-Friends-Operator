"""Declarative autonomous policies for server-advertised game mechanics.

This file is intentionally data only. ``farm.mechanics`` parses it with ``ast`` and
``literal_eval`` instead of importing it, then validates every entry against the
captured MCP contract and hard runtime ceilings. The author agent may update this
file; it cannot execute code, call tools, weaken verification, or change budgets.
"""

CAPABILITY_POLICIES = [
    {
        "id": "league_prestige",
        "tool": "prestige",
        "kind": "progression",
        "enabled": True,
        "priority": 100,
        "max_calls_per_cycle": 8,
        "max_cost_fraction": 0.0,
        "contract": {
            "description_sha": "16241be9cffd",
            "required": [],
        },
        "evidence_class": "direct_mechanism",
        "evidence_refs": [
            "state/contract.json#tools.prestige.description_sha=16241be9cffd",
            "state/raw/latest/list_farm_state.txt#prestige-available",
            "state/raw/latest/leaderboard.txt#league-first-ordering",
        ],
        "verify": [
            "league_level_increases",
            "lifetime_produce_preserved",
            "capacity_does_not_decrease",
        ],
        "reason": "The server says league is ranked before lifetime produce and prestige is the only way to advance it; tier changes may retain the current major-league cap.",
    },
    {
        "id": "bounded_crisis_resolution",
        "tool": "resolve_crisis",
        "kind": "crisis",
        "enabled": True,
        "priority": 210,
        "max_calls_per_cycle": 1,
        "max_cost_fraction": 0.45,
        "contract": {
            "description_sha": "5c4c7c93a10a",
            "required": [],
        },
        "evidence_class": "direct_mechanism",
        "evidence_refs": [
            "state/contract.json#tools.resolve_crisis.description_sha=5c4c7c93a10a",
            "visit_farm:Deep#rustlers-cost-35pct",
            "visit_farm:Guillermo-G#crop-blight-cost-30pct",
        ],
        "verify": [
            "crisis_cleared",
            "cost_within_declared_fraction",
        ],
        "activation": {
            "allow_when_progression_pending": True,
            "minimum_animal_capacity_fraction": 0.90,
            "minimum_plot_capacity_fraction": 0.50,
            "feed_runway_multiplier": 2.0,
        },
        "reason": "Resolve an observed active disaster only when protected production assets justify its declared 30-45% coin cost.",
    },
    {
        "id": "bounded_alien_response",
        "tool": "call_fbi",
        "kind": "crisis",
        "enabled": True,
        "priority": 220,
        "max_calls_per_cycle": 1,
        "max_cost_fraction": 0.80,
        "contract": {
            "description_sha": "3f96b400faf5",
            "required": [],
        },
        "evidence_class": "direct_mechanism",
        "evidence_refs": [
            "state/contract.json#tools.call_fbi.description_sha=3f96b400faf5",
            "state/meta.json#seen-risk-events-alien-abduction",
        ],
        "verify": [
            "crisis_cleared",
            "cost_within_declared_fraction",
        ],
        "activation": {
            "allow_when_progression_pending": True,
            "minimum_animal_capacity_fraction": 0.99,
            "minimum_plot_capacity_fraction": 1.0,
            "feed_runway_multiplier": 0.0,
        },
        "reason": "The FBI call is reserved for a confirmed active invasion when an imminent prestige makes the otherwise extreme 80% coin cost transient, or a nearly full herd is exposed.",
    },
]
