#!/usr/bin/env python3
"""Farm Friends operator entry point.

  run.py --cycle      execute one full loop, print a <=600 char summary
  run.py --dry-run    read live state, print the decision, mutate nothing
  run.py --review N   hourly digest of the last N recorded runs
  run.py --supervise  self-heal: keep the schedule alive, remediate alerts
  run.py --heal-status show healing knobs, recent remedies and cost ledger
  run.py --self-test  parser regression against saved fixtures

Exit codes: 0 routine, 3 anomaly (LLM should look), 4 hard failure.
"""

import argparse
import errno
import fcntl
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from farm import (  # noqa: E402
    analysis,
    canary,
    claims,
    contract,
    cycle,
    growth,
    heal,
    journal,
    ledger,
    llm,
    policy,
    probes,
    progress,
    questions,
    report,
    research,
    scheduler,
    tokens,
    watch,
    workorders,
)
from farm import release as release_info  # noqa: E402
from farm.mcp import Client, McpError, ToolError  # noqa: E402
from farm.parse import ParseDrift  # noqa: E402
from farm import rules  # noqa: E402

LOCK = os.path.join(cycle.STATE_DIR, ".lock")
LOG = os.path.join(cycle.STATE_DIR, "launchd.log")
LOG_MAX_BYTES = 1_000_000
JOURNAL_EVERY = rules.JOURNAL_EVERY


def _rotate_log() -> None:
    """Keep the launchd log bounded; it is append-only from two writers."""
    try:
        if os.path.getsize(LOG) < LOG_MAX_BYTES:
            return
        with open(LOG) as fh:
            tail = fh.readlines()[-300:]
        with open(LOG, "w") as fh:
            fh.writelines(tail)
    except OSError:
        pass


class Timeout(RuntimeError):
    pass


def _arm_watchdog(seconds: int) -> None:
    """Never let one run outlive its slot and block the next.

    A stuck run once held the lock for nine minutes, costing three cycles of
    compounding. The scheduler cannot fix that on its own, so the process caps
    its own lifetime.
    """

    def _fire(signum, frame):  # noqa: ANN001
        raise Timeout("run exceeded %ds hard timeout" % seconds)

    signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)


def _lock():
    os.makedirs(cycle.STATE_DIR, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as exc:
        if exc.errno in (errno.EAGAIN, errno.EACCES):
            print("FARM skipped: previous run still active")
            sys.exit(0)
        raise
    return fh


def _align(second: int = 35) -> None:
    """Wait until N seconds past the minute so collection follows the tick."""
    now = datetime.now(timezone.utc)
    delay = second - now.second
    if 0 < delay <= second:
        time.sleep(delay)


def _run_age_seconds() -> Optional[float]:
    """Seconds since the last recorded cycle, or None if there has never been one."""
    prev = cycle.last_history()
    stamp = cycle.parse_ts((prev or {}).get("ts"))
    if not stamp:
        return None
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def do_cycle(dry: bool) -> int:
    client = Client()
    run = cycle.Cycle(client, dry_run=dry)
    prev = cycle.last_history()
    row = run.run()
    audit_window = max(
        rules.AUDIT_WINDOW_RUNS,
        rules.AUDIT_KNOB_MAX_AGE_RUNS + 2,
        rules.RIVAL_WAKE_RECENT_INTERVALS + rules.RIVAL_WAKE_BASE_ROWS + 2,
    )
    history = cycle.tail_history(audit_window)
    anomalies, needs_llm = watch.evaluate(row, prev, history=history)
    row["anomalies"] = anomalies
    if dry:
        print(report.dry_run_plan(row))
        return 0

    cycle.append_history(row)
    journal.record_alerts(row, anomalies)
    ledger.record_cycle(row, prev, anomalies)
    # Claims and research are sidecars: evidence failures cost visibility, never
    # a successful mutation cycle. Their dedicated tests still fail closed at
    # release time.
    try:
        if int(row.get("run") or 0) % rules.CLAIM_REFRESH_RUNS == 0:
            claims.refresh()
        if int(row.get("run") or 0) % rules.RESEARCH_AUDIT_RUNS == 0:
            research.run_audit()
    except Exception as exc:  # noqa: BLE001
        message = "POLICY DRIFT: epistemic sidecar failed: %s: %s" % (
            exc.__class__.__name__, str(exc)[:120]
        )
        needs_llm = True
        ledger.record("epistemic.sidecar_failed", {"error": message}, run=row.get("run"))
        journal.record_alerts(row, [message])
        try:
            questions.open_or_update(
                "policy_drift",
                message,
                row=row,
                evidence_refs=["observations.ndjson#run=%s" % row.get("run")],
            )
        except Exception:  # noqa: BLE001 - the failure channel is also best effort
            pass
    # Log the zero so "routine runs cost nothing" is auditable, not asserted.
    tokens.record_cycle(row.get("run"))
    progress.finish(
        "ok",
        run=row.get("run"),
        duration_s=row.get("duration_s"),
        anomalies=len(anomalies),
        rank=row.get("rank"),
        animals=row.get("animals"),
        revenue=row.get("revenue"),
    )

    # Mutual watchdog: the cycle agent checks that the supervisor is still
    # loaded. Each agent can therefore resurrect the other, and a single
    # `launchctl bootout` can no longer silently stop the whole system.
    try:
        supervisor = scheduler.ensure(scheduler.SUPERVISOR_LABEL)
        for action in supervisor.get("actions") or []:
            print("scheduler: %s" % action)
    except Exception:  # noqa: BLE001 - never let watchdog noise fail a good run
        pass

    # The journal entry is generated from history, not written by a model.
    journal_note = ""
    if row["run"] % JOURNAL_EVERY == 0:
        window = cycle.tail_history(JOURNAL_EVERY)
        if journal.append_entry(window, cycle.load_meta()):
            journal_note = "journal: appended entry for runs %s-%s" % (
                window[0].get("run"),
                window[-1].get("run"),
            )

    print(report.cycle_summary(row, anomalies, needs_llm))
    if journal_note:
        print(journal_note)
    return 3 if needs_llm else 0


def do_alerts(clear: bool = True) -> int:
    """The supervisor's whole job: print unacknowledged alerts, or one line.

    Alerts already remediated in Python are filtered out here: they are recorded
    in state/heal.ndjson and cost nothing. Only what survived healing is worth a
    model's attention, and printing it is the moment tokens get spent, so that is
    where the cost is booked.
    """
    meta = cycle.load_meta()
    pending = journal.pending_alerts(meta.get("alerts_acked_ts"), heal.healed_keys())
    last = cycle.last_history()
    if not pending:
        print(
            "ALERTS none | last run=%s rank=%s animals=%s produce=%s at %s"
            % (
                (last or {}).get("run"),
                (last or {}).get("rank"),
                (last or {}).get("animals"),
                (last or {}).get("produce"),
                (last or {}).get("ts"),
            )
        )
        print(release_info.line())
        print("needs_llm: false")
        return 0
    lines = ["ALERTS %d pending" % len(pending)]
    for item in pending[-12:]:
        lines.append(
            "  run=%s %s | rank=%s animals=%s hunger=%s"
            % (
                item.get("run"),
                item.get("alert"),
                item.get("rank"),
                item.get("animals"),
                item.get("max_hunger"),
            )
        )
    payload = "\n".join(lines)
    print(payload)
    if clear:
        meta["alerts_acked_ts"] = pending[-1].get("ts")
        cycle.save_meta(meta)
    tokens.record_escalation(
        (last or {}).get("run"), payload, note="%d alert(s) shown to a model" % len(pending)
    )
    print(release_info.line())
    print("needs_llm: true")
    return 3


def do_contract_status() -> int:
    """What the server looks like, and what has changed since the code was written."""
    baseline = contract.load_baseline()
    if not baseline:
        print("no contract baseline yet; run experiments/contract_watch.py once")
        return 0
    tools = baseline.get("tools") or {}
    rely = baseline.get("reliance") or {}
    unused = sorted(set(tools) - set(rely))
    print("CONTRACT baseline %s captured %s"
          % (str(baseline.get("fingerprint"))[:12], baseline.get("ts")))
    print("  %d tools exposed, %d called by our code, %d response shapes tracked"
          % (len(tools), len(rely), len(baseline.get("shapes") or {})))
    if unused:
        print("  exposed but never called: %s" % ", ".join(unused))
    rows = contract.history(12)
    if rows:
        print("  recent scans:")
        for row in rows[-8:]:
            if row.get("event") == "baseline_established":
                print("    %s  baseline established (%s tools)" % (row.get("ts"), row.get("tools")))
                continue
            print("    %s  changes=%s actionable=%s filed=%s"
                  % (row.get("ts"), row.get("changes"), row.get("actionable"), row.get("orders_filed")))
            for item in (row.get("detail") or [])[:3]:
                print("        %-11s %s" % (item.get("severity"), str(item.get("summary"))[:88]))
    return 0


def do_orders() -> int:
    """The queue between detection and repair."""
    summary = workorders.summary()
    print("WORK ORDERS total=%d open=%d breaking=%d"
          % (summary["total"], summary["open"], summary["breaking_open"]))
    by_status = summary.get("by_status") or {}
    if by_status:
        print("  " + "  ".join("%s=%d" % (k, v) for k, v in sorted(by_status.items())))
    current = workorders.current()
    if not current:
        print("  (queue empty)")
        return 0
    for order in sorted(current.values(), key=lambda o: str(o.get("ts")), reverse=True)[:12]:
        print("  %-16s %-10s %-11s %-24s %s"
              % (order.get("id"), order.get("status"), order.get("severity"),
                 order.get("kind"), str(order.get("summary"))[:60]))
        if order.get("note"):
            print("      note: %s" % str(order["note"])[:120])
    return 3 if summary["breaking_open"] else 0


def do_vcs_status() -> int:
    """What the autonomous history looks like, and whether it is safe to author."""
    from farm import vcs
    if not vcs.available():
        print("VCS unavailable: not a git repository")
        print("  the author agent will fall back to copy-based staging (no diffs, no revert)")
        return 0
    print("VCS main at %s" % vcs.short(vcs.head()))
    dirty = vcs.dirty_paths()
    if dirty:
        # Worth flagging: the author agent forks from main, so uncommitted work in the
        # live tree is invisible to it and will not be part of what it gates.
        print("  %d uncommitted tracked file(s); the author agent forks from main and"
              % len(dirty))
        print("  will not see these: %s" % ", ".join(dirty[:6]))
    else:
        print("  working tree clean")
    stale = vcs.stale_worktrees()
    if stale:
        print("  %d stale worktree record(s) from a crashed pass" % len(stale))
    print()
    print("recent history:")
    for row in vcs.recent(10):
        print("  %s  %-14s %-16s %s" % (row["sha"], row["when"][:14], row["author"][:16],
                                        row["subject"][:74]))
    tags = vcs._run(["tag", "--list", vcs.TAG_PREFIX + "*", "--sort=-creatordate"],
                    check=False).stdout.split()
    if tags:
        print()
        print("release tags (newest first): %s" % ", ".join(tags[:6]))
    return 0


def do_canary_status() -> int:
    """Is a release currently on probation, and how is it doing?"""
    info = canary.status()
    if not info.get("status") or info["status"] == canary.INACTIVE:
        print("CANARY inactive: no provisional release")
        return 0
    print("CANARY %s revision=%s previous=%s armed=%s"
          % (info["status"], info.get("revision"), info.get("previous"), info.get("armed_ts")))
    if info.get("order_id"):
        print("  from work order %s" % info["order_id"])
    verdict = info.get("verdict")
    if verdict:
        baseline = verdict.get("baseline_rate")
        observed = verdict.get("observed_rate")
        print("  runs observed: %s" % verdict.get("runs_observed"))
        # Absolute rate is context only. The floor belongs on the per-animal line
        # because that is the figure the verdict is actually measured against;
        # printing a per-animal threshold beside absolute rates read as "floor=0.3".
        print("  produce/min: baseline=%s observed=%s"
              % ("%.1f" % baseline if baseline else "n/a",
                 "%.1f" % observed if observed else "n/a"))
        if verdict.get("observed_per_animal") is not None:
            print("  per animal:  baseline=%.4f observed=%.4f floor=%s  <- decides"
                  % (verdict.get("baseline_per_animal") or 0.0,
                     verdict.get("observed_per_animal") or 0.0,
                     "%.4f" % verdict["threshold"] if verdict.get("threshold") else "n/a"))
        if verdict.get("excluded_runs"):
            print("  excluded %s (%s): outside loss, not the release's doing"
                  % (verdict["excluded_runs"], verdict.get("excluded_reason")))
        print("  verdict: %s (%s)" % (verdict.get("status"), verdict.get("reason")))
    if info.get("resolution"):
        print("  resolved %s: %s" % (info.get("resolved_ts"), info["resolution"]))
    return 0


def do_llm_status() -> int:
    """Headless model reachability and what authoring has cost.

    Prints why the backend is dormant when it is, because 'the author agent did
    nothing' is otherwise indistinguishable from 'there was nothing to do'.
    """
    availability = llm.availability()
    print("MODEL %s" % ("available" if availability.get("available") else "DORMANT"))
    print("  reason: %s" % availability.get("reason"))
    if availability.get("expires_ts"):
        remaining = availability.get("remaining_seconds") or 0
        print("  token expires %s (%.1fh remaining, refreshed only by Glean Desktop)"
              % (availability["expires_ts"], remaining / 3600.0))
    if availability.get("available"):
        try:
            print("  model: %s" % llm.pick_model())
        except Exception as exc:  # noqa: BLE001
            print("  model selection failed: %s" % str(exc)[:160])

    author_rows = [r for r in tokens.tail(600) if r.get("kind") == "author"]
    spend = round(sum(float(r.get("cost_usd") or 0.0) for r in author_rows), 4)
    print("  authoring passes recorded: %d, total $%.4f" % (len(author_rows), spend))
    print("  budget: %d passes/day, $%.2f/day"
          % (rules.AUTHOR_MAX_ORDERS_PER_DAY, rules.AUTHOR_MAX_COST_USD_PER_DAY))
    for row in author_rows[-5:]:
        print("    %s  %6d in %6d out  $%.4f  %s"
              % (row.get("ts"), row.get("tokens_in") or 0, row.get("tokens_out") or 0,
                 float(row.get("cost_usd") or 0.0), str(row.get("note"))[:70]))
    return 0


def do_supervise(cadence: int = 180) -> int:
    """The self-healing pass. Cheap, deterministic, and safe to run often.

    Order matters: the schedule is repaired first (a dead scheduler makes every
    other signal meaningless), then a stale loop is recovered, then alerts are
    remediated. Only what healing could not fix is left pending for a model.
    """
    notes: List[str] = []
    age = _run_age_seconds()

    # 1. Keep the schedule alive.
    try:
        info = scheduler.ensure(scheduler.CYCLE_LABEL, stale_seconds=cadence * 2, age_seconds=age)
        notes.extend(info.get("actions") or [])
        supervisor = scheduler.ensure(scheduler.SUPERVISOR_LABEL)
        notes.extend(supervisor.get("actions") or [])
        # Herd growth is the score, so the expansion agent is load-bearing.
        expand = scheduler.ensure(scheduler.EXPAND_LABEL)
        notes.extend(expand.get("actions") or [])
        # The self-healing pair. A dead contract watcher means server changes go
        # unnoticed; a dead author means they go unrepaired.
        for label in (scheduler.CONTRACT_LABEL, scheduler.AUTHOR_LABEL, scheduler.RESEARCH_LABEL):
            try:
                agent = scheduler.ensure(label)
                notes.extend(agent.get("actions") or [])
            except Exception as exc:  # noqa: BLE001
                notes.append("%s check failed: %s" % (label.rsplit(".", 1)[-1], str(exc)[:60]))
        loaded = bool(info.get("loaded"))
    except Exception as exc:  # noqa: BLE001
        notes.append("scheduler check failed: %s" % str(exc)[:100])
        loaded = False

    # 1b. Judge any provisional release BEFORE anything else runs the code. A
    #     regressed release has to be reverted before the recovery cycle below
    #     executes it, or the recovery itself runs the broken build.
    canary_note = ""
    try:
        verdict = canary.evaluate()
        if verdict.get("status") in (canary.HEALTHY, canary.REGRESSED):
            outcome = canary.resolve(verdict)
            if verdict["status"] == canary.REGRESSED:
                canary_note = "canary REVERTED %s -> %s: %s" % (
                    outcome.get("revision"),
                    outcome.get("now_live") or outcome.get("previous"),
                    verdict.get("reason", ""),
                )
                if not outcome.get("reverted"):
                    canary_note += " (revert FAILED: %s)" % outcome.get("error")
            else:
                canary_note = "canary cleared %s: %s" % (
                    outcome.get("revision"), verdict.get("reason", ""),
                )
            notes.append(canary_note)
        elif verdict.get("status") == canary.WATCHING:
            canary_note = "canary watching %s (%s)" % (
                verdict.get("revision"), verdict.get("reason", ""),
            )
    except Exception as exc:  # noqa: BLE001
        notes.append("canary check failed: %s" % str(exc)[:100])

    # 2. Recover a stale loop by running one cycle inline, under the same lock
    #    the scheduled runs use, so this can never double-run the farm.
    recovered = False
    stale_limit = cadence * 4
    if age is None or age > stale_limit:
        handle = _lock()
        try:
            _arm_watchdog(rules.CYCLE_HARD_TIMEOUT)
            notes.append(
                "loop stale (%s) - running recovery cycle"
                % ("never run" if age is None else "%.0fs" % age)
            )
            do_cycle(dry=False)
            recovered = True
        except (ParseDrift, McpError, Timeout) as exc:
            notes.append("recovery cycle failed: %s: %s" % (exc.__class__.__name__, str(exc)[:80]))
        except SystemExit:
            notes.append("recovery skipped: a run already holds the lock")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    # 3. Remediate what can be remediated.
    meta = cycle.load_meta()
    last = cycle.last_history() or {}
    pending = journal.pending_alerts(meta.get("alerts_acked_ts"), heal.healed_keys())
    result = heal.process(pending, last, last.get("run"))
    healed = result["healed"]
    escalated = result["escalated"]
    questioned = result.get("questions") or []
    probe_result = None
    try:
        probe_result = probes.maybe_run(questions.open_questions(), last.get("run"))
    except Exception as exc:  # noqa: BLE001
        notes.append("probe scheduler failed: %s" % str(exc)[:100])
    if healed:
        tokens.record_heal(
            last.get("run"),
            len(healed),
            note="; ".join("%s: %s" % (h["class"], h["action"]) for h in healed)[:200],
        )

    summary = tokens.summary()
    age_text = "never" if age is None else "%.1fm" % (age / 60.0)
    print(
        "SUPERVISE scheduler=%s last_run=%s healed=%d questions=%d escalated=%d "
        "cost_run=$%.4f avoided=$%.4f"
        % (
            "ok" if loaded else "repaired" if notes else "down",
            age_text,
            len(healed),
            len(questioned),
            len(escalated),
            float(summary["latest"].get("cost_usd") or 0.0),
            float(summary["avoided_cost_usd"] or 0.0),
        )
    )
    for item in healed:
        print("  healed %s: %s" % (item["class"], item["action"]))
    if canary_note:
        print("  %s" % canary_note)
    for item in result.get("relaxed") or []:
        print("  relaxed %s" % item)
    for item in questioned:
        print(
            "  question %s %s occurrences=%s opened=%s"
            % (item["class"], item["question_id"], item["occurrences"], item["opened"])
        )
    if probe_result:
        print("  probe %s: %s" % (probe_result.get("probe_id"), probe_result.get("status")))
    escalation_payloads = []
    for item in escalated:
        print("  ESCALATE %s (%s): %s" % (item["class"], item["reason"], item["alert"]))
        if item.get("decision_bundle"):
            import json
            payload = json.dumps(
                {
                    "question_id": item.get("question_id"),
                    "alert": item.get("alert"),
                    "decision_bundle": item.get("decision_bundle"),
                },
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )[:30_000]
            escalation_payloads.append(payload)
            print("  DECISION_BUNDLE %s" % payload)
    if escalation_payloads:
        combined = "\n".join(escalation_payloads)
        tokens.record_escalation(
            last.get("run"),
            combined,
            note="%d first-occurrence strategic question(s)" % len(escalation_payloads),
        )
    for note in notes:
        print("  %s" % note)
    if recovered:
        print("  recovery cycle completed")
    print("needs_llm: %s" % ("true" if escalated else "false"))
    return 3 if escalated else 0


def do_heal_status() -> int:
    """Everything the healer is currently doing, and what it has cost."""
    knobs = heal.effective_knobs()
    summary = tokens.summary()
    print(
        "KNOBS rate=%.2f/s adopt_cap=%d workers=%d collect_passes=%d"
        % (
            knobs["rate_ceiling"],
            knobs["adopt_cap"],
            knobs["adopt_workers"],
            knobs["collect_passes"],
        )
    )
    print("overrides: %s" % (knobs["overrides"] or "none (all defaults)"))
    verdict = growth.status()
    print(
        "GROWTH %s cap=%s herd=%s recent=%s/min window=%s/min"
        % (
            "SATURATED" if verdict.get("saturated") else "growing",
            verdict.get("cap"),
            verdict.get("herd"),
            verdict.get("recent_units_per_min"),
            verdict.get("smaller_units_per_min"),
        )
    )
    print("  %s" % (verdict.get("reason") or "no measurement yet"))
    print(
        "COST run=$%.4f 24h=$%.4f total=$%.4f escalations=%d healed=%d avoided=$%.4f"
        % (
            float(summary["latest"].get("cost_usd") or 0.0),
            float(summary["window_cost_usd"] or 0.0),
            float(summary["total_cost_usd"] or 0.0),
            int(summary["total_escalations"] or 0),
            int(summary["total_healed"] or 0),
            float(summary["avoided_cost_usd"] or 0.0),
        )
    )
    for item in heal.recent(12):
        print(
            "  %s run=%s %s: %s"
            % (item.get("ts"), item.get("run"), item.get("class"), item.get("action"))
        )
    open_items = questions.open_questions()
    print("QUESTIONS open=%d" % len(open_items))
    for item in open_items[:12]:
        print(
            "  %s %s %s seen=%s x%s"
            % (
                item.get("priority"), item.get("id"), item.get("class"),
                item.get("last_seen_run"), item.get("occurrences"),
            )
        )
    print(release_info.line())
    return 0


def do_journal(force: bool) -> int:
    window = cycle.tail_history(JOURNAL_EVERY)
    if not window:
        print("journal: no history yet")
        return 0
    if not force and window[-1].get("run", 0) % JOURNAL_EVERY != 0:
        print("journal: not due (run %s)" % window[-1].get("run"))
        return 0
    journal.append_entry(window, cycle.load_meta())
    print(
        "journal: appended entry for runs %s-%s"
        % (window[0].get("run"), window[-1].get("run"))
    )
    return 0


def do_health(stale_minutes: int = 11) -> int:
    """Backstop: self-heal if the 5-minute loop is stale, else cheap guard."""
    prev = cycle.last_history()
    age = None
    if prev and prev.get("ts"):
        try:
            last = datetime.strptime(prev["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            age = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
        except ValueError:
            age = None

    if age is None or age > stale_minutes:
        print(
            "ANOMALY: primary loop stale (%s) - running a full cycle now"
            % ("never run" if age is None else "%.0f min" % age)
        )
        code = do_cycle(dry=False)
        return 3 if code == 0 else code

    client = Client()
    run = cycle.Cycle(client)
    with ledger.bind(
        actor="health",
        run=(prev or {}).get("run"),
        policy_id=run.policy.get("policy_id"),
        claim_registry_version=run.policy.get("claim_registry_version"),
    ):
        state = run.backstop()
    print(
        "HEALTH ok last_cycle=%.0fm animals=%s hunger=%s feed=%s/%s coins=%s acted=%s calls=%s"
        % (
            age,
            state["animals"],
            state["max_hunger"],
            state["feed"],
            state["reserve_target"],
            state["coins"],
            ", ".join(state["acted"]) or "nothing",
            state["calls"],
        )
    )
    print("needs_llm: %s" % ("true" if state["acted"] else "false"))
    return 3 if state["acted"] else 0


def do_review(n: int) -> int:
    rows = cycle.tail_history(n)
    meta = cycle.load_meta()
    journal_due = int(meta.get("run", 0)) % JOURNAL_EVERY == 0
    text = report.review(rows, journal_due, questions.open_questions())
    print(text)
    print(release_info.line())
    return 3 if text.rstrip().endswith("true") else 0


def do_questions(include_closed: bool = False) -> int:
    rows = questions.load_all() if include_closed else questions.open_questions()
    print("QUESTIONS %d %s" % (len(rows), "total" if include_closed else "open"))
    for item in rows:
        print(
            "%s %-8s %-18s seen=%s x%s %s"
            % (
                item.get("id"), item.get("status"), item.get("class"),
                item.get("last_seen_run"), item.get("occurrences"), item.get("alert"),
            )
        )
        print("  hypothesis: %s" % item.get("hypothesis"))
        print("  settles: %s" % item.get("settle_measurement"))
    return 0


def do_sweep() -> int:
    import json
    print(json.dumps(research.counterfactual_sweep(), indent=2, sort_keys=True, allow_nan=False))
    return 0


def do_knowledge_refresh(promote_policy: bool = False) -> int:
    registry = claims.refresh()
    audit = research.semantic_audit(registry=registry)
    candidate = policy.compile_snapshot(registry)
    print(
        "KNOWLEDGE registry=%s claims=%d run=%s semantic=%s policy=%s"
        % (
            registry.get("registry_version"), len(registry.get("claims") or []),
            registry.get("current_run"), "ok" if audit.get("ok") else "FAILED",
            candidate.get("policy_id"),
        )
    )
    for warning in audit.get("warnings") or []:
        print("  warning: %s" % warning)
    for error in audit.get("errors") or []:
        print("  ERROR: %s" % error)
    if not audit.get("ok"):
        return 4
    if promote_policy:
        promoted = policy.promote(candidate, registry)
        print("PROMOTED %s" % promoted.get("policy_id"))
    else:
        print("candidate only; use --promote-policy for the explicit behavior contract")
    return 0


def do_policy_status() -> int:
    import json
    print(json.dumps({
        "runtime": policy.runtime_context(),
        "promoted": policy.load(),
    }, indent=2, sort_keys=True, allow_nan=False))
    return 0


def do_research_audit() -> int:
    result = research.run_audit()
    print(
        "RESEARCH run=%s findings=%d semantic=%s"
        % (result.get("run"), len(result.get("findings") or []),
           "ok" if (result.get("semantic") or {}).get("ok") else "FAILED")
    )
    for finding in result.get("findings") or []:
        print("  %s" % finding.get("alert"))
    for item in result.get("questions") or []:
        print("  question %s opened=%s" % (item.get("id"), item.get("opened")))
    return 0 if (result.get("semantic") or {}).get("ok") else 4


def do_probes() -> int:
    import json
    print(json.dumps(probes.list_probes(), indent=2, sort_keys=True, allow_nan=False))
    return 0


def do_run_probe(probe_id: str) -> int:
    result = probes.run_probe(
        probe_id,
        explicit=True,
        run=(cycle.last_history() or {}).get("run"),
    )
    print("PROBE %s %s" % (probe_id, result.get("status")))
    if result.get("reason"):
        print("  %s" % result.get("reason"))
    if result.get("output"):
        print(result["output"])
    return 0 if result.get("status") in {"passed", "skipped"} else 4


def do_self_test() -> int:
    import json
    import shutil
    import tempfile

    from farm import journal as _journal
    from farm import parse, rules

    def text(path):
        d = json.load(open(path))
        return "\n".join(
            b.get("text", "") for b in (d.get("result", {}).get("content") or [])
        )

    checks = 0
    failures = []
    fixtures = {
        "farm": ["fixtures/start_list_farm.json", "fixtures/live_farm.json"],
        "board": ["fixtures/start_leaderboard.json", "fixtures/live_leaderboard.json"],
        "collect": ["fixtures/collect.json", "fixtures/live_collect.json"],
        "sell": ["fixtures/sell_egg.json", "fixtures/live_sell_eggs.json"],
        "feed": ["fixtures/buy_feed.json", "fixtures/live_buy_feed.json"],
    }
    fn = {
        "farm": parse.parse_farm,
        "board": parse.parse_leaderboard,
        "collect": parse.parse_collect,
        "sell": parse.parse_sell,
        "feed": parse.parse_buy_feed,
    }
    for kind, paths in fixtures.items():
        for path in paths:
            if not os.path.exists(path):
                continue
            checks += 1
            try:
                fn[kind](text(path))
            except Exception as exc:  # noqa: BLE001
                failures.append("%s/%s: %s" % (kind, path, exc))
    # adoption responses (one JSON-RPC envelope per line)
    if os.path.exists("fixtures/adoptions.ndjson"):
        import json as _json

        for line in open("fixtures/adoptions.ndjson"):
            line = line.strip()
            if not line:
                continue
            checks += 1
            env = _json.loads(line)
            body = "\n".join(
                b.get("text", "")
                for b in (env.get("result", {}).get("content") or [])
            )
            try:
                parse.parse_adopt(body)
            except Exception as exc:  # noqa: BLE001
                failures.append("adopt: %s" % exc)
                break
    # rule invariants
    checks += 1
    if rules.feed_reserve_target(258, 15) != rules.FEED_PER_ANIMAL_RESERVE * 258 + 15:
        failures.append("reserve arithmetic changed")

    # --- run 291 starvation: the regression suite for the whole failure class --
    # What happened: feed hit 0, the `feed` step ran BEFORE `buy_feed`, and
    # feed_animals raises ToolError on an empty larder. The error escaped run(),
    # so the cycle aborted before the purchase that would have fixed it. Every
    # cycle and supervisor pass then crashed identically -- an unrecoverable
    # deadlock with 3.5M coins in the bank. These checks pin each link shut.
    checks += 1
    # 1. Runway, not absolute count, is what the buffer is judged on.
    if rules.feed_buffer_minutes(23753, 11869) > 30:
        failures.append("23,753 feed at 11,869 animals must read as under 30 minutes")
    checks += 1
    if rules.feed_buffer_minutes(0, 11869) != 0:
        failures.append("an empty larder must read as zero runway")
    checks += 1
    # 2. The reserve must now survive an outage far longer than a cadence.
    healthy = rules.feed_reserve_target(11869, 0)
    if rules.feed_buffer_minutes(healthy, 11869) < rules.FEED_BUFFER_MIN_MINUTES:
        failures.append(
            "reserve target is only %.0f min of runway; floor is %d"
            % (rules.feed_buffer_minutes(healthy, 11869), rules.FEED_BUFFER_MIN_MINUTES)
        )
    checks += 1
    # 3. A thin reserve must alert even when hunger still reads 0, which is what
    #    the old detector missed: hunger was 0 right up to the cliff.
    thin = {
        "rank": 1, "animals": 11869, "feed": 23753, "reserve_target": healthy,
        "max_hunger": 0, "produce": 100, "ts": "2026-08-21T21:23:48Z",
        "interval_min": 5.0, "calls": 12,
    }
    alerts, _ = watch.evaluate(dict(thin), dict(thin))
    if not any("runway" in a for a in alerts):
        failures.append("a ~20 minute feed runway must alert: %s" % alerts)
    checks += 1
    # 4. A silent scheduler gap is itself the incident.
    gap = dict(thin, feed=healthy, interval_min=1159.7)
    alerts, _ = watch.evaluate(dict(gap), dict(gap))
    if not any("SCHEDULE GAP" in a for a in alerts):
        failures.append("a 19-hour gap between runs must alert: %s" % alerts)
    checks += 1
    # 5. The deadlock itself: an empty larder must buy and retry, not raise.
    class _StarvedClient(object):
        """Mimics the server that deadlocked us: feeding fails until feed > 0."""

        def __init__(self):
            self.feed = 0
            self.calls = []
            self.call_count = 0
            self.transport_errors = 0
            self.endpoint = "http://localhost/test"

        def scrub(self, t):
            return t

        def call(self, name, **kw):
            self.calls.append(name)
            self.call_count += 1
            if name == "list_farm":
                return (
                    "\U0001f33e Nick's Farm  \U0001fa99 900000 coins\n\nAnimals:\n"
                    "  \U0001f414 Pecky the chicken (#7) is starving. "
                    "hunger 100/100, happiness 10/100\n\n"
                    "Barn inventory: \U0001f331 feed x%d\n" % self.feed
                )
            if name == "buy_feed":
                self.feed += int(kw.get("qty", 0))
                return "\U0001f331 Bought %d feed for %d coins. %d coins left." % (
                    int(kw.get("qty", 0)),
                    int(kw.get("qty", 0)),
                    900000 - int(kw.get("qty", 0)),
                )
            if name == "feed_animals":
                if self.feed <= 0:
                    raise ToolError(
                        "feed_animals returned isError: \U0001f6ab You're out of "
                        "feed! Use buy_feed."
                    )
                return "Fed all animals."
            return ""

    stub = _StarvedClient()
    run_obj = cycle.Cycle(stub)
    try:
        farm = run_obj.read_state("selftest")
        farm = run_obj.ensure_feed_on_hand(farm)
        run_obj.feed_if_needed(farm, 1)
    except Exception as exc:  # noqa: BLE001
        failures.append("empty larder must not abort the run: %r" % exc)
    else:
        if "buy_feed" not in stub.calls:
            failures.append("an empty larder must trigger a feed purchase")
        if stub.calls.index("buy_feed") > stub.calls.index("feed_animals"):
            failures.append("feed must be bought BEFORE feed_animals is attempted")
        if stub.calls.count("feed_animals") != 1:
            failures.append("feeding must issue exactly one bulk operation per cycle")

    checks += 1
    # 6. A transport/gateway failure on the whole-herd feed must NOT kill the run.
    #    At ~26,000 animals feed_animals("all") takes ~97s and the gateway answers
    #    504. When that exception escaped, the cycle died before adopt/sell/buy_feed
    #    ever ran, so intervals stretched to ~24 minutes and hunger climbed - the
    #    failure fed itself. adopt_animal, by contrast, ran 1,483 times with zero
    #    failures, so this is about call weight, not call rate.
    class _GatewayFailClient(_StarvedClient):
        def __init__(self):
            _StarvedClient.__init__(self)
            self.feed = 10_000  # the larder is fine; the CALL is what fails

        def call(self, name, **kw):
            if name == "feed_animals":
                self.calls.append(name)
                self.call_count += 1
                raise McpError(
                    "transport failure after 3 tries: <HTTPError 504: 'Gateway Timeout'>"
                )
            return _StarvedClient.call(self, name, **kw)

    gw = _GatewayFailClient()
    gw_run = cycle.Cycle(gw)
    try:
        gw_run.feed_if_needed(gw_run.read_state("selftest"), 2)
    except Exception as exc:  # noqa: BLE001
        failures.append("a 504 on bulk feed must not abort the run: %r" % exc)

    checks += 1
    # Collection is one constant-time bulk drain every run, regardless of scale.
    if not rules.should_collect(1, 0, 26000) or not rules.should_collect(
        1, 10_000_000, 260000
    ):
        failures.append("bulk collection must run once at every herd/backlog size")
    checks += 1
    plan = rules.expansion_plan(382, 226, 411, 15)
    if plan["buy_feed"] + plan["adopt"] * 10 > 382:
        failures.append("expansion plan overspends")
    checks += 1
    if rules.sell_plan({"feed": 500, "egg": 3}) != [("egg", 3)]:
        failures.append("sell_plan would sell feed")
    # Property: adoption now happens BEFORE the feed top-up, and can stop early
    # on the wall-clock budget. Adopting must never make the feed reserve worse
    # than it already was -- a pre-existing deficit (too poor to reach target) is
    # allowed to persist, but expansion must not add unfunded obligations.
    checks += 1
    for coins in (0, 37, 500, 13095, 250000):
        for animals in (1, 628, 4871):
            for feed in (0, 1190, 10711):
                plan = rules.expansion_plan(coins, animals, feed, 15)
                n = plan["adopt"]
                if plan["buy_feed"] + n * rules.ANIMAL_COST["chicken"] > coins:
                    failures.append(
                        "plan overspends: coins=%d animals=%d feed=%d -> %s"
                        % (coins, animals, feed, plan)
                    )
                    break
                pre_deficit = max(0, rules.feed_reserve_target(animals, 15) - feed)
                for adopted in {0, n // 3, n // 2, n}:
                    need = max(
                        0, rules.feed_reserve_target(animals + adopted, 15) - feed
                    )
                    left = coins - adopted * rules.ANIMAL_COST["chicken"]
                    shortfall = max(0, need - left)
                    if shortfall > pre_deficit:
                        failures.append(
                            "adoption worsened the reserve: coins=%d animals=%d feed=%d "
                            "n=%d adopted=%d shortfall=%d pre_deficit=%d"
                            % (coins, animals, feed, n, adopted, shortfall, pre_deficit)
                        )
                        break
    # The rate limiter is load-bearing for every call, and a subtly wrong
    # version once throttled a run to a standstill. Measure it.
    checks += 1
    from farm import mcp as _mcp

    limiter = _mcp.RateLimiter(rate=20.0)
    t0 = time.time()
    for _ in range(10):
        limiter.acquire()
    elapsed = time.time() - t0
    if not (0.35 <= elapsed <= 0.75):
        failures.append(
            "rate limiter drift: 10 acquires at 20/s took %.2fs (want ~0.45s)" % elapsed
        )
    checks += 1
    import threading as _threading

    limiter = _mcp.RateLimiter(rate=20.0)
    t0 = time.time()
    threads = [
        _threading.Thread(target=lambda: [limiter.acquire() for _ in range(5)])
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0
    if not (0.8 <= elapsed <= 1.6):
        failures.append(
            "rate limiter under 4 threads: 20 acquires at 20/s took %.2fs (want ~1.0s)"
            % elapsed
        )

    # --- detector calibration ------------------------------------------------
    # The throughput and transport detectors were the two biggest sources of
    # token spend, so their exact firing conditions are pinned here.
    checks += 1
    if rules.transport_trouble(1, 35) or rules.transport_trouble(2, 35):
        failures.append("a stray retry in a 35-call run must not be an incident")
    checks += 1
    if not rules.transport_trouble(5, 35) or not rules.transport_trouble(3, 100):
        failures.append("sustained retries must still alarm")
    checks += 1
    # Run 29: drained barn, healthy hunger, low measured rate -> must be silent.
    quiet_row = {
        "rank": 1,
        "units_per_chicken_min": 0.0006,
        "interval_min": 9.63,
        "units_collected": 62,
        "ready_units": 666,
        "animals": 11069,
        "max_hunger": 6,
        # 600k feed is ~520 min of runway at this herd. The fixture used to say
        # 30,000, which reads fine as a count and is only ~24 minutes of feed --
        # exactly the blind spot that lost run 291.
        "feed": 600000,
        "reserve_target": 50 * 11069,
        "calls": 35,
        "transport_errors": 1,
        "call_rate": 0.5,
    }
    alerts, needs = watch.evaluate(dict(quiet_row), dict(quiet_row))
    if alerts or needs:
        failures.append("drained-barn run should not alert: %s" % alerts)
    checks += 1
    # Same low rate but produce piling up -> must alarm.
    alerts, _ = watch.evaluate(dict(quiet_row, ready_units=40000), dict(quiet_row))
    if not any("throughput" in a for a in alerts):
        failures.append("backlog build-up must still alert")

    # --- the score rate: what "maximize total produce" actually means ---------
    # Produce accrues as animals produce, not when we collect, and collect_produce
    # returns nothing while the herd is hungry (the produce banks during the feed
    # call instead). So an empty collection with a healthy score rate is routine,
    # while a collapsed score rate is the one incident that can lose the game.
    checks += 1
    banked_prev = {
        "rank": 1, "produce": 400_000, "ts": "2026-08-21T03:20:00Z", "animals": 11869,
        "max_hunger": 0, "feed": 700000, "reserve_target": 50 * 11869, "calls": 10,
        "interval_min": 4.0,
    }
    banked = dict(
        banked_prev,
        produce=406_500,        # 6,500 produce in 4 min = 1,625/min: healthy
        ts="2026-08-21T03:24:00Z",
        units_collected=0,      # collect_produce said "Nothing to collect"
        units_per_chicken_min=0.0,
        interval_min=4.0,
        ready_units=0,
        zero_streak=rules.ZERO_COLLECT_RUNS_TO_ALARM,
    )
    alerts, needs = watch.evaluate(dict(banked), dict(banked_prev))
    if alerts or needs:
        failures.append("empty collections with a healthy score rate must not alert: %s" % alerts)
    checks += 1
    # A single low window is jitter: produce arrives in bursts, so runs 40, 46 and
    # 55 each read 105-246/min immediately before a 1,600-2,000/min window.
    stalled = dict(banked, produce=400_200, zero_streak=0)   # 200 produce in 4 min = 50/min
    alerts, needs = watch.evaluate(dict(stalled), dict(banked_prev))
    if needs or any("PRODUCTION" in a for a in alerts):
        failures.append("one low window must not escalate: %s" % alerts)
    checks += 1
    # Two consecutive low windows is a real stall (hunger 70, or a server change)
    # and must escalate, even though collection looks ordinary.
    prev_low = dict(banked_prev, produce_per_min=50.0)
    alerts, needs = watch.evaluate(dict(stalled), prev_low)
    if not needs or not any("PRODUCTION" in a for a in alerts):
        failures.append("a sustained collapsed score rate must escalate: %s" % alerts)
    checks += 1
    if rules.produce_rate_trouble(100, 1.0, 11869) is not None:
        failures.append("a window shorter than the minimum must not be judged")
    checks += 1
    # The floor has to scale with herd size: an early 1,177-animal farm produced
    # ~180/min, which a fixed 600/min floor would have called an incident.
    if rules.produce_rate_trouble(700, 4.0, 1177) is not None:
        failures.append("a small farm's normal output must not look like a stall")
    checks += 1
    # Rival production is also bursty. One 50% share spike must not wake the model;
    # the same share on two consecutive windows is still a strategic alert.
    threat_prev = {
        "rank": 1,
        "produce": 100_000,
        "ts": "2026-08-21T03:20:00Z",
        "rivals": {"Moe": 10_000},
    }
    threat_jitter = {
        "rank": 1,
        "produce": 100_238,
        "ts": "2026-08-21T03:24:00Z",
        "rivals": {"Moe": 10_120},
    }
    alerts, needs = watch.evaluate(threat_jitter, threat_prev)
    if needs or any("THREAT" in a for a in alerts):
        failures.append("one rival share spike must not escalate: %s" % alerts)
    threat_next = {
        "rank": 1,
        "produce": 100_476,
        "ts": "2026-08-21T03:28:00Z",
        "rivals": {"Moe": 10_240},
    }
    alerts, needs = watch.evaluate(threat_next, threat_jitter)
    if not needs or not any("THREAT" in a for a in alerts):
        failures.append("a sustained rival share spike must escalate: %s" % alerts)
    checks += 1
    # Measured at run 50: only chickens ever produce. Adopting any other kind buys
    # feed cost and nothing else, so the ban has to be explicit, not incidental.
    if not rules.adoptable(rules.PRIMARY_KIND) or any(
        rules.adoptable(k) for k in ("cow", "sheep", "pig", "beehive")
    ):
        failures.append("only chickens may be adopted")

    # --- healing safety ------------------------------------------------------
    from farm import heal as _heal
    from farm import tokens as _tokens

    checks += 1
    for alert in (
        "RANK LOST: now #2",
        "THREAT: Moe gained 900 vs our 100",
        "tools/list changed - new or removed server capability",
    ):
        if _heal.classify(alert)[1] is not None:
            failures.append("strategic alert must escalate, not heal: %s" % alert)
    checks += 1
    if _heal.classify("3 transport retries across 200 calls")[1] is None:
        failures.append("transport retries should be healable")
    checks += 1
    wild = {
        "rate_ceiling": 999,
        "adopt_cap": 10 ** 6,
        "adopt_workers": 500,
        "collect_passes": 99,
    }
    if rules.rate_ceiling(wild) > rules.MAX_CALLS_PER_SECOND:
        failures.append("healing must never raise the call-rate ceiling")
    if rules.adopt_cap(wild) > rules.MAX_ADOPTIONS_PER_RUN:
        failures.append("healing must never raise the adoption cap")
    if rules.adopt_worker_count(wild) > rules.ADOPT_WORKERS:
        failures.append("healing must never raise the worker count")
    if rules.collect_passes(wild) > rules.MAX_COLLECT_PASSES:
        failures.append("collect passes must stay bounded")
    checks += 1
    junk = {"rate_ceiling": "fast", "adopt_cap": None, "collect_passes": []}
    if (
        rules.rate_ceiling(junk) != rules.MAX_CALLS_PER_SECOND
        or rules.adopt_cap(junk) != rules.MAX_ADOPTIONS_PER_RUN
        or rules.collect_passes(junk) != 1
    ):
        failures.append("corrupt knobs must fall back to defaults")
    checks += 1
    if rules.expansion_plan(10_000, 100, 0, 0, cap=3)["adopt"] > 3:
        failures.append("expansion plan ignored the supervisor's adoption cap")
    checks += 1
    if _heal._heal_hunger({}, {"max_hunger": rules.HUNGER_STOP}, {"knobs": {}}) is not None:
        failures.append("hunger at the production stop must escalate")
    checks += 1
    # Several queued copies of one condition must cost exactly one remedy step,
    # otherwise the healer over-corrects (a 5.0/s ceiling once fell to 2.05/s).
    same = [
        {"run": 41, "alert": "2 transport retries across 300 calls"},
        {"run": 42, "alert": "3 transport retries across 310 calls"},
        {"run": 43, "alert": "2 transport retries across 320 calls"},
    ]
    saved_store, saved_ledger = _heal.STORE, _heal.LEDGER
    tmpdir = tempfile.mkdtemp(prefix="farm-heal-test-")
    try:
        _heal.STORE = os.path.join(tmpdir, "heal.json")
        _heal.LEDGER = os.path.join(tmpdir, "heal.ndjson")
        outcome = _heal.process(same, {"animals": 100, "ready_units": 0}, 43)
        stepped = rules.rate_ceiling(outcome["knobs"])
        if len(outcome["healed"]) != 3 or outcome["escalated"]:
            failures.append("queued duplicates should all be healed: %s" % outcome)
        if stepped != round(rules.MAX_CALLS_PER_SECOND * 0.8, 2):
            failures.append("one condition must move the ceiling exactly one step")
    finally:
        _heal.STORE, _heal.LEDGER = saved_store, saved_ledger
        shutil.rmtree(tmpdir, ignore_errors=True)
    checks += 1
    # Healing must converge: repeated remedies stop instead of running away.
    store = {"knobs": {}}
    for _ in range(25):
        _heal._heal_transport({}, {}, store)
    if rules.rate_ceiling(store["knobs"]) < rules.MIN_CALLS_PER_SECOND:
        failures.append("rate healing undershot the floor")

    # --- cost accounting -----------------------------------------------------
    checks += 1
    if _tokens.cost(1_000_000, 0) != round(rules.LLM_INPUT_COST_PER_MTOK, 6):
        failures.append("input token pricing drifted")
    if _tokens.cost(0, 1_000_000) != round(rules.LLM_OUTPUT_COST_PER_MTOK, 6):
        failures.append("output token pricing drifted")
    checks += 1
    if _tokens.estimate_tokens("x" * 400) != 100:
        failures.append("token estimate should be chars/4")
    checks += 1
    if _tokens.cost(0, 0) != 0 or _tokens.avoided_cost(0) != 0:
        failures.append("a deterministic run must cost exactly zero")

    # --- growth saturation ---------------------------------------------------
    # Adoption is the largest recurring spend, so the gate that stops it has to be
    # right in both directions: stop when the marginal herd stops paying, and
    # never block growth that is still working.
    checks += 1
    # "Flat" now means flat. The old fixture used 1600 vs 1550 (+3.2%) and called
    # it flat only because the bar demanded +10%; that bar is what froze the herd
    # at 11,869 while a rival scaled to 42,859. See GROWTH_MIN_MARGINAL_GAIN.
    flat = [(11400, 1600.0)] * 5 + [(9000, 1600.0)] * 5
    v = rules.growth_verdict(flat, 11400, {})
    if not v["saturated"] or rules.adoption_cap(v, {})[0] != rules.MAINTENANCE_ADOPTIONS:
        failures.append("a flat output curve must stop adoption: %s" % v)
    checks += 1
    # A declining curve is the one case that genuinely must stop growth.
    falling = [(11400, 1400.0)] * 5 + [(9000, 1600.0)] * 5
    if not rules.growth_verdict(falling, 11400, {})["saturated"]:
        failures.append("a falling output curve must stop adoption")
    checks += 1
    # THE run 291 REGRESSION: sub-linear but strictly rising output must keep
    # growing. Total lifetime produce is the score, so a smaller marginal gain is
    # still a gain, and coins have no terminal value.
    sublinear = [(11869, 1629.0)] * 5 + [(9000, 1520.0)] * 5
    v = rules.growth_verdict(sublinear, 11869, {})
    if v["saturated"]:
        failures.append("sub-linear but rising output must keep growing: %s" % v)
    checks += 1
    # The gate must never be able to latch growth off forever on uncertain
    # marginal evidence. A confirmed score-engine halt is different: adding more
    # obligations cannot falsify or repair it and must fail closed at zero.
    if rules.MAINTENANCE_ADOPTIONS <= 0:
        failures.append("a saturated verdict must not freeze the herd permanently")
    checks += 1
    stalled = {"saturated": True, "production_stalled": True, "reason": "score flat"}
    if rules.adoption_cap(stalled, {})[0] != 0:
        failures.append("a confirmed production halt must pause every adoption")
    checks += 1
    stall_rows = [
        {"ts": "2026-08-25T00:00:00Z", "produce": 100, "verified": True, "max_hunger": 0},
        {"ts": "2026-08-25T00:05:00Z", "produce": 100, "verified": True, "max_hunger": 0},
        {"ts": "2026-08-25T00:10:00Z", "produce": 100, "verified": True, "max_hunger": 0},
    ]
    if growth.production_stall_windows(stall_rows) != 2:
        failures.append("two healthy flat score windows must be recognized as a production halt")
    interrupted_rows = stall_rows + [
        {"ts": "2026-08-25T00:15:00Z", "produce": 100, "verified": False, "max_hunger": 0}
    ]
    active, _ = growth.production_stall_active(
        interrupted_rows, {"production_stalled": True, "production_stall_windows": 2}
    )
    if not active:
        failures.append("an unrelated unverified row must not release a confirmed production halt")
    checks += 1
    resumed_rows = interrupted_rows + [
        {"ts": "2026-08-25T00:20:00Z", "produce": 101, "verified": True, "max_hunger": 0}
    ]
    active, _ = growth.production_stall_active(
        resumed_rows, {"production_stalled": True, "production_stall_windows": 2}
    )
    if active:
        failures.append("a positive score window must release the production-halt latch")
    checks += 1
    climbing = [(6000, 1500.0)] * 5 + [(4800, 900.0)] * 5
    v = rules.growth_verdict(climbing, 6000, {})
    if v["saturated"] or rules.adoption_cap(v, {})[0] != rules.MAX_ADOPTIONS_PER_RUN:
        failures.append("growth that is still paying must not be blocked: %s" % v)
    checks += 1
    # Comparing against a tiny historical herd always looks like growth works, so
    # the cohort must be a window just below the current herd.
    misleading = [(11400, 1600.0)] * 5 + [(9000, 1600.0)] * 5 + [(420, 75.0)] * 5
    if not rules.growth_verdict(misleading, 11400, {})["saturated"]:
        failures.append("ancient small-herd samples must not justify more growth")
    checks += 1
    small = [(1500, 200.0)] * 5 + [(1100, 200.0)] * 5
    if rules.growth_verdict(small, 1500, {})["saturated"]:
        failures.append("a herd below the saturation floor must keep growing")
    checks += 1
    held = rules.growth_verdict([(11400, 1600.0)], 11400, {"saturated": True, "plateau": 1550.0})
    if not held["saturated"] or held["plateau"] != 1550.0:
        failures.append("thin evidence must hold the previous verdict and plateau")
    checks += 1
    lifted = rules.growth_verdict(
        [(11400, 2500.0)] * 5 + [(9000, 1600.0)] * 5,
        11400,
        {"saturated": True, "plateau": 1550.0},
    )
    if lifted["saturated"] or rules.adoption_cap(lifted, {})[0] == 0:
        failures.append("output above the plateau must resume growth")
    checks += 1
    drifting = rules.growth_verdict(
        [(11400, 1700.0)] * 5 + [(9000, 1700.0)] * 5,
        11400,
        {"saturated": True, "plateau": 1550.0},
    )
    if drifting["plateau"] != 1550.0:
        failures.append("the plateau must be recorded once, not re-baselined")
    checks += 1
    if rules.adoption_cap(rules.growth_verdict(climbing, 6000, {}), {"adopt_cap": 4})[0] != 4:
        failures.append("a healing throttle must still bound the growth cap")
    checks += 1
    if rules.clean_samples([(1000, 0.5, 900, 0), (1000, 9.0, 0, 0), (1000, 9.0, 900, 99)]):
        failures.append("short, empty, and starving windows must all be rejected")

    # --- pipeline progress ---------------------------------------------------
    # The dashboard polls this file constantly, so a partial write or a lost
    # step would be visible immediately. Exercise the whole state machine.
    from farm import progress as _progress

    checks += 1
    saved_file = _progress.STATE_FILE
    tmpdir = tempfile.mkdtemp(prefix="farm-progress-test-")
    try:
        _progress.STATE_FILE = os.path.join(tmpdir, "progress.json")
        _progress.begin(77, False, 150, 170)
        state = _progress.read()
        if state["run"] != 77 or state["status"] != "running":
            failures.append("progress.begin did not initialise the run")
        if len(state["steps"]) != len(_progress.STEPS):
            failures.append("progress must expose the whole pipeline up front")
        if any(s["status"] != "pending" for s in state["steps"]):
            failures.append("every step should start pending")

        checks += 1
        _progress.start("collect")
        if _progress.read()["active"] != "collect":
            failures.append("an started step must be reported as active")
        _progress.done("collect", seconds=12.34, units=999)
        step = [s for s in _progress.read()["steps"] if s["name"] == "collect"][0]
        if step["status"] != "done" or step["seconds"] != 12.3:
            failures.append("progress.done lost the duration: %s" % step)
        if step["detail"].get("units") != 999:
            failures.append("progress.done lost step detail")
        if _progress.read()["active"] is not None:
            failures.append("a finished step must clear the active marker")

        checks += 1
        _progress.skip("harvest", "no harvestable food crops")
        step = [s for s in _progress.read()["steps"] if s["name"] == "harvest"][0]
        if step["status"] != "skipped" or not step["note"]:
            failures.append("a skipped step must be distinguishable from pending")

        checks += 1
        _progress.fail("adopt", "McpError: boom")
        state = _progress.read()
        step = [s for s in state["steps"] if s["name"] == "adopt"][0]
        if step["status"] != "failed" or state["status"] != "failed":
            failures.append("a failed step must mark the whole run failed")

        checks += 1
        _progress.finish("ok", run=77, duration_s=141.2)
        state = _progress.read()
        if state["status"] != "ok" or (state.get("summary") or {}).get("run") != 77:
            failures.append("progress.finish did not record the summary")

        checks += 1
        # A corrupt or missing file must degrade to an idle skeleton, never raise:
        # the dashboard reads this on every poll.
        with open(_progress.STATE_FILE, "w") as fh:
            fh.write("{not json")
        if _progress.read()["status"] != "idle":
            failures.append("corrupt progress file must read as idle")
        os.remove(_progress.STATE_FILE)
        if _progress.read()["status"] != "idle":
            failures.append("missing progress file must read as idle")
        # Writing against an unwritable path must not raise either.
        _progress.STATE_FILE = os.path.join(tmpdir, "nope", "deeper", "progress.json")
        os.makedirs(os.path.dirname(_progress.STATE_FILE))
        os.chmod(os.path.dirname(_progress.STATE_FILE), 0o500)
        _progress.begin(78, False, 150, 170)
        _progress.start("collect")
        _progress.done("collect", seconds=1.0)
    finally:
        try:
            os.chmod(os.path.join(tmpdir, "nope", "deeper"), 0o700)
        except OSError:
            pass
        _progress.STATE_FILE = saved_file
        shutil.rmtree(tmpdir, ignore_errors=True)

    # --- execution topology --------------------------------------------------
    # farm/topology.py is shipped in every release and read by the dashboard on
    # each poll, so a broken extraction must fail the release gate rather than
    # quietly emptying a panel. The full graph is asserted in
    # deploy/test_topology.py; this is the release-blocking smoke test.
    from farm import topology as _topology

    checks += 1
    graph = _topology.graph()
    if graph.get("errors"):
        failures.append("topology could not parse the loop: %s" % graph["errors"][:2])
    names = [step["name"] for step in graph.get("steps") or []]
    if names != [step["name"] for step in _progress.STEPS]:
        failures.append("topology steps disagree with progress.STEPS: %s" % names)

    checks += 1
    tools = {n["label"] for n in graph.get("nodes") or [] if n["kind"] == "tool"}
    for required in ("collect_produce", "feed_animals", "farm_events", "sell", "list_farm"):
        if required not in tools:
            failures.append("topology lost the %s server call" % required)
    if any(step["tools"] for step in graph["steps"] if step["name"] in ("plan", "finish")):
        failures.append("planning and recording must reach no server call")

    # --- throttles must not fire on things a throttle cannot fix -------------
    # Every one of these pins a specific, observed, expensive mistake. Each cost
    # real ground in the standings, so each gets a test.

    # 1. Constant-time bulk operations participate in normal transport health.
    checks += 1
    bulk_only = {"feed_animals": 3, "collect_produce": 3}
    if rules.core_transport_errors(bulk_only, 6) != 6:
        failures.append("bulk-operation retries must count as transport errors")
    checks += 1
    if not rules.transport_trouble(rules.core_transport_errors(bulk_only, 6), 141):
        failures.append("six bulk-operation retries must trigger transport handling")
    checks += 1
    if rules.core_transport_errors({"adopt_animal": 9}, 9) != 9:
        failures.append("adopt_animal retries must still count")
    checks += 1
    if rules.core_transport_errors(None, 7) != 7:
        failures.append("rows without attribution must fall back to the total")

    # 2. The row a real run writes must carry the attribution the detectors need.
    checks += 1
    _row_keys = {"transport_errors_by_tool", "transport_errors_core"}
    _src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "farm", "cycle.py")).read()
    for _k in _row_keys:
        if '"%s"' % _k not in _src:
            failures.append("cycle no longer records %s" % _k)

    # 3. A trivial reserve shortfall caused by concurrent adoption must not
    #    halve the adopt cap. Run 337 was short 390 feed of 789,135 (0.05%) with
    #    288 minutes of runway, and the cap went 400 -> 200 -> 100 -> 50.
    checks += 1
    _short = {"feed": 789135 - 390, "reserve_target": 789135, "animals": 26304}
    _alerts, _ = watch.evaluate(dict(_short, run=337, ts="2026-08-23T00:00:00Z"), None)
    if any("below reserve target" in a for a in _alerts):
        failures.append("a 0.05% reserve shortfall must not raise a reserve incident")

    # 4. And even if that alert did fire, the remedy must refuse to throttle
    #    while the runway is healthy. Defence in depth: the detector decides
    #    what is odd, this decides what is worth paying for.
    checks += 1
    _store = {"knobs": {}, "attempts": {}}
    _note = heal._heal_feed_reserve(
        {"alert": "feed below reserve target"},
        {"feed": 1741845, "animals": 58061},
        _store,
    )
    if "adopt_cap" in (_store.get("knobs") or {}):
        failures.append("feed_reserve remedy throttled adoption with a healthy runway")
    if not _note or "no action" not in _note:
        failures.append("feed_reserve remedy should explain why it did nothing")
    checks += 1
    _store2 = {"knobs": {}, "attempts": {}}
    heal._heal_feed_reserve(
        {"alert": "feed below reserve target"},
        {"feed": 1000, "animals": 58061},   # genuinely starving
        _store2,
    )
    if "adopt_cap" not in (_store2.get("knobs") or {}):
        failures.append("feed_reserve remedy must still throttle when runway is short")

    # 5. The projection has to answer the only question that matters, and it has
    #    to say NEVER when there is genuinely no path. Nothing else in this
    #    codebase measures the objective; the growth gate froze the herd for 246
    #    runs while every proxy looked perfect.
    checks += 1
    _win = rules.win_projection(5859239, 7695.8, 41293, 126.0, 8314321, 8983.0, 56061, 0.0)
    if not _win["eta_min"] or not (7 * 60 < _win["eta_min"] < 11 * 60):
        failures.append("win ETA at the run-352 position should be ~8-9 h, got %r" % _win["eta_min"])
    checks += 1
    _stuck = rules.win_projection(100, 1000.0, 1000, 0.0, 5_000_000, 9000.0, 56061, 0.0)
    if _stuck["eta_min"] is not None:
        failures.append("a farm that cannot catch up must project eta_min=None")
    checks += 1
    _ahead = rules.win_projection(9_000_000, 20000.0, 100000, 100.0, 5_000_000, 9000.0, 56061, 0.0)
    if not _ahead["ahead"] or _ahead["eta_min"] != 0.0:
        failures.append("already leading must report ahead with eta 0")
    checks += 1
    if rules.herd_to_out_rate(8983.0, 0.1864) not in range(48000, 48400):
        failures.append("herd needed to match an 8,983/min rival is ~48,200")

    # 6. A rival waking up is a strategic event. John sat frozen at 56,061 on 76
    #    coins; produce alone cannot tell "got fed" from "started adopting".
    checks += 1
    _base = {
        "run": 400, "ts": "2026-08-23T00:00:00Z", "animals": 58061,
        "produce": 6_000_000, "produce_per_min": 10800.0, "interval_min": 9.0,
        "rivals": {"John": 8_400_000}, "rival_coins": {"John": 76},
    }
    _prev = dict(_base, run=399, produce=5_900_000, rivals={"John": 8_300_000},
                 rival_herds={"John": 56061}, animals=57000)
    _grew, _ = watch.evaluate(dict(_base, rival_herds={"John": 62000}), _prev)
    if not any("RIVAL GROWING" in a for a in _grew):
        failures.append("a rival adding 5,939 animals must raise RIVAL GROWING")
    checks += 1
    _flat, _ = watch.evaluate(dict(_base, rival_herds={"John": 56065}), _prev)
    if any("RIVAL GROWING" in a for a in _flat):
        failures.append("a rival adding 4 animals must stay quiet")

    # 7. Bulk collection always fits and always runs once, even at huge scale.
    checks += 1
    if not rules.collect_fits_budget(40_000) or not rules.collect_fits_budget(800_000):
        failures.append("constant-time collection must fit at every herd size")
    checks += 1
    if not rules.should_collect(373, 0, 580_610, 2_482_673, 1_741_845):
        failures.append("bulk collection must not be skipped at herd scale")

    # 8. Daily risk event parsing recognizes every announced loss class and bills.
    checks += 1
    risk_text = "\n".join([
        "12:00 UTC Wolves took 3 chickens.",
        "12:01 UTC Sickness spread; vet charged 40 coins.",
        "12:02 UTC A storm damaged a plot; repair cost 25 coins.",
        "12:03 UTC 4 eggs spoiled in the barn.",
        "12:04 UTC Aliens abducted 3 chickens!",
        "12:05 UTC A UFO beamed up two sheep.",
    ])
    parsed_risks = parse.parse_events(risk_text)
    if [event.risk_kind for event in parsed_risks] != [
        "wolves", "sickness", "storm", "spoilage", "aliens", "aliens"
    ]:
        failures.append("farm_events risk classes were not normalized")
    if sum(event.charged_coins for event in parsed_risks) != 65:
        failures.append("automatic vet/repair charges were not parsed")

    # Abduction is the only risk class that removes production capacity outright,
    # so an unclassified invasion is invisible loss: events without a risk_kind are
    # dropped by cycle.read_risk_events(). The server shipped call_fbi (and with it
    # aliens) while these patterns covered only wolves, sickness, storm and
    # spoilage, which is exactly the silent gap the contract watcher exists to find.
    checks += 1
    if parse.parse_events("12:06 UTC An alien invasion has begun!")[0].risk_kind != "aliens":
        failures.append("an alien invasion was not classified as a risk event")
    # A laid egg must not become a risk event just because detection was widened.
    if parse.parse_events("12:07 UTC Clucky laid an egg.")[0].risk_kind is not None:
        failures.append("ordinary production was misclassified as a risk event")

    checks += 1
    reserve_plan = rules.expansion_plan(100_000, 100, 100_000, 0)
    spent = reserve_plan["buy_feed"] + reserve_plan["adopt"] * rules.ANIMAL_COST["chicken"]
    if 100_000 - spent < rules.RISK_COIN_RESERVE:
        failures.append("expansion spent the daily-risk coin reserve")

    # 9. The courtesy ease-off must be provoked by the server, not by its own

    #    throttling. `started` used to be captured before client.call(), which
    #    blocks in LIMITER.acquire(), so with W workers at R calls/s every call
    #    measured ~W/R: at 6 workers and 2.5/s that is 2.4s (observed 2.42s)
    #    against a 1.2s threshold, so the rate was cut every run, which
    #    lengthened the wait, which cut it again. Measured true server service
    #    time is ~0.67s, less than SLOW_CALL_SECONDS, so a correct measurement
    #    does not throttle at all.
    checks += 1
    _cyc_src = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "farm", "cycle.py")
    ).read()
    if "client.last_service_seconds" not in _cyc_src:
        failures.append("adopt ease-off must measure server service time, not wall time")
    checks += 1
    if 'state.get("wall_seconds"' not in _cyc_src:
        failures.append("the adopt budget check must still predict with wall time")
    checks += 1
    _mcp_src = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "farm", "mcp.py")
    ).read()
    if "last_service_seconds" not in _mcp_src:
        failures.append("mcp.Client must expose service time excluding limiter wait")
    checks += 1
    # The ease-off threshold must sit clear of the server's own latency, or it
    # fires in normal operation. Measured median service time is 0.670s.
    if rules.SLOW_CALL_SECONDS <= rules.MEASURED_SERVICE_SECONDS * 1.5:
        failures.append(
            "SLOW_CALL_SECONDS (%.2fs) leaves no headroom over measured service "
            "time (%.3fs); the courtesy ease-off will throttle adoption routinely"
            % (rules.SLOW_CALL_SECONDS, rules.MEASURED_SERVICE_SECONDS)
        )

    # 9. The adoption floor must not charge coins for feed already in the barn.
    #    At run 379 it reserved 1,905,660 coins against a barn holding 2,222,305
    #    feed (538 min of runway) and so allowed only 1,048 more animals while
    #    1.9M coins sat idle. endgame.py shows that cap never wins; counting the
    #    barn allows herd ~112,000, which passes John in 6.4h without starving.
    checks += 1
    if rules.affordable_adoptions(1947603, 63522, 2222305) < 40000:
        failures.append("feed already owned must not be paid for twice")
    checks += 1
    if rules.affordable_adoptions(1947603, 63522, 0) > 2000:
        failures.append("an empty barn must still force a conservative floor")
    checks += 1
    if rules.affordable_adoptions(0, 63522, 2222305) != 0:
        failures.append("no coins must mean no adoptions")
    checks += 1
    # Never adopt an animal we cannot also feed: the cost basis must still
    # include a full reserve for each NEW animal.
    _n = rules.affordable_adoptions(1_000_000, 10, 10_000_000)
    if _n * (rules.ANIMAL_COST[rules.PRIMARY_KIND]
             + rules.FEED_PER_ANIMAL_RESERVE * rules.FEED_COST) > 1_000_000:
        failures.append("new animals must still be charged their own feed reserve")

    # 10. The supervisor must survive a malformed state file rather than escalate.
    #     One hand-appended dict in heal.json's `healed` list made set() raise
    #     TypeError, which failed the supervise pass and set needs_llm - a
    #     dedup helper billed us for a model wake-up.
    checks += 1
    _hp = os.path.join(tempfile.mkdtemp(), "heal.json")
    with open(_hp, "w") as _fh:
        json.dump({"healed": ["31:ok", {"unhashable": True}, "32:ok"], "knobs": {}}, _fh)
    _orig = heal.STORE
    try:
        heal.STORE = _hp
        _keys = heal.healed_keys()
        if _keys != {"31:ok", "32:ok"}:
            failures.append(
                "healed_keys must skip unhashable entries, got %r" % (_keys,)
            )
    finally:
        heal.STORE = _orig

    # 11. Standings facts must not wake a model while we are on track. "We are
    #     #2", "the leader is ahead", "the leader is still producing" are the
    #     PREMISE of the race, and they escalated on every run asking for a
    #     judgement that rules.win_projection() now computes for free. Being
    #     behind is not news; being behind with no path to the front is.
    checks += 1
    _on = {
        "run": 500, "ts": "2026-08-23T20:00:00Z", "rank": 2, "animals": 65815,
        "produce": 8_300_000, "produce_per_min": 11300.0, "interval_min": 8.4,
        "rivals": {"John": 9_813_797}, "rival_herds": {"John": 58436},
        "rival_coins": {"John": 410321},
        "feed": 30 * 65815, "reserve_target": 30 * 65815, "coins": 1_900_000,
    }
    _on_prev = dict(
        _on, run=499, produce=8_200_000, animals=64599,
        rivals={"John": 9_730_000}, rival_herds={"John": 58180},
    )
    _a, _ = watch.evaluate(dict(_on), _on_prev)
    _standings = [
        x for x in _a
        if "RANK LOST" in x or "passed us" in x or "THREAT" in x or "RIVAL GROWING" in x
    ]
    if _standings:
        failures.append(
            "standings facts must not escalate while on track, got %r" % (_standings,)
        )
    checks += 1
    # ...but they must escalate when the projection offers no path.
    _off = dict(_on, rivals={"John": 99_000_000}, rival_herds={"John": 900000})
    _off_prev = dict(_on_prev, rivals={"John": 90_000_000}, rival_herds={"John": 500000})
    _b, _ = watch.evaluate(_off, _off_prev)
    if not any("NO PATH TO WIN" in x for x in _b):
        failures.append("a hopeless projection must raise NO PATH TO WIN")
    if not any("RANK LOST" in x for x in _b):
        failures.append("standings facts must escalate once there is no path")

    # 12. Constant-time bulk feeding retires the synthetic herd-size hunger wall.
    #     Actual hunger and feed runway remain direct safety signals.
    checks += 1
    if rules.hunger_safe_herd_ceiling() < 10 ** 9:
        failures.append("bulk feeding must not impose the retired 120k herd ceiling")
    checks += 1
    if rules.projected_max_hunger(10_000_000) != 0:
        failures.append("herd size alone must not project partial-feed hunger")
    checks += 1
    _large_healthy = dict(
        _on, animals=1_000_000, feed=30_000_000, reserve_target=30_000_000,
        max_hunger=0, rank=1, rivals={}, rival_herds={},
    )
    _w, _ = watch.evaluate(_large_healthy, None)
    if any("HUNGER WALL" in x or "hunger-safe ceiling" in x for x in _w):
        failures.append("a large, healthy herd must not trigger the retired hunger wall")
    checks += 1
    _src_x = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "experiments", "expand.py")
    ).read()
    if "hunger_safe_herd_ceiling" in _src_x:
        failures.append("expand.py must not cap growth on the retired hunger wall")

    # 13. Alerts must expire. An alert describes one instant, but a remedy is
    #     applied to the farm as it is NOW. Unbounded, the queue re-threw an
    #     alert from run 28 at a farm 350 runs later and the healer cut the
    #     call-rate ceiling and adopt workers on it - twice, after both had
    #     already been reset, because the justification never aged out.
    checks += 1
    if not getattr(rules, "ALERT_STALE_HOURS", 0):
        failures.append("alerts must have a staleness bound")
    checks += 1
    _cut = _journal._stale_cutoff("2026-08-23T20:00:00Z")
    if _cut != "2026-08-23T18:00:00Z":
        failures.append("stale cutoff should be ALERT_STALE_HOURS before now, got %r" % _cut)
    checks += 1
    _adir = tempfile.mkdtemp()
    _ap = os.path.join(_adir, "alerts.ndjson")
    with open(_ap, "w") as _fh:
        _fh.write(json.dumps({"run": 28, "ts": "2026-08-20T01:00:00Z",
                              "alert": "2 transport retries across 42 calls"}) + "\n")
        _fh.write(json.dumps({"run": 384, "ts": "2026-08-23T19:59:00Z",
                              "alert": "hunger 42 at/above alarm"}) + "\n")
    _oap = _journal.ALERTS
    try:
        _journal.ALERTS = _ap
        _got = _journal.pending_alerts(None, set(), now="2026-08-23T20:00:00Z")
        if [r["run"] for r in _got] != [384]:
            failures.append(
                "a run-28 alert must not be actionable 350 runs later, got %r"
                % ([r["run"] for r in _got],)
            )
    finally:
        _journal.ALERTS = _oap

    print("self-test: %d checks, %d failures" % (checks, len(failures)))
    for f in failures:
        print("  FAIL", f)
    return 0 if not failures else 4


def main() -> int:
    ap = argparse.ArgumentParser(description="Farm Friends deterministic operator")
    ap.add_argument("--cycle", action="store_true", help="run the full loop")
    ap.add_argument("--dry-run", action="store_true", help="decide but do not act")
    ap.add_argument("--review", type=int, nargs="?", const=12, help="digest last N runs")
    ap.add_argument("--alerts", action="store_true", help="pending anomalies only")
    ap.add_argument("--journal", action="store_true", help="append a generated journal entry")
    ap.add_argument("--force", action="store_true", help="with --journal, ignore the cadence")
    ap.add_argument("--health", action="store_true", help="backstop check, self-heals if stale")
    ap.add_argument(
        "--supervise",
        action="store_true",
        help="self-healing pass: repair the schedule, remediate alerts",
    )
    ap.add_argument(
        "--heal-status", action="store_true", help="show healing knobs and token cost"
    )
    ap.add_argument("--self-test", action="store_true", help="parser regression")
    ap.add_argument("--contract-status", action="store_true",
                    help="show the server contract baseline and recent drift")
    ap.add_argument("--orders", action="store_true",
                    help="show the code-change work order queue")
    ap.add_argument("--canary-status", action="store_true",
                    help="show the provisional release under observation")
    ap.add_argument("--llm-status", action="store_true",
                    help="show headless model availability and authoring spend")
    ap.add_argument("--vcs-status", action="store_true",
                    help="show git history, autonomous commits and release tags")
    ap.add_argument("--questions", action="store_true", help="show durable open strategy questions")
    ap.add_argument("--all-questions", action="store_true", help="include answered questions")
    ap.add_argument("--sweep", action="store_true", help="pure counterfactual strategy replay")
    ap.add_argument("--research-audit", action="store_true", help="run semantic and model-drift audits")
    ap.add_argument("--knowledge-refresh", action="store_true", help="rebuild the claim registry")
    ap.add_argument("--promote-policy", action="store_true", help="explicitly promote a passing policy snapshot")
    ap.add_argument("--policy-status", action="store_true", help="show promoted/runtime policy compatibility")
    ap.add_argument("--probes", action="store_true", help="list bounded research probes")
    ap.add_argument("--run-probe", metavar="ID", help="explicitly run one registered bounded probe")
    ap.add_argument("--align", action="store_true", help="wait for :35s before acting")
    args = ap.parse_args()

    if args.self_test:
        return do_self_test()
    if args.questions or args.all_questions:
        return do_questions(include_closed=args.all_questions)
    if args.sweep:
        return do_sweep()
    if args.research_audit:
        return do_research_audit()
    if args.knowledge_refresh or args.promote_policy:
        return do_knowledge_refresh(promote_policy=args.promote_policy)
    if args.policy_status:
        return do_policy_status()
    if args.probes:
        return do_probes()
    if args.run_probe:
        return do_run_probe(args.run_probe)
    if args.heal_status:
        return do_heal_status()
    if args.contract_status:
        return do_contract_status()
    if args.orders:
        return do_orders()
    if args.canary_status:
        return do_canary_status()
    if args.llm_status:
        return do_llm_status()
    if args.vcs_status:
        return do_vcs_status()
    if args.supervise:
        try:
            return do_supervise()
        except Exception:  # noqa: BLE001
            print("SUPERVISE CRASHED")
            traceback.print_exc(limit=3)
            print("needs_llm: true")
            return 4
    if args.alerts:
        return do_alerts()
    if args.journal:
        return do_journal(args.force)
    if args.review is not None:
        return do_review(args.review)
    if args.health:
        handle = _lock()
        try:
            _arm_watchdog(rules.CYCLE_HARD_TIMEOUT)
            return do_health()
        except (ParseDrift, McpError, Timeout) as exc:
            print("HEALTH FAILED: %s: %s" % (exc.__class__.__name__, exc))
            print("needs_llm: true")
            return 4
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
    if not (args.cycle or args.dry_run):
        ap.print_help()
        return 0

    handle = _lock()
    try:
        _rotate_log()
        _arm_watchdog(rules.CYCLE_HARD_TIMEOUT)
        if args.align:
            _align()
        return do_cycle(dry=args.dry_run)
    except (ParseDrift, McpError, Timeout) as exc:
        progress.finish("failed", error="%s: %s" % (exc.__class__.__name__, exc))
        print("FARM FAILED: %s: %s" % (exc.__class__.__name__, exc))
        print("raw responses in %s" % cycle.RAW_DIR)
        print("needs_llm: true")
        return 4
    except Exception:  # noqa: BLE001
        progress.finish("failed", error="unhandled exception")
        print("FARM CRASHED")
        traceback.print_exc(limit=3)
        print("needs_llm: true")
        return 4
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


if __name__ == "__main__":
    sys.exit(main())
