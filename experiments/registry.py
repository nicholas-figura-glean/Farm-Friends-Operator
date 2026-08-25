"""Declarative bounded-probe registry.

Mutating probes are never autonomous. The supervisor may schedule only entries
marked both read_only and autonomous, and every execution still runs under the
farm lock with a wall-time ceiling.
"""

PROBES = {
    "counterfactual_sweep": {
        "hypothesis": "A neighbouring decision constant changes historical outcomes.",
        "question_classes": ["strategy_stale", "knob_age", "policy_drift"],
        "subject_patterns": ["farm", "growth", "policy", "output_linear"],
        "command": ["run.py", "--sweep"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 60},
        "stop_condition": "pure replay completes or wall-time expires",
        "evidence_destination": "state/audits.ndjson",
    },
    "endgame_replay": {
        "hypothesis": "A safe affordable herd target restores or preserves the objective path.",
        "question_classes": ["rank_lost", "no_path_to_win", "win_eta"],
        "command": ["experiments/endgame.py"],
        "read_only": True,
        "autonomous": True,
        "budget": {"coins": 0, "calls": 0, "wall_seconds": 30},
        "stop_condition": "simulation table completes or wall-time expires",
        "evidence_destination": "state/experiments.ndjson",
    },
    "species_mix": {
        "hypothesis": "An alternative species beats chickens on lifetime produce per total cost.",
        "question_classes": ["model_drift"],
        "command": ["experiments/species_probe.py"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 10_000, "calls": 500, "wall_seconds": 300},
        "stop_condition": "one bounded mixed-species batch and its observation window",
        "evidence_destination": "state/experiments.ndjson and claim strategy.chicken_engine",
        "status": "historically answered; explicit re-probe only",
    },
    "beehive_scale": {
        "hypothesis": "A beehive beats a chicken on lifetime-produce proxy per latency-limited adoption call at current scale.",
        "question_classes": ["model_drift", "strategy_stale"],
        "command": ["experiments/beehive_probe.py", "--execute"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 80_000, "calls": 1_010, "wall_seconds": 2_400},
        "stop_condition": "1,000 bounded adoptions and five healthy verified observation windows",
        "evidence_destination": "state/beehive_probe.json and state/experiments.ndjson",
        "status": "completed at baseline run 639; chicken retained because promotion gate failed",
    },
    "feed_economics": {
        "hypothesis": "Current feeding cadence maximizes net score production without crossing hunger risk.",
        "question_classes": ["model_drift", "hunger_wall"],
        "command": ["experiments/feed_economics.py"],
        "read_only": False,
        "autonomous": False,
        "budget": {"coins": 50_000, "calls": 30, "wall_seconds": 600},
        "stop_condition": "bounded cadence comparison completes or hunger reaches the alarm guard",
        "evidence_destination": "state/experiments.ndjson and safety.bulk_husbandry",
    },
}
