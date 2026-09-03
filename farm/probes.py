"""Budgeted probe execution under the existing farm mutation lock."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import analysis, compaction, mcp, probe_guard, provenance, questions, rules, sandbox

PROJECT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1


class QuestionBindingUnavailable(RuntimeError):
    """The selected question changed state before the worker could start."""


def _registry() -> Dict[str, Dict[str, Any]]:
    from experiments.registry import PROBES
    return {name: dict(value) for name, value in PROBES.items()}


def _state_dir() -> Path:
    override = os.environ.get("FARM_STATE_DIR")
    return Path(override).resolve() if override else PROJECT / "state"


def _ledger() -> Path:
    return Path(os.environ.get("FARM_EXPERIMENT_LOG", str(_state_dir() / "experiments.ndjson")))


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _tail_file(path: Path, limit: int = 4_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max(1, int(limit))))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _append(row: Dict[str, Any]) -> None:
    path = _ledger()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.write(json.dumps(row, sort_keys=True, allow_nan=False, default=str) + "\n")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def list_probes() -> List[Dict[str, Any]]:
    return [dict({"id": name}, **value) for name, value in sorted(_registry().items())]


def _command(spec: Dict[str, Any]) -> List[str]:
    raw = list(spec.get("command") or [])
    if not raw:
        raise ValueError("probe has no command")
    relative = str(raw[0]).replace("\\", "/")
    if relative.startswith("/") or ".." in relative.split("/"):
        raise ValueError("probe command escapes the project: %s" % relative)
    candidate = PROJECT / relative
    if candidate.is_symlink():
        raise ValueError("probe script is symlinked: %s" % relative)
    script = candidate.resolve()
    try:
        script.relative_to(PROJECT.resolve())
    except ValueError as exc:
        raise ValueError("probe command escapes the project: %s" % relative) from exc
    if not script.is_file():
        raise ValueError("probe script missing or unsafe: %s" % relative)
    # Isolated mode prevents an editable sibling such as experiments/json.py
    # from shadowing the standard library inside a pinned autonomous script.
    return [sys.executable, "-I", str(script)] + [str(value) for value in raw[1:]]


def _ensure_registration(spec: Dict[str, Any], question_ids: List[str], probe_id: str) -> None:
    identity = str(spec.get("hypothesis_id") or "")
    if not identity:
        return
    registrations = [
        row for row in provenance.events()
        if row.get("event") == "hypothesis.registered" and row.get("node") == identity
    ]
    if registrations:
        registered_questions = set(str(value) for value in registrations[-1].get("question_ids") or [])
        requested_questions = set(question_ids)
        if registered_questions != requested_questions:
            raise provenance.ProvenanceError(
                "probe %s hypothesis is registered to different questions" % probe_id
            )
        registered_generations = registrations[-1].get("question_generations") or {}
        registered_seen = registrations[-1].get("question_last_seen_runs") or {}
        current = {str(row.get("id")): row for row in questions.load_all()}
        if requested_questions and any(
            int((current.get(identity) or {}).get("generation") or 0)
                != int(registered_generations.get(identity) or 0)
            or (current.get(identity) or {}).get("last_seen_run")
                != registered_seen.get(identity)
            for identity in requested_questions
        ):
            raise provenance.ProvenanceError(
                "probe %s hypothesis binding is stale for the active question generation" % probe_id
            )
        return
    current = {str(row.get("id")): row for row in questions.load_all()}
    registration = provenance.register_hypothesis(
        spec,
        ["question:%s" % value for value in question_ids] or ["explicit-probe:%s" % probe_id],
        question_ids=question_ids,
        question_generations={
            identity: int((current.get(identity) or {}).get("generation") or 1)
            for identity in question_ids
        },
        question_last_seen_runs={
            identity: (current.get(identity) or {}).get("last_seen_run")
            for identity in question_ids
        },
    )
    if registration.get("id") != identity:
        raise provenance.ProvenanceError(
            "probe %s declares hypothesis %s but semantics resolve to %s"
            % (probe_id, identity, registration.get("id"))
        )


def _attributed_calls(execution_id: str) -> int:
    """Legacy diagnostic count; authoritative usage comes from the parent grant."""
    tool_log = _state_dir() / "tool_calls.ndjson"
    return sum(
        1 for row in compaction.read_rows(tool_log, limit=10_000)
        if row.get("probe_id") == execution_id and row.get("event") == "start"
        and row.get("tool") != "tools/list"
    )


def _outputs(spec: Dict[str, Any]) -> List[str]:
    names = sorted(set(str(value) for value in (spec.get("outputs") or []) if value))
    if sum(name.endswith(".json") for name in names) > 1:
        raise ValueError("probe may declare at most one policy-driving JSON output")
    for name in names:
        path = Path(name)
        if path.is_absolute() or len(path.parts) != 1 or ".." in path.parts:
            raise ValueError("probe output escapes projected state: %s" % name)
        if path.suffix not in {".json", ".ndjson"}:
            raise ValueError("unsupported probe output type: %s" % name)
    return names


def _validated_provenance_result(
    projection: Dict[str, Any],
    hypothesis_id: str,
    spec: Dict[str, Any],
    declared_outputs: List[str],
) -> Optional[Dict[str, Any]]:
    """Validate a worker claim and derive evidence identity from admitted bytes."""
    if not hypothesis_id:
        return None
    root = Path(projection["root"])
    path = root / "provenance.ndjson"
    baseline = (projection.get("baselines") or {}).get("provenance.ndjson") or {}
    size = int(baseline.get("size") or 0)
    if not path.is_file() or path.is_symlink():
        raise sandbox.ResultValidationError("probe provenance result is missing or unsafe")
    data = path.read_bytes()
    if (len(data) < size
            or hashlib.sha256(data[:size]).hexdigest() != baseline.get("sha256")):
        raise sandbox.ResultValidationError("probe rewrote projected provenance")
    rows = []
    for line in data[size:].decode("utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict) and value.get("event") == "hypothesis.result":
                rows.append(value)
    matching = [row for row in rows if row.get("hypothesis_id") == hypothesis_id]
    if len(matching) > 1:
        raise sandbox.ResultValidationError("probe emitted multiple hypothesis results")
    if not matching:
        return None
    row = matching[0]
    status = str(row.get("status") or "")
    if status not in {"supported", "falsified", "rejected", "inconclusive"}:
        raise sandbox.ResultValidationError("probe emitted an invalid result status")
    evidence_class = str(spec.get("evidence_class") or "")
    if not evidence_class or row.get("evidence_class") != evidence_class:
        raise sandbox.ResultValidationError("probe cannot choose its evidence class")
    effect = row.get("effect") if isinstance(row.get("effect"), dict) else {}
    try:
        encoded_effect = json.dumps(effect, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise sandbox.ResultValidationError("probe effect is not strict JSON") from exc
    if len(encoded_effect) > 20_000:
        raise sandbox.ResultValidationError("probe effect exceeds its size limit")
    refs: List[str] = []
    for name in declared_outputs:
        # JSON results are canonicalized by admit_outputs before persistence. Hash
        # that exact representation rather than worker formatting; NDJSON ledgers
        # are supporting audit streams, not the content-addressed result object.
        if not name.endswith(".json") or name == "provenance.ndjson":
            continue
        output = root / name
        if output.is_file() and not output.is_symlink():
            try:
                value = json.loads(output.read_text(encoding="utf-8"))
                canonical = (
                    json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
                ).encode("utf-8")
            except (OSError, TypeError, ValueError) as exc:
                raise sandbox.ResultValidationError("probe evidence JSON is invalid") from exc
            refs.append("state/%s#sha256=%s" % (
                name, hashlib.sha256(canonical).hexdigest(),
            ))
    if not refs:
        raise sandbox.ResultValidationError("hypothesis result has no declared JSON evidence output")
    return {
        # These are a worker proposal, never a trusted causal result. A protected
        # hypothesis-specific adjudicator must independently recompute the metric
        # before provenance.record_result may be called.
        "proposed_status": status,
        "evidence_class": evidence_class,
        "validation_evidence": sorted(refs),
        "proposed_effect": effect,
        "trusted": False,
    }


def _terminate_group(process: subprocess.Popen) -> None:
    """Terminate the whole worker group, including children of an exited leader."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (OSError, ProcessLookupError):
            break
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _executor_identity() -> str:
    """Fingerprint the protected scheduler/boundary implementation.

    Failure backoff is scoped to these bytes so a repaired release earns one
    immediate retry without erasing the immutable history of the old failure.
    """
    digest = hashlib.sha256()
    for module_path in (Path(__file__), Path(sandbox.__file__), Path(probe_guard.__file__)):
        digest.update(module_path.read_bytes())
    return digest.hexdigest()[:16]


def _trusted_adjudication(
    probe_id: str,
    live_state: Path,
    prepared: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Interpret admitted probe bytes in trusted code.

    Editable workers may produce measurements, but they cannot choose which
    question classes they settle. That mapping lives here, behind the protected
    release boundary.
    """
    prepared_json = {
        str(item.get("path")): item
        for item in prepared
        if item.get("kind") == "json" and item.get("path")
    }

    def load(name: str) -> Dict[str, Any]:
        item = prepared_json.get(name) or {}
        value = item.get("value")
        return dict(value) if isinstance(value, dict) else {}

    def normalized(value: Any, ignored: set[str]) -> Any:
        if isinstance(value, dict):
            return {
                key: normalized(child, ignored)
                for key, child in value.items() if key not in ignored
            }
        if isinstance(value, list):
            return [normalized(child, ignored) for child in value]
        return value

    def require_equal(actual: Dict[str, Any], expected: Dict[str, Any], ignored: set[str] = set()) -> None:
        if normalized(actual, ignored) != normalized(expected, ignored):
            raise sandbox.ResultValidationError(
                "probe output does not match trusted recomputation: %s" % probe_id
            )

    def recompute_stateful_analysis(
        module: Any,
        filename: str,
        start_probe_id: str,
        required_tools: Dict[str, int],
    ) -> Dict[str, Any]:
        baseline = live_state / filename
        if not baseline.is_file() or baseline.is_symlink():
            raise sandbox.ResultValidationError("probe analysis has no trusted baseline: %s" % filename)
        try:
            baseline_value = json.loads(baseline.read_text(encoding="utf-8"))
            start_material = {
                key: value for key, value in baseline_value.items()
                if key not in {"result", "completed_ts"}
            }
            canonical = (
                json.dumps(start_material, indent=2, sort_keys=True, allow_nan=False) + "\n"
            ).encode("utf-8")
        except (OSError, TypeError, ValueError) as exc:
            raise sandbox.ResultValidationError("probe analysis baseline is invalid") from exc
        baseline_sha = hashlib.sha256(canonical).hexdigest()
        attestations = [
            event for event in analysis.read_ndjson(live_state / "experiments.ndjson", limit=2_000)
            if event.get("probe_id") == start_probe_id
            and event.get("status") == "passed"
            and event.get("explicit") is True
            and any(
                output.get("path") == filename and output.get("sha256") == baseline_sha
                for output in event.get("admitted_outputs") or []
                if isinstance(output, dict)
            )
        ]
        attestation = attestations[-1] if attestations else None
        usage = (attestation or {}).get("tool_usage") or {}
        if not attestation or any(
            int(usage.get(tool) or 0) != count
            for tool, count in required_tools.items()
        ):
            raise sandbox.ResultValidationError(
                "probe analysis baseline lacks a matching parent/broker attestation"
            )
        if not str(baseline_value.get("intervention_id") or "").startswith("int-"):
            raise sandbox.ResultValidationError("probe analysis baseline has no intervention identity")
        with sandbox.scratch_dir("farm-trusted-adjudication-") as temp_name:
            temp = Path(temp_name)
            probe_path = temp / filename
            probe_path.write_bytes(baseline.read_bytes())
            experiments_path = temp / "experiments.ndjson"
            experiments_path.touch()
            saved = (module.PROBE, module.TOOL_CALLS, module.EXPERIMENTS)
            try:
                module.PROBE = probe_path
                module.TOOL_CALLS = live_state / "tool_calls.ndjson"
                module.EXPERIMENTS = experiments_path
                module.analyze()
                value = json.loads(probe_path.read_text(encoding="utf-8"))
            finally:
                module.PROBE, module.TOOL_CALLS, module.EXPERIMENTS = saved
        if not isinstance(value, dict):
            raise sandbox.ResultValidationError("trusted probe recomputation was not an object")
        return value

    if probe_id == "activity_replay":
        value = load("activity_probe.json")
        from experiments import activity_probe
        expected = activity_probe.build(
            analysis.read_ndjson(live_state / "history.ndjson", limit=200)
        )
        require_equal(value, expected)
        trade_runs = [
            int(item) for item in (value.get("trade_decision_runs") or [])
            if isinstance(item, int)
        ]
        rival_runs = [
            int(item) for item in (value.get("rival_change_runs") or [])
            if isinstance(item, int)
        ]
        trade_ids = [str(item) for item in (value.get("trade_ids") or []) if isinstance(item, int)]
        rival_players = sorted({
            str(item.get("player") or "").strip().lower()
            for item in (value.get("material_rival_changes") or [])
            if isinstance(item, dict) and item.get("player")
        })
        coverage = {
            "activity_novelty_trade": {
                "settled": bool(trade_runs and trade_ids),
                "status": "supported" if trade_runs and trade_ids else "inconclusive",
                "evidence_cutoff_run": max(trade_runs) if trade_runs else None,
                "subjects": (
                    ["trade-" + value for value in trade_ids]
                    + (["trade-" + ",".join(trade_ids)] if trade_ids else [])
                ),
                "answer": "Trade replay classified %d completed decision(s) and %d held trade evidence record(s)."
                          % (int(value.get("decisions_observed") or 0),
                             len(value.get("held_trade_evidence") or [])),
            },
            "activity_novelty_rival": {
                "settled": bool(rival_runs and rival_players),
                "status": "supported" if rival_runs and rival_players else "inconclusive",
                "evidence_cutoff_run": max(rival_runs) if rival_runs else None,
                "subjects": rival_players + ([",".join(rival_players)] if rival_players else []),
                "answer": "Rival replay measured %d material regime-change interval(s)."
                          % len(value.get("material_rival_changes") or []),
            },
        }
        settled = any(item["settled"] for item in coverage.values())
        cutoffs = [
            item.get("evidence_cutoff_run") for item in coverage.values()
            if isinstance(item.get("evidence_cutoff_run"), int)
        ]
        return {
            "settled": settled,
            "status": "supported" if settled else "inconclusive",
            "evidence_cutoff_run": max(cutoffs) if cutoffs else None,
            "question_classes": ["activity_novelty_trade", "activity_novelty_rival"],
            "subjects": [],
            "coverage": coverage,
            "answer": str(value.get("finding") or "activity replay found no deciding event")[:400],
            "residual_uncertainty": (
                "future activity signatures remain independently reopenable"
                if settled else "the projected history window contained no deciding event"
            ),
        }

    if probe_id == "counterfactual_sweep":
        value = load("counterfactual_sweep.json")
        from . import research
        expected = research.counterfactual_sweep(
            analysis.read_ndjson(live_state / "history.ndjson")
        )
        require_equal(value, expected, {"generated_ts"})
        cutoff = value.get("run_to") if isinstance(value.get("run_to"), int) else None
        dimensions = [
            str(item.get("parameter")) for item in (value.get("dimensions") or [])
            if isinstance(item, dict) and item.get("parameter")
        ]
        return {
            "settled": False,
            "status": "inconclusive",
            "evidence_cutoff_run": cutoff,
            "question_classes": ["strategy_stale", "idle_capital", "knob_age", "policy_drift"],
            "subjects": [],
            "answer": "Counterfactual replay measured decision sensitivity for %s but did not establish outcome superiority."
                      % ", ".join(dimensions),
            "residual_uncertainty": "an outcome-labelled holdout or conservative invariant is still required",
        }

    if probe_id == "crop_timer_analysis":
        value = load("dual_cap_probe.json")
        from experiments import crop_timer_probe
        expected = recompute_stateful_analysis(
            crop_timer_probe,
            "dual_cap_probe.json",
            "crop_timer_revalidation",
            {"list_farm": 2, "plant": 3},
        )
        require_equal(value, expected, {"evaluated_ts", "completed_ts"})
        result = value.get("result") if isinstance(value.get("result"), dict) else value
        observations = result.get("observations") if isinstance(result.get("observations"), dict) else {}
        runs = [
            int(item.get("run")) for item in observations.values()
            if isinstance(item, dict) and isinstance(item.get("run"), int)
        ]
        complete = result.get("status") == "complete" and set(observations) == {"wheat", "corn", "pumpkin"}
        supported = complete and bool(result.get("all_timers_supported"))
        mechanically_active = complete and all(
            int(item.get("yield") or 0) > 0 for item in observations.values()
        )
        delayed = mechanically_active and not supported
        return {
            "settled": complete,
            "status": "supported" if mechanically_active else "falsified" if complete else "inconclusive",
            "evidence_cutoff_run": max(runs) if runs else result.get("baseline_run"),
            "question_classes": ["knob_age", "model_drift", "strategy_stale"],
            "subjects": ["mechanic.crop_timers_active", "mechanic.crop_timers_delayed"],
            "subject_adjudications": {
                "mechanic.crop_timers_active": {
                    "settled": complete,
                    "status": "supported" if supported else "falsified" if complete else "inconclusive",
                    "evidence_cutoff_run": max(runs) if runs else result.get("baseline_run"),
                    "answer": "The bounded three-crop cohort %s the declared timer contract."
                              % ("supports" if supported else "falsifies" if complete else "is still observing"),
                },
                "mechanic.crop_timers_delayed": {
                    "settled": complete,
                    "status": "supported" if delayed else "falsified" if complete else "inconclusive",
                    "evidence_cutoff_run": max(runs) if runs else result.get("baseline_run"),
                    "answer": "The bounded three-crop cohort %s the delayed-but-active mechanic claim."
                              % ("supports" if delayed else "falsifies" if complete else "is still observing"),
                },
            },
            "answer": (
                "The bounded three-crop cohort %s the declared timer contract."
                % ("supports" if supported else "falsifies" if complete else "is still observing")
            ),
            "residual_uncertainty": "crop contribution to league score is adjudicated separately",
            "retry_runs": 2,
        }

    if probe_id == "crop_score_analysis":
        value = load("crop_score_probe.json")
        from experiments import crop_score_probe
        expected = recompute_stateful_analysis(
            crop_score_probe,
            "crop_score_probe.json",
            "crop_score_holdout",
            {"list_farm": 2, "plant": 1},
        )
        require_equal(value, expected, {"evaluated_ts", "completed_ts"})
        result = value.get("result") if isinstance(value.get("result"), dict) else value
        harvest = result.get("harvest") if isinstance(result.get("harvest"), dict) else {}
        complete = result.get("status") == "complete" and isinstance(harvest.get("run"), int)
        crop_adds_score = complete and bool(result.get("supported"))
        return {
            "settled": complete,
            "status": "falsified" if crop_adds_score else "supported" if complete else "inconclusive",
            "evidence_cutoff_run": int(harvest["run"]) if complete else result.get("baseline_run"),
            "question_classes": ["knob_age", "model_drift", "strategy_stale", "idle_capital"],
            "subjects": ["strategy.food_crop_score"],
            "answer": (
                "The bounded wheat holdout %s the existing zero-score-residual claim."
                % ("falsifies" if crop_adds_score else "supports" if complete else "is still observing")
            ),
            "residual_uncertainty": "coin profitability is separate from the league-score objective",
            "retry_runs": 2,
        }

    if probe_id == "endgame_replay":
        value = load("endgame_replay.json")
        from experiments import endgame
        history = analysis.read_ndjson(live_state / "history.ndjson", limit=40)
        latest = next((row for row in reversed(history) if row.get("coins")), None)
        if latest is None:
            raise sandbox.ResultValidationError("endgame replay has no authoritative current row")
        expected = endgame.analyze(latest)
        require_equal(value, expected)
        safe_path = bool(value.get("safe_path"))
        cutoff = value.get("run") if isinstance(value.get("run"), int) else None
        return {
            "settled": safe_path,
            "status": "supported" if safe_path else "inconclusive",
            "evidence_cutoff_run": cutoff,
            "question_classes": ["rank_lost", "no_path_to_win", "win_eta"],
            "subjects": [],
            "answer": (
                "Bounded endgame replay found a safe objective path."
                if safe_path else "No safe path was found inside the declared 24-hour target grid."
            ),
            "residual_uncertainty": "a changed rival regime or policy can reopen the projection",
            "retry_runs": 2 if not safe_path else rules.PROBE_MIN_INTERVAL_RUNS,
        }

    if probe_id == "dual_cap_audit":
        value = load("dual_cap_audit.json")
        from experiments import dual_cap_audit
        from . import parse
        try:
            farm_state = parse.parse_farm(
                (live_state / "raw" / "latest" / "list_farm_final.txt").read_text(encoding="utf-8")
            )
        except (OSError, parse.ParseDrift):
            farm_state = None
        expected = dual_cap_audit.analyze(
            analysis.read_ndjson(live_state / "history.ndjson", limit=2_000),
            farm=farm_state,
        )
        require_equal(value, expected, {"evaluated_ts"})
        animal = value.get("animal_regime") if isinstance(value.get("animal_regime"), dict) else {}
        runs = [int(item) for item in (animal.get("runs") or []) if isinstance(item, int)]
        measured = (
            len(runs) >= 5
            and int(animal.get("windows") or 0) == len(runs)
            and isinstance(animal.get("supported"), bool)
        )
        supported = bool(animal.get("supported"))
        ratio = animal.get("median_beehive_vs_chicken")
        return {
            "settled": measured,
            "status": "supported" if supported else "falsified" if measured else "inconclusive",
            "evidence_cutoff_run": max(runs) if runs else None,
            "question_classes": ["knob_age", "model_drift", "strategy_stale", "idle_capital", "policy_drift"],
            "subjects": [
                "strategy.capped_slot_efficiency", "strategy.chicken_engine", "semantic_contract",
            ],
            "answer": (
                "Fresh capped mixed-species replay %s the promoted denominator on %d windows (median ratio %s)."
                % ("supports" if supported else "falsifies", len(runs), ratio)
            ),
            "residual_uncertainty": "crop scoring and timer claims require their own bounded interventions",
        }

    return {}


def _finish_questions(
    question_ids: List[str],
    probe_id: str,
    run: Optional[int],
    result: Dict[str, Any],
) -> None:
    status = str(result.get("status") or "failed")
    hypothesis_id = str(result.get("hypothesis_id") or "")
    hypothesis_result = provenance.latest_result(hypothesis_id) if hypothesis_id else None
    evidence_ref = "probe:%s:%s" % (probe_id, result.get("started_ts") or result.get("ts"))
    refs = [evidence_ref]
    for item in result.get("admitted_outputs") or []:
        if not item.get("path"):
            continue
        ref = "state/%s" % item["path"]
        if item.get("sha256"):
            ref += "#sha256=%s" % item["sha256"]
        refs.append(ref)
    for ref in (hypothesis_result or {}).get("validation_evidence") or []:
        refs.append(str(ref))
    refs = sorted(set(refs))

    supported = {"supported", "accepted", "passed", "falsified", "rejected"}
    adjudication = result.get("adjudication") if isinstance(result.get("adjudication"), dict) else {}
    bindings = result.get("question_bindings") if isinstance(result.get("question_bindings"), dict) else {}
    coverage = adjudication.get("coverage") if isinstance(adjudication.get("coverage"), dict) else {}
    question_map = {row.get("id"): row for row in questions.load_all()}
    policy_reconciliation: Optional[Dict[str, Any]] = None
    for question_id in question_ids:
        question = question_map.get(question_id) or {}
        binding = bindings.get(question_id) if isinstance(bindings.get(question_id), dict) else {}
        expected_generation = binding.get("generation")
        scoped_adjudication = (
            coverage.get(str(question.get("class") or ""), {})
            if coverage else adjudication
        )
        if not isinstance(scoped_adjudication, dict):
            scoped_adjudication = {}
        subject_adjudications = (
            adjudication.get("subject_adjudications")
            if isinstance(adjudication.get("subject_adjudications"), dict) else {}
        )
        subject_specific = subject_adjudications.get(str(question.get("subject") or ""))
        if isinstance(subject_specific, dict):
            scoped_adjudication = dict(scoped_adjudication, **subject_specific)
        result_status = str(
            ((hypothesis_result or {}).get("status")
             or scoped_adjudication.get("status") or status)
            if status == "passed" else status
        )
        settled = bool(
            status == "passed"
            and result_status in supported
            and (
                bool(hypothesis_id)
                or bool(scoped_adjudication.get("settled"))
            )
        )
        base_cutoff = (
            scoped_adjudication.get("evidence_cutoff_run")
            if isinstance(scoped_adjudication.get("evidence_cutoff_run"), int)
            else run if hypothesis_id else None
        )
        allowed_classes = set(adjudication.get("question_classes") or [])
        allowed_subjects = set(
            scoped_adjudication.get("subjects") or adjudication.get("subjects") or []
        )
        if adjudication and (
            (allowed_classes and question.get("class") not in allowed_classes)
            or (allowed_subjects and question.get("subject") not in allowed_subjects)
            or (coverage and not scoped_adjudication)
        ):
            settled = False
            result_status = "scope_mismatch"
        latest_obligation = max(
            int(question.get("generation_opened_run") or 0),
            int(question.get("last_seen_run") or 0),
        )
        if settled and (not isinstance(base_cutoff, int) or base_cutoff < latest_obligation):
            settled = False
            result_status = "stale_evidence"

        # A policy audit exiting zero means only that it completed. It may not
        # close a policy-drift question until rebuilding the claims restores the
        # exact promoted policy fingerprint.
        if question.get("class") == "policy_drift" and status == "passed":
            if policy_reconciliation is None:
                from . import claims, policy
                registry = claims.refresh()
                policy_reconciliation = policy.runtime_context(registry)
            compatible = bool(policy_reconciliation.get("compatible"))
            settled = compatible and settled
            result_status = (
                "compatible" if settled else "evidence_missing" if compatible else "incompatible"
            )
        if settled:
            answer = str(scoped_adjudication.get("answer") or adjudication.get("answer") or "").strip() or (
                "Probe %s adjudicated the active falsifier as %s."
                % (probe_id, result_status)
            )
            questions.set_status(
                question_id,
                "answered",
                answer=answer,
                evidence_refs=refs,
                run=run,
                probe_id=probe_id,
                result_status=result_status,
                expected_generation=expected_generation,
                expected_status="probing",
                expected_probe_id=probe_id,
                evidence_cutoff_run=base_cutoff,
                resolution_kind="falsified" if result_status in {"falsified", "rejected"} else "supported",
                residual_uncertainty=str(
                    scoped_adjudication.get("residual_uncertainty")
                    or adjudication.get("residual_uncertainty") or ""
                ),
            )
        else:
            reason = result.get("reason") or result_status
            if policy_reconciliation and not policy_reconciliation.get("compatible"):
                reason = "; ".join(policy_reconciliation.get("errors") or []) or reason
            answer = "Probe %s did not settle the active question generation: %s." % (probe_id, reason)
            questions.set_status(
                question_id,
                "open",
                answer=answer,
                evidence_refs=refs,
                run=run,
                probe_id=probe_id,
                result_status=result_status,
                expected_generation=expected_generation,
                expected_status="probing",
                expected_probe_id=probe_id,
            )


def run_probe(
    probe_id: str,
    explicit: bool = False,
    run: Optional[int] = None,
    question_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    registry = _registry()
    if probe_id not in registry:
        raise ValueError("unknown probe: %s" % probe_id)
    spec = registry[probe_id]
    validated = probe_guard.validate_spec(probe_id, spec)
    if not explicit and (not validated["read_only"] or not validated["autonomous"]):
        raise ValueError("probe %s requires explicit invocation" % probe_id)
    declared_outputs = _outputs(spec)
    command = _command(spec)
    bound_questions = sorted(set(str(value) for value in (question_ids or []) if value))
    _ensure_registration(spec, bound_questions, probe_id)

    live_state = _state_dir()
    lock_path = live_state / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                result = {
                    "schema_version": SCHEMA_VERSION,
                    "ts": _utcnow(),
                    "run": run,
                    "probe_id": probe_id,
                    "executor_identity": _executor_identity(),
                    "status": "skipped",
                    "reason": "farm cycle holds the mutation lock",
                    "budget": validated["budget"],
                }
                _append(result)
                return result
            raise

        started = _utcnow()
        grant = probe_guard.new_grant(probe_id, spec)
        execution_id = str(grant["execution_id"])
        hypothesis_id = str(spec.get("hypothesis_id") or "")
        budget = dict(validated["budget"])
        timeout = max(1, int(budget.get("wall_seconds") or 60))
        status = "failed"
        reason: Optional[str] = None
        returncode: Optional[int] = None
        output = ""
        admitted: List[Dict[str, Any]] = []
        adjudication: Dict[str, Any] = {}
        validation_result: Optional[Dict[str, Any]] = None
        question_bindings: Dict[str, Dict[str, Any]] = {}

        with sandbox.scratch_dir("farm-probe-%s-" % probe_id) as scratch_name:
            scratch = Path(scratch_name).resolve()
            writable = set(declared_outputs)
            fresh_names = set()
            if int(budget.get("calls") or 0) > 0:
                writable.add("tool_calls.ndjson")
                fresh_names.add("tool_calls.ndjson")
            if hypothesis_id:
                writable.add("provenance.ndjson")
            projection = sandbox.project_state(
                live_state, scratch, writable, fresh_names=fresh_names,
            )
            state_view = Path(projection["root"])

            request_read, request_write = os.pipe()
            response_read, response_write = os.pipe()
            client: Optional[mcp.Client] = None
            if budget.get("calls"):
                # Endpoint and TLS context exist only in this trusted process.
                client = mcp.Client()

            def transport(payload: Dict[str, Any], request_timeout: int, retries: int) -> Dict[str, Any]:
                if client is None:
                    raise probe_guard.AuthorizationError("zero-call probe attempted MCP transport")
                params = payload.get("params") or {}
                tool = str(params.get("name") or "")
                trace_id = "%s:%s" % (execution_id, payload.get("id"))
                trace_started = time.monotonic()
                base_trace = {
                    "id": trace_id,
                    "ts": _utcnow(),
                    "tool": tool,
                    "probe_id": execution_id,
                    "registered_probe_id": probe_id,
                    "actor": "probe_broker",
                    "authoritative": True,
                }
                compaction.append_json(
                    live_state / "tool_calls.ndjson",
                    dict(base_trace, event="start", arguments=mcp._safe_arguments(params.get("arguments") or {})),
                    strict=False,
                )
                try:
                    response = client._post(payload, timeout=request_timeout, retries=retries)
                except Exception as exc:
                    compaction.append_json(
                        live_state / "tool_calls.ndjson",
                        dict(base_trace, event="end", ts=_utcnow(), ok=False,
                             duration_ms=round((time.monotonic() - trace_started) * 1000, 1),
                             error=str(exc)[:240]),
                        strict=False,
                    )
                    raise
                compaction.append_json(
                    live_state / "tool_calls.ndjson",
                    dict(base_trace, event="end", ts=_utcnow(), ok=True,
                         duration_ms=round((time.monotonic() - trace_started) * 1000, 1)),
                    strict=False,
                )
                return response

            broker = threading.Thread(
                target=probe_guard.serve,
                args=(request_read, response_write, grant, transport),
                name="probe-broker-%s" % probe_id,
                daemon=True,
            )
            extra_env = {
                probe_guard.ENFORCEMENT_ENV: "1",
                probe_guard.REQUEST_FD_ENV: str(request_write),
                probe_guard.RESPONSE_FD_ENV: str(response_read),
                "FARM_PROBE_ID": execution_id,
                "FARM_TOOL_CALL_LOG": str(state_view / "tool_calls.ndjson"),
            }
            if hypothesis_id:
                extra_env["FARM_HYPOTHESIS_ID"] = hypothesis_id
                extra_env["FARM_EVIDENCE_CLASS"] = str(spec.get("evidence_class") or "holdout")
                extra_env["FARM_PROVENANCE_LOG"] = str(state_view / "provenance.ndjson")
            env = sandbox.environment(scratch, state_view, extra_env)
            process: Optional[subprocess.Popen] = None
            stdout_path, stderr_path = scratch / "worker.stdout", scratch / "worker.stderr"
            stdout_handle = stderr_handle = None
            try:
                wrapped = sandbox.wrap(
                    command,
                    PROJECT,
                    state_view,
                    scratch,
                    read_roots=tuple(projection.get("read_roots") or ()),
                    allow_processes=False,
                )
                requested_binding = bool(bound_questions)
                current_questions = {row.get("id"): row for row in questions.load_all()}
                active_bound: List[str] = []
                for question_id in bound_questions:
                    question = current_questions.get(question_id) or {}
                    generation = int(question.get("generation") or 1)
                    transitioned = questions.set_status(
                        question_id,
                        "probing",
                        run=run,
                        probe_id=probe_id,
                        expected_generation=generation,
                        expected_status="open",
                    )
                    if transitioned is None:
                        continue
                    active_bound.append(question_id)
                    question_bindings[question_id] = {
                        "generation": generation,
                        "generation_opened_run": question.get("generation_opened_run"),
                        "last_seen_run": question.get("last_seen_run"),
                        "class": question.get("class"),
                        "subject": question.get("subject"),
                    }
                bound_questions = active_bound
                if requested_binding and not bound_questions:
                    raise QuestionBindingUnavailable(
                        "selected question is no longer open for this probe"
                    )
                # Regular scratch files let us wait on the direct worker rather
                # than waiting for pipe EOF from descendants it may have forked.
                stdout_handle = stdout_path.open("w", encoding="utf-8")
                stderr_handle = stderr_path.open("w", encoding="utf-8")
                process = subprocess.Popen(
                    wrapped,
                    cwd=str(scratch),
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    pass_fds=(request_write, response_read) + tuple(projection.get("read_fds") or ()),
                    start_new_session=True,
                )
                os.close(request_write)
                os.close(response_read)
                request_write = response_read = -1
                broker.start()
                try:
                    returncode = process.wait(timeout=timeout)
                    status = "passed" if returncode == 0 else "failed"
                except subprocess.TimeoutExpired:
                    probe_guard.close(grant)
                    _terminate_group(process)
                    status = "timeout"
                    reason = "wall-time budget exceeded"
            except QuestionBindingUnavailable as exc:
                status = "skipped"
                reason = str(exc)
            except sandbox.SandboxUnavailable as exc:
                status = "sandbox_unavailable"
                reason = str(exc)
            except OSError as exc:
                status = "failed"
                reason = "sandbox launch failed: %s" % type(exc).__name__
            finally:
                probe_guard.close(grant)
                for handle in (stdout_handle, stderr_handle):
                    if handle is not None:
                        try:
                            handle.close()
                        except OSError:
                            pass
                for fd in (request_write, response_read):
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                if broker.ident is None:
                    for fd in (request_read, response_write):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                else:
                    if process is not None:
                        # A forked descendant can retain the request pipe after the
                        # direct worker exits. Kill the whole group before waiting
                        # for EOF so it cannot hold the global farm lock forever.
                        _terminate_group(process)
                    if broker.is_alive():
                        # Authorized parent transport has its own hard timeout.
                        broker.join(timeout=probe_guard.MAX_BROKER_CALL_SECONDS + 5)
                    if broker.is_alive():
                        status = "broker_timeout"
                        reason = "parent broker exceeded its hard shutdown bound"
                output = (_tail_file(stdout_path) + _tail_file(stderr_path))[-8_000:]
                sandbox.close_projection(projection)

            authoritative_usage = probe_guard.usage(grant)
            if authoritative_usage.get("denials"):
                status = "capability_violation"
                reason = "probe requested authority outside its protected grant"
            if status == "passed":
                try:
                    validation_result = _validated_provenance_result(
                        projection, hypothesis_id, spec, declared_outputs,
                    ) if hypothesis_id else None
                    if hypothesis_id and validation_result is None:
                        admitted = []
                    else:
                        def validate_prepared(prepared: List[Dict[str, Any]]) -> None:
                            nonlocal adjudication
                            if not hypothesis_id:
                                adjudication = _trusted_adjudication(
                                    probe_id, live_state, prepared
                                )

                        admitted = sandbox.admit_outputs(
                            live_state,
                            projection,
                            [name for name in declared_outputs if name != "provenance.ndjson"],
                            validator=validate_prepared,
                        )
                    # Worker-proposed hypothesis outcomes remain measurements.
                    # Only a protected hypothesis-specific adjudicator may call
                    # provenance.record_result and create promotion authority.
                except (OSError, TypeError, ValueError, sandbox.ResultValidationError) as exc:
                    status = "result_rejected"
                    reason = "probe result admission failed: %s" % str(exc)[:300]

        if (
            status == "passed"
            and adjudication.get("settled")
            and probe_id in {"dual_cap_audit", "crop_timer_analysis", "crop_score_analysis"}
        ):
            try:
                from . import claims
                claims.refresh()
            except Exception as exc:  # noqa: BLE001 - claim admission is part of completion
                status = "result_rejected"
                reason = "admitted probe could not refresh its claim registry: %s" % str(exc)[:240]
        if status == "passed" and hypothesis_id:
            if validation_result is None:
                status = "evidence_missing"
                reason = "hypothesis-linked probe did not record a validation measurement"
            else:
                status = "awaiting_adjudication"
                reason = "worker measurement is hashed but has no protected causal adjudicator"
        elif status == "passed" and bound_questions:
            if not adjudication:
                status = "evidence_missing"
                reason = "probe has no trusted adjudicator or admitted deciding evidence"
            elif not adjudication.get("settled"):
                status = "inconclusive"
                reason = str(adjudication.get("answer") or "probe did not satisfy its falsifier")[:300]
            else:
                coverage = adjudication.get("coverage") if isinstance(adjudication.get("coverage"), dict) else {}
                current_coverage = 0
                for binding in question_bindings.values():
                    scoped = coverage.get(str(binding.get("class") or "")) if coverage else adjudication
                    if isinstance(scoped, dict):
                        subject_map = (
                            adjudication.get("subject_adjudications")
                            if isinstance(adjudication.get("subject_adjudications"), dict) else {}
                        )
                        subject_specific = subject_map.get(str(binding.get("subject") or ""))
                        if isinstance(subject_specific, dict):
                            scoped = dict(scoped, **subject_specific)
                    if not isinstance(scoped, dict) or not scoped.get("settled"):
                        continue
                    allowed_classes = set(adjudication.get("question_classes") or [])
                    allowed_subjects = set(
                        scoped.get("subjects") or adjudication.get("subjects") or []
                    )
                    if (
                        (allowed_classes and binding.get("class") not in allowed_classes)
                        or (allowed_subjects and binding.get("subject") not in allowed_subjects)
                    ):
                        continue
                    cutoff = scoped.get("evidence_cutoff_run")
                    obligation = max(
                        int(binding.get("generation_opened_run") or 0),
                        int(binding.get("last_seen_run") or 0),
                    )
                    if isinstance(cutoff, int) and cutoff >= obligation:
                        current_coverage += 1
                if current_coverage == 0:
                    status = "inconclusive"
                    reason = "admitted evidence does not cover any active question generation"
        authoritative_usage = probe_guard.usage(grant)
        result = {
            "schema_version": SCHEMA_VERSION,
            "ts": _utcnow(),
            "started_ts": started,
            "run": run,
            "probe_id": probe_id,
            "execution_id": execution_id,
            "executor_identity": _executor_identity(),
            "question_ids": bound_questions,
            "question_bindings": question_bindings,
            "status": status,
            "returncode": returncode,
            "reason": reason,
            "read_only": bool(validated["read_only"]),
            "explicit": bool(explicit),
            "hypothesis": spec.get("hypothesis"),
            "hypothesis_id": hypothesis_id or None,
            "evidence_class": spec.get("evidence_class"),
            "budget": budget,
            "calls": int(authoritative_usage.get("calls") or 0),
            "coins_reserved": int(authoritative_usage.get("coins") or 0),
            "transport_attempts": int(authoritative_usage.get("transport_attempts") or 0),
            "capability_denials": int(authoritative_usage.get("denials") or 0),
            "tool_usage": authoritative_usage.get("by_tool") or {},
            "admitted_outputs": admitted,
            "adjudication": adjudication,
            "candidate_adjudication": validation_result,
            "stop_condition": spec.get("stop_condition"),
            "evidence_destination": spec.get("evidence_destination"),
            "output": output,
        }
        _append(result)
        _finish_questions(bound_questions, probe_id, run, result)
        return result
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_handle.close()


def _recent_events() -> List[Dict[str, Any]]:
    return analysis.read_ndjson(_ledger())


_PROBE_FAILURES = {
    "failed", "timeout", "sandbox_unavailable", "broker_timeout",
    "capability_violation", "result_rejected", "evidence_missing",
}
_PROBE_PRIORITY = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_ANALYSIS_BASELINES = {
    "crop_timer_analysis": "dual_cap_probe.json",
    "crop_score_analysis": "crop_score_probe.json",
}


def _analysis_ready(probe_id: str, matching: List[Dict[str, Any]]) -> bool:
    filename = _ANALYSIS_BASELINES.get(probe_id)
    if not filename:
        return True
    try:
        value = json.loads((_state_dir() / filename).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    baseline = value.get("baseline_run") if isinstance(value, dict) else None
    obligation = max(
        (int(question.get("generation_opened_run"))
         for question in matching if isinstance(question.get("generation_opened_run"), int)),
        default=None,
    )
    return isinstance(baseline, int) and isinstance(obligation, int) and baseline >= obligation


def _probe_backoff(
    probe_id: str,
    run: int,
    events: List[Dict[str, Any]],
    executor_identity: str,
) -> Optional[int]:
    """Return remaining blocked runs, or None when this probe may run.

    Infrastructure failures back off exponentially under the protected
    scheduler. Inconclusive measurements wait one normal probe cadence. A new
    executor identity gets one immediate retry after a code repair.
    """
    attempts = [
        event for event in events
        if event.get("probe_id") == probe_id and event.get("status") != "skipped"
        and isinstance(event.get("run"), int)
        and event.get("executor_identity") == executor_identity
    ]
    if not attempts:
        return None
    latest = attempts[-1]
    age = run - int(latest["run"])
    if age <= 0:
        return max(1, 1 - age)
    if latest.get("status") == "inconclusive":
        retry_runs = int((latest.get("adjudication") or {}).get("retry_runs")
                         or rules.PROBE_MIN_INTERVAL_RUNS)
        return max(0, max(1, retry_runs) - age) or None
    failures = 0
    for event in reversed(attempts):
        if event.get("status") not in _PROBE_FAILURES:
            break
        failures += 1
    if failures:
        delay = min(rules.QUESTION_MAX_AGE_RUNS, 2 ** min(failures - 1, 8))
        return max(0, delay - age) or None
    return None


def maybe_run(open_questions: List[Dict[str, Any]], run: Optional[int]) -> Optional[Dict[str, Any]]:
    """Run one fair, failure-bounded autonomous read-only probe.

    A broken local replay cannot monopolize the lexical head of the registry or
    consume the scarce remote-call cadence. Selection is least-recently-attempted
    first, then highest question priority and oldest active generation.
    """
    if not isinstance(run, int) or not open_questions:
        return None
    events = _recent_events()
    executor_identity = _executor_identity()
    remote_runs = [
        int(event["run"])
        for event in events
        if event.get("status") != "skipped"
        and isinstance(event.get("run"), int)
        and int((event.get("budget") or {}).get("calls") or 0) > 0
    ]
    remote_throttled = bool(
        remote_runs and run - max(remote_runs) < rules.PROBE_MIN_INTERVAL_RUNS
    )
    candidates: List[Dict[str, Any]] = []
    for probe_id, spec in sorted(_registry().items()):
        if not spec.get("read_only") or not spec.get("autonomous"):
            continue
        call_budget = int((spec.get("budget") or {}).get("calls") or 0)
        if call_budget > 0 and remote_throttled:
            continue
        if _probe_backoff(probe_id, run, events, executor_identity) is not None:
            continue
        allowed_classes = set(spec.get("question_classes") or [])
        subject_patterns = [str(value).lower() for value in spec.get("subject_patterns") or []]
        matching = []
        for question in open_questions:
            if question.get("status") != "open":
                continue
            if question.get("class") not in allowed_classes:
                continue
            subject = "%s %s" % (question.get("subject") or "", question.get("key") or "")
            if subject_patterns and not any(pattern in subject.lower() for pattern in subject_patterns):
                continue
            matching.append(question)
        if not matching or not _analysis_ready(probe_id, matching):
            continue
        prior = [
            event for event in events
            if event.get("probe_id") == probe_id and event.get("status") != "skipped"
            and isinstance(event.get("run"), int)
        ]
        last_attempt = max((int(event["run"]) for event in prior), default=-1)
        priority = max(
            (_PROBE_PRIORITY.get(str(question.get("priority") or "medium"), 1)
             for question in matching),
            default=1,
        )
        oldest = min(
            (int(question.get("generation_opened_run"))
             for question in matching if isinstance(question.get("generation_opened_run"), int)),
            default=run,
        )
        candidates.append({
            "probe_id": probe_id,
            "matching": matching,
            "order": (last_attempt, -priority, oldest, probe_id),
        })
    if not candidates:
        return None
    selected = min(candidates, key=lambda item: item["order"])
    return run_probe(
        str(selected["probe_id"]),
        explicit=False,
        run=run,
        question_ids=[
            str(question.get("id")) for question in selected["matching"] if question.get("id")
        ],
    )
