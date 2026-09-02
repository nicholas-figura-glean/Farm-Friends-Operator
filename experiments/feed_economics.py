"""Measure the real feed economics, because the reserve is our largest idle asset.

Why this exists
---------------
`rules.FEED_BURN_PER_ANIMAL_MIN = 0.104` was measured once, at 11,869 animals,
and every coin we withhold from adoption is justified by it:
`FEED_PER_ANIMAL_RESERVE = 30` exists to give that burn a 288-minute runway.

At 41,293 animals that reserve is 1.24M feed - 1.24M coins - which is 124,000
unadopted chickens, i.e. ~23,000 produce/min we are not making. So the burn
constant is worth more than any other number in the codebase.

Accounting-based estimates over runs 294-353 put the real burn at 0.000-0.012
feed/animal/min, an order of magnitude below the model, but that inference is
unreliable: `feed_bought` includes reconciliation purchases, expansion runs
concurrently, and every bulk feed has been returning 504 for hours, so it is not
even clear the feeds execute. Inference cannot separate "cheap" from "never ran".

This probe measures the two quantities directly and cheaply:

  1. cost per animal fed - feed one SINGLE animal by id and diff the barn.
  2. hunger rise per minute - re-read the same animals after a wait, with no
     feeding in between, and diff their hunger.

Both are read-mostly, single-animal, fast calls: no bulk call, no 504 window,
nothing that competes with the cycle. It mutates exactly one animal's hunger,
which the next scheduled bulk feed would have done anyway.

Deliberately does NOT change any rule. It prints findings and writes them to
state/probe.ndjson; retuning the reserve is a separate, reviewed decision.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from farm import mcp, parse  # noqa: E402

STATE = os.path.realpath(os.environ.get(
    "FARM_STATE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state"),
))
LOG = os.path.join(STATE, "probe.ndjson")


def log(event, **fields):
    fields["event"] = event
    fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    os.makedirs(STATE, exist_ok=True)
    with open(LOG, "a") as fh:
        fh.write(json.dumps(fields, sort_keys=True) + "\n")
    return fields


def hunger_histogram(farm):
    hist = {}
    for a in farm.animals:
        hist[a.hunger] = hist.get(a.hunger, 0) + 1
    return dict(sorted(hist.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--wait",
        type=float,
        default=300.0,
        help="seconds between the two reads used to measure hunger drift",
    )
    ap.add_argument(
        "--skip-drift",
        action="store_true",
        help="only measure cost per animal fed",
    )
    args = ap.parse_args()

    c = mcp.Client()

    # ---- snapshot 1 -------------------------------------------------------
    t0 = time.time()
    farm0 = parse.parse_farm(c.call("list_farm"))
    read_s = time.time() - t0
    hist0 = hunger_histogram(farm0)
    print(
        "snapshot 1: %d animals, %d feed, %d coins, hunger=%s (read %.1fs)"
        % (farm0.animal_count, farm0.feed, farm0.coins, hist0, read_s)
    )

    # ---- cost of feeding exactly one animal -------------------------------
    # Pick the hungriest animal, so a hunger reset is unambiguous.
    target = max(farm0.animals, key=lambda a: a.hunger)
    print(
        "\nfeeding ONE animal: #%d %s (%s) hunger %d"
        % (target.id, target.name, target.kind, target.hunger)
    )
    reply = c.call("feed_animals", animal_id=str(target.id))
    print("  reply: %s" % reply.strip()[:200])

    farm1 = parse.parse_farm(c.call("list_farm"))
    after = {a.id: a for a in farm1.animals}.get(target.id)
    feed_delta = farm1.feed - farm0.feed
    print(
        "  barn feed %d -> %d (delta %+d)" % (farm0.feed, farm1.feed, feed_delta)
    )
    if after is not None:
        print(
            "  animal hunger %d -> %d  (feed works: %s)"
            % (target.hunger, after.hunger, after.hunger < target.hunger)
        )

    one = log(
        "feed_one",
        animal_id=target.id,
        kind=target.kind,
        hunger_before=target.hunger,
        hunger_after=(after.hunger if after else None),
        feed_before=farm0.feed,
        feed_after=farm1.feed,
        feed_delta=feed_delta,
        animals=farm1.animal_count,
    )

    if args.skip_drift:
        print("\n%s" % json.dumps(one, sort_keys=True))
        return 0

    # ---- hunger drift, with no feeding in between -------------------------
    # Sample a fixed set of ids from snapshot 1 and re-read them later. Any
    # animal fed by a concurrent cycle shows hunger DOWN, so those are dropped
    # and the remainder still gives a clean rise rate.
    print("\nwaiting %.0fs to measure hunger drift (no feeding)..." % args.wait)
    ids0 = {a.id: a.hunger for a in farm1.animals}
    time.sleep(args.wait)

    farm2 = parse.parse_farm(c.call("list_farm"))
    elapsed = args.wait
    rose = []
    fell = 0
    for a in farm2.animals:
        if a.id not in ids0:
            continue  # adopted during the wait
        d = a.hunger - ids0[a.id]
        if d < 0:
            fell += 1
        else:
            rose.append(d)

    hist2 = hunger_histogram(farm2)
    print(
        "snapshot 2: %d animals, %d feed, hunger=%s"
        % (farm2.animal_count, farm2.feed, hist2)
    )
    print("  %d tracked animals fed by a concurrent cycle (dropped)" % fell)

    if rose:
        mean_rise = sum(rose) / len(rose)
        per_min = mean_rise / (elapsed / 60.0)
        print(
            "  hunger rise: mean %+.2f over %.1f min = %.4f hunger/min (n=%d)"
            % (mean_rise, elapsed / 60.0, per_min, len(rose))
        )
        if per_min > 0:
            print(
                "  time from hunger 0 to production stop (70): %.0f min"
                % (70.0 / per_min)
            )

    feed_used = (farm1.feed - farm2.feed) if farm2.feed <= farm1.feed else 0
    print(
        "  barn feed %d -> %d over %.1f min => %.5f feed/animal/min"
        % (
            farm1.feed,
            farm2.feed,
            elapsed / 60.0,
            feed_used / (elapsed / 60.0) / max(farm2.animal_count, 1),
        )
    )
    print("  (rules.FEED_BURN_PER_ANIMAL_MIN = 0.104)")

    log(
        "hunger_drift",
        wait_s=elapsed,
        tracked=len(rose),
        fed_concurrently=fell,
        mean_rise=(sum(rose) / len(rose)) if rose else None,
        hunger_per_min=((sum(rose) / len(rose)) / (elapsed / 60.0)) if rose else None,
        feed_before=farm1.feed,
        feed_after=farm2.feed,
        animals=farm2.animal_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
