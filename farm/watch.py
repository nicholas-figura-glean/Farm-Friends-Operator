"""Anomaly detection: the only thing that should ever wake an LLM.

Each detector returns a short human-readable string. An empty list means the run
was routine and no model needs to think about it. False positives are expensive
(they cost tokens and attention), so every detector here is written to tolerate
normal variation: short intervals, growth dilution, and empty collections.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import rules


def _wall_minutes(a: Optional[str], b: Optional[str]) -> Optional[float]:
    if not a or not b:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        return (datetime.strptime(a, fmt) - datetime.strptime(b, fmt)).total_seconds() / 60.0
    except ValueError:
        return None


def evaluate(
    row: Dict[str, Any],
    prev: Optional[Dict[str, Any]],
    history: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[str], bool]:
    out: List[str] = []
    # Observations that belong in the record but must never wake a model.
    soft: List[str] = []
    # Facts about the standings. Every one of these is a PREMISE of the race we
    # are deliberately running, not an incident: we are #2, the leader is ahead on
    # lifetime produce, and he is still producing. They fired on every single run
    # and each one escalated to an LLM "for judgement", which is precisely the
    # cost this project exists to eliminate - and the judgement they asked for is
    # now computed for free by rules.win_projection().
    #
    # So they are held here and resolved once the projection is known: soft while
    # we are on track to pass the leader, escalated when we are not. Being behind
    # is not news; being behind with no path to the front is.
    competitive: List[str] = []

    if row.get("rank") != 1:
        competitive.append("RANK LOST: now #%s" % row.get("rank"))

    # The score rate: lifetime produce per minute. This is the authoritative
    # production signal because produce accrues as animals produce, whether or not
    # we collect it, and because collect_produce returns nothing while the herd is
    # hungry (the produce then banks during feed_animals). A collapse here is the
    # only failure that can actually lose the game - notably hunger 70, where
    # production stops while every other signal still looks ordinary.
    produce_minutes = _wall_minutes(row.get("ts"), (prev or {}).get("ts"))
    produce_delta = None
    if prev and row.get("produce") is not None and prev.get("produce") is not None:
        produce_delta = row["produce"] - prev["produce"]
    bad_rate = rules.produce_rate_trouble(produce_delta, produce_minutes, row.get("animals"))
    # Record the rate on the row so the next run can see this one. Lifetime
    # produce arrives in bursts (per-animal timers, and the leaderboard figure
    # lags), so single windows are lumpy: replaying history, runs 40, 46 and 55
    # each read 105-246/min immediately before a 1,600-2,000/min window. A real
    # stall - hunger 70, or a server change - persists, so alerting requires two
    # consecutive low windows. That costs one cycle of detection latency and
    # removes three false wake-ups out of 56 runs.
    if produce_delta is not None and produce_minutes:
        row["produce_per_min"] = round(produce_delta / produce_minutes, 1)
    prev_rate = (prev or {}).get("produce_per_min")
    prev_low = (
        prev_rate is not None
        and prev_rate < rules.produce_floor((prev or {}).get("animals") or row.get("animals"))
    )
    produce_healthy = (
        produce_delta is not None
        and produce_minutes is not None
        and produce_minutes >= rules.MIN_INTERVAL_FOR_PRODUCE_CHECK
        and bad_rate is None
    )
    if bad_rate is not None and prev_low:
        out.append(
            "PRODUCTION: %.0f produce/min over %.1f min, below the %.0f/min floor "
            "for two runs running (hunger %s, %d animals)"
            % (
                bad_rate,
                produce_minutes,
                rules.produce_floor(row.get("animals")),
                row.get("max_hunger"),
                row.get("animals") or 0,
            )
        )
    elif bad_rate is not None:
        soft.append(
            "score rate %.0f produce/min below floor for one run - watching" % bad_rate
        )
    elif produce_healthy:
        soft.append(
            "score rate %.0f produce/min over %.1f min"
            % (produce_delta / produce_minutes, produce_minutes)
        )

    # Throughput, measured per chicken per minute over the real interval. Only
    # judged on samples long enough to mean anything, and only treated as an
    # incident when produce is actually piling up or the herd is going hungry.
    # Without the backlog test this detector fired on almost every run at herd
    # scale and was the single largest source of token spend.
    rate = row.get("units_per_chicken_min")
    interval = row.get("interval_min")
    lo, hi = rules.UNITS_PER_CHICKEN_MIN_BAND
    if (
        rate is not None
        and interval is not None
        and interval >= rules.MIN_INTERVAL_FOR_RATE_CHECK
        and row.get("units_collected", 0) > 0
        and not (lo <= rate <= hi)
    ):
        drained = rules.backlog_drained(row.get("ready_units") or 0, row.get("animals") or 0)
        hunger_ok = (row.get("max_hunger") or 0) < rules.HUNGER_ALARM
        if rate < lo and (drained or produce_healthy) and hunger_ok:
            # Nothing was left to collect, or the score rate proves production is
            # fine and this run simply banked its produce during feed_animals.
            # Either way the loop is keeping up: a measurement artifact, not an
            # incident. Without this the detector fires nearly every run.
            soft.append(
                "throughput %.3f below band but %s and hunger %s"
                % (
                    rate,
                    "backlog drained (%s ready)" % row.get("ready_units")
                    if drained
                    else "score rate healthy",
                    row.get("max_hunger"),
                )
            )
        else:
            out.append(
                "throughput %.3f units/chicken/min outside band %.2f-%.2f over %.1f min "
                "(%s ready, hunger %s)"
                % (rate, lo, hi, interval, row.get("ready_units"), row.get("max_hunger"))
            )

    # A sustained streak of empty collections used to mean production had stopped.
    # It no longer does on its own: collect_produce returns nothing whenever the
    # herd is hungry, and the produce banks during the feed call instead. Only
    # escalate when the score rate does not contradict it.
    streak = int(row.get("zero_streak", 0))
    if streak >= rules.ZERO_COLLECT_RUNS_TO_ALARM and not produce_healthy:
        out.append(
            "no produce collected in %d consecutive runs - production may have stopped"
            % streak
        )

    if row.get("max_hunger", 0) >= rules.HUNGER_ALARM:
        out.append(
            "hunger %d at/above alarm %d (production stops at %d)"
            % (row["max_hunger"], rules.HUNGER_ALARM, rules.HUNGER_STOP)
        )

    # A shortfall against the ABSOLUTE target is a proxy, and it must be material
    # before it is allowed to throttle adoption (its remedy halves the adopt cap,
    # i.e. it slows the only thing that scores).
    #
    # Runs 337-347 are why there is a tolerance. Expansion adopts concurrently, so
    # `reserve_target` is computed from the FINAL animal count while `buy_feed` was
    # sized from an earlier one. That guarantees a small structural shortfall
    # whenever we are growing fast: run 337 was short 390 feed out of 789,135
    # (0.05%) and the healer responded by cutting the adopt cap 400 -> 200, then
    # 200 -> 100, then 100 -> 50 over the next three runs. 15 of 20 firings in the
    # last 120 runs had a completely healthy runway.
    #
    # So: tolerate a shortfall smaller than one round of concurrent growth, and
    # let the runway detector below be the real safety net - it is expressed in
    # the unit of the threat (lesson 1).
    feed_now = row.get("feed", 0)
    target = row.get("reserve_target", 0)
    shortfall = target - feed_now
    tolerance = max(
        rules.FEED_RESERVE_TOLERANCE_MIN,
        int(target * rules.FEED_RESERVE_TOLERANCE_FRACTION),
    )
    if shortfall > 0:
        if shortfall <= tolerance:
            soft.append(
                "feed %d is %d under reserve target %d (within %d tolerance, "
                "concurrent adoption)" % (feed_now, shortfall, target, tolerance)
            )
        else:
            out.append("feed %d below reserve target %d" % (feed_now, target))

    # Runway, not absolute count. Before run 291 the reserve alert fired at
    # "feed 17513 below reserve target 23753" and was healed as routine, because
    # nothing said that the whole reserve was only ~20 minutes of feed. The loop
    # can be asleep for hours, so the buffer has to be judged in minutes.
    buffer_min = rules.feed_buffer_minutes(row.get("feed", 0), row.get("animals", 0) or 0)
    if buffer_min < rules.FEED_BUFFER_MIN_MINUTES:
        out.append(
            "feed runway %.0f min below %d min floor (%d feed at %d animals) - "
            "an outage longer than this starves the herd"
            % (
                buffer_min,
                rules.FEED_BUFFER_MIN_MINUTES,
                row.get("feed", 0),
                row.get("animals", 0) or 0,
            )
        )

    # A long gap between runs is itself the incident: launchd StartInterval does
    # not fire while the Mac is asleep, and run 291 lost 19.3 hours this way.
    interval = row.get("interval_min") or 0
    if interval > rules.RUN_GAP_ALARM_MINUTES:
        out.append(
            "SCHEDULE GAP: %.0f min since the previous run (cadence is %ds) - "
            "the loop was not running"
            % (interval, rules.CYCLE_INTERVAL_SECONDS)
        )

    if row.get("adopt_failures"):
        out.append("%d adopt call failures" % row["adopt_failures"])

    if row.get("tools_changed"):
        out.append("tools/list changed - new or removed server capability")

    # The pre-action sentinel has already failed closed in the named domains.
    # Emit only rising-edge signals (plus bounded routing reminders) so the
    # question/probe pipeline investigates the regime change without turning a
    # persistent hold into repetitive model spend.
    for signal in (row.get("novelty") or {}).get("signals") or []:
        alert = signal.get("alert")
        if alert and alert not in out:
            out.append(str(alert))

    if row.get("trades_in"):
        out.append("%d incoming trade(s) pending review" % row["trades_in"])

    # Coin outflow through trade is a strategic invariant, not merely a nominal
    # value check. Feed can always be bought from the neutral store at 1:1;
    # transferring coins to a rival lets them compound immediately into animals.
    if row.get("trade_coin_outflow"):
        out.append(
            "TRADE POLICY BREACH: %d coins transferred through inbound trade"
            % row["trade_coin_outflow"]
        )
    blocked_trade_coins = int(row.get("trade_coin_outflow_blocked") or 0)
    if blocked_trade_coins:
        soft.append(
            "trade guard preserved %d coins from inbound offers" % blocked_trade_coins
        )

    # A stray retry among dozens of calls is ordinary network noise, not an
    # incident. Bulk operations are now constant-time, so every tool participates
    # in the same transport-health signal.
    retries = rules.core_transport_errors(
        row.get("transport_errors_by_tool"), row.get("transport_errors") or 0
    )
    calls = row.get("calls") or 0
    transport_trouble = rules.transport_trouble(retries, calls)
    if transport_trouble:
        out.append("%d transport retries across %d calls" % (retries, calls))
    elif retries:
        soft.append("%d transport retries across %d calls (below alarm)" % (retries, calls))

    risk_counts = row.get("risk_event_counts") or {}
    if risk_counts:
        soft.append(
            "daily risk observed: %s; automatic charges %d coins"
            % (
                ", ".join("%s=%d" % item for item in sorted(risk_counts.items())),
                int(row.get("risk_charges") or 0),
            )
        )
    if (row.get("coins") or 0) < rules.RISK_COIN_RESERVE:
        soft.append(
            "cash %d below %d daily-risk reserve; expansion remains paused until replenished"
            % (row.get("coins") or 0, rules.RISK_COIN_RESERVE)
        )

    # A backed-off rate recovers on its own, so this is only an incident when the
    # rate is pinned near the floor AND trouble is sustained across two runs.
    # Alerting on a single bad run made recovery itself look like an incident.
    prev_trouble = bool(prev) and (
        rules.transport_trouble(
            rules.core_transport_errors(
                prev.get("transport_errors_by_tool"),
                prev.get("transport_errors") or 0,
            ),
            prev.get("calls") or 0,
        )
        or prev.get("adopt_failures")
    )
    if (
        (row.get("call_rate") or 0) <= rules.MIN_CALLS_PER_SECOND * 1.2
        and (row.get("adopt_failures") or transport_trouble)
        and prev_trouble
    ):
        out.append(
            "call rate pinned at %.2f/s with sustained failures - server pushing back"
            % row["call_rate"]
        )

    for note in row.get("notes") or []:
        out.append(note)

    if prev:
        ours = None
        if row.get("produce") is not None and prev.get("produce") is not None:
            ours = row["produce"] - prev["produce"]
        prev_rivals = prev.get("rivals") or {}
        prior_ours = prev.get("our_produce_gain")
        prior_rival_gains = prev.get("rival_gains") or {}
        rival_gains = {}
        for name, produce in (row.get("rivals") or {}).items():
            before = prev_rivals.get(name)
            if before is not None and ours and ours > 0:
                gained = produce - before
                rival_gains[name] = gained
                # Rival and farm produce arrive in lumpy, independently sampled
                # windows. A one-window share spike is not a strategic threat;
                # require the same rival to keep that share for two windows.
                prior_gained = prior_rival_gains.get(name)
                sustained = (
                    prior_ours is not None
                    and prior_ours > 0
                    and prior_gained is not None
                    and prior_gained >= rules.THREAT_SHARE * prior_ours
                )
                if sustained and gained >= rules.THREAT_SHARE * ours:
                    competitive.append(
                        "THREAT: %s gained %d vs our %d (>= %d%%)"
                        % (name, gained, ours, int(rules.THREAT_SHARE * 100))
                    )
            if produce >= (row.get("produce") or 0):
                competitive.append("%s has passed us on lifetime produce" % name)
        if ours is not None:
            row["our_produce_gain"] = ours
        if rival_gains:
            row["rival_gains"] = rival_gains

        # --- the objective ------------------------------------------------
        # Every other detector watches a proxy. This one watches the score.
        #
        # It exists because the two most expensive faults in this project's
        # history were both invisible to the proxies: the growth gate froze the
        # herd for 246 runs, and the healer ratcheted the adopt cap 400 -> 50,
        # and in each case hunger, runway, throughput and call rate all looked
        # perfect. A projection that says "we now pass the leader NEVER instead
        # of in 9 hours" would have caught both on the run they started.
        interval = row.get("interval_min") or 0
        our_growth = 0.0
        if interval > 0 and prev.get("animals"):
            our_growth = max(0.0, (row.get("animals", 0) - prev["animals"]) / interval)

        herds = row.get("rival_herds") or {}
        prev_herds = prev.get("rival_herds") or {}
        leader = None
        for name, produce in (row.get("rivals") or {}).items():
            if produce > (row.get("produce") or 0) and (
                leader is None or produce > row["rivals"][leader]
            ):
                leader = name

        if leader and interval > 0 and rival_gains.get(leader) is not None:
            r_herd = herds.get(leader) or 0
            r_growth = 0.0
            if prev_herds.get(leader) is not None and r_herd:
                r_growth = max(0.0, (r_herd - prev_herds[leader]) / interval)

            # The leader's produce arrives in lumpy, independently sampled
            # windows: across runs 344-353 John's measured rate swung between
            # 884 and 8,321 produce/min while his herd never moved. Feeding a
            # single sample into the projection made the ETA swing 6-12 h, which
            # is worthless as an alert - one unlucky sample would either raise a
            # false "NO PATH TO WIN" (an LLM escalation, so real money) or hide a
            # genuine one behind a lucky sample.
            #
            # So the rate is smoothed exponentially. Only `prev` is available
            # here, so the smoothed value is carried forward in the row itself.
            sample = rival_gains[leader] / interval
            prior = (prev.get("projection") or {}).get("rival_rate_ewma")
            if prior is None:
                r_rate = sample
            else:
                a_ = rules.RIVAL_RATE_EWMA_ALPHA
                r_rate = a_ * sample + (1.0 - a_) * prior

            proj = rules.win_projection(
                our_produce=row.get("produce") or 0,
                our_rate=row.get("produce_per_min") or 0.0,
                our_herd=row.get("animals") or 0,
                our_growth_per_min=our_growth,
                rival_produce=row["rivals"][leader],
                rival_rate=r_rate,
                rival_herd=r_herd,
                rival_growth_per_min=r_growth,
            )
            proj["leader"] = leader
            proj["rival_rate_ewma"] = round(r_rate, 1)
            proj["rival_rate_sample"] = round(sample, 1)
            proj["our_growth_per_min"] = round(our_growth, 1)
            proj["rival_growth_per_min"] = round(r_growth, 1)
            proj["herd_to_match_rate"] = rules.herd_to_out_rate(
                r_rate, proj["our_yield"]
            )
            row["projection"] = proj

            eta = proj.get("eta_min")
            if eta is None:
                # No positive root: waiting does not win. Either their rate is
                # higher and they are growing at least as fast as us, or we have
                # stopped adopting. This is the alert that should never be
                # healed by throttling anything.
                out.append(
                    "NO PATH TO WIN: %s leads by %d at %+.0f produce/min; we adopt "
                    "%.0f/min vs their %.0f/min - need herd %d to match their rate "
                    "(we have %d)"
                    % (
                        leader,
                        proj["lead"],
                        proj["deficit_rate"],
                        our_growth,
                        r_growth,
                        proj["herd_to_match_rate"],
                        row.get("animals") or 0,
                    )
                )
            elif eta > rules.WIN_ETA_ALARM_HOURS * 60:
                out.append(
                    "WIN ETA %.1f h exceeds %.0f h: %s leads by %d, we adopt %.0f/min"
                    % (
                        eta / 60.0,
                        rules.WIN_ETA_ALARM_HOURS,
                        leader,
                        proj["lead"],
                        our_growth,
                    )
                )
            else:
                soft.append(
                    "on track: pass %s in %.1f h (lead %d, we adopt %.0f/min, "
                    "they adopt %.0f/min)"
                    % (leader, eta / 60.0, proj["lead"], our_growth, r_growth)
                )

        # A rival whose herd starts growing again is a different game: their rate
        # stops being capped. John sat frozen at 56,061 animals on 76 coins while
        # we closed 480k of his lead; if he starts adopting, the projection above
        # goes from 9 hours to never, and we would want to know on run one.
        for name, herd in herds.items():
            before = prev_herds.get(name)
            if before is None or not herd:
                continue
            grew = herd - before
            if grew >= rules.RIVAL_HERD_GROWTH_ALARM and herd > (row.get("animals") or 0) * 0.5:
                competitive.append(
                    "RIVAL GROWING: %s herd %d -> %d (+%d) on %s coins"
                    % (name, before, herd, grew, (row.get("rival_coins") or {}).get(name))
                )

        if row.get("animals", 0) < prev.get("animals", 0):
            out.append("animal count fell from %d to %d" % (prev["animals"], row["animals"]))

    # Pure multi-run self-audit. This is intentionally late in evaluation: it
    # watches whether standing decisions still pay, not whether this cycle is
    # operationally healthy. Every finding opens a durable question and is
    # structurally unreachable from healing remedies.
    if history is not None:
        audit_rows = list(history)
        if not audit_rows or audit_rows[-1].get("run") != row.get("run"):
            audit_rows.append(row)
        findings = rules.strategy_audit(audit_rows)
        row["strategy_audit"] = findings
        for finding in findings:
            alert = finding.get("alert")
            if alert and alert not in out:
                out.append(alert)
    for alert in row.get("recon_findings") or []:
        if alert and alert not in out:
            out.append(alert)

    # Resolve the standings facts against the projection. Being behind is the
    # premise of the race; being behind with no way through is the incident.
    if competitive:
        proj = row.get("projection") or {}
        eta = proj.get("eta_min")
        on_track = (
            bool(proj)
            and eta is not None
            and eta <= rules.WIN_ETA_ALARM_HOURS * 60
        )
        if on_track:
            soft.append(
                "standings unchanged and on track (%s); not escalating: %s"
                % (
                    "pass %s in %.1f h" % (proj.get("leader"), eta / 60.0),
                    "; ".join(competitive),
                )
            )
        else:
            # No projection, or it says we do not get there: this is the case
            # that genuinely needs judgement.
            out.extend(competitive)

    if soft:
        row.setdefault("notes_soft", []).extend(soft)
    return out, bool(out)


def per_kind_table(rows: List[Dict[str, Any]]) -> List[str]:
    """Per-kind economics for the generated journal entry."""
    lines = []
    latest = rows[-1] if rows else {}
    by_kind = latest.get("by_kind") or {}
    for kind, count in sorted(by_kind.items()):
        cost = rules.ANIMAL_COST.get(kind, 0)
        measured = rules.MEASURED_UNITS_PER_COIN_TICK.get(kind)
        lines.append(
            "  %-8s n=%-5d cost=%-3d cumulative units/coin/tick=%s"
            % (kind, count, cost, ("%.4f" % measured) if measured is not None else "n/a")
        )
    return lines
