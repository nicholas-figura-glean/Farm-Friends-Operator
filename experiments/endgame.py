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
    path = os.path.join(ROOT, "state", "history.ndjson")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--growth", type=float, default=None,
                    help="our adoption rate, animals/min (default: measured)")
    ap.add_argument("--rival", default="John")
    args = ap.parse_args()

    row = latest_row()
    proj = row.get("projection") or {}
    A0 = row["animals"]
    P0 = row["produce"]
    C0 = row["coins"]
    F0 = row["feed"]
    Aj = (row.get("rival_herds") or {}).get(args.rival) or 0
    Pj = (row.get("rivals") or {}).get(args.rival) or 0
    gj = proj.get("rival_growth_per_min") or 0.0
    g = args.growth if args.growth is not None else (proj.get("our_growth_per_min") or 118.0)

    print("run %s" % row["run"])
    print("  us   : herd %-8d produce %-12d coins %-10d feed %d"
          % (A0, P0, C0, F0))
    print("  %-5s: herd %-8d produce %-12d growth %.1f/min"
          % (args.rival, Aj, Pj, gj))
    print("  our adoption %.1f/min | yield %.4f/animal/min | burn %.3f feed/animal/min"
          % (g, YIELD, BURN))
    print("  lead to close: %d" % (Pj - P0))
    print()

    print("%-9s %-9s %-9s %-9s %-9s %s" %
          ("target", "win(h)", "herd@win", "coins@win", "starved", "verdict"))
    best = None
    for target in (A0, 70000, 80000, 90000, 100000, 112000, 125000, 150000, 200000):
        if target < A0:
            continue
        t, A, C, starved = simulate(A0, P0, C0, F0, Pj, Aj, gj, g, target)
        win_h = (t / 60.0) if t else None
        verdict = []
        if starved:
            verdict.append("STARVED at %.1fh" % (starved / 60.0))
        if t is None:
            verdict.append("no win within 24h")
        if not verdict:
            verdict.append("wins")
        line = "%-9d %-9s %-9d %-9d %-9s %s" % (
            target,
            ("%.2f" % win_h) if win_h else "-",
            A, max(int(C), 0),
            ("yes" if starved else "no"),
            ", ".join(verdict),
        )
        print(line)
        if t and not starved and (best is None or t < best[0]):
            best = (t, target, A, C)

    print()
    if best:
        t, target, A, C = best
        print("RECOMMENDED herd ceiling: %d" % target)
        print("  passes %s in %.2f h at herd %d with %d coins left, never starving"
              % (args.rival, t / 60.0, A, max(int(C), 0)))
    else:
        print("No safe winning policy found at this adoption rate.")
        print("  the binding constraint is coins: raise income or lower the reserve")

    # What the current expand.py floor allows, for contrast.
    spendable = C0 - rules.FEED_PER_ANIMAL_RESERVE * A0 * rules.FEED_COST
    can = max(0, int(spendable // (rules.ANIMAL_COST[rules.PRIMARY_KIND]
                                   + rules.FEED_PER_ANIMAL_RESERVE)))
    print()
    print("current expand.py floor reserves %d coins for feed already in the barn"
          % (rules.FEED_PER_ANIMAL_RESERVE * A0 * rules.FEED_COST))
    print("  -> it thinks only %d more animals are affordable (herd %d)" % (can, A0 + can))
    print("  barn already holds %d feed = %.0f min of runway at measured burn"
          % (F0, F0 / (BURN * A0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
