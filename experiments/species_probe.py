"""Decisive test: is the production ceiling per-farm or per-kind?

The growth gate in farm/rules.py concluded production is capped per FARM, but
that conclusion was measured by varying chicken count only. Counter-evidence:
across runs 7-47 the egg rate per chicken fell 0.25 -> 0.134 units/min while the
2 pigs and 1 beehive held 0.25-0.29 units/animal/min throughout. A real per-farm
cap would have diluted them too.

Each kind produces a DISTINCT item (egg/truffle/wool/milk/honey), so one batch
measures four rates independently in a single collection interval:

  per-kind cap  -> eggs hold near 1550/min and the new kinds ADD output
  per-farm cap  -> eggs fall proportionally and total output stays ~1550/min

Bounded by design: 100 of each kind (9,500 coins, 1.7% of the 570k idle coins),
plus one plot each of wheat/corn/pumpkin (17 coins) to test whether crops are a
separate bucket too. Runs under the cycle's own flock so it can never double-act
with a scheduled run, and in chunks so it cannot hold the lock across a slot.
"""

import fcntl
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from farm import mcp, parse, rules  # noqa: E402

STATE = os.path.realpath(os.environ.get(
    "FARM_STATE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state"),
))
LOCK = os.path.join(STATE, ".lock")
LOG = os.path.join(STATE, "probe.ndjson")

BATCH = {"pig": 100, "sheep": 100, "cow": 100, "beehive": 100}
CROPS = ["wheat", "corn", "pumpkin"]
CHUNK = 200          # adopt calls per lock acquisition (~40s at 5/s)
WORKERS = 6


def log(event, **fields):
    fields["event"] = event
    fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(LOG, "a") as fh:
        fh.write(json.dumps(fields, sort_keys=True) + "\n")
    print(json.dumps(fields, sort_keys=True))


def acquire(timeout=240):
    """Take the cycle's lock, waiting for a scheduled run to finish."""
    deadline = time.time() + timeout
    fh = open(LOCK, "w")
    while time.time() < deadline:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except IOError:
            time.sleep(3)
    fh.close()
    raise SystemExit("could not take the lock within %ds - a cycle is busy" % timeout)


def release(fh):
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()


def adopt_many(endpoint, jobs):
    """Adopt in parallel under the shared global rate limiter."""
    done, failed = {}, []

    def one(kind):
        client = mcp.Client(endpoint)
        try:
            text = client.call("adopt_animal", kind=kind)
            return kind, text, None
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            return kind, None, repr(exc)[:160]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for kind, text, err in pool.map(one, jobs):
            if err:
                failed.append((kind, err))
            else:
                done[kind] = done.get(kind, 0) + 1
    return done, failed


def main():
    mcp.LIMITER.set_rate(rules.MAX_CALLS_PER_SECOND)
    client = mcp.Client()
    endpoint = client.endpoint

    jobs = [kind for kind, n in BATCH.items() for _ in range(n)]
    cost = sum(rules.ANIMAL_COST[k] * n for k, n in BATCH.items())

    handle = acquire()
    try:
        before = parse.parse_farm(client.call("list_farm"))
        log("baseline", coins=before.coins, animals=before.animal_count,
            by_kind=before.counts_by_kind, plots=len(before.plots), planned_cost=cost)
        if before.coins < cost * 3:
            raise SystemExit("refusing: coins %d too close to batch cost %d" % (before.coins, cost))
    finally:
        release(handle)

    adopted_total, failures = {}, []
    for start in range(0, len(jobs), CHUNK):
        chunk = jobs[start:start + CHUNK]
        handle = acquire()
        t0 = time.time()
        try:
            done, failed = adopt_many(endpoint, chunk)
        finally:
            release(handle)
        for k, v in done.items():
            adopted_total[k] = adopted_total.get(k, 0) + v
        failures.extend(failed)
        log("chunk", n=len(chunk), adopted=done, failures=len(failed),
            seconds=round(time.time() - t0, 1))
        time.sleep(2)

    handle = acquire()
    try:
        planted = []
        for crop in CROPS:
            try:
                planted.append({crop: client.call("plant", kind=crop)[:120]})
            except Exception as exc:  # noqa: BLE001
                planted.append({crop: "FAILED %r" % exc})
        after = parse.parse_farm(client.call("list_farm"))
        log("done", adopted=adopted_total, failures=failures[:5],
            coins=after.coins, animals=after.animal_count, by_kind=after.counts_by_kind,
            plots=[(p.crop, p.status) for p in after.plots], planted=planted)
    finally:
        release(handle)


if __name__ == "__main__":
    main()
