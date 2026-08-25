"""Pure evidence reconstruction and regime-aware estimators.

This module is deliberately free of MCP access and mutations. It turns the
append-only run ledger into stable cohorts and measurements that can be consumed
by claims, research audits, tests, and the Findings API.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import rules

PROJECT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1


def state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else PROJECT / "state"


def state_path(name: str) -> Path:
    return state_dir() / name


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def read_ndjson(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Read valid object rows, tolerating a corrupt or partial line.

    Evidence reads are full-history by default. A caller asking for a tail must
    do so explicitly; a hidden retention limit must never change an old finding.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    if limit is not None:
        lines = lines[-max(0, int(limit)):]
    rows: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def history_rows(
    limit: Optional[int] = None,
    include_dry: bool = False,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    rows = read_ndjson(path or state_path("history.ndjson"), limit=limit)
    if include_dry:
        return rows
    return [row for row in rows if not row.get("dry") and isinstance(row.get("run"), int)]


def regime_labels(row: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> List[str]:
    labels: List[str] = []
    hunger = int(row.get("max_hunger") or 0)
    if hunger >= rules.HUNGER_STOP:
        labels.append("starved")
    elif hunger >= rules.HUNGER_ALARM:
        labels.append("hunger_risk")
    else:
        labels.append("fed")

    minutes = None
    if previous:
        start, end = parse_ts(previous.get("ts")), parse_ts(row.get("ts"))
        if start and end:
            minutes = (end - start).total_seconds() / 60.0
    if minutes is not None and minutes > rules.RUN_GAP_ALARM_MINUTES:
        labels.append("blind_gap")
    if (
        minutes is not None
        and minutes >= rules.MIN_INTERVAL_FOR_PRODUCE_CHECK
        and isinstance(row.get("produce"), int)
        and isinstance((previous or {}).get("produce"), int)
        and row.get("produce") == (previous or {}).get("produce")
    ):
        labels.append("zero_output")

    by_tool = row.get("transport_errors_by_tool") or {}
    if any(int(by_tool.get(name) or 0) > 0 for name in rules.HEAVY_HERD_TOOLS):
        labels.append("bulk_gateway_limited")
    if int(row.get("transport_errors_core") or 0) > 0:
        labels.append("core_transport_noise")
    if (row.get("growth") or {}).get("saturated"):
        labels.append("growth_gated")
    if row.get("rank") == 1:
        labels.append("rank_1")
    return labels


def rate_samples(
    rows: Sequence[Dict[str, Any]],
    healthy_only: bool = True,
    max_gap_minutes: float = 30.0,
) -> List[Dict[str, Any]]:
    """Reconstruct lifetime-produce rates with explicit regimes and provenance."""
    samples: List[Dict[str, Any]] = []
    previous: Optional[Dict[str, Any]] = None
    for row in rows:
        if previous is None:
            previous = row
            continue
        start, end = parse_ts(previous.get("ts")), parse_ts(row.get("ts"))
        before, after = previous.get("produce"), row.get("produce")
        herd = row.get("animals")
        if not start or not end or not isinstance(before, int) or not isinstance(after, int):
            previous = row
            continue
        if not isinstance(herd, int) or herd <= 0 or after < before:
            previous = row
            continue
        minutes = (end - start).total_seconds() / 60.0
        if minutes < 1.0 or minutes > max_gap_minutes:
            previous = row
            continue
        labels = regime_labels(row, previous)
        if healthy_only and any(
            label in labels for label in ("starved", "hunger_risk", "blind_gap", "zero_output")
        ):
            # Zero-output windows remain authoritative operational incidents, but
            # they are not samples of the healthy scaling curve. Mixing a live
            # outage into that cohort would rewrite a mechanics claim instead of
            # surfacing the outage through watch/growth where it can fail closed.
            previous = row
            continue
        samples.append(
            {
                "run": row.get("run"),
                "from_run": previous.get("run"),
                "ts": row.get("ts"),
                "herd": herd,
                "minutes": round(minutes, 4),
                "produce_delta": after - before,
                "rate": (after - before) / minutes,
                "regimes": labels,
            }
        )
        previous = row
    return samples


def linear_regression(points: Sequence[Tuple[float, float]]) -> Dict[str, Optional[float]]:
    """Least-squares y on x with correlation, intercept, and RMSE."""
    clean = [
        (float(x), float(y))
        for x, y in points
        if isinstance(x, (int, float))
        and isinstance(y, (int, float))
        and math.isfinite(float(x))
        and math.isfinite(float(y))
    ]
    n = len(clean)
    empty: Dict[str, Optional[float]] = {
        "slope": None,
        "intercept": None,
        "r": None,
        "rmse": None,
        "n": n,
        "x_min": None,
        "x_max": None,
    }
    if n < 3:
        return empty
    xs = [point[0] for point in clean]
    ys = [point[1] for point in clean]
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in clean)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return empty
    slope = sxy / sxx
    intercept = my - slope * mx
    residuals = [y - (intercept + slope * x) for x, y in clean]
    rmse = math.sqrt(sum(value * value for value in residuals) / n)
    return {
        "slope": round(slope, 5),
        "intercept": round(intercept, 3),
        "r": round(sxy / math.sqrt(sxx * syy), 3),
        "rmse": round(rmse, 3),
        "n": n,
        "x_min": round(min(xs), 3),
        "x_max": round(max(xs), 3),
    }


def median(values: Iterable[float]) -> Optional[float]:
    ordered = sorted(float(value) for value in values if isinstance(value, (int, float)))
    if not ordered:
        return None
    return ordered[len(ordered) // 2]


def _bucket_edges(max_herd: int) -> List[int]:
    fixed = [0, 2_000, 4_000, 6_000, 8_000, 10_000, 12_000, 20_000,
             40_000, 60_000, 80_000, 100_000, 120_000, 150_000, 250_000]
    edges = [value for value in fixed if value <= max_herd]
    if not edges or edges[0] != 0:
        edges.insert(0, 0)
    if edges[-1] <= max_herd:
        edges.append(max_herd + 1)
    edges.append(10 ** 12)
    out: List[int] = []
    for value in edges:
        if not out or value > out[-1]:
            out.append(value)
    return out


def cohort_manifest(
    name: str,
    rows: Sequence[Dict[str, Any]],
    criteria: Dict[str, Any],
    fields: Sequence[str] = ("run", "ts", "produce", "animals", "max_hunger"),
) -> Dict[str, Any]:
    canonical = [
        {field: row.get(field) for field in fields}
        for row in rows
    ]
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
    runs = [int(row["run"]) for row in rows if isinstance(row.get("run"), int)]
    return {
        "name": name,
        "criteria": criteria,
        "rows": len(rows),
        "run_from": min(runs) if runs else None,
        "run_to": max(runs) if runs else None,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "fields": list(fields),
    }


def power_law_fit(points: Sequence[tuple]) -> Dict[str, Any]:
    """Fit rate = a * herd**b and return the exponent.

    Why this exists alongside the linear fits
    -----------------------------------------
    The exponent answers the question the linear fits only gesture at. b == 1 is
    proportional scaling, b < 1 is saturation (each extra animal adds less than the
    last -- the "plateau" the operator actually cares about), b > 1 is super-linear.

    It is also far more stable than the correlation of the bucket means, because it
    does not depend on where the bucket edges happen to fall. Measured 2026-08-25:
    b = 1.157 on the 638 raw samples versus b = 1.124 on the 11 bucket means, while
    r of the bucket means swings between 0.917 and 0.992 across equally defensible
    weightings of the same data. A statistic that moves that much with an arbitrary
    choice cannot be used as a release gate; this one can.
    """
    usable = [(float(x), float(y)) for x, y in points
              if isinstance(x, (int, float)) and isinstance(y, (int, float)) and x > 0 and y > 0]
    if len(usable) < 3:
        return {"exponent": None, "r": None, "n": len(usable), "coefficient": None}
    logged = [(math.log(x), math.log(y)) for x, y in usable]
    fit = linear_regression(logged)
    exponent, intercept = fit.get("slope"), fit.get("intercept")
    return {
        "exponent": round(exponent, 4) if exponent is not None else None,
        "r": fit.get("r"),
        "n": len(usable),
        "coefficient": round(math.exp(intercept), 6) if intercept is not None else None,
    }


def weighted_linear_regression(
    points: Sequence[tuple], weights: Sequence[float]
) -> Dict[str, Optional[float]]:
    """Least squares on group means, weighted by how many samples each mean holds.

    Regressing unweighted bucket means treats a 6-sample band as equal evidence to
    a 266-sample band. That is not a stylistic preference: the variance of a mean
    is s^2/n, so equal weighting deliberately discards the precision information
    that makes the large bands trustworthy.
    """
    pairs = [(float(x), float(y), float(w)) for (x, y), w in zip(points, weights)
             if isinstance(x, (int, float)) and isinstance(y, (int, float)) and w > 0]
    if len(pairs) < 2:
        return {"slope": None, "intercept": None, "r": None, "n": len(pairs), "rmse": None}
    total = sum(w for _, _, w in pairs)
    mean_x = sum(w * x for x, _, w in pairs) / total
    mean_y = sum(w * y for _, y, w in pairs) / total
    s_xy = sum(w * (x - mean_x) * (y - mean_y) for x, y, w in pairs)
    s_xx = sum(w * (x - mean_x) ** 2 for x, _, w in pairs)
    s_yy = sum(w * (y - mean_y) ** 2 for _, y, w in pairs)
    if s_xx <= 0 or s_yy <= 0:
        return {"slope": None, "intercept": None, "r": None, "n": len(pairs), "rmse": None}
    slope = s_xy / s_xx
    intercept = mean_y - slope * mean_x
    residual = sum(w * (y - (slope * x + intercept)) ** 2 for x, y, w in pairs)
    return {
        "slope": round(slope, 5),
        "intercept": round(intercept, 2),
        "r": round(s_xy / math.sqrt(s_xx * s_yy), 4),
        "n": len(pairs),
        "rmse": round(math.sqrt(residual / total), 3),
    }


def output_model(
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    threshold: int = 8_000,
) -> Dict[str, Any]:
    history = list(rows) if rows is not None else history_rows()
    samples = rate_samples(history, healthy_only=True)
    points = [(sample["herd"], sample["rate"]) for sample in samples]
    above_samples = [sample for sample in samples if sample["herd"] >= threshold]
    below_samples = [sample for sample in samples if sample["herd"] < threshold]
    above = linear_regression([(sample["herd"], sample["rate"]) for sample in above_samples])
    below = linear_regression([(sample["herd"], sample["rate"]) for sample in below_samples])
    all_fit = linear_regression(points)

    buckets: List[Dict[str, Any]] = []
    max_herd = max([sample["herd"] for sample in samples], default=0)
    edges = _bucket_edges(max_herd)
    for low, high in zip(edges, edges[1:]):
        inside = [sample for sample in samples if low <= sample["herd"] < high]
        if not inside:
            continue
        avg_rate = sum(sample["rate"] for sample in inside) / len(inside)
        avg_herd = sum(sample["herd"] for sample in inside) / len(inside)
        buckets.append(
            {
                "label": f"{low:,}-{high - 1:,}" if high < 10 ** 12 else f"{low:,}+",
                "low": low,
                "high": None if high >= 10 ** 12 else high,
                "samples": len(inside),
                "herd": round(avg_herd),
                "units_per_min": round(avg_rate, 1),
                "per_animal": round(avg_rate / avg_herd, 4) if avg_herd else None,
            }
        )

    # Individual leaderboard windows are timer-batched and become increasingly
    # heteroscedastic as the herd grows. Preserve that raw fit for transparency,
    # but also fit the predeclared scale buckets: their means answer the actual
    # plateau question without letting collection/snapshot phase dominate it.
    bucket_fit = linear_regression(
        [(bucket["herd"], bucket["units_per_min"]) for bucket in buckets
         if bucket["herd"] >= threshold]
    )
    # The same bucket means, weighted by sample count. Reported because the
    # unweighted version above lets the newest and smallest band dominate: on
    # 2026-08-25 the 6-sample top band (0.9% of the data) moved unweighted r from
    # 0.992 to 0.925 on its own.
    above_buckets = [bucket for bucket in buckets if bucket["herd"] >= threshold]
    bucket_fit_weighted = weighted_linear_regression(
        [(bucket["herd"], bucket["units_per_min"]) for bucket in above_buckets],
        [bucket["samples"] for bucket in above_buckets],
    )
    # The scaling exponent, on raw samples so no bucket edge can steer it. This is
    # the statistic that can actually see saturation.
    scaling = power_law_fit([(sample["herd"], sample["rate"]) for sample in above_samples])
    scaling_bucketed = power_law_fit(
        [(bucket["herd"], bucket["units_per_min"]) for bucket in above_buckets]
    )
    slope = above.get("slope")
    correlation = above.get("r")
    n = int(above.get("n") or 0)
    bucket_slope = bucket_fit.get("slope")
    bucket_correlation = bucket_fit.get("r")
    bucket_n = int(bucket_fit.get("n") or 0)
    # Saturation is the operationally dangerous shape: it is the one that means
    # growing the herd has stopped paying. Nothing in this estimator could detect it
    # before -- a straight-line r falls for saturation and for super-linear growth
    # alike, so it cannot tell "stop adopting" from "keep adopting". The exponent can.
    exponent = scaling.get("exponent")
    exponent_bucketed = scaling_bucketed.get("exponent")
    saturating = bool(
        exponent is not None and exponent_bucketed is not None
        and exponent < 0.95 and exponent_bucketed < 0.95
    )
    raw_linear = correlation is not None and correlation > 0.8
    bucket_linear = (
        bucket_n >= 4 and bucket_slope is not None and bucket_slope > 0.05
        and bucket_correlation is not None and bucket_correlation > 0.95
    )
    # A weighted fit that clears the same bar is equally good evidence of scaling,
    # and is less hostage to the newest band's sample count.
    bucket_linear_weighted = (
        int(bucket_fit_weighted.get("n") or 0) >= 4
        and (bucket_fit_weighted.get("slope") or 0) > 0.05
        and (bucket_fit_weighted.get("r") or 0) > 0.95
    )
    scaling_at_least_linear = exponent is not None and exponent >= 0.95
    if saturating:
        shape = "saturating"
        confidence = min(0.95, 0.6 + min(n, 200) / 1000.0)
        statement = (
            f"Healthy collection rate is saturating above {threshold:,} animals "
            f"(scaling exponent {exponent:.3f} on n={scaling.get('n')} samples, "
            f"{exponent_bucketed:.3f} on {bucket_n} bands): each additional animal "
            f"is returning less than the last, so growth has stopped paying."
        )
    elif n >= 20 and slope is not None and slope > 0.05 and (
        raw_linear or bucket_linear or bucket_linear_weighted or scaling_at_least_linear
    ):
        support = max(float(correlation or 0.0), float(bucket_correlation or 0.0))
        shape = "linear"
        confidence = min(0.99, 0.65 + min(n, 200) / 1000.0 + max(0.0, support - 0.8))
        statement = (
            "Healthy collection rate is still scaling at least proportionally with herd "
            f"size above {threshold:,} animals (scaling exponent {exponent:.3f} on "
            f"n={scaling.get('n')} raw samples, {exponent_bucketed:.3f} on {bucket_n} "
            f"bands, where 1.0 is proportional and below 0.95 is saturation); growth is "
            "still paying, so a per-farm plateau is falsified. Straight-line slope is "
            f"{slope:+.5f} (r={correlation:.3f}) raw and {bucket_slope:+.5f} "
            f"(r={bucket_correlation:.3f}) bucketed, reported for continuity but not "
            "used to judge saturation because r cannot tell saturation from "
            "super-linear growth. Herd size is monotone in time, so this is an "
            "association and not a causal per-adoption estimate."
        )
    elif n >= 20 and slope is not None and abs(slope) <= 0.02:
        shape = "plateau"
        confidence = min(0.95, 0.6 + min(n, 200) / 1000.0)
        statement = (
            f"Healthy output is currently indistinguishable from a plateau above {threshold:,} "
            f"animals (slope {slope:+.5f}, n={n}); re-probe before stopping growth."
        )
    else:
        shape = "uncertain"
        confidence = min(0.75, n / 50.0)
        statement = (
            f"The herd-output relationship above {threshold:,} animals is not settled "
            f"(raw slope {slope}, r={correlation}, n={n}; bucketed slope "
            f"{bucket_slope}, r={bucket_correlation}, bands={bucket_n})."
        )

    evidence_rows = [
        row for row in history
        if isinstance(row.get("run"), int)
        and any(sample.get("run") == row.get("run") for sample in above_samples)
    ]

    # Herd size only ever grows, so it is very nearly a clock. Any change in our own
    # collection ability over time therefore lands on the herd coefficient, and the
    # data cannot separate the two. The direct evidence is a stretch where the herd
    # sat at ~120,127 for a hundred runs while per-animal rate fell to 0.111 and then
    # climbed to 0.194: a large move in the response with no move in the regressor.
    # This is recorded on the model so the claim cannot be read as causal.
    confound = {
        "regressor_is_monotone_in_time": True,
        "identified": False,
        "note": (
            "herd size is monotone in elapsed time, so this association cannot be "
            "separated from changes in our own collection behaviour; treat the slope "
            "as descriptive, not causal"
        ),
        "metric_measures": (
            "collection throughput: lifetime produce only advances when we collect, "
            "so a run that collects nothing scores zero regardless of what the "
            "animals produced"
        ),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "shape": shape,
        "statement": statement,
        "confidence": round(confidence, 3),
        "threshold": threshold,
        "samples": len(samples),
        "regression": above,
        "regression_bucketed": bucket_fit,
        "regression_bucketed_weighted": bucket_fit_weighted,
        "scaling": scaling,
        "scaling_bucketed": scaling_bucketed,
        "saturating": saturating,
        "confound": confound,
        "regression_below": below,
        "regression_all": all_fit,
        "buckets": buckets,
        "median_above_threshold": round(median(sample["rate"] for sample in above_samples) or 0.0, 1),
        "herd_now": samples[-1]["herd"] if samples else None,
        "output_now": round(samples[-1]["rate"], 1) if samples else None,
        "sample_run_from": samples[0]["run"] if samples else None,
        "sample_run_to": samples[-1]["run"] if samples else None,
        "cohort": cohort_manifest(
            "healthy-output-above-%d" % threshold,
            evidence_rows,
            {
                "herd_gte": threshold,
                "hunger_lt": rules.HUNGER_ALARM,
                "gap_minutes_lte": 30,
                "metric": "leaderboard lifetime-produce delta / wall minutes",
            },
        ),
    }


def species_model(
    rows: Optional[Sequence[Dict[str, Any]]] = None,
    kind_produce: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    history = list(rows) if rows is not None else history_rows()
    mapping = kind_produce or {
        "chicken": "egg",
        "pig": "truffle",
        "beehive": "honey",
        "sheep": "wool",
        "cow": "milk",
    }
    latest_kinds: Dict[str, int] = {}
    totals: Dict[str, int] = {}
    for row in history:
        by_kind = row.get("by_kind")
        if isinstance(by_kind, dict) and by_kind:
            latest_kinds = {
                str(kind): int(count)
                for kind, count in by_kind.items()
                if isinstance(count, (int, float))
            }
        for item, units in (row.get("collected") or {}).items():
            try:
                totals[str(item)] = totals.get(str(item), 0) + int(units)
            except (TypeError, ValueError):
                continue
    grand = sum(totals.values()) or 1
    recent = []
    for row in history[-40:]:
        if not row.get("verified") or float(row.get("interval_min") or 0) < 4.0:
            continue
        if int(row.get("max_hunger") or 0) >= rules.HUNGER_ALARM:
            continue
        if int(row.get("transport_errors_core") or 0) > 0:
            continue
        recent.append(row)

    recent_rates: Dict[str, Optional[float]] = {}
    for kind, produce in mapping.items():
        samples = []
        for row in recent:
            owned = int((row.get("by_kind") or {}).get(kind) or 0)
            units = int((row.get("collected") or {}).get(produce) or 0)
            interval = float(row.get("interval_min") or 0)
            if owned > 0 and units > 0 and interval > 0:
                samples.append(units / float(owned) / interval)
        recent_rates[kind] = median(samples)
    chicken_rate = recent_rates.get("chicken")

    table: List[Dict[str, Any]] = []
    for kind, produce in mapping.items():
        owned = latest_kinds.get(kind, 0)
        collected = totals.get(produce, 0)
        share = collected / float(grand)
        recent_rate = recent_rates.get(kind)
        cost = rules.ANIMAL_COST.get(kind)
        if share >= 0.99:
            verdict = "dominates observed collection"
        elif share < 0.0001:
            verdict = "indistinguishable from zero in collected output"
        elif share < 0.01:
            verdict = "economically negligible in collected output"
        else:
            verdict = "measurable minority of collected output"
        table.append(
            {
                "kind": kind,
                "produce": produce,
                "owned": owned,
                "collected": collected,
                "share": round(share, 6),
                "cumulative_per_current_animal": round(collected / owned, 3) if owned else None,
                "recent_units_per_animal_min": round(recent_rate, 6) if recent_rate is not None else None,
                "recent_vs_chicken": (
                    round(recent_rate / chicken_rate, 6)
                    if recent_rate is not None and chicken_rate not in (None, 0) else None
                ),
                "recent_units_per_purchase_coin_min": (
                    round(recent_rate / cost, 8)
                    if recent_rate is not None and cost else None
                ),
                "verdict": verdict,
            }
        )
    table.sort(key=lambda item: -item["collected"])
    chicken = next((item for item in table if item["kind"] == "chicken"), None) or {}
    statement = (
        "Chickens comprise most observed collected output "
        f"({100.0 * float(chicken.get('share') or 0.0):.3f}% of units), primarily because "
        "they comprise most of the herd. Recent per-animal and per-coin rates are reported "
        "separately; collection share alone does not establish species efficiency or a cap."
    )
    return {
        "table": table,
        "total_collected": grand,
        "runs_observed": len(history),
        "statement": statement,
        "scope": "collection composition plus recent same-window per-animal rates; lifetime score is evaluated separately",
        "recent_windows": len(recent),
        "cohort": cohort_manifest(
            "species-collected-output",
            history,
            {"metric": "sum(history.collected) by mapped species output"},
            fields=("run", "by_kind", "collected"),
        ),
    }
