"""Protected loader for literal, evidence-linked game strategy decisions."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT = Path(__file__).resolve().parent.parent
POLICY_RELATIVE = "experiments/strategy_policy.py"
POLICY_FILE = PROJECT / POLICY_RELATIVE
CONTRACT = PROJECT / "state" / "contract.json"
CONTRACT_LIVE = PROJECT / "state" / "contract_live.json"
SCHEMA_VERSION = 1

SAFE_DEFAULT = {
    "schema_version": SCHEMA_VERSION,
    "animal": {
        "growth_kind": "chicken",
        "capped_replacement_kind": "chicken",
        "replacement_at_capacity_fraction": 1.0,
        "minimum_wildflowers_for_replacement": 8,
    },
    "plots": {
        "minimum_wildflowers": 8,
        "food_crop_kind": None,
        "target_capacity_fraction": 0.0,
        "max_plant_per_cycle": 0,
        "status": "disabled_without_supported_evidence",
    },
}


def _json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _literal(path: Path) -> tuple[Dict[str, Any], list[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return {}, ["strategy policy unreadable: %s" % str(exc)[:160]]
    assignment = None
    errors: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            if isinstance(node.value.value, str):
                continue
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if names == ["STRATEGY_POLICY"] and assignment is None:
                assignment = node.value
                continue
        errors.append("strategy policy may contain only a docstring and STRATEGY_POLICY literal")
    if assignment is None:
        return {}, errors + ["STRATEGY_POLICY assignment is missing"]
    try:
        value = ast.literal_eval(assignment)
    except (TypeError, ValueError, SyntaxError) as exc:
        return {}, errors + ["STRATEGY_POLICY is not literal data: %s" % str(exc)[:160]]
    if not isinstance(value, dict):
        return {}, errors + ["STRATEGY_POLICY must be an object"]
    return dict(value), errors


def load(
    path: Optional[Path] = None,
    contract_snapshot: Optional[Dict[str, Any]] = None,
    verify_lineage: bool = True,
) -> Dict[str, Any]:
    target = Path(path) if path is not None else POLICY_FILE
    declared, errors = _literal(target)
    contract = contract_snapshot if contract_snapshot is not None else (
        _json(CONTRACT_LIVE) or _json(CONTRACT)
    )
    if int(declared.get("schema_version") or 0) != SCHEMA_VERSION:
        errors.append("strategy policy schema version is unsupported")
    animal = declared.get("animal") if isinstance(declared.get("animal"), dict) else {}
    plots = declared.get("plots") if isinstance(declared.get("plots"), dict) else {}

    adopt = ((contract.get("tools") or {}).get("adopt_animal") or {})
    plant = ((contract.get("tools") or {}).get("plant") or {})
    animal_enum = set((((adopt.get("args") or {}).get("kind") or {}).get("enum") or []))
    crop_enum = set((((plant.get("args") or {}).get("kind") or {}).get("enum") or []))
    growth_kind = str(animal.get("growth_kind") or "")
    replacement_kind = str(animal.get("capped_replacement_kind") or "")
    if growth_kind not in animal_enum or replacement_kind not in animal_enum:
        errors.append("animal strategy kind is absent from the captured adopt contract")
    if str(animal.get("contract_description_sha") or "") != str(adopt.get("description_sha") or ""):
        errors.append("animal strategy contract fingerprint is stale")
    try:
        threshold = float(animal.get("replacement_at_capacity_fraction"))
    except (TypeError, ValueError):
        threshold = -1.0
    if not 0.50 <= threshold <= 1.0:
        errors.append("replacement capacity threshold must be between 0.50 and 1.0")
    min_flowers = int(animal.get("minimum_wildflowers_for_replacement") or 0)
    if not 0 <= min_flowers <= 100:
        errors.append("minimum wildflowers is outside the bounded strategy range")

    hypothesis_id = str(animal.get("hypothesis_id") or "")
    result_node = str(animal.get("result_node") or "")
    evidence_class = str(animal.get("evidence_class") or "")
    if not hypothesis_id or not result_node or evidence_class not in {"holdout", "intervention"}:
        errors.append("animal strategy lacks causal result lineage")
    elif verify_lineage:
        try:
            from . import provenance

            result = provenance.latest_result(hypothesis_id) or {}
            if (
                result.get("node") != result_node
                or result.get("status") not in {"supported", "accepted", "passed"}
                or result.get("evidence_class") != evidence_class
            ):
                errors.append("animal strategy result lineage is not currently supported")
        except Exception as exc:  # noqa: BLE001
            errors.append("animal strategy lineage could not be verified: %s" % str(exc)[:120])

    crop = plots.get("food_crop_kind")
    if crop is not None and str(crop) not in crop_enum - {"wildflowers"}:
        errors.append("food crop strategy kind is absent from the captured plant contract")
    try:
        plot_target = float(plots.get("target_capacity_fraction") or 0.0)
        max_plant = int(plots.get("max_plant_per_cycle") or 0)
    except (TypeError, ValueError):
        plot_target, max_plant = -1.0, -1
    if not 0.0 <= plot_target <= 1.0 or not 0 <= max_plant <= 5_000:
        errors.append("plot strategy exceeds target or per-cycle planting bounds")
    plot_hypothesis = str(plots.get("hypothesis_id") or "")
    plot_result_node = str(plots.get("result_node") or "")
    plot_evidence_class = str(plots.get("evidence_class") or "")
    if crop is not None and (not plot_hypothesis or not plot_result_node):
        errors.append("active food crop strategy lacks result lineage")
    if plots.get("status") == "disabled_for_league_score":
        if not plot_hypothesis or not plot_result_node or plot_evidence_class != "intervention":
            errors.append("disabled crop-score policy lacks falsification lineage")
        elif verify_lineage:
            try:
                from . import provenance

                result = provenance.latest_result(plot_hypothesis) or {}
                if result.get("node") != plot_result_node or result.get("status") != "falsified":
                    errors.append("crop-score disablement is not linked to the durable falsified result")
            except Exception as exc:  # noqa: BLE001
                errors.append("crop-score lineage could not be verified: %s" % str(exc)[:120])

    material = json.dumps(declared, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "declared": declared,
        "effective": SAFE_DEFAULT if errors else declared,
        "errors": sorted(set(errors)),
        "fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest()[:16],
        "path": str(target),
    }


def animal_kind(
    animal_count: int,
    capacity: Optional[int],
    crop_counts: Optional[Dict[str, int]] = None,
    loaded: Optional[Dict[str, Any]] = None,
) -> str:
    policy = (loaded or load()).get("effective") or SAFE_DEFAULT
    animal = policy.get("animal") or {}
    growth = str(animal.get("growth_kind") or "chicken")
    replacement = str(animal.get("capped_replacement_kind") or growth)
    threshold = float(animal.get("replacement_at_capacity_fraction") or 1.0)
    min_flowers = int(animal.get("minimum_wildflowers_for_replacement") or 0)
    flowers = int((crop_counts or {}).get("wildflowers") or 0)
    utilization = animal_count / float(capacity) if capacity else 0.0
    if utilization >= threshold and flowers >= min_flowers:
        return replacement
    return growth


def plot_policy(loaded: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    policy = (loaded or load()).get("effective") or SAFE_DEFAULT
    return dict(policy.get("plots") or {})


def flower_plan(farm: Any, loaded: Optional[Dict[str, Any]] = None) -> int:
    """Restore only the contract-declared whole-farm beehive bonus floor."""
    plots = plot_policy(loaded)
    minimum = max(0, int(plots.get("minimum_wildflowers") or 0))
    current = int((getattr(farm, "counts_by_crop", {}) or {}).get("wildflowers") or 0)
    capacity = getattr(farm, "plot_capacity", None)
    room = max(0, int(capacity) - int(getattr(farm, "plot_count", 0))) if capacity else minimum
    return min(max(0, minimum - current), room, 8)


def parameters() -> Dict[str, Any]:
    loaded = load()
    policy = loaded.get("effective") or SAFE_DEFAULT
    animal = policy.get("animal") or {}
    plots = policy.get("plots") or {}
    return {
        "strategy_policy_fingerprint": loaded.get("fingerprint"),
        "growth_kind": animal.get("growth_kind"),
        "capped_replacement_kind": animal.get("capped_replacement_kind"),
        "replacement_at_capacity_fraction": animal.get("replacement_at_capacity_fraction"),
        "minimum_wildflowers": plots.get("minimum_wildflowers"),
        "food_crop_kind": plots.get("food_crop_kind"),
        "food_crop_target_fraction": plots.get("target_capacity_fraction"),
        "max_plant_per_cycle": plots.get("max_plant_per_cycle"),
    }


def status() -> Dict[str, Any]:
    loaded = load()
    return {
        "fingerprint": loaded.get("fingerprint"),
        "effective": loaded.get("effective"),
        "errors": loaded.get("errors"),
    }
