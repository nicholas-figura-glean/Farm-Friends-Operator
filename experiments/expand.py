#!/usr/bin/env python3
"""Bounded expansion sprint: convert surplus coins into herd, fast.

Why this exists as a separate job rather than a bigger adoption step in the
cycle: it provides a separately bounded adoption-only sprint without extending
routine husbandry. Feed and collection are now constant-time bulk operations;
this worker exists for explicit growth targets, not as a latency workaround.

Guardrails, in order of importance:
  1. Never spend the feed or daily-risk cash reserves. Coins are floored at the
     reserve needed to keep the target herd fed plus automatic bill liquidity.
  2. Never outrun the server. Rate is capped below the measured ceiling so the
     concurrent cycle keeps its headroom.
  3. Always stoppable. --max-adopt and --max-seconds both bound the run, and the
     farm is re-read at the end so the report reflects reality.

It deliberately does NOT take the cycle lock: it only adopts, and adoption is
additive. It never feeds, sells, or collects.

    python3 experiments/expand.py --target 30000 --max-seconds 900
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import queue
import signal
import sys
import threading
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from farm import growth, ledger, mcp, parse, policy, rules  # noqa: E402
from farm.mcp import Client, McpError, ToolError  # noqa: E402

LOCK = os.path.join(PROJECT, "state", ".expand.lock")


def _lock():
    """Exactly one sprint at a time.

    Without this the agent was self-destructive. A sprint can stall on a slow
    call (MCP timeout is 120s and retries 3 times), so it outlives its own
    --max-seconds deadline, which is only checked between calls. launchd then
    fires the next sprint on schedule, and the two overlap: 16 workers becomes
    32, then 48. The server started returning 504 Gateway Timeout, the CYCLE
    began failing and skipping, and herd growth collapsed to +25 animals in 13
    minutes - worse than running nothing at all.
    """
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            print("EXPAND skipped: previous sprint still running")
            sys.exit(0)
        raise
    return fh


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
    """How many animals we can adopt AND still keep fed.

    Delegates to rules.affordable_adoptions so the floor is testable and lives
    with the rest of the strategy. See that docstring: the version that lived
    here charged coins for the reserve of the whole herd, ignoring the feed
    already in the barn, and so reported 1,048 affordable animals at run 379
    while 1.9M coins sat idle next to 538 minutes of feed.
    """
    return rules.affordable_adoptions(coins, herd, feed_on_hand)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=30000, help="herd size to build toward")
    ap.add_argument("--max-seconds", type=float, default=900.0)
    ap.add_argument("--rate", type=float, default=3.0, help="calls/s, below the 5/s ceiling")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

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
            "workers": args.workers,
            "dry": args.dry_run,
            "policy_compatible": runtime_policy.get("compatible"),
            "policy_errors": runtime_policy.get("errors"),
        },
    )

    handle = _lock()  # noqa: F841 - held for the process lifetime
    # Hard ceiling well inside the 300s launchd interval, so a sprint can never
    # still be alive when the next one is due.
    _arm_watchdog(int(args.max_seconds) + 20)

    policy_parameters = runtime_policy.get("parameters") or {}
    primary_kind = str(policy_parameters.get("primary_kind") or rules.PRIMARY_KIND)
    call_ceiling = float(
        policy_parameters.get("max_calls_per_second") or rules.MAX_CALLS_PER_SECOND
    )
    mcp.LIMITER.set_rate(min(args.rate, call_ceiling))
    c = Client()
    farm = parse.parse_farm(c.call("list_farm"))
    stalled, stalled_windows = growth.production_stall_active(model=growth.load())
    if stalled:
        reason = (
            "lifetime produce unchanged for %d healthy verified windows; "
            "expansion paused until production resumes" % stalled_windows
        )
        print("EXPAND skipped: %s" % reason)
        ledger.record(
            "expansion.completed",
            {"status": "skipped", "adopted": 0, "reason": reason},
        )
        return 0
    can_afford = affordable(farm.coins, farm.animal_count, farm.feed)

    # Whole-herd feeding is now constant-time, so the old gateway-derived herd
    # ceiling is retired. Affordability still includes the full feed reserve.
    target = args.target
    room = max(0, target - farm.animal_count)
    want = min(room, can_afford)

    print("herd=%d coins=%d feed=%d" % (farm.animal_count, farm.coins, farm.feed))
    print(
        "target=%d (room %d)  affordable %d (keeps %d feed/animal reserved)  -> adopt %d %ss"
        % (target, room, can_afford, rules.FEED_PER_ANIMAL_RESERVE, want,
           primary_kind)
    )
    print("policy=%s compatible=%s" % (runtime_policy.get("policy_id"), runtime_policy.get("compatible")))
    intervention_id = ledger.intervention(
        "expand_adoption_batch",
        "planned",
        {
            "herd_before": farm.animal_count,
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
        ledger.intervention(
            "expand_adoption_batch",
            "skipped",
            {"reason": "dry_run" if args.dry_run else "nothing_safe_or_affordable"},
            intervention_id=intervention_id,
        )
        ledger.record("expansion.completed", {"status": "skipped", "adopted": 0})
        return 0

    work = queue.Queue()
    for _ in range(want):
        work.put(1)
    done = {"ok": 0, "fail": 0}
    lock = threading.Lock()
    stop = threading.Event()
    deadline = time.time() + args.max_seconds

    worker_context = ledger.current()

    def worker(endpoint):
        ledger.set_context(
            **dict(worker_context, worker=threading.current_thread().name)
        )
        client = Client(endpoint)
        while not stop.is_set() and time.time() < deadline:
            try:
                work.get_nowait()
            except queue.Empty:
                return
            try:
                client.call("adopt_animal", kind=primary_kind)
                with lock:
                    done["ok"] += 1
            except (ToolError, McpError) as exc:
                with lock:
                    done["fail"] += 1
                    if done["fail"] <= 3:
                        print("  adopt failed: %s" % str(exc)[:120])
                    too_many = done["fail"] > 25
                # Out of coins or sustained pushback: stop the sprint rather than
                # hammering a server that is already refusing.
                if "coin" in str(exc).lower() or too_many:
                    stop.set()
                    return

    threads = [
        threading.Thread(target=worker, args=(c.endpoint,), daemon=True)
        for _ in range(max(1, args.workers))
    ]
    started = time.time()
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(5)
        with lock:
            ok, fail = done["ok"], done["fail"]
        print("  adopted %d (fail %d) %.1f/s" % (ok, fail, ok / max(1e-6, time.time() - started)))
    for t in threads:
        t.join(timeout=5)

    final = parse.parse_farm(c.call("list_farm"))
    print(
        "DONE adopted=%d failed=%d in %.0fs | herd %d -> %d | coins %d -> %d | feed %d"
        % (done["ok"], done["fail"], time.time() - started, farm.animal_count,
           final.animal_count, farm.coins, final.coins, final.feed)
    )
    buffer_min = rules.feed_buffer_minutes(final.feed, final.animal_count)
    print("feed runway now %.0f min (floor %d)" % (buffer_min, rules.FEED_BUFFER_MIN_MINUTES))
    if buffer_min < rules.FEED_BUFFER_MIN_MINUTES:
        print("WARNING: runway below floor - the next cycle must top up feed")
    ledger.intervention(
        "expand_adoption_batch",
        "outcome",
        {
            "adopted": done["ok"],
            "failed": done["fail"],
            "herd_before": farm.animal_count,
            "herd_after": final.animal_count,
            "animal_residual": final.animal_count - farm.animal_count - done["ok"],
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
            "status": "ok" if done["fail"] == 0 else "partial",
            "adopted": done["ok"],
            "failed": done["fail"],
            "herd_before": farm.animal_count,
            "herd_after": final.animal_count,
            "duration_s": round(time.time() - started, 2),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
