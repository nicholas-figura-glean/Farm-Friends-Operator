#!/usr/bin/env python3
"""EMERGENCY: break the out-of-feed deadlock and restart production.

Context (runs 291-293): feed hit 0, so the `feed` pipeline step raised
ToolError("You're out of feed") BEFORE the `buy_feed` step could ever run.
Every cycle and every supervisor pass crashed at the same line, so the
self-healing loop could not heal itself out of it. Coins were never the
constraint: 3.5M were banked while the herd starved.

This script does the minimum, in the only order that works:
    buy feed  ->  feed all  ->  re-read  ->  repeat until hunger is safe

It takes the same flock as the scheduled runs, so it cannot double-act.
Usage: python3 experiments/rescue_feed.py [--target-feed N] [--max-rounds N]
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import sys
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from farm import parse, rules  # noqa: E402
from farm.mcp import Client, McpError, ToolError  # noqa: E402

LOCK = os.path.join(PROJECT, "state", ".lock")


def _lock():
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            print("RESCUE aborted: a scheduled run holds the lock")
            sys.exit(0)
        raise
    return fh


def read(c: Client) -> parse.Farm:
    return parse.parse_farm(c.call("list_farm"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-feed", type=int, default=600_000,
                    help="feed to hold on hand (default 600k ~ 8h of buffer)")
    ap.add_argument("--max-rounds", type=int, default=6)
    args = ap.parse_args()

    fh = _lock()  # noqa: F841 - held for the process lifetime
    c = Client()

    farm = read(c)
    print("BEFORE animals=%d feed=%d coins=%d max_hunger=%d ready=%d"
          % (farm.animal_count, farm.feed, farm.coins, farm.max_hunger, farm.ready_units))

    coins = farm.coins
    want = min(max(0, args.target_feed - farm.feed), coins // rules.FEED_COST)
    if want > 0:
        print("buying %d feed (cost %d of %d coins)" % (want, want * rules.FEED_COST, coins))
        text = c.call("buy_feed", qty=want)
        got = parse.parse_buy_feed(text)
        coins = int(got["coins_after"])
        print("bought %s feed, coins now %d" % (got["qty"], coins))
    else:
        print("feed already at/above target; skipping purchase")

    # Feed repeatedly: one bulk call does not fully reset a herd this hungry.
    for round_no in range(1, args.max_rounds + 1):
        farm = read(c)
        if farm.max_hunger < rules.FEED_AT_HUNGER:
            print("round %d: max_hunger %d is already safe" % (round_no, farm.max_hunger))
            break
        print("round %d: max_hunger=%d feed=%d -> feed_animals(all)"
              % (round_no, farm.max_hunger, farm.feed))
        try:
            c.call("feed_animals", animal_id="all")
        except (ToolError, McpError) as exc:
            print("round %d: bulk feed failed: %s" % (round_no, str(exc)[:160]))
            farm = read(c)
            if farm.feed <= 0 and coins > 0:
                topup = min(args.target_feed, coins // rules.FEED_COST)
                if topup > 0:
                    print("  out of feed again; buying %d more" % topup)
                    got = parse.parse_buy_feed(c.call("buy_feed", qty=topup))
                    coins = int(got["coins_after"])
                    continue
            break
        time.sleep(1.0)

    farm = read(c)
    print("AFTER  animals=%d feed=%d coins=%d max_hunger=%d ready=%d"
          % (farm.animal_count, farm.feed, farm.coins, farm.max_hunger, farm.ready_units))

    # Bank whatever the restarted herd has already produced.
    if farm.ready_units > 0:
        print("collecting %d ready units" % farm.ready_units)
        for _ in range(3):
            try:
                got = parse.parse_collect(c.call("collect_produce"))
            except (ToolError, McpError) as exc:
                print("collect failed: %s" % str(exc)[:160])
                break
            print("  collected %s" % (got or "nothing"))
            if not got:
                break

    hungry = sum(1 for a in farm.animals if a.hunger >= rules.HUNGER_STOP)
    print("VERDICT feed=%d max_hunger=%d animals_at_or_above_stop=%d production=%s"
          % (farm.feed, farm.max_hunger, hungry,
             "STOPPED" if farm.max_hunger >= rules.HUNGER_STOP else "running"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
