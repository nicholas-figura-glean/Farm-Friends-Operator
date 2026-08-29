"""Provisional releases: prove a flip helped, or undo it without a human.

Autonomous code publishing is only defensible if it is reversible. The release
machinery already gives us the hard part -- immutable `releases/<rev>/` trees and
an atomic symlink flip -- but nothing ever used it to go *backwards*. This module
is that missing half.

When the author agent flips a release it **arms** a canary recording the previous
revision and a pre-flip performance baseline. The supervisor then evaluates on
every pass:

    watching   -> not enough clean post-flip runs yet to judge
    healthy    -> safety passed and efficacy/equivalence accepted; clear it
    regressed  -> breakage, regression, or unproven strategy; revert and record why

Why produce_per_min and not a test result
----------------------------------------
The gate matrix already proves the code is *correct*. It cannot prove the code is
good for the lexicographic objective (league first, lifetime produce second). A
change can pass every suite and still halve output -- POSTMORTEM-run377 documents
exactly that: three throttles aimed at the wrong variable, all individually
reasonable, which together nearly lost first place. The canary therefore combines
a fast emergency floor with the independent champion/candidate evaluator.

Why the band is loose
---------------------
`produce_per_min` moves with herd size, wolves, sickness and server latency. A
tight threshold would revert good releases on ordinary variance and thrash the
pointer. The canary is here to catch a genuine break -- a parser silently
returning zero, a feed step no longer running -- not to micro-optimise.

Safety properties this module must preserve:

  * **Never revert to a missing tree.** The previous revision is verified to still
    exist before the pointer moves; release pruning can remove old trees.
  * **Never revert more than once.** A revert clears the canary, so a flapping
    metric cannot walk the pointer backwards through history.
  * **Never block the farm.** Every failure path leaves the pointer alone.
  * **Refresh the operator view.** A live rollback restarts the monitor after the
    pointer moves so its import-time HTML and routes match the restored release.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import compaction, control, evaluation, rules, workorders

STORE = os.path.join("state", "canary.json")
# The real project root. Used only to decide whether a caller is operating on live
# state or on a temp directory, which is what keeps a test suite from rewriting
# real git history. See record_inverse_commit().
PROJECT = str(control.project_root(Path(__file__).resolve().parent.parent))
HISTORY = os.path.join("state", "canary.ndjson")
RUN_HISTORY = os.path.join("state", "history.ndjson")

WATCHING = "watching"
HEALTHY = "healthy"
REGRESSED = "regressed"
INCONCLUSIVE = "inconclusive"
INACTIVE = "inactive"


class CanaryActiveError(RuntimeError):
    """A second candidate cannot replace unresolved release evidence."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_seconds(value: Any) -> Optional[int]:
    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: str, value: Dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _append(path: str, row: Dict[str, Any]) -> Dict[str, Any]:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return row


def _revision_commit(revision: str, history: str = HISTORY) -> str:
    """Newest recorded source commit for an immutable release revision."""
    if not revision:
        return ""
    try:
        rows = compaction.read_rows(history, limit=2_000)
    except Exception:  # noqa: BLE001 - missing provenance keeps legacy fallback
        return ""
    for row in reversed(rows):
        if row.get("revision") == revision and row.get("commit"):
            return str(row.get("commit"))
    return ""


def _runs(path: str = RUN_HISTORY, limit: int = 400) -> List[Dict[str, Any]]:
    """Recent logical run rows, oldest first, across active and archived history."""
    return [row for row in compaction.read_rows(path, limit=limit) if row.get("run") is not None]


def _exogenous_loss(row: Dict[str, Any]) -> Optional[str]:
    """Did an outside event, not the release, depress this run?

    Abduction is the case that forced this. An alien invasion removes animals *and*
    the produce they had accumulated, so lifetime produce actually falls and the run
    scores far below any baseline. Attributing that to whatever release happened to
    be on probation is simply wrong, and during the first live invasion it came
    within 2,160 units/min of auto-reverting the very release that added abduction
    detection -- the release that made the invasion visible in the first place.

    `_looks_broken` already refuses to treat risk events as breakage. The rate
    comparison needed the same exclusion and did not have it.
    """
    counts = row.get("risk_event_counts") or {}
    if isinstance(counts, dict) and int(counts.get("aliens") or 0) > 0:
        return "aliens"
    # A falling lifetime counter can only mean loss inflicted from outside; normal
    # operation cannot un-produce.
    value = row.get("produce_per_min")
    if isinstance(value, (int, float)) and value < 0:
        return "negative produce delta"
    return None


def _per_animal(row: Dict[str, Any]) -> Optional[float]:
    """Produce rate divided by herd size.

    The canary must compare like with like, and absolute rate is not like-for-like
    when the herd changes underneath it. During the first alien invasion the herd fell
    13% (256,163 -> 222,406), which by itself dragged absolute rate 23% below
    baseline -- almost through the 25% revert floor -- while per-animal output was
    only 11.5% down. The release under probation had nothing to do with either number.

    This is the same confound that made the herd/output estimator unreliable: herd
    size moves for reasons unrelated to the thing being measured, so anything
    compared across a herd change has to be normalised first.
    """
    value = row.get("produce_per_min")
    herd = row.get("animals")
    if isinstance(value, (int, float)) and value >= 0 and isinstance(herd, int) and herd > 0:
        return float(value) / float(herd)
    return None


def _rate(row: Dict[str, Any]) -> Optional[float]:
    value = row.get("produce_per_min")
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _mean(values: List[float]) -> Optional[float]:
    clean = [v for v in values if isinstance(v, (int, float))]
    return sum(clean) / len(clean) if clean else None


def _sample_weight(row: Dict[str, Any]) -> float:
    """Minutes represented by a rate sample, bounded to the stall horizon.

    A hand-started cycle can land seconds before launchd starts the scheduled one.
    Giving that ten-second zero the same weight as a ten-minute score window cut the
    measured candidate rate in half and repeatedly rolled back an unchanged UI.
    Historical rows without interval telemetry retain equal weight for compatibility.
    """
    try:
        value = float(row.get("interval_min"))
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value) or value <= 0:
        return 1.0
    return min(value, rules.CANARY_STALL_SECONDS / 60.0)


def _weighted_mean(
    rows: List[Dict[str, Any]],
    getter: Callable[[Dict[str, Any]], Optional[float]],
) -> Optional[float]:
    samples = []
    for row in rows:
        value = getter(row)
        if value is not None:
            samples.append((value, _sample_weight(row)))
    total_weight = sum(weight for _, weight in samples)
    if not samples or total_weight <= 0:
        return None
    return sum(value * weight for value, weight in samples) / total_weight


def _baseline_stall_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The consecutive pre-arm rows proving score production was already stalled."""
    eligible = [row for row in rows if not _exogenous_loss(row) and _rate(row) is not None]
    recent = eligible[-rules.CANARY_MIN_RUNS :]
    if len(recent) < rules.CANARY_MIN_RUNS:
        return []
    if all(
        float(_rate(row) or 0.0) < rules.produce_floor(int(row.get("animals") or 0))
        for row in recent
    ):
        return recent
    return []


def _progression_transition_runs(rows: List[Dict[str, Any]]) -> set[int]:
    """Runs whose rate denominator is invalid across an intentional herd reset.

    The action row contains output mostly earned by the retiring herd, and the next
    leaderboard interval can still flush lagging score. Dividing either by the one
    replacement animal produced a real 443.8/min/animal canary baseline and guaranteed
    a false rollback. Preserve the rows, but exclude both from rate comparisons.
    """
    excluded: set[int] = set()
    for index, row in enumerate(rows):
        transition = int(row.get("prestige_count") or 0) > 0
        for action in row.get("mechanic_actions") or []:
            if not isinstance(action, dict) or action.get("kind") != "progression":
                continue
            verification = action.get("verification") or {}
            before = verification.get("before") or {}
            after = verification.get("after") or {}
            try:
                transition = transition or (
                    int(after.get("league_level")) > int(before.get("league_level"))
                    and int(after.get("lifetime_produce")) >= int(before.get("lifetime_produce"))
                )
            except (TypeError, ValueError):
                pass
        if not transition:
            continue
        run = row.get("run")
        if isinstance(run, int):
            excluded.add(run)
        if index + 1 < len(rows) and isinstance(rows[index + 1].get("run"), int):
            excluded.add(int(rows[index + 1]["run"]))
    return excluded


def _latest_progression_run(rows: List[Dict[str, Any]]) -> Optional[int]:
    latest: Optional[int] = None
    for row in rows:
        transition = int(row.get("prestige_count") or 0) > 0
        for action in row.get("mechanic_actions") or []:
            if not isinstance(action, dict) or action.get("kind") != "progression":
                continue
            verification = action.get("verification") or {}
            before, after = verification.get("before") or {}, verification.get("after") or {}
            try:
                transition = transition or (
                    int(after.get("league_level")) > int(before.get("league_level"))
                    and int(after.get("lifetime_produce")) >= int(before.get("lifetime_produce"))
                )
            except (TypeError, ValueError):
                pass
        if transition and isinstance(row.get("run"), int):
            latest = int(row["run"])
    return latest


def _verified_progression(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A post-release prestige that proved the league-first objective directly."""
    for index, row in enumerate(rows):
        for action in row.get("mechanic_actions") or []:
            if action.get("kind") != "progression" or action.get("status") not in {"verified", "reconciled"}:
                continue
            verification = action.get("verification") or {}
            checks = verification.get("checks") or {}
            before = verification.get("before") or {}
            after = verification.get("after") or {}
            if not verification.get("ok") or not all(
                checks.get(name) for name in (
                    "league_level_increases",
                    "lifetime_produce_preserved",
                    "capacity_does_not_decrease",
                )
            ):
                continue
            try:
                if int(after.get("league_level")) <= int(before.get("league_level")):
                    continue
                if int(after.get("capacity")) < int(before.get("capacity")):
                    continue
                if int(after.get("lifetime_produce")) < int(before.get("lifetime_produce")):
                    continue
            except (TypeError, ValueError):
                continue
            return {
                "row_index": index,
                "run": row.get("run"),
                "before": before,
                "after": after,
                "tool": action.get("tool"),
                "policy_id": action.get("policy_id"),
            }
    return None


def _verified_capped_replacement(
    record: Dict[str, Any], rows: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    if record.get("strategy_intent") != "capped_replacement":
        return None
    baseline = record.get("baseline_by_kind") or {}
    baseline_bees = int(baseline.get("beehive") or 0)
    expected_fingerprint = str(record.get("strategy_policy_fingerprint") or "")
    for row in rows:
        if row.get("strategy_policy_errors"):
            continue
        if expected_fingerprint and row.get("strategy_policy_fingerprint") != expected_fingerprint:
            continue
        capacity = int(row.get("animal_capacity") or 0)
        animals = int(row.get("animals") or 0)
        bees = int((row.get("by_kind") or {}).get("beehive") or 0)
        if capacity and animals / float(capacity) >= 0.90 and bees > baseline_bees:
            return {
                "run": row.get("run"),
                "baseline_beehives": baseline_bees,
                "candidate_beehives": bees,
                "added_beehives": bees - baseline_bees,
                "animals": animals,
                "capacity": capacity,
                "strategy_policy_fingerprint": row.get("strategy_policy_fingerprint"),
            }
    return None


def baseline_rate(runs: Optional[List[Dict[str, Any]]] = None) -> Optional[float]:
    """Time-weighted produce rate over the runs immediately before now."""
    rows = runs if runs is not None else _runs()
    return _weighted_mean(rows[-rules.CANARY_BASELINE_RUNS :], _rate)


def baseline_per_animal(runs: Optional[List[Dict[str, Any]]] = None) -> Optional[float]:
    """Time-weighted per-animal rate over the runs immediately before now.

    This is the figure the verdict actually uses. `baseline_rate` is still recorded
    because it is what an operator recognises, but it must not decide a revert.
    """
    rows = runs if runs is not None else _runs()
    return _weighted_mean(rows[-rules.CANARY_BASELINE_RUNS :], _per_animal)


def latest_run(runs: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
    rows = runs if runs is not None else _runs()
    return int(rows[-1]["run"]) if rows else None


OBSERVABILITY_EXACT = {
    "README.md", "monitor.py",
    "experiments/dashboard_agent.py", "experiments/research_agent.py",
    "farm/architecture.py", "farm/autonomy.py", "farm/control.py",
    "farm/evidence.py", "farm/observability.py", "farm/progress.py",
    "farm/research.py", "farm/watch.py",
}
OBSERVABILITY_PREFIXES = ("dashboard/", "docs/", "deploy/test_")


def release_diff(project: Any, revision: str, previous: str) -> List[str]:
    """All source/artifact paths whose content differs across two releases."""
    root = Path(project)
    candidate = root / "releases" / str(revision)
    rollback = root / "releases" / str(previous)
    relative: set[str] = set()
    for tree in (candidate, rollback):
        if not tree.is_dir():
            continue
        try:
            for path in tree.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    rel = path.relative_to(tree).as_posix()
                    if rel not in {"RELEASED", "SOURCE_COMMIT"}:
                        relative.add(rel)
        except OSError:
            continue
    changed: List[str] = []
    for rel in sorted(relative):
        left, right = candidate / rel, rollback / rel
        try:
            if not left.is_file() or not right.is_file() or left.read_bytes() != right.read_bytes():
                changed.append(rel)
        except OSError:
            changed.append(rel)
    return changed


def observability_release_errors(project: Any, revision: str, previous: str) -> List[str]:
    changed = release_diff(project, revision, previous)
    return [
        path for path in changed
        if path not in OBSERVABILITY_EXACT
        and not any(path.startswith(prefix) for prefix in OBSERVABILITY_PREFIXES)
    ]


def release_editable_diff(project: Any, revision: str, previous: str) -> List[str]:
    """Editable implementation files that differ across the release boundary.

    The release directories are immutable, so this is stronger provenance than a
    dirty working-tree diff. Protected control-plane files are deliberately omitted:
    a regression involving one of those must remain visible for manual repair rather
    than granting the author agent permission to rewrite its own judge.
    """
    changed = [
        rel for rel in release_diff(project, revision, previous)
        if control.author_editable(rel)
    ]
    priority = {
        "farm/format_compat.py": 0,
        "farm/parse.py": 1,
        "monitor.py": 2,
    }
    return sorted(changed, key=lambda rel: (priority.get(rel, 10), rel))


def arm(
    revision: str,
    previous: str,
    reason: str = "",
    order_id: str = "",
    commit: str = "",
    base_commit: str = "",
    change_class: str = "reliability",
    hypothesis_id: str = "",
    policy_id: str = "",
    expected_improvement: float = 0.0,
    strategy_intent: str = "",
    files: Optional[List[str]] = None,
    store: str = STORE,
    history: str = HISTORY,
    run_history: str = RUN_HISTORY,
) -> Dict[str, Any]:
    """Record that `revision` is live provisionally and must prove itself."""
    watching = active(store)
    if watching:
        raise CanaryActiveError(
            "canary %s is still watching; refusing to arm %s"
            % (watching.get("revision"), revision)
        )
    runs = _runs(run_history)
    evaluation.ensure_champion(store, previous, run=latest_run(runs))
    transition_exclusions = _progression_transition_runs(runs)
    latest_progression_run = _latest_progression_run(runs)
    current_run = latest_run(runs)
    progression_recovery = bool(
        isinstance(latest_progression_run, int)
        and isinstance(current_run, int)
        and current_run - latest_progression_run <= rules.CANARY_PROGRESSION_RECOVERY_RUNS
    )
    comparable_runs = [
        row for row in runs if int(row.get("run") or -1) not in transition_exclusions
    ]
    efficacy_baseline = evaluation.baseline_samples(comparable_runs)
    baseline_stall_rows = _baseline_stall_rows(comparable_runs)
    try:
        from . import strategy

        strategy_fingerprint = strategy.status().get("fingerprint")
    except Exception:  # noqa: BLE001
        strategy_fingerprint = None
    last_row = runs[-1] if runs else {}
    record = {
        "schema_version": 1,
        "status": WATCHING,
        "revision": revision,
        "previous": previous,
        "reason": reason[:300],
        "order_id": order_id,
        # The commit this release was built from, so a revert can undo the change by
        # content and not only by re-pointing at the previous directory.
        "commit": commit,
        # Source rollback must cover every commit included since the immutable
        # runtime base, not merely the last commit named by the publisher.
        "base_commit": base_commit or _revision_commit(previous, history),
        "change_class": change_class if change_class in {"reliability", "compatibility", "observability", "strategy", "research_probe"} else "reliability",
        "hypothesis_id": hypothesis_id,
        "policy_id": policy_id,
        "expected_improvement": max(0.0, float(expected_improvement or 0.0)),
        "strategy_intent": str(strategy_intent or "")[:80],
        "strategy_policy_fingerprint": strategy_fingerprint,
        "baseline_by_kind": dict(last_row.get("by_kind") or {}),
        "files": [control.normalize_path(str(path)) for path in (files or [])
                  if control.author_editable(str(path))],
        "armed_ts": _utcnow(),
        "armed_at_run": latest_run(runs),
        "baseline_rate": baseline_rate(comparable_runs),
        "baseline_per_animal": baseline_per_animal(comparable_runs),
        "baseline_transition_excluded_runs": sorted(transition_exclusions),
        "preexisting_progression_recovery": progression_recovery,
        "preexisting_progression_run": latest_progression_run,
        "baseline_runs": rules.CANARY_BASELINE_RUNS,
        "baseline_stalled": bool(baseline_stall_rows),
        "baseline_stall_runs": [int(row.get("run") or 0) for row in baseline_stall_rows],
        "efficacy_metric": efficacy_baseline["metric"],
        "efficacy_baseline_samples": efficacy_baseline["samples"],
        "efficacy_baseline_runs": rules.EFFICACY_BASELINE_RUNS,
    }
    _write_json(store, record)
    _append(history, dict(record, event="armed"))
    return record


def active(store: str = STORE) -> Optional[Dict[str, Any]]:
    record = _read_json(store)
    return record if record.get("status") == WATCHING else None


def _efficacy_verdict(
    record: Dict[str, Any],
    usable: List[Dict[str, Any]],
    store: str,
    verdict: Dict[str, Any],
) -> Dict[str, Any]:
    result = evaluation.judge(record, usable, store)
    verdict["efficacy"] = result
    verdict["last_run"] = int(usable[-1].get("run") or 0) if usable else None
    verdict["reason"] = result.get("reason") or "efficacy evaluation produced no reason"
    verdict["status"] = (
        HEALTHY if result.get("accepted")
        else INCONCLUSIVE if result.get("status") == evaluation.INCONCLUSIVE
        else REGRESSED
    )
    return verdict


def evaluate(
    store: str = STORE,
    run_history: str = RUN_HISTORY,
) -> Dict[str, Any]:
    """Judge the live canary. Pure measurement; moves nothing."""
    record = _read_json(store)
    if record.get("status") != WATCHING:
        return {"status": INACTIVE, "reason": "no canary armed"}

    armed_at = record.get("armed_at_run")
    runs = _runs(run_history)
    after = [r for r in runs if armed_at is None or int(r.get("run") or 0) > int(armed_at)]
    # Runs an outside event ruined say nothing about the release, so they are not
    # evidence either way. They are still counted as observed so a long invasion
    # cannot hold a canary open forever.
    transition_exclusions = _progression_transition_runs(after)
    contaminated = [
        row for row in after
        if _exogenous_loss(row) or int(row.get("run") or -1) in transition_exclusions
    ]
    usable = [row for row in after if row not in contaminated]

    # Absolute rate is reported for legibility; the per-animal figure decides. Rate
    # windows are time-weighted so a rapid duplicate run cannot outweigh minutes of
    # measured production merely because it contributes another row.
    baseline = record.get("baseline_rate")
    observed = _weighted_mean(usable, _rate)
    baseline_pa = record.get("baseline_per_animal")
    observed_pa = _weighted_mean(usable, _per_animal)

    verdict: Dict[str, Any] = {
        "status": WATCHING,
        "revision": record.get("revision"),
        "previous": record.get("previous"),
        "order_id": record.get("order_id"),
        "runs_observed": len(after),
        "baseline_rate": baseline,
        "observed_rate": observed,
        "threshold": None,
        "reason": "",
    }

    progress_ts = after[-1].get("ts") if after else record.get("armed_ts")
    progress_age = _age_seconds(progress_ts)
    verdict["progress_age_seconds"] = progress_age
    if isinstance(progress_age, int) and progress_age > rules.CANARY_STALL_SECONDS:
        verdict["status"] = REGRESSED
        verdict["reason"] = (
            "no completed post-release run for %d minutes (limit %d minutes)"
            % (progress_age // 60, rules.CANARY_STALL_SECONDS // 60)
        )
        return verdict

    # A core transport/parser failure is decisive on its own: waiting for a rate
    # average would keep broken code live for several more cycles. Empty collections
    # are not independently decisive. The server banks produce and updates lifetime
    # score in bursts, so seven empty collections occurred while rank, herd, and the
    # time-weighted score rate stayed healthy. Sustained inactivity is still rejected
    # by the authoritative rate floor below.
    broken: List[tuple[Dict[str, Any], str]] = []
    for row in after:
        failure = _breakage(row)
        if failure:
            broken.append((row, failure))
    if broken:
        row, failure = broken[0]
        verdict["status"] = REGRESSED
        verdict["failure_kind"] = failure
        verdict["reason"] = "run %s failed under the new release: %s" % (
            row.get("run"),
            ", ".join(str(a) for a in (row.get("anomalies") or [])[:3]) or failure,
        )
        return verdict

    if len(after) < rules.CANARY_MIN_RUNS:
        verdict["reason"] = "%d/%d runs observed" % (len(after), rules.CANARY_MIN_RUNS)
        return verdict

    if record.get("change_class") == "observability":
        verdict["status"] = HEALTHY
        verdict["last_run"] = int(after[-1].get("run") or 0)
        verdict["efficacy"] = {
            "status": evaluation.EQUIVALENT,
            "accepted": True,
            "change_class": "observability",
            "metric": "clean_completed_cycles_and_current_readouts",
            "baseline": None,
            "candidate": len(after),
            "effect": 0.0,
            "reason": "path-gated observability release completed clean cycles; production is not a causal metric for this diff",
        }
        verdict["reason"] = (
            "observability release completed %d clean run(s); dashboard/readout gates passed and no game-action failure occurred"
            % len(after)
        )
        return verdict

    # League is the primary leaderboard key. A prestige that independently proves
    # level increased, capacity did not fall, and lifetime produce was preserved is direct
    # objective evidence; comparing its intentionally reset herd with the old herd's
    # raw production distribution would optimize the secondary metric and undo the
    # winning move. Still require production to resume and ordinary breakage checks
    # to stay clean before accepting the release.
    progression = _verified_progression(after)
    if progression:
        transition_rows = after[int(progression["row_index"]) + 1:]
        # The action row's score delta was mostly earned by the retiring herd.
        # Require a later completed interval to prove the replacement herd runs.
        resumed = any((_rate(row) or 0.0) > 0 for row in transition_rows)
        verdict["progression"] = progression
        if not resumed:
            if len(after) >= rules.CANARY_MAX_RUNS:
                verdict["status"] = REGRESSED
                verdict["reason"] = (
                    "league progression verified but production did not resume in %d post-release runs"
                    % len(after)
                )
            else:
                verdict["reason"] = (
                    "league progression verified; waiting for post-reset production (%d/%d runs)"
                    % (len(transition_rows), rules.CANARY_MAX_RUNS)
                )
            return verdict
        before = progression["before"]
        after_state = progression["after"]
        verdict["status"] = HEALTHY
        verdict["last_run"] = int(after[-1].get("run") or 0)
        verdict["efficacy"] = {
            "status": evaluation.IMPROVED,
            "accepted": True,
            "change_class": record.get("change_class") or "strategy",
            "metric": "league_level_then_lifetime_produce",
            "baseline": before.get("league_level"),
            "candidate": after_state.get("league_level"),
            "effect": int(after_state.get("league_level")) - int(before.get("league_level")),
            "capacity_before": before.get("capacity"),
            "capacity_after": after_state.get("capacity"),
            "lifetime_before": before.get("lifetime_produce"),
            "lifetime_after": after_state.get("lifetime_produce"),
            "reason": "verified lexicographic leaderboard progression",
        }
        verdict["reason"] = (
            "verified league progression level %s -> %s, capacity %s -> %s (nondecreasing), "
            "lifetime produce preserved, and post-reset production resumed"
            % (
                before.get("league_level"),
                after_state.get("league_level"),
                before.get("capacity"),
                after_state.get("capacity"),
            )
        )
        return verdict

    replacement = _verified_capped_replacement(record, after)
    if replacement:
        resumed = any((_rate(row) or 0.0) > 0 for row in after)
        verdict["capped_replacement"] = replacement
        if not resumed:
            if len(after) >= rules.CANARY_MAX_RUNS:
                verdict["status"] = REGRESSED
                verdict["reason"] = "capped replacement applied but production did not resume"
            else:
                verdict["reason"] = "capped replacement applied; waiting for production"
            return verdict
        effect = max(0.0, float(record.get("expected_improvement") or 0.0))
        verdict["status"] = HEALTHY
        verdict["last_run"] = int(after[-1].get("run") or 0)
        verdict["efficacy"] = {
            "status": evaluation.IMPROVED,
            "accepted": True,
            "change_class": "strategy",
            "metric": "capped_animal_slot_output",
            "baseline": replacement.get("baseline_beehives"),
            "candidate": replacement.get("candidate_beehives"),
            "effect": effect,
            "reason": "supported capped-slot policy was applied to a live natural vacancy",
        }
        verdict["reason"] = (
            "verified capped-slot strategy: beehives %d -> %d at %d/%d animals; "
            "production resumed under the evidence-linked policy"
            % (
                replacement.get("baseline_beehives"),
                replacement.get("candidate_beehives"),
                replacement.get("animals"),
                replacement.get("capacity"),
            )
        )
        return verdict

    # A reliability correction armed during an intentional post-prestige rebuild
    # cannot be compared with the retired herd. Once one clean post-arm interval
    # proves production resumed, keep it live without promoting it as an efficacy
    # champion. A bounded no-production window still fails closed.
    if record.get("preexisting_progression_recovery") and record.get("change_class") != "strategy":
        resumed = any((_rate(row) or 0.0) > 0 for row in usable)
        verdict["preexisting_progression_recovery"] = True
        verdict["preexisting_progression_run"] = record.get("preexisting_progression_run")
        if resumed and len(after) >= rules.CANARY_MIN_RUNS:
            verdict["status"] = INCONCLUSIVE
            verdict["last_run"] = int(after[-1].get("run") or 0)
            verdict["reason"] = (
                "release armed during verified post-prestige recovery; production resumed "
                "through %d clean run(s), keeping reliability changes without champion promotion"
                % len(usable)
            )
            return verdict
        if len(after) >= rules.CANARY_MAX_RUNS:
            verdict["status"] = REGRESSED
            verdict["reason"] = (
                "post-prestige production did not resume in %d post-release runs"
                % len(after)
            )
            return verdict
        verdict["reason"] = (
            "pre-existing post-prestige recovery; waiting for production (%d/%d runs)"
            % (len(after), rules.CANARY_MAX_RUNS)
        )
        return verdict

    # A candidate cannot cause a condition observed before it existed. Reliability
    # and compatibility releases armed during a proven score stall keep running after
    # a minimum clean post-arm window, but they do not become the champion: there was
    # no healthy baseline against which efficacy could be measured. Strategy remains
    # fail-closed because an unmeasurable gain is not evidence of improvement.
    if record.get("baseline_stalled"):
        verdict["baseline_stalled"] = True
        verdict["baseline_stall_runs"] = list(record.get("baseline_stall_runs") or [])
        if len(usable) < rules.CANARY_MIN_RUNS:
            verdict["reason"] = "pre-existing score stall; %d/%d clean attribution runs" % (
                len(usable), rules.CANARY_MIN_RUNS,
            )
            return verdict
        if record.get("change_class") == "strategy":
            verdict["status"] = REGRESSED
            verdict["reason"] = (
                "strategy candidate cannot prove improvement because score production "
                "was already stalled before release"
            )
        else:
            verdict["status"] = INCONCLUSIVE
            verdict["reason"] = (
                "pre-existing score stall continued through %d clean candidate runs; "
                "keeping the reliability release without champion promotion"
                % len(usable)
            )
        return verdict

    if len(after) >= rules.EFFICACY_MIN_RUNS * 2 and len(usable) < rules.EFFICACY_MIN_RUNS:
        verdict["status"] = REGRESSED
        verdict["reason"] = "insufficient clean efficacy evidence after %d observed runs" % len(after)
        return verdict

    # A strategy candidate may never promote on missing evidence. Reliability
    # releases can remain provisionally live, but still wait through the complete
    # efficacy window before equivalence is adjudicated.
    if baseline is None or observed is None or baseline <= 0:
        if len(usable) >= rules.EFFICACY_MIN_RUNS:
            return _efficacy_verdict(record, usable, store, verdict)
        verdict["reason"] = "no comparable baseline; %d/%d efficacy runs observed" % (
            len(usable), rules.EFFICACY_MIN_RUNS,
        )
        return verdict

    if contaminated:
        verdict["excluded_runs"] = [int(r.get("run") or 0) for r in contaminated]
        verdict["excluded_reason"] = (
            _exogenous_loss(contaminated[0])
            or "progression transition and first lagging score interval"
        )

    # Prefer the herd-normalised comparison. Fall back to absolute only for a canary
    # armed before this field existed, so an in-flight canary still resolves.
    if baseline_pa and observed_pa is not None:
        floor = baseline_pa * (1.0 - rules.CANARY_REGRESSION_TOLERANCE)
        verdict["baseline_per_animal"] = round(baseline_pa, 6)
        verdict["observed_per_animal"] = round(observed_pa, 6)
        verdict["threshold"] = floor
        if observed_pa < floor:
            verdict["status"] = REGRESSED
            verdict["reason"] = (
                "per-animal produce %.4f vs baseline %.4f (floor %.4f) over %d run(s)"
                % (observed_pa, baseline_pa, floor, len(usable))
            )
            return verdict
        if len(usable) >= rules.EFFICACY_MIN_RUNS:
            return _efficacy_verdict(record, usable, store, verdict)
        verdict["reason"] = "per-animal %.4f vs baseline %.4f, %d/%d runs" % (
            observed_pa, baseline_pa, len(after), rules.CANARY_MAX_RUNS,
        )
        return verdict


    threshold = baseline * (1.0 - rules.CANARY_REGRESSION_TOLERANCE)
    verdict["threshold"] = threshold
    if observed < threshold:
        verdict["status"] = REGRESSED
        verdict["reason"] = "produce %.1f/min vs baseline %.1f/min (floor %.1f) over %d runs" % (
            observed, baseline, threshold, len(after),
        )
        return verdict

    if len(usable) >= rules.EFFICACY_MIN_RUNS:
        return _efficacy_verdict(record, usable, store, verdict)

    verdict["reason"] = "%.1f/min vs baseline %.1f/min, %d/%d runs" % (
        observed, baseline, len(after), rules.CANARY_MAX_RUNS,
    )
    return verdict


def _quantity(value: Any) -> float:
    """Normalize scalar and structured counters from historical run schemas.

    ``collected`` used to be a scalar and is now a per-produce mapping. Canary
    evaluation spans releases, so it must understand both shapes rather than
    crashing while a previous release is still under observation.
    """
    if isinstance(value, dict):
        return sum(_quantity(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_quantity(item) for item in value)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _breakage(row: Dict[str, Any], zero_streak: Optional[int] = None) -> str:
    """Classify decisive breakage; collection-only streaks defer to score rate."""
    del zero_streak  # retained for callers that replay the historical signature
    if int(row.get("mechanic_failures") or 0) > 0:
        return "adaptive_mechanic_verification_failure"
    if int(row.get("plot_failures") or 0) > 0:
        return "adaptive_plot_verification_failure"
    if row.get("strategy_policy_errors"):
        return "adaptive_strategy_policy_invalid"
    if any(
        action.get("status") == "failed"
        for action in row.get("mechanic_actions") or []
        if isinstance(action, dict)
    ):
        return "adaptive_mechanic_verification_failure"
    if _quantity(row.get("transport_errors_core")) > 0 and _quantity(row.get("collected")) == 0:
        return "core_transport_or_parse_failure"
    return ""


def _looks_broken(row: Dict[str, Any], zero_streak: Optional[int] = None) -> bool:
    """Did this run fail outright?

    Deliberately narrow. Risk events (wolves, sickness) are expected stochastic
    losses and must not count, or the canary would revert good releases every time
    a wolf turned up -- the precise confusion POSTMORTEM-run377 warns about.
    """
    return bool(_breakage(row, zero_streak=zero_streak))


def _regression_order(
    record: Dict[str, Any],
    verdict: Dict[str, Any],
    queue: str,
) -> Dict[str, Any]:
    """File one durable repair order for a genuine candidate regression."""
    identity = str(record.get("order_id") or record.get("commit") or record.get("revision") or "unknown")
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in identity).strip("-")[:120]
    order_id = "canary-regression-%s" % (slug or "unknown")
    files = [control.normalize_path(str(path)) for path in (record.get("files") or [])
             if control.author_editable(str(path))]
    change = {
        "id": order_id,
        "kind": "canary_regression",
        "severity": "breaking",
        "summary": "Release %s regressed: %s" % (
            record.get("revision"), str(verdict.get("reason") or "unknown failure")[:260]
        ),
        "detail": {
            "revision": record.get("revision"),
            "previous": record.get("previous"),
            "commit": record.get("commit"),
            "originating_order": record.get("order_id"),
            "failure_kind": verdict.get("failure_kind"),
            "candidate_zero_streak": verdict.get("candidate_zero_streak"),
            "verdict": dict(verdict),
        },
        "sites": files,
        "we_use_it": True,
    }
    submitted = workorders.submit(
        change,
        source="release_canary",
        intent=(
            "Repair the implementation regression observed only after release %s. "
            "Use the captured candidate-scoped verdict; do not weaken canary, safety, "
            "rollback, cost, or protected-file controls."
        ) % record.get("revision"),
        acceptance=[
            "the captured candidate regression is reproduced by a deterministic test",
            "the candidate completes clean post-arm runs without suppressing genuine failures",
            "the full guarded release matrix passes",
        ],
        files=files,
        path=queue,
        provenance={
            "change_class": "reliability",
            "rejected_revision": str(record.get("revision") or ""),
            "rejected_commit": str(record.get("commit") or ""),
        },
    )
    current = workorders.current(queue).get(order_id) or {}
    return {"id": order_id, "created": submitted is not None, "status": current.get("status")}


def resolve(
    verdict: Dict[str, Any],
    project: Optional[str] = None,
    store: str = STORE,
    history: str = HISTORY,
) -> Dict[str, Any]:
    """Act on a verdict: accept, revert, or clear un-attributable probation."""
    project = project or PROJECT
    status = verdict.get("status")
    if status not in (HEALTHY, REGRESSED, INCONCLUSIVE):
        return {"acted": False, "status": status, "reason": verdict.get("reason", "")}

    record = _read_json(store)
    outcome: Dict[str, Any] = {
        "acted": True,
        "status": status,
        "revision": record.get("revision"),
        "previous": record.get("previous"),
        "order_id": record.get("order_id"),
        "reason": verdict.get("reason", ""),
        "reverted": False,
    }

    if status == REGRESSED:
        outcome.update(revert(str(record.get("previous") or ""), project))
        # Only once the pointer has actually moved, and only when operating on the
        # real project, refresh the import-time UI and record the inverse commit.
        # Guarding on `project` keeps temp-directory tests from touching launchd or
        # real git history.
        if outcome.get("reverted") and os.path.abspath(project) == os.path.abspath(str(PROJECT)):
            outcome.update(_restart_monitor())
            inverse = record_inverse_commit(store)
            if inverse.get("commit"):
                outcome["inverse_commit"] = inverse["commit"]
            if inverse.get("pushed"):
                outcome["inverse_remote"] = inverse.get("remote")
                outcome["inverse_remote_commit"] = inverse.get("remote_commit")
            if inverse.get("error"):
                # Runtime safety has already been restored by the pointer flip. Keep
                # any GitHub bookkeeping failure explicit in the immutable event so
                # an operator never mistakes a local inverse for a synchronized one.
                outcome["inverse_push_error"] = inverse["error"]
        queue = os.path.join(os.path.dirname(store), os.path.basename(workorders.QUEUE))
        try:
            outcome["work_order"] = _regression_order(record, verdict, queue)
        except Exception as exc:  # noqa: BLE001 - rollback remains authoritative
            outcome["work_order_error"] = str(exc)[:200]

    # Clear either way. A regressed canary must not stay armed, or the next
    # supervisor pass would try to revert again and walk the pointer backwards.
    resolved_ts = _utcnow()
    # Preserve the structured decision after probation ends. Without it the
    # dashboard can say only "regressed" and loses the measured baseline, observed
    # value, threshold, excluded runs, and sample count that explain why.
    durable_verdict = dict(verdict)
    _write_json(store, dict(record, status=status, resolved_ts=resolved_ts,
                            resolution=verdict.get("reason", "")[:300],
                            verdict=durable_verdict,
                            efficacy=verdict.get("efficacy") or {}))
    _append(history, dict(outcome, event="resolved", ts=resolved_ts,
                          verdict=durable_verdict,
                          efficacy=verdict.get("efficacy") or {}))
    try:
        evaluation.record_resolution(record, verdict, store)
    except Exception as exc:  # noqa: BLE001 - pointer safety already decided
        outcome["efficacy_record_error"] = str(exc)[:200]
    if status == HEALTHY:
        try:
            outcome["compaction_compatibility"] = compaction.mark_compatible(
                Path(store).resolve().parent, str(record.get("revision") or "")
            )
        except Exception as exc:  # noqa: BLE001 - release verdict remains durable
            outcome["compaction_compatibility_error"] = str(exc)[:200]
    return outcome


def _restart_monitor() -> Dict[str, Any]:
    """Restart the installed monitor after a live pointer change.

    monitor.py composes its document and registers routes at import time. A pointer
    rollback without a process restart therefore keeps serving the rejected UI even
    though every scheduled agent has returned to the previous release. The browser's
    revision guard handles an already-open document; this shared control-plane restart
    handles the server without duplicating launchd identity outside the registry.
    """
    result = control.restart_service("monitor")
    out: Dict[str, Any] = {
        "monitor_restarted": bool(result.get("restarted")),
        "monitor_restart": result.get("restart") or result.get("label"),
    }
    if result.get("restart_error"):
        out["monitor_restart_error"] = result["restart_error"]
    return out


def revert(previous: str, project: Optional[str] = None) -> Dict[str, Any]:
    """Point `release` back at `previous` with the same atomic rename(2) flip.

    `mv` is not usable here: BSD mv follows an existing symlink-to-directory and
    moves the new link *inside* the old release, silently leaving the pointer
    stale. That bug already pinned launchd to old code once, so the flip is done
    with os.replace exactly as deploy/release.sh does it.
    """
    project = project or PROJECT
    if not previous:
        return {"reverted": False, "error": "no previous revision recorded"}

    target = os.path.join(project, "releases", previous)
    link = os.path.join(project, "release")
    if not os.path.isdir(target):
        # Pruning may have removed it. Reverting to nothing would be worse than
        # leaving the suspect release live and shouting about it.
        return {"reverted": False, "error": "previous release %s no longer on disk" % previous}

    tmp = "%s.revert.%d" % (link, os.getpid())
    try:
        if os.path.islink(tmp) or os.path.exists(tmp):
            os.remove(tmp)
        os.symlink(os.path.abspath(target), tmp)
        os.replace(tmp, link)
    except OSError as exc:
        return {"reverted": False, "error": "flip failed: %s" % exc.__class__.__name__}

    resolved = os.path.realpath(link)
    if os.path.realpath(target) != resolved:
        return {"reverted": False, "error": "flip did not take: %s" % resolved}
    return {"reverted": True, "now_live": os.path.basename(resolved)}


def record_inverse_commit(store: str = STORE) -> Dict[str, Any]:
    """Record and push an inverse commit for the change the canary rejected.

    Deliberately NOT called from revert(). It used to be, and that was a mistake
    with real consequences: revert() takes a `project` argument so it can flip a
    symlink inside a temp directory, which is exactly how deploy/test_author.py
    exercises it -- but the git work ignored that argument and operated on the real
    repository. The author suite therefore rewrote the live `main` branch and undid
    a genuine production commit (the alien-abduction detection, 46ee691) while
    reporting all checks green.

    Two lessons are encoded here. A function whose job is to flip a symlink should
    flip a symlink and nothing else. And a consequential side effect belongs at the
    one call site that has actually decided to take it -- the supervisor's canary
    adjudication -- not buried in a helper that tests call with fake paths.

    Runtime restoration is the pointer flip and always comes first. This helper is
    durable bookkeeping: it records the inverse on local main, pushes that exact SHA
    to the allowlisted origin, and reports either proof or a bounded error for the
    canary ledger. It never turns a successful runtime rollback back into a failure.
    """
    record = _read_json(store)
    commit = record.get("commit")
    if not commit:
        return {}
    try:
        from . import vcs
        if not vcs.available():
            return {"error": "version control unavailable after canary rollback"}
        base_commit = str(record.get("base_commit") or "")
        inverse = vcs.revert_range(
            base_commit,
            commit,
            "Revert: canary rejected release %s\n\n"
            "Production regressed after this change shipped, so the release pointer\n"
            "was flipped back to %s automatically. This inverse commit exists so the\n"
            "next release does not re-publish the same candidate range.\n\n"
            "Reverted range: %s..%s\n"
            "Reverted by: farm/canary.py, unattended."
            % (record.get("revision"), record.get("previous"), base_commit or "<single>", commit),
        )
        if not inverse:
            return {"error": "could not record inverse commit after canary rollback"}
        result: Dict[str, Any] = {"commit": inverse, "pushed": False}
        try:
            pushed = vcs.push_main(
                inverse,
                expected_remote_sha=str(commit),
                expected_local_sha=inverse,
            )
            result.update({"pushed": True,
                           "remote": "%s/%s" % (pushed.get("remote"), pushed.get("branch")),
                           "remote_commit": pushed.get("sha")})
        except Exception as exc:  # noqa: BLE001 - runtime rollback already succeeded
            result["error"] = "inverse commit was not pushed: %s" % str(exc)[:300]
        return result
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never undo a good revert
        return {"error": "inverse commit bookkeeping failed: %s" % str(exc)[:300]}


def status(store: str = STORE, run_history: str = RUN_HISTORY) -> Dict[str, Any]:
    """Read-only summary for the dashboard and --canary-status."""
    record = _read_json(store)
    if not record:
        return {"armed": False, "status": INACTIVE}
    out = {
        "armed": record.get("status") == WATCHING,
        "status": record.get("status"),
        "revision": record.get("revision"),
        "previous": record.get("previous"),
        "order_id": record.get("order_id"),
        "armed_ts": record.get("armed_ts"),
        "resolved_ts": record.get("resolved_ts"),
        "resolution": record.get("resolution"),
        "change_class": record.get("change_class") or "reliability",
        "hypothesis_id": record.get("hypothesis_id"),
        "policy_id": record.get("policy_id"),
        "efficacy": record.get("efficacy") or {},
        "champion": evaluation.champion(store),
        "verdict": record.get("verdict") or {},
    }
    if out["armed"]:
        out["verdict"] = evaluate(store, run_history)
    return out
