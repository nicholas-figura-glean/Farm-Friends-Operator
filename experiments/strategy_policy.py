"""Literal strategy decisions promoted from regime-scoped evidence.

Parsed with ``ast.literal_eval`` by ``farm.strategy``; never imported or executed.
"""

STRATEGY_POLICY = {
    "schema_version": 1,
    "animal": {
        "growth_kind": "chicken",
        "capped_replacement_kind": "beehive",
        "replacement_at_capacity_fraction": 0.90,
        "minimum_wildflowers_for_replacement": 8,
        "contract_description_sha": "9b000e15051c",
        "hypothesis_id": "hyp-8e6877445d5c3058",
        "result_node": "result-727fdc437c37ff26",
        "evidence_class": "holdout",
        "evidence_refs": [
            "state/beehive_probe.json#runs=642-644-steady-state-ratio-above-1.24",
            "state/dual_cap_audit.json#cohort=b1b3b41547ebefba291c0be12195454f524dc9f3f265935896decfb1bf7565cc",
            "state/history.ndjson#runs=1186-1235-capped-mixed-species",
        ],
    },
    "plots": {
        "minimum_wildflowers": 8,
        "food_crop_kind": None,
        "target_capacity_fraction": 0.0,
        "max_plant_per_cycle": 0,
        "status": "disabled_for_league_score",
        "hypothesis_id": "hyp-48c3ef4f083ecd99",
        "result_node": "result-ac64fbbe925c5017",
        "evidence_class": "intervention",
        "evidence_refs": [
            "state/dual_cap_probe.json#current-timers-and-yields-supported",
            "state/crop_score_probe.json#corrected-residual=0",
            "state/tool_calls.ndjson#runs=1391-1392-animal-collections-and-wheat-harvest",
        ],
    },
}
