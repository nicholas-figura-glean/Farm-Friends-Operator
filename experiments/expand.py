#!/usr/bin/env python3
"""Bounded expansion sprint: convert surplus coins into herd capacity.

The server now accepts ``adopt_animal.qty`` as one bounded bulk operation. This
worker therefore makes at most one adoption call per sprint, never loops per
animal, never adopts past the parsed league capacity, and yields immediately when
an earned prestige or eligible active crisis belongs to the main cycle.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from farm import growth, ledger, mcp, mechanics, parse, policy, rules  # noqa: E402
from farm.mcp import Client, McpError, ToolError  # noqa: E402

LOCK = os.path.join(PROJECT, "state", ".expand.lock")
BULK_STATE = os.path.join(PROJECT, "state", "bulk_adopt.json")
MAX_BULK_ADOPT = 200_000
BULK_REPROBE_SECONDS = 60 * 60
BULK_IMPLEMENTATION_ERROR = "cap is not a function"


def _lock():
    """Exactly one expansion sprint or progression action at a time."""
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    handle = open(LOCK, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            print("EXPAND skipped: previous sprint or progression action still running")
            sys.exit(0)
        raise
    return handle


def _arm_watchdog(seconds: int) -> None:
    """Guarantee the process exits, even blocked inside a socket read."""

    def _fire(signum, frame):  # noqa: ANN001
        ledger.record(
            "expansion.failed",
            {"reason": "hard_timeout", "timeout_s": seconds},
        )
        print("EXPAND hard timeout after %ds - exiting" % seconds)
        os._exit(0)

    signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)


def affordable(coins: int, herd: int, feed_on_hand: int = 0) -> int:
    """How many animals we can adopt and still preserve feed/cash reserves."""
    return rules.affordable_adoptions(coins, herd, feed_on_hand)


def _read_json(path: str) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: str, value: Dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("%s.tmp.%d" % (target, os.getpid()))
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(target))


def _adopt_contract_sha() -> str:
    snapshot = _read_json(os.path.join(PROJECT, "state", "contract_live.json"))
    if not snapshot:
        snapshot = _read_json(os.path.join(PROJECT, "state", "contract.json"))
    return str((((snapshot.get("tools") or {}).get("adopt_animal") or {}).get("description_sha") or ""))


def bulk_due(now: float = None, state_path: str = None) -> bool:
    """Whether the advertised qty path should be attempted or re-probed."""
    stored = _read_json(state_path or BULK_STATE)
    if not stored.get("disabled"):
        return True
    if stored.get("description_sha") != _adopt_contract_sha():
        return True
    failed_at = float(stored.get("failed_at_epoch") or 0.0)
    return (time.time() if now is None else now) - failed_at >= BULK_REPROBE_SECONDS


def _mark_bulk_broken(error: str) -> None:
    _write_json(BULK_STATE, {
        "schema_version": 1,
        "disabled": True,
        "failed_at_epoch": time.time(),
        "description_sha": _adopt_contract_sha(),
        "error": str(error)[:240],
        "reprobe_after_seconds": BULK_REPROBE_SECONDS,
    })


def _mark_bulk_healthy() -> None:
    _write_json(BULK_STATE, {
        "schema_version": 1,
        "disabled": False,
        "validated_at_epoch": time.time(),
        "description_sha": _adopt_contract_sha(),
    })


def _individual_fallback(
    client: Client,
    kind: str,
    count: int,
    deadline: float,
    workers: int,
) -> Dict[str, Any]:
    """Bounded legacy path used only after definitive bulk implementation failure."""
    work: queue.Queue = queue.Queue()
    for _ in range(max(0, int(count))):
        work.put(1)
    result: Dict[str, Any] = {"ok": 0, "failed": 0, "last_error": None}
    lock = threading.Lock()
    stop = threading.Event()
    clients = [client] + [Client(client.endpoint) for _ in range(max(0, int(workers) - 1))]
    worker_context = ledger.current()

    def worker(current: Client) -> None:
        ledger.set_context(
            **dict(worker_context, worker=threading.current_thread().name)
        )
        while not stop.is_set() and time.time() < deadline:
            try:
                work.get_nowait()
            except queue.Empty:
                return
            try:
                timeout = max(1, min(30, int(deadline - time.time())))
                current.call(
                    "adopt_animal",
                    kind=kind,
                    _transport_retries=1,
                    _transport_timeout=timeout,
                )
                with lock:
                    result["ok"] += 1
            except (ToolError, McpError) as exc:
                message = str(exc)[:240]
                with lock:
                    result["failed"] += 1
                    result["last_error"] = message
                    failures = result["failed"]
                if "coin" in message.lower() or "barn is full" in message.lower() or failures > 25:
                    stop.set()
                    return

    threads = [threading.Thread(target=worker, args=(current,), daemon=True) for current in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        remaining = max(0.0, deadline - time.time())
        thread.join(timeout=remaining)
    stop.set()
    return result


def bounded_target(farm: parse.Farm, requested: int) -> int:
    """Requested growth capped by the server-advertised league capacity."""
    target = max(0, int(requested))
    if isinstance(farm.capacity, int):
        target = min(target, farm.capacity)
    return target


def _record_skip(reason: str, farm: parse.Farm, requested: int) -> int:
    print("EXPAND skipped: %s" % reason)
    ledger.record(
        "expansion.completed",
        {
            "status": "skipped",
            "adopted": 0,
            "reason": reason,
            "target_requested": requested,
            "herd": farm.animal_count,
            "capacity": farm.capacity,
            "prestige_available": farm.prestige_available,
            "active_crisis": farm.crisis.kind if farm.crisis else None,
        },
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=30_000, help="herd size to build toward")
    parser.add_argument("--max-seconds", type=float, default=900.0)
    parser.add_argument("--rate", type=float, default=3.0, help="upper call rate; one bulk call is used")
    parser.add_argument("--workers", type=int, default=1, help="retained for launchd compatibility; bulk mode uses one")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    runtime_policy = policy.runtime_context()
    sprint_id = ledger.new_id("sprint")
    ledger.set_context(
        actor="expand",
        sprint=sprint_id,
        policy_id=runtime_policy.get("policy_id"),
        claim_registry_version=runtime_policy.get("claim_registry_version"),
    )
    ledger.record(
        "expansion.started",
        {
            "target_requested": args.target,
            "max_seconds": args.max_seconds,
            "rate": args.rate,
            "workers_requested": args.workers,
            "execution": "bulk_qty_with_circuit_breaker",
            "dry": args.dry_run,
            "policy_compatible": runtime_policy.get("compatible"),
            "policy_errors": runtime_policy.get("errors"),
        },
    )

    handle = _lock()  # noqa: F841 - held for the process lifetime
    _arm_watchdog(int(args.max_seconds) + 20)

    parameters = runtime_policy.get("parameters") or {}
    primary_kind = str(parameters.get("primary_kind") or rules.PRIMARY_KIND)
    call_ceiling = float(parameters.get("max_calls_per_second") or rules.MAX_CALLS_PER_SECOND)
    mcp.LIMITER.set_rate(min(args.rate, call_ceiling))
    client = Client()
    farm = parse.parse_farm(client.call("list_farm"))

    # Progression owns the herd reset. Continuing to adopt here caused 1,122
    # guaranteed barn-full errors while prestige was visibly available.
    if farm.prestige_available:
        return _record_skip(
            "earned prestige is pending in the main cycle; expansion must not buy a herd that will be retired",
            farm,
            args.target,
        )

    loaded = mechanics.load_policies()
    mechanic = mechanics.next_decision(
        farm,
        mechanics.active_tools(loaded),
        attempted={},
        loaded=loaded,
    ).get("decision")
    if mechanic and mechanic.get("kind") == "crisis":
        return _record_skip(
            "eligible active crisis is pending in the main cycle via %s" % mechanic.get("tool"),
            farm,
            args.target,
        )

    stalled, stalled_windows = growth.production_stall_active(model=growth.load())
    if stalled:
        return _record_skip(
            "lifetime produce unchanged for %d healthy verified windows; expansion waits for production"
            % stalled_windows,
            farm,
            args.target,
        )

    target = bounded_target(farm, args.target)
    room = max(0, target - farm.animal_count)
    can_afford = affordable(farm.coins, farm.animal_count, farm.feed)
    want = min(room, can_afford, MAX_BULK_ADOPT)

    print("herd=%d/%s coins=%d feed=%d" % (
        farm.animal_count,
        farm.capacity if farm.capacity is not None else "?",
        farm.coins,
        farm.feed,
    ))
    try_bulk = bulk_due()
    execution = "bulk_qty" if try_bulk else "bounded_individual_fallback"
    print(
        "target=%d (requested %d, room %d) affordable=%d -> %s qty=%d %s"
        % (target, args.target, room, can_afford, execution, want, primary_kind)
    )
    print("policy=%s compatible=%s" % (
        runtime_policy.get("policy_id"), runtime_policy.get("compatible")
    ))

    intervention_id = ledger.intervention(
        "expand_adoption_batch",
        "planned",
        {
            "execution": execution,
            "herd_before": farm.animal_count,
            "capacity": farm.capacity,
            "coins_before": farm.coins,
            "feed_before": farm.feed,
            "target_requested": args.target,
            "target_safe": target,
            "affordable": can_afford,
            "adopt_planned": want,
            "kind": primary_kind,
        },
    )
    if args.dry_run or want <= 0:
        reason = "dry_run" if args.dry_run else "league capacity reached or nothing reserve-safe is affordable"
        ledger.intervention(
            "expand_adoption_batch",
            "skipped",
            {"reason": reason},
            intervention_id=intervention_id,
        )
        return _record_skip(reason, farm, args.target)

    started = time.time()
    deadline = started + max(1.0, float(args.max_seconds))
    call_error = None
    response = ""
    fallback: Dict[str, Any] = {"ok": 0, "failed": 0, "last_error": None}
    if try_bulk:
        try:
            # A bulk mutation is not transport-retried: the first request may have
            # committed before a gateway failure. Only a definitive ToolError that
            # proves the server's advertised qty implementation is broken may enter
            # the individual fallback; transport ambiguity never does.
            response = client.call(
                "adopt_animal",
                kind=primary_kind,
                qty=want,
                _transport_retries=1,
            )
            _mark_bulk_healthy()
        except ToolError as exc:
            call_error = str(exc)[:240]
            if BULK_IMPLEMENTATION_ERROR in call_error.lower():
                _mark_bulk_broken(call_error)
                execution = "bounded_individual_fallback_after_bulk_error"
                fallback = _individual_fallback(
                    client, primary_kind, want, deadline, max(1, args.workers)
                )
        except McpError as exc:
            call_error = str(exc)[:240]
    else:
        fallback = _individual_fallback(
            client, primary_kind, want, deadline, max(1, args.workers)
        )

    final = parse.parse_farm(client.call("list_farm"))
    observed_delta = final.animal_count - farm.animal_count
    adopted = max(0, observed_delta)
    if adopted > 0 and execution.startswith("bounded_individual"):
        status = "fallback_ok" if fallback.get("failed", 0) == 0 else "fallback_partial"
    else:
        status = "ok" if call_error is None and adopted > 0 else (
            "reconciled" if adopted > 0 else "failed"
        )
    duration = time.time() - started
    print(
        "DONE status=%s requested=%d observed_delta=%d in %.1fs | herd %d -> %d | coins %d -> %d"
        % (
            status, want, observed_delta, duration, farm.animal_count,
            final.animal_count, farm.coins, final.coins,
        )
    )
    if call_error:
        print("  bulk call outcome required reconciliation: %s" % call_error)

    buffer_min = rules.feed_buffer_minutes(final.feed, final.animal_count)
    print("feed runway now %.0f min (floor %d)" % (
        buffer_min, rules.FEED_BUFFER_MIN_MINUTES
    ))
    ledger.intervention(
        "expand_adoption_batch",
        "outcome",
        {
            "status": status,
            "execution": execution,
            "requested": want,
            "adopted_observed": adopted,
            "call_error": call_error,
            "fallback": fallback,
            "response_preview": response[:240],
            "herd_before": farm.animal_count,
            "herd_after": final.animal_count,
            "capacity": final.capacity,
            "coins_before": farm.coins,
            "coins_after": final.coins,
            "feed_after": final.feed,
            "feed_runway_min": round(buffer_min, 2),
        },
        intervention_id=intervention_id,
    )
    ledger.record(
        "expansion.completed",
        {
            "status": status,
            "execution": execution,
            "adopted": adopted,
            "requested": want,
            "failed": 1 if status == "failed" else 0,
            "call_error": call_error,
            "fallback": fallback,
            "herd_before": farm.animal_count,
            "herd_after": final.animal_count,
            "capacity": final.capacity,
            "duration_s": round(duration, 2),
        },
    )
    # A failed/ambiguous bulk mutation is contained and retried only on the next
    # normal cadence; a nonzero exit could make supervision repeat an irreversible
    # request immediately.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
