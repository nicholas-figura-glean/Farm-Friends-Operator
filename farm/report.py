"""Compact rendering. Everything the LLM sees comes from here.

Budget: routine cycle summary stays under ~600 characters so a supervising
model pays almost nothing to confirm a healthy run.
"""

from typing import Any, Dict, List, Optional

from . import rules, watch


def _fmt_items(items: Dict[str, int]) -> str:
    if not items:
        return "none"
    return ", ".join("%s %d" % (k, v) for k, v in sorted(items.items()))


def cycle_summary(row: Dict[str, Any], anomalies: List[str], needs_llm: bool) -> str:
    rivals = row.get("rivals") or {}
    top_rival, top_produce = ("none", 0)
    if rivals:
        top_rival, top_produce = max(rivals.items(), key=lambda kv: kv[1])
    produce = row.get("produce") or 0
    by_kind = row.get("by_kind") or {}
    kinds = ", ".join("%s %d" % (k, v) for k, v in sorted(by_kind.items()))
    lines = [
        "FARM %s run=%s %s"
        % (row.get("ts"), row.get("run"), "DRY" if row.get("dry") else "ok"),
        "rank=%s produce=%s gap=+%d next=%s(%s)"
        % (row.get("rank"), produce, produce - top_produce, top_rival, top_produce),
        "animals=%s (%s) hunger=%s feed=%s/%s coins=%s"
        % (
            row.get("animals"),
            kinds,
            row.get("max_hunger"),
            row.get("feed"),
            row.get("reserve_target"),
            row.get("coins"),
        ),
        "collected %s | sold %dc | feed +%d | adopted %d/%s (%s) | fed=%s"
        % (
            _fmt_items(row.get("collected") or {}),
            row.get("revenue") or 0,
            row.get("feed_bought") or 0,
            row.get("adopted") or 0,
            row.get("adopt_requested") or 0,
            row.get("adopt_stopped"),
            "y" if row.get("fed") else "n",
        ),
        "rate=%s u/chicken/min over %smin | ready=%s | verified=%s"
        % (
            row.get("units_per_chicken_min"),
            row.get("interval_min"),
            row.get("ready_units"),
            "y" if row.get("verified") else "n",
        ),
        "trades out=%s in=%s sent=%s acc=%s dec=%s | calls=%s @%s/s %.0fs"
        % (
            row.get("trades_out"),
            row.get("trades_in"),
            row.get("trades_sent"),
            row.get("trades_accepted"),
            row.get("trades_declined"),
            row.get("calls"),
            row.get("call_rate"),
            row.get("duration_s") or 0,
        ),
    ]
    for item in anomalies:
        lines.append("ANOMALY: %s" % item)
    lines.append("needs_llm: %s" % ("true" if needs_llm else "false"))
    return "\n".join(lines)


def review(
    rows: List[Dict[str, Any]],
    journal_due: bool,
    open_questions: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Digest: run trends, aggregates, and durable strategy questions."""
    if not rows:
        return "no runs recorded\nneeds_llm: true"
    lines = ["REVIEW last %d runs (%s -> %s)" % (len(rows), rows[0].get("ts"), rows[-1].get("ts"))]
    for r in rows:
        lines.append(
            "  %s run=%-4s prod=%-7s animals=%-6s adopt=%-4s rev=%-6s rate=%-7s hunger=%-3s %s"
            % (
                (r.get("ts") or "")[11:16],
                r.get("run"),
                r.get("produce"),
                r.get("animals"),
                r.get("adopted"),
                r.get("revenue"),
                r.get("units_per_chicken_min"),
                r.get("max_hunger"),
                "ANOM" if (r.get("anomalies") or r.get("notes")) else "",
            )
        )
    first, last = rows[0], rows[-1]
    gained = (last.get("produce") or 0) - (first.get("produce") or 0)
    grown = (last.get("animals") or 0) - (first.get("animals") or 0)
    revenue = sum(r.get("revenue") or 0 for r in rows)
    feed_spend = sum(r.get("feed_bought") or 0 for r in rows)
    lines.append(
        "totals: produce +%d, animals +%d, revenue %dc, feed %dc (%.1f%% of revenue)"
        % (gained, grown, revenue, feed_spend, (100.0 * feed_spend / revenue) if revenue else 0.0)
    )
    rivals = last.get("rivals") or {}
    first_rivals = first.get("rivals") or {}
    deltas = [
        "%s +%d" % (name, produce - first_rivals.get(name, produce))
        for name, produce in sorted(rivals.items(), key=lambda kv: -kv[1])
    ]
    lines.append("rivals over window: " + ", ".join(deltas))
    if journal_due:
        lines.append("journal due - per kind units/coin/animal-tick:")
        lines.extend(watch.per_kind_table(rows))
    all_anoms = [n for r in rows for n in (r.get("notes") or [])]
    for note in all_anoms[-5:]:
        lines.append("ANOMALY: %s" % note)
    questions = list(open_questions or [])
    lines.append("open strategy questions: %d" % len(questions))
    for item in questions[:5]:
        lines.append(
            "  %s %s %s (seen %sx, last run %s)"
            % (
                item.get("priority"), item.get("id"), item.get("class"),
                item.get("occurrences"), item.get("last_seen_run"),
            )
        )
    lines.append("needs_llm: %s" % ("true" if (all_anoms or journal_due) else "false"))
    return "\n".join(lines)


def dry_run_plan(row: Dict[str, Any]) -> str:
    plan = row.get("plan") or {}
    return "\n".join(
        [
            "DRY RUN (no mutations)",
            "state: animals=%s feed=%s/%s coins=%s hunger=%s ready_now=%s"
            % (
                row.get("animals"),
                row.get("feed"),
                row.get("reserve_target"),
                row.get("coins"),
                row.get("max_hunger"),
                row.get("ready_units"),
            ),
            "would: feed=%s buy_feed=%s adopt=%s"            % (
                "yes" if (row.get("max_hunger") or 0) >= rules.FEED_AT_HUNGER else "no",
                plan.get("buy_feed"),
                plan.get("adopt"),
            ),
        ]
    )
