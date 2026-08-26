"""Provisional releases: prove a flip helped, or undo it without a human.

Autonomous code publishing is only defensible if it is reversible. The release
machinery already gives us the hard part -- immutable `releases/<rev>/` trees and
an atomic symlink flip -- but nothing ever used it to go *backwards*. This module
is that missing half.

When the author agent flips a release it **arms** a canary recording the previous
revision and a pre-flip performance baseline. The supervisor then evaluates on
every pass:

    watching   -> not enough post-flip runs yet to judge
    healthy    -> the farm is producing at least as fast as before; clear it
    regressed  -> revert the pointer to the previous revision and file the reason

Why produce_per_min and not a test result
----------------------------------------
The gate matrix already proves the code is *correct*. It cannot prove the code is
*good for the score*, and the score is the only thing that decides the game. A
change can pass every suite and still halve output -- POSTMORTEM-run377 documents
exactly that: three throttles aimed at the wrong variable, all individually
reasonable, which together nearly lost first place. So the canary watches the one
number that matters.

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
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import control, rules

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
INACTIVE = "inactive"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _runs(path: str = RUN_HISTORY, limit: int = 400) -> List[Dict[str, Any]]:
    """Recent run rows, oldest first. Bounded: history.ndjson is megabytes."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("run") is not None:
            out.append(row)
    return out


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


def baseline_rate(runs: Optional[List[Dict[str, Any]]] = None) -> Optional[float]:
    """Mean produce rate over the runs immediately before now."""
    rows = runs if runs is not None else _runs()
    rates = [r for r in (_rate(row) for row in rows[-rules.CANARY_BASELINE_RUNS :]) if r is not None]
    return _mean(rates)


def baseline_per_animal(runs: Optional[List[Dict[str, Any]]] = None) -> Optional[float]:
    """Mean per-animal produce rate over the runs immediately before now.

    This is the figure the verdict actually uses. `baseline_rate` is still recorded
    because it is what an operator recognises, but it must not decide a revert.
    """
    rows = runs if runs is not None else _runs()
    rates = [r for r in (_per_animal(row) for row in rows[-rules.CANARY_BASELINE_RUNS :])
             if r is not None]
    return _mean(rates)


def latest_run(runs: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
    rows = runs if runs is not None else _runs()
    return int(rows[-1]["run"]) if rows else None


def arm(
    revision: str,
    previous: str,
    reason: str = "",
    order_id: str = "",
    commit: str = "",
    store: str = STORE,
    history: str = HISTORY,
    run_history: str = RUN_HISTORY,
) -> Dict[str, Any]:
    """Record that `revision` is live provisionally and must prove itself."""
    runs = _runs(run_history)
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
        "armed_ts": _utcnow(),
        "armed_at_run": latest_run(runs),
        "baseline_rate": baseline_rate(runs),
        "baseline_per_animal": baseline_per_animal(runs),
        "baseline_runs": rules.CANARY_BASELINE_RUNS,
    }
    _write_json(store, record)
    _append(history, dict(record, event="armed"))
    return record


def active(store: str = STORE) -> Optional[Dict[str, Any]]:
    record = _read_json(store)
    return record if record.get("status") == WATCHING else None


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
    contaminated = [r for r in after if _exogenous_loss(r)]
    usable = [r for r in after if not _exogenous_loss(r)]
    rates = [r for r in (_rate(row) for row in usable) if r is not None]
    per_animal = [r for r in (_per_animal(row) for row in usable) if r is not None]

    # Absolute rate is reported for legibility; the per-animal figure decides.
    baseline = record.get("baseline_rate")
    observed = _mean(rates)
    baseline_pa = record.get("baseline_per_animal")
    observed_pa = _mean(per_animal)

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

    # A run that ends in a hard failure is decisive on its own: the canary exists
    # to catch exactly this, and waiting for a rate average would keep broken code
    # live for several more cycles.
    broken = [r for r in after if _looks_broken(r)]
    if broken:
        verdict["status"] = REGRESSED
        verdict["reason"] = "run %s failed under the new release: %s" % (
            broken[0].get("run"),
            ", ".join(str(a) for a in (broken[0].get("anomalies") or [])[:3]) or "no produce recorded",
        )
        return verdict

    if len(after) < rules.CANARY_MIN_RUNS:
        verdict["reason"] = "%d/%d runs observed" % (len(after), rules.CANARY_MIN_RUNS)
        return verdict

    # No usable baseline (a fresh install, or history without rates) means there is
    # nothing to compare against. Clear rather than revert on no evidence.
    if baseline is None or observed is None or baseline <= 0:
        verdict["status"] = HEALTHY
        verdict["reason"] = "no comparable baseline; accepting after %d clean runs" % len(after)
        return verdict

    if contaminated:
        verdict["excluded_runs"] = [int(r.get("run") or 0) for r in contaminated]
        verdict["excluded_reason"] = _exogenous_loss(contaminated[0])

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
        if len(after) >= rules.CANARY_MAX_RUNS:
            verdict["status"] = HEALTHY
            verdict["reason"] = (
                "per-animal produce %.4f vs baseline %.4f over %d run(s)"
                % (observed_pa, baseline_pa, len(usable))
            )
            return verdict
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

    if len(after) >= rules.CANARY_MAX_RUNS:
        verdict["status"] = HEALTHY
        verdict["reason"] = "produce %.1f/min vs baseline %.1f/min over %d runs" % (
            observed, baseline, len(after),
        )
        return verdict

    verdict["reason"] = "%.1f/min vs baseline %.1f/min, %d/%d runs" % (
        observed, baseline, len(after), rules.CANARY_MAX_RUNS,
    )
    return verdict


def _looks_broken(row: Dict[str, Any]) -> bool:
    """Did this run fail outright?

    Deliberately narrow. Risk events (wolves, sickness) are expected stochastic
    losses and must not count, or the canary would revert good releases every time
    a wolf turned up -- the precise confusion POSTMORTEM-run377 warns about.
    """
    rate = _rate(row)
    if rate is not None and rate == 0:
        return True
    if int(row.get("zero_streak") or 0) >= 3:
        return True
    if int(row.get("transport_errors_core") or 0) > 0 and int(row.get("collected") or 0) == 0:
        return True
    return False


def resolve(
    verdict: Dict[str, Any],
    project: Optional[str] = None,
    store: str = STORE,
    history: str = HISTORY,
) -> Dict[str, Any]:
    """Act on a verdict: clear a healthy canary, revert a regressed one."""
    project = project or PROJECT
    status = verdict.get("status")
    if status not in (HEALTHY, REGRESSED):
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
        # real project, record the inverse commit. Guarding on `project` keeps test
        # suites that flip a symlink in a temp directory from rewriting real history.
        if outcome.get("reverted") and os.path.abspath(project) == os.path.abspath(str(PROJECT)):
            inverse = record_inverse_commit(store)
            if inverse:
                outcome["inverse_commit"] = inverse

    # Clear either way. A regressed canary must not stay armed, or the next
    # supervisor pass would try to revert again and walk the pointer backwards.
    _write_json(store, dict(record, status=status, resolved_ts=_utcnow(),
                            resolution=verdict.get("reason", "")[:300]))
    _append(history, dict(outcome, event="resolved", ts=_utcnow()))
    return outcome


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


def record_inverse_commit(store: str = STORE) -> Optional[str]:
    """Record an inverse commit for the change the canary just rejected.

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

    Returns the inverse commit sha, or None if there was nothing to do.
    """
    record = _read_json(store)
    commit = record.get("commit")
    if not commit:
        return None
    try:
        from . import vcs
        if not vcs.available():
            return None
        return vcs.revert_commit(
            commit,
            "Revert: canary rejected release %s\n\n"
            "Production regressed after this change shipped, so the release pointer\n"
            "was flipped back to %s automatically. This inverse commit exists so the\n"
            "next release does not re-publish the same change.\n\n"
            "Reverted commit: %s\n"
            "Reverted by: farm/canary.py, unattended."
            % (record.get("revision"), record.get("previous"), commit),
        )
    except Exception:  # noqa: BLE001 - bookkeeping must never undo a good revert
        return None


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
    }
    if out["armed"]:
        out["verdict"] = evaluate(store, run_history)
    return out
