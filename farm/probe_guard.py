"""Fail-closed capability and budget boundary for model-authored probes.

Probe registry entries request authority. This protected module decides what can
actually cross the MCP transport boundary, reserves the worst-case cost before
forwarding, and keeps credentials in the trusted parent process. Probe workers
receive only two inherited pipe descriptors; they never receive the endpoint.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import rules

SCHEMA_VERSION = 1
MAX_FRAME_BYTES = 1_000_000
MAX_CALLS = 2_000
MAX_COINS = 100_000
MAX_WALL_SECONDS = 3_600
MAX_BROKER_CALL_SECONDS = 120
REQUEST_FD_ENV = "FARM_MCP_BROKER_REQUEST_FD"
RESPONSE_FD_ENV = "FARM_MCP_BROKER_RESPONSE_FD"
ENFORCEMENT_ENV = "FARM_PROBE_ENFORCEMENT"


class AuthorizationError(RuntimeError):
    """A probe requested authority outside its protected grant."""


# Cost and mutability are protected facts. Editable registry data can narrow
# these profiles but can never create a new capability or make a mutation read-only.
TOOL_PROFILES: Dict[str, Dict[str, Any]] = {
    "list_farm": {"read_only": True, "cost": "zero"},
    "leaderboard": {"read_only": True, "cost": "zero"},
    "get_scoreboard": {"read_only": True, "cost": "zero"},
    "visit_farm": {"read_only": True, "cost": "zero"},
    "plant": {"read_only": False, "cost": "crop"},
    "adopt_animal": {"read_only": False, "cost": "animal"},
    "buy_feed": {"read_only": False, "cost": "feed"},
    "feed_animals": {"read_only": False, "cost": "zero"},
    "collect_produce": {"read_only": False, "cost": "zero"},
}

CROP_COST = {"wheat": 4, "corn": 5, "pumpkin": 8, "wildflowers": 10}

# Autonomous authority is pinned in protected code. A registry edit can propose a
# new spec, but it cannot make that spec schedulable without an independent TCB
# change and its release review.
PINNED_AUTONOMOUS = {
    "activity_replay": "346df4841c1a4430",
    "counterfactual_sweep": "aeeff94fd1081370",
    "dual_cap_audit": "6fcc1d52e494acb1",
    "endgame_replay": "b1f882a198999a11",
}


def spec_fingerprint(probe_id: str, spec: Dict[str, Any]) -> str:
    command = list(spec.get("command") or [])
    script_sha256 = None
    if command and isinstance(command[0], str):
        script = Path(__file__).resolve().parent.parent / command[0]
        if script.is_file() and not script.is_symlink():
            script_sha256 = hashlib.sha256(script.read_bytes()).hexdigest()
    payload = {
        "probe_id": str(probe_id),
        "command": spec.get("command"),
        "read_only": spec.get("read_only"),
        "autonomous": spec.get("autonomous"),
        "budget": spec.get("budget"),
        "tools": spec.get("tools"),
        "outputs": spec.get("outputs"),
        "script_sha256": script_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AuthorizationError("%s must be an integer" % name)
    if value < minimum:
        raise AuthorizationError("%s must be at least %d" % (name, minimum))
    return value


def _constraint_value(arguments: Dict[str, Any], name: str, rule: Dict[str, Any]) -> Any:
    if name in arguments:
        value = arguments[name]
    elif "default" in rule:
        value = rule["default"]
    elif rule.get("required"):
        raise AuthorizationError("required argument missing: %s" % name)
    else:
        return None
    if rule.get("integer"):
        value = _integer(value, name, int(rule.get("min", 0)))
    if "enum" in rule and value not in set(rule.get("enum") or []):
        raise AuthorizationError("argument %s is outside its allowlist" % name)
    if "equals" in rule and value != rule.get("equals"):
        raise AuthorizationError("argument %s does not match its fixed value" % name)
    if "not_equals" in rule and value == rule.get("not_equals"):
        raise AuthorizationError("argument %s matches a forbidden value" % name)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise AuthorizationError("argument %s must be finite" % name)
        if "min" in rule and value < rule["min"]:
            raise AuthorizationError("argument %s is below its minimum" % name)
        if "max" in rule and value > rule["max"]:
            raise AuthorizationError("argument %s exceeds its maximum" % name)
    if isinstance(value, str) and "max_length" in rule and len(value) > int(rule["max_length"]):
        raise AuthorizationError("argument %s exceeds its length limit" % name)
    return value


def _validate_arguments(arguments: Dict[str, Any], constraints: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AuthorizationError("tool arguments must be an object")
    unknown = sorted(set(arguments) - set(constraints))
    if unknown:
        raise AuthorizationError("unexpected tool arguments: %s" % ", ".join(unknown))
    resolved: Dict[str, Any] = {}
    for name, rule in constraints.items():
        if not isinstance(rule, dict):
            raise AuthorizationError("invalid argument constraint: %s" % name)
        value = _constraint_value(arguments, name, rule)
        if value is not None:
            resolved[name] = value
    return resolved


def _coin_cost(tool: str, arguments: Dict[str, Any]) -> int:
    profile = TOOL_PROFILES[tool]
    kind = profile["cost"]
    if kind == "zero":
        return 0
    qty = _integer(arguments.get("qty", 1), "qty", 1)
    if kind == "crop":
        crop = str(arguments.get("kind") or "")
        if crop not in CROP_COST:
            raise AuthorizationError("unknown crop cost: %s" % crop)
        return CROP_COST[crop] * qty
    if kind == "animal":
        animal = str(arguments.get("kind") or "")
        if animal not in rules.ANIMAL_COST:
            raise AuthorizationError("unknown animal cost: %s" % animal)
        return int(rules.ANIMAL_COST[animal]) * qty
    if kind == "feed":
        return int(rules.FEED_COST) * qty
    raise AuthorizationError("tool has no protected cost model: %s" % tool)


def validate_spec(probe_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and sanitize a registry request without granting new authority."""
    if not isinstance(spec, dict):
        raise AuthorizationError("probe specification must be an object")
    budget = spec.get("budget") or {}
    calls = _integer(budget.get("calls", 0), "budget.calls")
    coins = _integer(budget.get("coins", 0), "budget.coins")
    wall = _integer(budget.get("wall_seconds", 0), "budget.wall_seconds", 1)
    if calls > MAX_CALLS or coins > MAX_COINS or wall > MAX_WALL_SECONDS:
        raise AuthorizationError("probe budget exceeds protected ceiling")

    requested_tools = spec.get("tools") or {}
    if not isinstance(requested_tools, dict):
        raise AuthorizationError("probe tools must be an object")
    sanitized_tools: Dict[str, Any] = {}
    declared_total = 0
    for tool, request in requested_tools.items():
        if tool not in TOOL_PROFILES:
            raise AuthorizationError("unknown probe tool: %s" % tool)
        if not isinstance(request, dict):
            raise AuthorizationError("tool request must be an object: %s" % tool)
        maximum = _integer(request.get("max_calls", 0), "%s.max_calls" % tool)
        if maximum <= 0:
            raise AuthorizationError("tool %s must have a positive call limit" % tool)
        constraints = request.get("arguments") or {}
        if not isinstance(constraints, dict):
            raise AuthorizationError("tool argument constraints must be an object")
        sanitized_tools[tool] = {
            "max_calls": maximum,
            "arguments": json.loads(json.dumps(constraints, allow_nan=False)),
            "read_only": bool(TOOL_PROFILES[tool]["read_only"]),
        }
        declared_total += maximum

    if declared_total > calls:
        raise AuthorizationError("per-tool call limits exceed aggregate call budget")
    if calls and not sanitized_tools:
        raise AuthorizationError("positive call budget requires an explicit tool allowlist")
    if bool(spec.get("read_only")):
        mutating = sorted(tool for tool, item in sanitized_tools.items() if not item["read_only"])
        if mutating or coins:
            raise AuthorizationError("read-only probe requests mutation authority")
    if bool(spec.get("autonomous")) and not bool(spec.get("read_only")):
        raise AuthorizationError("autonomous probe must be read-only")
    if bool(spec.get("autonomous")) and spec.get("hypothesis_id"):
        raise AuthorizationError("hypothesis adjudication requires explicit independent invocation")
    if bool(spec.get("autonomous")):
        actual = spec_fingerprint(probe_id, spec)
        if PINNED_AUTONOMOUS.get(str(probe_id)) != actual:
            raise AuthorizationError("autonomous probe is not pinned by the trusted boundary")

    command = list(spec.get("command") or [])
    if not command or not isinstance(command[0], str):
        raise AuthorizationError("probe has no command")
    first = command[0].replace("\\", "/")
    if first.startswith("/") or ".." in first.split("/"):
        raise AuthorizationError("probe command escapes the project")

    return {
        "probe_id": str(probe_id),
        "read_only": bool(spec.get("read_only")),
        "autonomous": bool(spec.get("autonomous")),
        "budget": {"calls": calls, "coins": coins, "wall_seconds": wall},
        "tools": sanitized_tools,
    }


def new_grant(probe_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    validated = validate_spec(probe_id, spec)
    now = time.monotonic()
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_id": "px-" + secrets.token_hex(16),
        "probe_id": validated["probe_id"],
        "read_only": validated["read_only"],
        "state": "active",
        "deadline": now + validated["budget"]["wall_seconds"],
        "budget": validated["budget"],
        "tools": validated["tools"],
        "usage": {"calls": 0, "coins": 0, "transport_attempts": 0, "denials": 0, "by_tool": {}},
        "lock": threading.Lock(),
    }


def close(grant: Dict[str, Any]) -> None:
    with grant["lock"]:
        grant["state"] = "closed"


def usage(grant: Dict[str, Any]) -> Dict[str, Any]:
    with grant["lock"]:
        return json.loads(json.dumps(grant.get("usage") or {}))


def authorize(grant: Dict[str, Any], payload: Dict[str, Any], retries: int) -> Dict[str, Any]:
    """Atomically reserve one logical call before transport."""
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        raise AuthorizationError("probe broker permits only tools/call")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise AuthorizationError("tools/call params must be an object")
    tool = str(params.get("name") or "")
    requested = (grant.get("tools") or {}).get(tool)
    if not requested:
        raise AuthorizationError("tool is not granted: %s" % tool)
    arguments = params.get("arguments") or {}
    resolved = _validate_arguments(arguments, requested.get("arguments") or {})
    cost = _coin_cost(tool, resolved)
    profile = TOOL_PROFILES[tool]
    attempts = max(1, int(retries))
    if attempts != 1:
        raise AuthorizationError("managed probe calls must use exactly one transport attempt")

    with grant["lock"]:
        if grant.get("state") != "active":
            raise AuthorizationError("probe grant is not active")
        if time.monotonic() > float(grant.get("deadline") or 0):
            grant["state"] = "expired"
            raise AuthorizationError("probe grant expired")
        current = grant["usage"]
        used_tool = int((current.get("by_tool") or {}).get(tool, 0))
        if used_tool + 1 > int(requested["max_calls"]):
            raise AuthorizationError("per-tool call budget exhausted: %s" % tool)
        if int(current.get("calls") or 0) + 1 > int(grant["budget"]["calls"]):
            raise AuthorizationError("aggregate call budget exhausted")
        if int(current.get("coins") or 0) + cost > int(grant["budget"]["coins"]):
            raise AuthorizationError("coin budget exhausted")
        current["calls"] = int(current.get("calls") or 0) + 1
        current["coins"] = int(current.get("coins") or 0) + cost
        current["transport_attempts"] = int(current.get("transport_attempts") or 0) + attempts
        current.setdefault("by_tool", {})[tool] = used_tool + 1
    return {"tool": tool, "arguments": resolved, "reserved_coins": cost}


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("broker pipe closed")
        view = view[written:]


def _read_exact(fd: int, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise EOFError("broker pipe closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_frame(fd: int, value: Dict[str, Any]) -> None:
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise AuthorizationError("broker frame exceeds limit")
    _write_all(fd, struct.pack("!I", len(payload)) + payload)


def _read_frame(fd: int) -> Dict[str, Any]:
    header = _read_exact(fd, 4)
    size = struct.unpack("!I", header)[0]
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise AuthorizationError("invalid broker frame size")
    value = json.loads(_read_exact(fd, size).decode("utf-8"))
    if not isinstance(value, dict):
        raise AuthorizationError("broker frame must be an object")
    return value


_BROKER_CLIENT_LOCK = threading.Lock()


def broker_post(payload: Dict[str, Any], timeout: int, retries: int) -> Dict[str, Any]:
    """Send an MCP request to the trusted parent over inherited pipes."""
    try:
        request_fd = int(os.environ[REQUEST_FD_ENV])
        response_fd = int(os.environ[RESPONSE_FD_ENV])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationError("probe broker descriptors are unavailable") from exc
    with _BROKER_CLIENT_LOCK:
        _write_frame(request_fd, {"payload": payload, "timeout": timeout, "retries": retries})
        response = _read_frame(response_fd)
    if not response.get("ok"):
        raise AuthorizationError(str(response.get("error") or "probe broker denied request"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise AuthorizationError("probe broker returned an invalid result")
    return result


def serve(
    request_fd: int,
    response_fd: int,
    grant: Dict[str, Any],
    transport: Callable[[Dict[str, Any], int, int], Dict[str, Any]],
) -> None:
    """Serve one probe worker until it closes its inherited request pipe."""
    try:
        while True:
            try:
                request = _read_frame(request_fd)
            except EOFError:
                return
            try:
                payload = request.get("payload")
                remaining = max(0.0, float(grant.get("deadline") or 0) - time.monotonic())
                if remaining <= 0:
                    raise AuthorizationError("probe grant expired")
                timeout = max(1, min(
                    MAX_BROKER_CALL_SECONDS,
                    int(remaining) or 1,
                    int(request.get("timeout") or MAX_BROKER_CALL_SECONDS),
                ))
                retries = max(1, min(3, int(request.get("retries") or 1)))
                authorize(grant, payload, retries)
                result = transport(payload, timeout, retries)
                response = {"ok": True, "result": result}
            except Exception as exc:  # the child receives only bounded, non-secret text
                if isinstance(exc, AuthorizationError):
                    with grant["lock"]:
                        grant["usage"]["denials"] = int(grant["usage"].get("denials") or 0) + 1
                response = {"ok": False, "error": ("%s: %s" % (type(exc).__name__, str(exc)))[:500]}
            _write_frame(response_fd, response)
    except (BrokenPipeError, EOFError, OSError):
        return
    finally:
        for fd in (request_fd, response_fd):
            try:
                os.close(fd)
            except OSError:
                pass
