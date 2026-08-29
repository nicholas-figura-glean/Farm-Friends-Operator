"""Bounded execution for newly advertised game mechanics.

The editable policy surface lives in ``experiments/capability_policies.py`` and is
literal data, never imported. This trusted module parses that assignment with
``ast.literal_eval``, validates it against the captured MCP contract and hard
ceilings, decides from parsed farm state, performs at most the declared one-shot
calls, and verifies the resulting state. A model can propose a policy entry; it
cannot execute Python or weaken this executor.
"""

from __future__ import annotations

import ast
import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from . import parse, rules

PROJECT = Path(__file__).resolve().parent.parent
POLICY_SCHEMA_VERSION = 1
POLICY_RELATIVE = "experiments/capability_policies.py"
POLICY_FILE = PROJECT / POLICY_RELATIVE
CONTRACT_FILE = PROJECT / "state" / "contract.json"
OBSERVED_CONTRACT_FILE = PROJECT / "state" / "contract_live.json"
EXPANSION_LOCK = PROJECT / "state" / ".expand.lock"

ALLOWED_KINDS = {"progression", "crisis"}
EVIDENCE_CLASSES = {"direct_mechanism", "intervention", "holdout"}
# Existing routine/social/economy tools have dedicated owners and richer argument
# policies. The adaptive lane is only for newly advertised, no-argument mechanics.
DISALLOWED_TOOLS = {
    "adopt_animal", "buy_feed", "collect_produce", "farm_events",
    "feed_animals", "gift", "harvest", "leaderboard", "list_farm",
    "name_animal", "plant", "propose_trade", "respond_to_trade", "sell",
    "visit_farm",
}
HARD_MAX_CALLS = {"progression": 8, "crisis": 1}
HARD_MAX_COST = {"prestige": 0.0, "resolve_crisis": 0.45, "call_fbi": 0.80}
PROGRESSION_VERIFY = {
    "league_level_increases", "lifetime_produce_preserved", "capacity_does_not_decrease",
}
CRISIS_VERIFY = {"crisis_cleared", "cost_within_declared_fraction"}
ANIMAL_CRISES = {"wolf_pack", "wolves", "rustlers", "barn_fire"}
FIELD_CRISES = {"crop_blight", "locust_swarm"}
INVENTORY_CRISES = {"barn_fire"}
HIGH_IMPACT_CRISES = {"barn_fire"}


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _literal_policy_rows(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Read exactly one literal assignment without executing the editable file."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (FileNotFoundError, OSError, SyntaxError) as exc:
        return [], ["policy file is unreadable: %s" % str(exc)[:180]]

    assignment: Optional[ast.AST] = None
    errors: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), (ast.Str, ast.Constant)):
            value = getattr(node, "value", None)
            if isinstance(value, ast.Constant) and not isinstance(value.value, str):
                errors.append("policy file contains a non-string expression")
            continue
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if names == ["CAPABILITY_POLICIES"] and assignment is None:
                assignment = node.value
                continue
        errors.append("policy file may contain only a docstring and CAPABILITY_POLICIES literal")
    if assignment is None:
        errors.append("CAPABILITY_POLICIES assignment is missing")
        return [], errors
    try:
        value = ast.literal_eval(assignment)
    except (TypeError, ValueError, SyntaxError) as exc:
        errors.append("CAPABILITY_POLICIES is not literal data: %s" % str(exc)[:160])
        return [], errors
    if not isinstance(value, list):
        return [], errors + ["CAPABILITY_POLICIES must be a list"]
    rows = [dict(item) for item in value if isinstance(item, dict)]
    if len(rows) != len(value):
        errors.append("every capability policy must be an object")
    return rows, errors


def _contract_tools(snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    source = snapshot if snapshot is not None else (
        _read_json(OBSERVED_CONTRACT_FILE) or _read_json(CONTRACT_FILE)
    )
    tools = source.get("tools") if isinstance(source, dict) else None
    return {str(key): dict(value) for key, value in (tools or {}).items() if isinstance(value, dict)}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _validate_policy(row: Dict[str, Any], tools: Dict[str, Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    identity = str(row.get("id") or "")
    tool = str(row.get("tool") or "")
    kind = str(row.get("kind") or "")
    prefix = identity or tool or "unnamed policy"
    if not re.match(r"^[a-z][a-z0-9_]{2,80}$", identity):
        errors.append("%s has an invalid id" % prefix)
    if not re.match(r"^[a-z][a-z0-9_]{1,80}$", tool):
        errors.append("%s has an invalid tool" % prefix)
    if tool in DISALLOWED_TOOLS:
        errors.append("%s targets routine/social tool %s" % (prefix, tool))
    if kind not in ALLOWED_KINDS:
        errors.append("%s has unsupported kind %s" % (prefix, kind))
    if row.get("enabled") is not True:
        return errors

    maximum_calls = int(row.get("max_calls_per_cycle") or 0)
    if maximum_calls < 1 or maximum_calls > HARD_MAX_CALLS.get(kind, 0):
        errors.append("%s call bound %d exceeds %s ceiling" % (prefix, maximum_calls, kind))
    fraction = _float(row.get("max_cost_fraction"), -1.0)
    hard_cost = HARD_MAX_COST.get(tool, 0.50 if kind == "crisis" else 0.0)
    if fraction < 0 or fraction > hard_cost:
        errors.append("%s cost fraction %.3f exceeds hard %.3f" % (prefix, fraction, hard_cost))

    evidence_class = str(row.get("evidence_class") or "")
    refs = [str(value) for value in row.get("evidence_refs") or [] if str(value)]
    if evidence_class not in EVIDENCE_CLASSES:
        errors.append("%s has unsupported evidence class" % prefix)
    if not refs:
        errors.append("%s has no evidence references" % prefix)
    if evidence_class == "direct_mechanism" and not any("contract" in ref for ref in refs):
        errors.append("%s direct mechanism has no contract evidence" % prefix)

    declared = row.get("contract") if isinstance(row.get("contract"), dict) else {}
    live = tools.get(tool)
    if live is None:
        errors.append("%s is absent from the captured contract" % tool)
    else:
        expected_sha = str(declared.get("description_sha") or "")
        if not expected_sha or expected_sha != str(live.get("description_sha") or ""):
            errors.append("%s description fingerprint does not match the captured contract" % tool)
        required = sorted(str(value) for value in live.get("required") or [])
        expected_required = sorted(str(value) for value in declared.get("required") or [])
        if required != expected_required or required:
            errors.append("%s adaptive actions must have no required arguments" % tool)
        semantic = classify_capability(tool, str(live.get("description") or ""), required)
        if evidence_class == "direct_mechanism":
            if not semantic.get("direct") or semantic.get("kind") != kind:
                errors.append("%s contract does not directly establish a %s mechanic" % (tool, kind))
        else:
            identity = str(row.get("hypothesis_id") or "")
            result_node = str(row.get("result_node") or "")
            if not identity or not result_node:
                errors.append("%s causal policy is missing hypothesis/result lineage" % prefix)
            else:
                try:
                    from . import provenance

                    result = provenance.latest_result(identity) or {}
                    if (
                        result.get("node") != result_node
                        or result.get("status") not in {"supported", "accepted", "passed"}
                        or result.get("evidence_class") != evidence_class
                    ):
                        errors.append("%s has no matching durable supporting result" % prefix)
                except Exception as exc:  # noqa: BLE001
                    errors.append("%s result lineage could not be verified: %s" % (prefix, str(exc)[:120]))

    checks = set(str(value) for value in row.get("verify") or [])
    needed = PROGRESSION_VERIFY if kind == "progression" else CRISIS_VERIFY
    if not needed.issubset(checks):
        errors.append("%s is missing verification: %s" % (prefix, ", ".join(sorted(needed - checks))))
    return errors


def load_policies(
    path: Optional[Path] = None,
    contract_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return validated active policy rows plus bounded diagnostics."""
    target = Path(path) if path is not None else POLICY_FILE
    rows, errors = _literal_policy_rows(target)
    tools = _contract_tools(contract_snapshot)
    seen: Set[str] = set()
    accepted: List[Dict[str, Any]] = []
    for row in rows:
        identity = str(row.get("id") or "")
        if identity in seen:
            errors.append("duplicate capability policy id: %s" % identity)
            continue
        seen.add(identity)
        row_errors = _validate_policy(row, tools)
        if row_errors:
            errors.extend(row_errors)
            continue
        if row.get("enabled") is True:
            accepted.append(row)
    accepted.sort(key=lambda item: (-int(item.get("priority") or 0), str(item.get("id") or "")))
    material = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "policies": accepted,
        "declared_policies": rows,
        "declared": len(rows),
        "errors": sorted(set(errors)),
        "fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest()[:16],
        "path": str(target),
    }


def active_tools(result: Optional[Dict[str, Any]] = None) -> Set[str]:
    loaded = result or load_policies()
    return {str(row.get("tool")) for row in loaded.get("policies") or []}


def handled_risk_kinds(result: Optional[Dict[str, Any]] = None) -> Set[str]:
    loaded = result or load_policies()
    if not any(row.get("kind") == "crisis" for row in loaded.get("policies") or []):
        return set()
    return ANIMAL_CRISES | FIELD_CRISES | INVENTORY_CRISES | {"alien_invasion", "aliens"}


def policy_reliance(root: str = ".") -> Dict[str, Dict[str, Any]]:
    """Static contract reliance contributed by validated declarative actions."""
    path = Path(root) / POLICY_RELATIVE
    observed = Path(root) / "state" / "contract_live.json"
    contract_path = observed if observed.is_file() else Path(root) / "state" / "contract.json"
    result = load_policies(path=path, contract_snapshot=_read_json(contract_path))
    return {
        str(row["tool"]): {
            "args": [],
            "sites": ["%s:%s" % (POLICY_RELATIVE, row.get("id"))],
        }
        for row in result.get("policies") or []
    }


def classify_capability(name: str, description: str, required: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Classify direct objective mechanics separately from speculative tools."""
    tool = str(name or "")
    text = str(description or "").lower()
    no_args = not list(required or [])
    if no_args and (
        tool == "prestige"
        or ("league" in text and ("only way" in text or "rank" in text) and "lifetime produce" in text)
    ):
        return {
            "class": "direct_progression",
            "kind": "progression",
            "direct": True,
            "primary_metric": "verified league-level increase with lifetime produce preserved and capacity nondecreasing",
            "falsifier": "The action fails to increase league level, reduces lifetime produce, or lowers capacity.",
        }
    if no_args and (
        tool in {"resolve_crisis", "call_fbi"}
        or ("active" in text and ("disaster" in text or "invasion" in text) and "%" in text)
    ):
        return {
            "class": "direct_crisis",
            "kind": "crisis",
            "direct": True,
            "primary_metric": "active crisis cleared within its declared coin fraction while operating reserves remain",
            "falsifier": "The crisis remains active, cost exceeds the declared fraction, or protected reserves are crossed.",
        }
    return {
        "class": "exploratory",
        "kind": "probe",
        "direct": False,
        "primary_metric": "lifetime produce gained per bounded call or coin",
        "falsifier": "The bounded probe shows no objective gain or violates its safety budget.",
    }


def _capacity_fraction(farm: parse.Farm) -> float:
    if not farm.capacity:
        return 0.0
    return float(farm.animal_count) / float(farm.capacity)


def _plot_fraction(farm: parse.Farm) -> float:
    if not farm.plot_capacity:
        return 0.0
    return float(farm.plot_count) / float(farm.plot_capacity)


def _operating_coin_floor(farm: parse.Farm) -> int:
    gap = max(0, int(farm.capacity or farm.animal_count) - farm.animal_count)
    immediate = min(gap, rules.MAX_ADOPTIONS_PER_RUN)
    replacement_kind = rules.adoption_kind(
        farm.animal_count, farm.capacity, farm.counts_by_crop
    )
    per_animal = rules.ANIMAL_COST[replacement_kind] + rules.FEED_PER_ANIMAL_RESERVE
    return rules.RISK_COIN_RESERVE + immediate * per_animal


def _crisis_material(farm: parse.Farm, policy: Dict[str, Any]) -> Tuple[bool, str]:
    crisis = farm.crisis
    if crisis is None:
        return False, "no active crisis"
    activation = policy.get("activation") if isinstance(policy.get("activation"), dict) else {}
    if farm.prestige_available and activation.get("allow_when_progression_pending") is True:
        return True, "clear the active crisis before an already-earned prestige resets coins"
    kind = crisis.kind
    if kind in HIGH_IMPACT_CRISES:
        return True, "active barn fire directly destroys animals; preserve irreversible score capacity while coins remain replaceable"
    if kind in ANIMAL_CRISES or kind in {"alien_invasion", "aliens"}:
        threshold = _float(activation.get("minimum_animal_capacity_fraction"), 1.0)
        if _capacity_fraction(farm) >= threshold:
            return True, "active animal-loss crisis threatens a %.1f%% full barn" % (_capacity_fraction(farm) * 100.0)
        return False, "animal-loss crisis is cheaper to replace below the capacity threshold"
    if kind in FIELD_CRISES:
        threshold = _float(activation.get("minimum_plot_capacity_fraction"), 1.0)
        if _plot_fraction(farm) >= threshold:
            return True, "active field-loss crisis threatens persistent plots"
        return False, "field utilization is below the resolution threshold"
    if kind in INVENTORY_CRISES:
        multiplier = _float(activation.get("feed_runway_multiplier"), 0.0)
        runway = rules.feed_buffer_minutes(farm.feed, farm.animal_count)
        if multiplier > 0 and runway <= rules.FEED_BUFFER_MIN_MINUTES * multiplier:
            return True, "active inventory-loss crisis threatens the feed runway"
        return False, "inventory runway remains above the resolution threshold"
    return False, "unclassified crisis target is not eligible for automatic spend"


def next_decision(
    farm: parse.Farm,
    live_tools: Iterable[str],
    run: Optional[int] = None,
    attempted: Optional[Dict[str, int]] = None,
    loaded: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the highest-priority due action and every held reason."""
    policies = loaded or load_policies()
    available = set(str(value) for value in live_tools)
    counts = dict(attempted or {})
    held: List[Dict[str, Any]] = []
    for policy in policies.get("policies") or []:
        identity = str(policy["id"])
        tool = str(policy["tool"])
        maximum = int(policy.get("max_calls_per_cycle") or 0)
        if counts.get(identity, 0) >= maximum:
            held.append({"id": identity, "tool": tool, "reason": "per-cycle call bound reached"})
            continue
        if tool not in available:
            held.append({"id": identity, "tool": tool, "reason": "tool absent from live handshake"})
            continue
        if policy.get("kind") == "progression":
            if not farm.prestige_available:
                continue
            return {
                "decision": {
                    "policy_id": identity,
                    "tool": tool,
                    "kind": "progression",
                    "reason": str(policy.get("reason") or "verified progression is available"),
                    "max_cost_fraction": 0.0,
                    "run": run,
                },
                "held": held,
                "errors": list(policies.get("errors") or []),
            }
        if policy.get("kind") == "crisis":
            crisis = farm.crisis
            if crisis is None or crisis.resolver != tool:
                continue
            fraction = float(crisis.cost_fraction)
            maximum_fraction = _float(policy.get("max_cost_fraction"), 0.0)
            if fraction > maximum_fraction:
                held.append({"id": identity, "tool": tool, "reason": "declared cost exceeds policy bound"})
                continue
            material, reason = _crisis_material(farm, policy)
            if not material:
                held.append({"id": identity, "tool": tool, "reason": reason})
                continue
            remaining = int(math.floor(farm.coins * (1.0 - fraction)))
            floor = _operating_coin_floor(farm)
            if remaining < floor and not farm.prestige_available:
                held.append({
                    "id": identity,
                    "tool": tool,
                    "reason": "post-action coins %d would cross operating floor %d" % (remaining, floor),
                })
                continue
            return {
                "decision": {
                    "policy_id": identity,
                    "tool": tool,
                    "kind": "crisis",
                    "crisis_kind": crisis.kind,
                    "declared_cost_fraction": fraction,
                    "max_cost_fraction": maximum_fraction,
                    "operating_coin_floor": floor,
                    "reason": reason,
                    "run": run,
                },
                "held": held,
                "errors": list(policies.get("errors") or []),
            }
    return {"decision": None, "held": held, "errors": list(policies.get("errors") or [])}


def farm_snapshot(farm: parse.Farm) -> Dict[str, Any]:
    return {
        "league": farm.league,
        "league_level": farm.league_level,
        "lifetime_produce": farm.lifetime_produce,
        "animals": farm.animal_count,
        "capacity": farm.capacity,
        "plots": farm.plot_count,
        "plot_capacity": farm.plot_capacity,
        "coins": farm.coins,
        "feed": farm.feed,
        "prestige_available": farm.prestige_available,
        "crisis_kind": farm.crisis.kind if farm.crisis else None,
        "crisis_resolver": farm.crisis.resolver if farm.crisis else None,
        "crisis_cost_fraction": farm.crisis.cost_fraction if farm.crisis else None,
    }


def verify_action(decision: Dict[str, Any], before: parse.Farm, after: parse.Farm) -> Dict[str, Any]:
    checks: Dict[str, bool] = {}
    kind = str(decision.get("kind") or "")
    if kind == "progression":
        checks = {
            "league_level_increases": (
                isinstance(before.league_level, int)
                and isinstance(after.league_level, int)
                and after.league_level > before.league_level
            ),
            "lifetime_produce_preserved": (
                isinstance(before.lifetime_produce, int)
                and isinstance(after.lifetime_produce, int)
                and after.lifetime_produce >= before.lifetime_produce
            ),
            "capacity_does_not_decrease": (
                isinstance(before.capacity, int)
                and isinstance(after.capacity, int)
                and after.capacity >= before.capacity
            ),
        }
    elif kind == "crisis":
        spent = max(0, before.coins - after.coins)
        allowed = int(math.ceil(before.coins * _float(decision.get("max_cost_fraction"), 0.0))) + 1
        checks = {
            "crisis_cleared": after.crisis is None,
            "cost_within_declared_fraction": spent <= allowed,
        }
    else:
        checks = {"known_kind": False}
    return {
        "ok": bool(checks) and all(checks.values()),
        "checks": checks,
        "before": farm_snapshot(before),
        "after": farm_snapshot(after),
    }


def invoke(client: Any, tool: str) -> str:
    """Perform one non-retried, no-argument mechanic call.

    Literal branches keep current tools visible to static contract/topology scans.
    The validated generic branch lets a future no-argument progression/crisis policy
    use the same executor without granting it arbitrary arguments.
    """
    if tool == "prestige":
        return client.call("prestige", _transport_retries=1)
    if tool == "resolve_crisis":
        return client.call("resolve_crisis", _transport_retries=1)
    if tool == "call_fbi":
        return client.call("call_fbi", _transport_retries=1)
    if tool in DISALLOWED_TOOLS:
        raise ValueError("adaptive executor refuses routine/social tool %s" % tool)
    return client.call(tool, _transport_retries=1)


@contextlib.contextmanager
def exclusive_expansion_lock(path: Optional[Path] = None) -> Iterator[bool]:
    """Prevent prestige/crisis mutation from racing the independent expand worker."""
    target = Path(path) if path is not None else EXPANSION_LOCK
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = open(target, "a+", encoding="utf-8")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except IOError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def status() -> Dict[str, Any]:
    loaded = load_policies()
    return {
        "fingerprint": loaded.get("fingerprint"),
        "declared": loaded.get("declared"),
        "active": [
            {
                "id": row.get("id"), "tool": row.get("tool"), "kind": row.get("kind"),
                "max_calls_per_cycle": row.get("max_calls_per_cycle"),
                "max_cost_fraction": row.get("max_cost_fraction"),
                "evidence_class": row.get("evidence_class"),
            }
            for row in loaded.get("policies") or []
        ],
        "errors": list(loaded.get("errors") or []),
    }
