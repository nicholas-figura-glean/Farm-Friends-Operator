"""Endgame planner: how large a herd can we afford WITHOUT starving, and do we win?

Why this exists
---------------
Two constraints crossed over at run 379 and made the old policy wrong in both
directions at once:

  coin runway  3.0 h
  win ETA      4.7 h

Spending coins to zero buys the biggest herd and then starves it - that is
exactly the spiral that left John frozen at 56,061 animals on 76 coins while we
closed 480k of his lead. But hoarding coins caps the herd, and herd size is the
only thing that generates score.

The existing floor in experiments/expand.py gets this wrong conservatively: it
reserves FEED_PER_ANIMAL_RESERVE * herd coins - 1.9M at this herd - to buy feed
that is ALREADY IN THE BARN. Coins and feed are both counted as if only coins
existed, so expansion throttles itself to ~1,000 affordable animals while
sitting on 1.9M feed and 1.9M coins.

The real constraint is a single budget over the remaining race:

    10 * (A - A0)            adoption, one-off
  + max(0, b*A*T - F)        feed we must still BUY, after the barn is drained
  <= C

where b is the measured burn (0.065 feed/animal/min), F is feed on hand, C is
coins, T is how long the race still has to run, and A is the herd we settle at.
Bigger A finishes sooner (more produce/min) but costs more to feed for longer.

Everything here is measured, not assumed:
  y = 0.1717 produce/min/animal   linear fit, 371 samples, 648-63,522 animals
  b = 0.065  feed/animal/min      probe: 18,133 animals fed in 7.0 min
  hunger 1.26/min, stops at 70    probe: whole histogram shifted 6 per tick

Read-only. Prints a table and the recommended herd ceiling; changes nothing.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from farm import rules  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

YIELD = 0.1717           # produce/min per animal (measured)
BURN = 0.065             # feed/animal/min (measured)
SAFETY_MIN = 60.0        # keep this many minutes of feed buyable at all times


def latest_row():
    state = os.path.realpath(os.environ.get("FARM_STATE_DIR", os.path.join(ROOT, "state")))
    path = os.path.join(state, "history.ndjson")
    row = None
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            if not r.get("dry") and r.get("coins"):
                row = r
    return row


def simulate(A0, P0, C0, F0, Pj, Aj, gj, g, a_target, horizon=1440.0, dt=1.0):
    """Step the race forward a minute at a time. Returns (win_minute, why)."""
    A, P, C, F = float(A0), float(P0), float(C0), float(F0)
    Pj_, Aj_ = float(Pj), float(Aj)
    starved_at = None
    for t in range(1, int(horizon / dt) + 1):
        # --- adopt, if we can afford it and still want more herd -----------
        if A < a_target:
            want = min(g * dt, a_target - A)
            cost = want * rules.ANIMAL_COST[rules.PRIMARY_KIND]
            if C - cost >= 0:
                A += want
                C -= cost
                # every new animal also needs its feed bought eventually
        # --- feed: drain the barn, buy more with coins ---------------------
        need = BURN * A * dt
        if F >= need:
            F -= need
        else:
            short = need - F
            F = 0.0
            if C >= short:
                C -= short          # feed costs 1 coin per unit
            else:
                if starved_at is None:
                    starved_at = t
        # --- produce -------------------------------------------------------
        producing = A if starved_at is None else A * 0.35  # starved herds collapse
        P += YIELD * producing * dt
        Aj_ += gj * dt
        Pj_ += YIELD * Aj_ * dt
        if P >= Pj_:
            return t, A, C, starved_at
    return None, A, C, starved_at


def _objective_rival(row, requested=None):
    rivals = row.get("rivals") or {}
    leader = str(row.get("leader") or "").strip()
    if row.get("rank") != 1:
        if leader and leader.lower() != "nick" and leader in rivals:
            return leader
        return None
    if requested and requested in rivals:
        return requested
    return max(rivals, key=lambda name: float(rivals.get(name) or 0), default=None)


def analyze(row, rival=None, growth=None):
    proj = row.get("projection") or {}
    rival = _objective_rival(row, rival)
    rival_missing = row.get("rank") != 1 and not rival
    A0, P0, C0, F0 = row["animals"], row["produce"], row["coins"], row["feed"]
    Aj = (row.get("rival_herds") or {}).get(rival) or 0
    Pj = (row.get("rivals") or {}).get(rival) or 0
    gj = proj.get("rival_growth_per_min") or 0.0
    projected_growth = proj.get("our_growth_per_min")
    g = float(growth) if growth is not None else (
        float(projected_growth) if isinstance(projected_growth, (int, float)) else 0.0
    )
    capacity_observed = isinstance(row.get("animal_capacity"), int) and int(row["animal_capacity"]) >= int(A0)
    capacity = int(row["animal_capacity"]) if capacity_observed else int(A0)
    candidates = sorted(set(
        [int(A0), int(capacity)]
        + [target for target in (70000, 80000, 90000, 100000, 112000, 125000, 150000, 200000)
           if A0 <= target <= capacity]
    ))
    options = []
    best = None
    for target in candidates:
        if target < A0:
            continue
        t, animals, coins, starved = simulate(A0, P0, C0, F0, Pj, Aj, gj, g, target)
        option = {
            "target": target,
            "win_minutes": t,
            "animals_at_end": int(animals),
            "coins_at_end": max(int(coins), 0),
            "starved_at_minute": starved,
            "safe_win": bool(t and not starved),
        }
        options.append(option)
        if option["safe_win"] and (best is None or int(t) < int(best["win_minutes"])):
            best = option
    rival_league = (row.get("rival_leagues") or {}).get(rival)
    own_league = row.get("league")
    same_league = bool(rival_league and own_league and rival_league == own_league)
    evidence_complete = bool(
        row.get("rank") == 1
        or (not rival_missing and capacity_observed and rival_league and own_league)
    )
    return {
        "schema_version": 1,
        "kind": "endgame_replay",
        "run": row.get("run"),
        "rival": rival,
        "rank": row.get("rank"),
        "animal_capacity": capacity,
        "same_league": same_league,
        "objective_rival_observed": not rival_missing,
        "capacity_observed": capacity_observed,
        "objective_evidence_complete": evidence_complete,
        "inputs": {
            "animals": A0, "produce": P0, "coins": C0, "feed": F0,
            "rival_animals": Aj, "rival_produce": Pj,
            "rival_growth_per_min": gj, "our_growth_per_min": g,
        },
        "options": options,
        "best": best,
        "safe_path": bool(
            row.get("rank") == 1
            or (evidence_complete and best and same_league)
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--growth", type=float, default=None,
                    help="our adoption rate, animals/min (default: measured)")
    ap.add_argument("--rival", default=None)
    args = ap.parse_args()

    row = latest_row()
    result = analyze(row, args.rival, args.growth)
    inputs = result["inputs"]
    print("run %s" % result["run"])
    print("  us   : herd %-8d produce %-12d coins %-10d feed %d"
          % (inputs["animals"], inputs["produce"], inputs["coins"], inputs["feed"]))
    print("  %-5s: herd %-8d produce %-12d growth %.1f/min"
          % (result["rival"], inputs["rival_animals"], inputs["rival_produce"], inputs["rival_growth_per_min"]))
    print("  our adoption %.1f/min | yield %.4f/animal/min | burn %.3f feed/animal/min"
          % (inputs["our_growth_per_min"], YIELD, BURN))
    print("%-9s %-9s %-9s %-9s %-9s %s" %
          ("target", "win(h)", "herd@win", "coins@win", "starved", "verdict"))
    for option in result["options"]:
        print("%-9d %-9s %-9d %-9d %-9s %s" % (
            option["target"],
            ("%.2f" % (option["win_minutes"] / 60.0)) if option["win_minutes"] else "-",
            option["animals_at_end"], option["coins_at_end"],
            "yes" if option["starved_at_minute"] else "no",
            "wins" if option["safe_win"] else "no safe win within 24h",
        ))
    if result["best"]:
        print("RECOMMENDED herd ceiling: %d" % result["best"]["target"])
    else:
        print("No safe winning policy found at this adoption rate.")
    if os.environ.get("FARM_PROBE_ID"):
        state = os.path.realpath(os.environ.get("FARM_STATE_DIR", os.path.join(ROOT, "state")))
        with open(os.path.join(state, "endgame_replay.json"), "w") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
