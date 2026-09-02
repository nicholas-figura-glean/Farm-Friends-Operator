#!/usr/bin/env python3
"""Research agent: find out whether the strategy should change, and prove it.

Scheduled hourly. It never mutates the farm and never edits code. Its output is
evidence and, when the evidence is strong enough, a work order that goes through
the same author -> gates -> canary path as a bug fix.

Why strategy changes are routed through work orders
--------------------------------------------------
POSTMORTEM-run291 and run377 are both stories about plausible strategy changes
that lost ground: three throttles aimed at the wrong variable, a growth gate that
starved the herd. Neither was a coding error, so no test suite would have caught
them. What was missing was a measured before/after and a way back.

So this agent is deliberately not allowed to change a constant directly. It files
an order, the author implements it, the gate matrix proves it is correct, and the
canary proves it did not slow production down -- reverting automatically if it did.
That gives strategy changes the one property the postmortems say they lacked:
they are reversible without a human noticing first.

Four sources of hypotheses, cheapest first
-----------------------------------------
1. **Unused capability.** Tools the server exposes that our code has never called.
   This is free to detect (the contract baseline already knows) and is the most
   concrete kind of unexplored strategy space.
2. **Parameter sensitivity.** `research.counterfactual_sweep` replays history
   against alternative constants and reports which ones would have changed
   decisions. Zero MCP calls.
3. **Outcome correlation.** For sensitive parameters, compare realised produce
   rate across the runs the alternative would have altered. This is correlational,
   not causal, and is reported as such -- it prioritises probes, it does not
   justify a change on its own.
4. **Model hypotheses.** Bounded to once a day, and only when the cheaper sources
   are exhausted. The model sees the journal, claims and the leaderboard gap, and
   must return falsifiable proposals with a bounded probe design.

Exit codes: 0 pass completed (including queued proposals), 4 the agent broke.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import (  # noqa: E402
    analysis, canary, claims, contract, journal, ledger, llm, mechanics, policy, provenance,
    questions, research, rules, strategy, workorders,
)

STATE = PROJECT / "state"
STORE = STATE / "research_agent.json"
LOCK = STATE / ".research.lock"
FINDINGS = STATE / "research_findings.ndjson"

# A capability probe is worth proposing once. Re-proposing it every hour would
# flood the queue, so proposals are remembered by a stable key.
MAX_PROPOSALS_PER_PASS = 2
MODEL_HYPOTHESIS_INTERVAL_HOURS = 24

# Tools that are never worth probing even though they are unused: purely cosmetic
# capabilities cannot move lifetime produce, which is the only score.
IGNORED_CAPABILITIES = ("name_animal",)


def proposal_capacity(order_rows: List[Dict[str, Any]]) -> int:
    """Remaining research-order WIP slots; per-pass limits are only a burst cap."""
    active = [
        order for order in order_rows
        if order.get("source") == "research_agent"
        and order.get("status") in {"open", "claimed"}
    ]
    return min(MAX_PROPOSALS_PER_PASS, max(0, rules.MAX_RESEARCH_ORDER_WIP - len(active)))


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def record_finding(row: Dict[str, Any]) -> Dict[str, Any]:
    FINDINGS.parent.mkdir(parents=True, exist_ok=True)
    with FINDINGS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row, ts=utcnow()), sort_keys=True, default=str) + "\n")
    return row


# -- 1. unused capability ----------------------------------------------------


def unused_capabilities() -> List[Dict[str, Any]]:
    """Tools the server offers that our code has never once called.

    The contract baseline already holds both halves of this comparison, so this
    costs nothing. An unused tool is the clearest form of unexplored strategy: the
    server is offering a move we have never evaluated.
    """
    baseline = contract.load_baseline(str(PROJECT / contract.BASELINE))
    if not baseline:
        return []
    tools = baseline.get("tools") or {}
    rely = baseline.get("reliance") or {}
    out = []
    already_active = mechanics.active_tools()
    for name in sorted(set(tools) - set(rely)):
        if name in IGNORED_CAPABILITIES or name in already_active:
            continue
        spec = tools[name] or {}
        out.append({
            "capability": name,
            "description": str(spec.get("description") or "")[:300],
            "description_sha": str(spec.get("description_sha") or ""),
            "required": spec.get("required") or [],
            "args": sorted((spec.get("args") or {}).keys()),
        })
    return out


def _probe_path(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:48] or "generated"
    return "experiments/%s_probe.py" % slug


def capability_proposal(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Route direct mechanics to implementation; uncertain tools to a probe."""
    name = entry["capability"]
    classification = mechanics.classify_capability(
        name, entry.get("description", ""), entry.get("required") or []
    )
    detail = dict(entry, capability_classification=classification)
    if classification.get("direct"):
        change = {
            "id": "capability-policy-%s" % name,
            "severity": "degraded",
            "kind": "capability_policy",
            "tool": name,
            "summary": "%s is a directly documented %s mechanic: %s"
                       % (name, classification.get("kind"), entry["description"][:140]),
            "we_use_it": False,
            "sites": [],
            "detail": detail,
        }
        intent = (
            "Implement `%s` in the literal capability-policy surface at %s. The server "
            "contract directly states its trigger, objective effect, and bounded cost; "
            "this is direct-mechanism evidence, not a request for a speculative lifetime-"
            "produce probe. Add one enabled entry with the captured description hash, "
            "hard call/cost bounds, and required post-action verification. The protected "
            "executor calls it; do not edit farm/cycle.py or farm/mechanics.py."
            % (name, mechanics.POLICY_RELATIVE)
        )
        acceptance = [
            "%s has an enabled literal capability policy" % name,
            "the policy pins the captured contract fingerprint and direct evidence",
            "the protected validator accepts every call, cost, and outcome bound",
            "farm/cycle.py and farm/mechanics.py are unchanged by this order",
        ]
        return {
            "change": change,
            "intent": intent,
            "acceptance": acceptance,
            "files": [mechanics.POLICY_RELATIVE],
            "evidence_spec": {
                "hypothesis": "Contract-bounded use of %s performs its documented %s objective."
                              % (name, classification.get("kind")),
                "null_hypothesis": "%s fails its documented objective or crosses its declared bound."
                                   % name,
                "falsifier": classification.get("falsifier"),
                "primary_metric": classification.get("primary_metric"),
                "expected_improvement": 0.01,
            },
            "change_class": "strategy",
        }

    change = {
        "id": "research-capability-%s" % name,
        "severity": "opportunity",
        "kind": "unused_capability",
        "tool": name,
        "summary": "%s is exposed by the server but never called: %s"
                   % (name, entry["description"][:120]),
        "we_use_it": False,
        "sites": [],
        "detail": detail,
    }
    intent = (
        "The server exposes `%s` (%s), but its objective effect is not directly "
        "established. Add a bounded non-autonomous probe candidate under experiments/ "
        "with a literal PROPOSED_SPEC. Do not edit the protected registry or claim that "
        "defining the probe implements the mechanic; independent review must register it."
        % (name, entry["description"][:160])
    )
    acceptance = [
        "a non-autonomous probe candidate for %s exists under experiments/" % name,
        "the script carries a literal requested capability, argument, output, and budget spec",
        "the protected registry and farm/cycle.py are unchanged by this order",
        "registration and execution remain blocked pending independent review",
    ]
    return {
        "change": change,
        "intent": intent,
        "acceptance": acceptance,
        "files": [_probe_path(name)],
        "evidence_spec": {
            "hypothesis": "A bounded use of %s improves the league-first objective." % name,
            "null_hypothesis": "%s produces no measurable objective benefit." % name,
            "falsifier": classification.get("falsifier"),
            "primary_metric": classification.get("primary_metric"),
            "expected_improvement": 0.01,
        },
        "change_class": "research_probe",
    }


def dual_cap_strategy_proposals() -> List[Tuple[Dict[str, Any], str]]:
    """Bridge regime audit results into the literal strategy implementation."""
    try:
        audit = json.loads((STATE / "dual_cap_audit.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return []
    current = strategy.load()
    effective = current.get("effective") or {}
    animal = effective.get("animal") or {}
    plots = effective.get("plots") or {}
    proposals: List[Tuple[Dict[str, Any], str]] = []
    cap = audit.get("animal_regime") or {}
    recommended = (audit.get("decision") or {}).get("capped_replacement_kind")
    if cap.get("supported") and recommended and animal.get("capped_replacement_kind") != recommended:
        key = "dual-cap-animal:%s:%s" % (recommended, (audit.get("cohort") or {}).get("sha256"))
        proposals.append(({
            "change": {
                "id": "research-strategy-dual-cap-animal",
                "severity": "degraded",
                "kind": "strategy_policy",
                "summary": "Capped slot evidence recommends %s replacements while live strategy uses %s"
                           % (recommended, animal.get("capped_replacement_kind")),
                "detail": {"audit": audit, "strategy_errors": current.get("errors")},
            },
            "intent": (
                "Update only %s so below-cap growth retains its capital-efficient kind "
                "and near-cap natural-loss replacement uses `%s` with the measured flower "
                "and capacity preconditions. Pin the supported result lineage."
                % (strategy.POLICY_RELATIVE, recommended)
            ),
            "acceptance": [
                "literal strategy policy matches the supported dual-cap cohort",
                "below-cap growth and near-cap replacement remain separate decisions",
                "farm/strategy.py, farm/rules.py, and farm/cycle.py are unchanged by this order",
                "strategy and full release gates pass",
            ],
            "files": [strategy.POLICY_RELATIVE],
            "evidence_spec": {
                "hypothesis": "The capped replacement kind improves output per scarce animal slot.",
                "null_hypothesis": "The replacement kind does not clear the per-slot gate.",
                "falsifier": audit.get("falsifier"),
                "primary_metric": cap.get("scarce_slot_metric"),
                "expected_improvement": 0.10,
            },
            "change_class": "strategy",
        }, key))
    crop = audit.get("plot_regime") or {}
    if not crop.get("crop_score_supported") and plots.get("food_crop_kind") is not None:
        key = "dual-cap-plots:disable:%s" % crop.get("crop_score_residual")
        proposals.append(({
            "change": {
                "id": "research-strategy-dual-cap-plots",
                "severity": "degraded",
                "kind": "strategy_policy",
                "summary": "Scaled crop holdout has no league-score residual; active crop ramp must stop",
                "detail": {"audit": audit},
            },
            "intent": "Disable food-crop ramp fields in %s while retaining the eight-flower beehive floor."
                      % strategy.POLICY_RELATIVE,
            "acceptance": [
                "food_crop_kind is null and target fraction is zero",
                "falsified crop-score result lineage is retained",
                "flower bonus maintenance remains enabled",
            ],
            "files": [strategy.POLICY_RELATIVE],
            "change_class": "strategy",
        }, key))
    return proposals


# -- 2 & 3. parameter sensitivity and outcome correlation --------------------


def sensitive_parameters() -> List[Dict[str, Any]]:
    """Constants whose alternatives would have changed real decisions."""
    try:
        sweep = research.counterfactual_sweep()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for dimension in sweep.get("dimensions") or []:
        name = dimension.get("parameter")
        live = dimension.get("live")
        for alternative in dimension.get("alternatives") or []:
            if alternative.get("value") == live:
                continue
            changed = int(alternative.get("changed_runs") or 0)
            if changed <= 0:
                continue
            out.append({
                "parameter": name,
                "live": live,
                "alternative": alternative.get("value"),
                "changed_runs": changed,
                "ranges": alternative.get("ranges") or [],
            })
    out.sort(key=lambda item: -item["changed_runs"])
    return out


def unbacked_parameters(sensitive: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sensitive constants with no claim justifying their current value.

    A parameter that measurably steers decisions but rests on no recorded evidence
    is a strategy assumption nobody has checked. That is a question, not yet a
    change: it gets opened for the durable question registry rather than turned
    into a code edit.
    """
    try:
        registry = claims.load()
        backing = json.dumps(registry, default=str)
    except Exception:  # noqa: BLE001
        return []
    out = []
    seen = set()
    for item in sensitive:
        name = str(item.get("parameter") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        if name not in backing:
            out.append(item)
    return out


def rate_context() -> Dict[str, Any]:
    """Realised production trend, used to judge whether anything needs changing.

    If output per animal is flat or rising and we are comfortably ahead, the
    correct research posture is patience: a change with no measured upside is pure
    downside risk.
    """
    try:
        rows = analysis.history_rows()
    except Exception:  # noqa: BLE001
        return {}
    recent = rows[-40:]
    points: List[Tuple[float, float]] = []
    for row in recent:
        rate = row.get("produce_per_min")
        animals = row.get("animals")
        if isinstance(rate, (int, float)) and isinstance(animals, int) and animals > 0:
            points.append((float(animals), float(rate)))
    trend = analysis.linear_regression(points) if len(points) >= 3 else {}

    latest = recent[-1] if recent else {}
    rivals = latest.get("rival_gains") or {}
    best_rival = 0
    if isinstance(rivals, dict) and rivals:
        numeric = [v for v in rivals.values() if isinstance(v, (int, float))]
        best_rival = max(numeric) if numeric else 0
    return {
        "runs": len(recent),
        "run": latest.get("run"),
        "rank": latest.get("rank"),
        "produce_per_min": latest.get("produce_per_min"),
        "animals": latest.get("animals"),
        "our_gain": latest.get("our_produce_gain"),
        "best_rival_gain": best_rival,
        "rate_vs_herd_slope": trend.get("slope"),
        "lead_secure": bool(latest.get("rank") == 1 and (latest.get("our_produce_gain") or 0) > best_rival),
    }


# -- 4. model hypotheses -----------------------------------------------------

HYPOTHESIS_SYSTEM = """\
You are a research analyst for an automated farming game agent that must maximize
the server's lexicographic leaderboard: league level first, lifetime produce second.
You do not write production code. You propose falsifiable experiments.

Rules:

* Reply with a JSON array only. No prose, no markdown fences.
* Each element must be an object with exactly these keys:
    "question_id" one ID from the supplied open_questions list
    "title"      short imperative name
    "hypothesis" what you believe is true, stated so it can be wrong
    "falsifier"  the observation that would disprove it
    "probe"      a bounded experiment design, naming the tools it would call
    "metric"     the single number that decides it, e.g. produce per coin
    "risk"       what it could cost if the hypothesis is wrong
* Propose at most 3, ordered by expected value.
* Prefer experiments that are read-only or cheaply reversible.
* Do not propose anything that would slow the 300s cycle or add server load
  proportional to herd size.
* Do not propose changes to feeding cadence or the growth gate unless the evidence
  given to you specifically contradicts the current claim; both have prior
  incidents.
* Treat a directly documented mandatory progression rule differently from a speculative
  optimization: it belongs in the bounded capability-policy implementation path.
* If the evidence suggests no change is warranted, reply with an empty array [].
"""


def model_hypotheses(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ask the gateway for falsifiable strategy proposals."""
    user = json.dumps(context, indent=2, sort_keys=True, default=str)[:24_000]
    result = llm.complete(
        HYPOTHESIS_SYSTEM,
        "Here is the current evidence about the farm.\n\n" + user,
        max_output_tokens=16_000,
        run=canary.latest_run(),
        note="research hypotheses",
        actor="research",
        purpose="hypothesis_generation",
    )
    if result["truncated"]:
        return []
    text = (result.get("text") or "").strip()
    # Tolerate a fenced reply even though the prompt forbids it.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    required = ("question_id", "title", "hypothesis", "falsifier", "probe", "metric")
    allowed_questions = {
        str(item.get("id")) for item in context.get("open_questions") or [] if item.get("id")
    }
    out = []
    for item in parsed[:3]:
        if (isinstance(item, dict)
                and all(isinstance(item.get(k), str) and item.get(k) for k in required)
                and item.get("question_id") in allowed_questions):
            out.append({k: str(item.get(k) or "")[:800] for k in list(required) + ["risk"]})
    return out


def hypothesis_proposal(item: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a model hypothesis into a probe work order."""
    key = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")[:40]
    change = {
        "id": "research-hypothesis-%s" % key,
        "severity": "opportunity",
        "kind": "strategy_hypothesis",
        "tool": "",
        "summary": item["title"][:200],
        "we_use_it": False,
        "sites": [],
        "detail": item,
    }
    intent = (
        "Build a bounded non-autonomous probe candidate to test this hypothesis. Do not "
        "change live strategy or the protected registry.\n\nHypothesis: %s\n\nFalsifier: %s"
        "\n\nProbe design: %s\n\nDeciding metric: %s\n\nThe script must declare its "
        "requested tools, arguments, outputs, and budget in a literal PROPOSED_SPEC value. "
        "Independent review is required before that spec can be copied into the registry."
        % (item["hypothesis"], item["falsifier"], item["probe"], item["metric"])
    )
    acceptance = [
        "a non-autonomous probe candidate tests the stated hypothesis",
        "the probe records the deciding metric (%s)" % item["metric"][:80],
        "the protected registry and farm/cycle.py are unchanged by this order",
        "the script carries a literal requested capability and budget specification",
    ]
    return {
        "change": change,
        "intent": intent,
        "acceptance": acceptance,
        "files": [_probe_path(key)],
        "question_ids": [item["question_id"]],
        "change_class": "research_probe",
    }


def file_supported_implementations(queue: str) -> List[Dict[str, Any]]:
    """Turn supported capability probes into executable policy work orders.

    This is the handoff the original architecture omitted: durable evidence used
    to stop at ``hypothesis.result`` with no consumer. The implementation remains
    isolated, gated, canaried, and bounded by the protected policy executor.
    """
    active = mechanics.active_tools()
    filed: List[Dict[str, Any]] = []
    for order in workorders.current(queue).values():
        if order.get("source") != "research_agent" or order.get("kind") != "unused_capability":
            continue
        if order.get("status") != workorders.PUBLISHED:
            continue
        tool = str(order.get("tool") or "")
        if not tool or tool in active:
            continue
        lineage = dict(order.get("provenance") or {})
        identity = str(lineage.get("hypothesis_id") or "")
        result = provenance.latest_result(identity) if identity else None
        if not result or result.get("status") not in {"supported", "accepted", "passed"}:
            continue
        change = {
            "id": "capability-policy-%s" % tool,
            "severity": "degraded",
            "kind": "capability_policy",
            "tool": tool,
            "summary": "Supported probe for %s is awaiting executable capability policy" % tool,
            "we_use_it": False,
            "sites": [],
            "detail": {
                "source_probe_order": order.get("id"),
                "hypothesis_id": identity,
                "result_node": result.get("node"),
                "effect": result.get("effect") or {},
                "capability_classification": (order.get("detail") or {}).get("capability_classification") or {},
            },
        }
        promotion = dict(
            lineage,
            change_class="strategy",
            evidence_class=result.get("evidence_class"),
            validation_evidence=result.get("validation_evidence") or [],
            result_node=result.get("node"),
        )
        submitted = workorders.submit(
            change,
            source="research_agent",
            intent=(
                "Implement the supported `%s` result as one literal entry in %s. "
                "Do not edit the protected executor or cycle; preserve the result's "
                "call/cost bounds and required outcome verification."
                % (tool, mechanics.POLICY_RELATIVE)
            ),
            acceptance=[
                "the supported result node %s remains linked" % result.get("node"),
                "%s has one enabled literal capability policy" % tool,
                "the protected capability-policy validator passes",
                "the complete release matrix and strategy canary judge the implementation",
            ],
            files=[mechanics.POLICY_RELATIVE],
            path=queue,
            provenance=promotion,
        )
        if submitted:
            filed.append(submitted)
    return filed


def settle_superseded_questions() -> List[str]:
    """Remove resolved historical questions from the current Findings queue."""
    registry = claims.load() or claims.build()
    claim_by_id = claims.claim_map(registry)
    contract_state = read_json(STATE / "contract_watch.json")
    contract_rows = contract.history(limit=1, path=str(STATE / "contract.ndjson"))
    latest_contract = contract_rows[-1] if contract_rows else {}
    latest_run = canary.latest_run()
    settled: List[str] = []
    for question in questions.open_questions():
        identity = str(question.get("id") or "")
        qclass = str(question.get("class") or "")
        last_seen = question.get("last_seen_run")
        age = latest_run - int(last_seen) if isinstance(last_seen, int) else None
        answer = None
        refs: List[str] = []
        if qclass == "hunger_wall" and (
            claim_by_id.get("safety.bulk_husbandry") or {}
        ).get("status") == "accepted":
            answer = "Superseded by measured constant-time whole-herd feeding and direct hunger/runway guards."
            refs = ["claims.json#safety.bulk_husbandry", "farm/cycle.py#feed_if_needed"]
        elif qclass == "tools_changed" and age is not None and age > 30:
            actionable = latest_contract.get("actionable")
            if actionable == 0 and not latest_contract.get("error") and not contract_state.get("last_error"):
                answer = "Superseded by repeated clean contract scans and the current captured capability policy."
                refs = ["state/contract_watch.json#latest-clean-scan", "state/contract.json"]
        if not answer:
            continue
        questions.set_status(
            identity,
            "answered",
            answer=answer,
            evidence_refs=refs,
            run=latest_run,
            probe_id="research-reconciliation",
            result_status="supported",
        )
        settled.append(identity)
    return settled


def settle_active_capability_questions() -> List[str]:
    """Close only novelty questions whose named mechanic is now executable."""
    active = mechanics.active_tools()
    settled: List[str] = []
    for question in questions.open_questions():
        if question.get("class") not in {"activity_novelty_tools", "activity_novelty_risk"}:
            continue
        text = "%s %s %s" % (
            question.get("alert") or "",
            question.get("question") or "",
            question.get("subject") or "",
        )
        matched = sorted(tool for tool in active if tool in text)
        # The state alert says "prestige is available" even when it does not
        # include the exact policy id; that exact token is still unambiguous.
        if "prestige" in text and "prestige" in active and "prestige" not in matched:
            matched.append("prestige")
        if not matched:
            continue
        identity = str(question.get("id") or "")
        questions.set_status(
            identity,
            "answered",
            answer="Validated executable capability policy is active for %s."
                   % ", ".join(matched),
            evidence_refs=["capability-policy:%s" % tool for tool in matched],
            run=canary.latest_run(),
            probe_id="capability-policy",
            result_status="supported",
        )
        settled.append(identity)
    return settled


def reconcile_active_capability_orders(queue: str) -> List[Dict[str, Any]]:
    """Retire probe-only orders only after an executable policy is active."""
    active = mechanics.active_tools()
    resolved: List[Dict[str, Any]] = []
    for order in workorders.current(queue).values():
        if order.get("status") != workorders.OPEN:
            continue
        tool = str(order.get("tool") or "")
        if tool not in active or order.get("kind") not in {"unused_capability", "capability_policy"}:
            continue
        row = workorders.resolve(
            str(order.get("id")),
            workorders.SUPERSEDED,
            note="superseded only after validated executable capability policy became active",
            path=queue,
        )
        if row:
            resolved.append(row)
    return resolved


# -- main --------------------------------------------------------------------


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("RESEARCH skipped: previous pass still active")
        return 0

    stored = read_json(STORE)
    proposed = set(stored.get("proposed") or [])
    queue = str(PROJECT / workorders.QUEUE)
    reconciled_capabilities = reconcile_active_capability_orders(queue)
    settled_superseded_questions = settle_superseded_questions()
    settled_capability_questions = settle_active_capability_questions()
    implementation_orders = file_supported_implementations(queue)
    current_orders = list(workorders.current(queue).values())
    proposal_limit = proposal_capacity(current_orders)

    runtime = policy.runtime_context()
    ledger.set_context(actor="research_agent", run=canary.latest_run(),
                       policy_id=runtime.get("policy_id"),
                       claim_registry_version=runtime.get("claim_registry_version"),
                       step="research")

    context = rate_context()
    capabilities = unused_capabilities()
    sensitive = sensitive_parameters()
    unbacked = unbacked_parameters(sensitive)

    print("RESEARCH rank=%s rate=%s animals=%s | unused_tools=%d sensitive_params=%d"
          % (context.get("rank"), context.get("produce_per_min"), context.get("animals"),
             len(capabilities), len({s['parameter'] for s in sensitive})))

    record_finding({
        "event": "scan",
        "context": context,
        "unused_capabilities": [c["capability"] for c in capabilities],
        "sensitive_parameters": sorted({s["parameter"] for s in sensitive}),
        "unbacked_parameters": sorted({s["parameter"] for s in unbacked}),
    })

    # A sensitive parameter with no supporting claim is an open question, not a
    # change. Opening it is free and keeps the uncertainty durable.
    for item in unbacked[:3]:
        try:
            questions.open_or_update(
                "strategy.unbacked_parameter",
                "%s steers decisions (%d runs would differ at %s) but no claim justifies its value"
                % (item["parameter"], item["changed_runs"], item["alternative"]),
                item={"run": context.get("run"), "ts": utcnow()},
                subject=item["parameter"],
                owner="research",
                next_step=(
                    "Define the falsifier and bind a replay or holdout for %s before proposing a value change."
                    % item["parameter"]
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print("  could not open question for %s: %s" % (item["parameter"], str(exc)[:80]))

    proposals: List[Tuple[Dict[str, Any], str]] = []

    # A measured server-regime change outranks speculative capability work because
    # it can invalidate the denominator of the live strategy.
    for proposal, key in dual_cap_strategy_proposals():
        if len(proposals) >= proposal_limit:
            break
        if key not in proposed:
            proposals.append((proposal, key))

    # Cheapest source next: capabilities we have never tried. Directly documented
    # progression/crisis mechanics route to the literal implementation surface;
    # uncertain tools still earn a probe first.
    for entry in capabilities:
        if len(proposals) >= proposal_limit:
            break
        key = "capability:%s" % entry["capability"]
        if key in proposed:
            continue
        proposals.append((capability_proposal(entry), key))
        if len(proposals) >= proposal_limit:
            break

    # Only pay for model hypotheses when the free sources are exhausted, and at
    # most once a day.
    if proposal_limit > 0 and not proposals and _model_due(stored):
        availability = llm.availability()
        if availability.get("available"):
            payload = {
                "production": context,
                "unused_capabilities": capabilities,
                "sensitive_parameters": sensitive[:8],
                "open_questions": [
                    {
                        "id": q.get("id"), "class": q.get("class"),
                        "subject": q.get("subject"), "question": str(q.get("alert"))[:200],
                    }
                    for q in (questions.open_questions() or [])[:10]
                ],
                "guardrails": {
                    "cycle_budget_seconds": rules.CYCLE_HARD_TIMEOUT,
                    "note": "feeding cadence and the growth gate have prior incidents",
                },
            }
            try:
                for item in model_hypotheses(payload):
                    semantic = provenance.semantic_fingerprint({
                        "hypothesis": item["hypothesis"],
                        "null_hypothesis": "No measurable improvement in %s." % item["metric"],
                        "falsifier": item["falsifier"],
                        "primary_metric": item["metric"],
                    })
                    key = "hypothesis:%s" % semantic[:20]
                    # Semantic lineage, not title memory, decides whether this is a
                    # duplicate or a legitimately re-opened hypothesis with new data.
                    proposals.append((hypothesis_proposal(item), key))
                    record_finding({"event": "hypothesis", "item": item})
                    if len(proposals) >= proposal_limit:
                        break
                stored["last_model_ts"] = utcnow()
            except llm.Dormant as exc:
                print("  model dormant: %s" % exc)
            except llm.GatewayError as exc:
                print("  gateway error: %s" % str(exc)[:120])
        else:
            print("  model dormant: %s" % availability.get("reason"))

    filed = list(implementation_orders)
    discovery_refs = [
        "history.ndjson#run=%s" % context.get("run"),
        "policy.json#%s" % runtime.get("policy_id"),
    ]
    for proposal, key in proposals:
        change = proposal["change"]
        detail = change.get("detail") or {}
        # Lineage must name only the question that originated this proposal. An
        # earlier implementation attached every open question, letting one
        # unrelated result appear to cover the entire backlog.
        question_ids = sorted(set(
            str(value) for value in (
                proposal.get("question_ids") or detail.get("question_ids") or []
            ) if value
        ))
        if proposal.get("evidence_spec"):
            spec = dict(proposal["evidence_spec"])
        elif change.get("kind") == "strategy_hypothesis":
            spec = {
                "hypothesis": detail.get("hypothesis"),
                "null_hypothesis": "The probe produces no measurable improvement in %s."
                                   % detail.get("metric"),
                "falsifier": detail.get("falsifier"),
                "primary_metric": detail.get("metric"),
                "expected_improvement": 0.01,
            }
        else:
            capability = str(change.get("tool") or change.get("summary") or "capability")
            spec = {
                "hypothesis": "A bounded use of %s improves the league-first objective."
                              % capability,
                "null_hypothesis": "%s produces no measurable objective benefit."
                                   % capability,
                "falsifier": "The bounded probe shows no gain or violates a declared safety budget.",
                "primary_metric": "league level, then lifetime produce, per bounded cost",
                "expected_improvement": 0.01,
            }
        registered = provenance.register_hypothesis(
            spec,
            discovery_refs,
            context_policy_id=runtime.get("policy_id"),
            question_ids=question_ids,
        )
        proposed.add(key)
        if not registered.get("accepted"):
            print("  skipped %s: %s" % (registered.get("id"), registered.get("reason")))
            continue
        lineage = dict(
            spec,
            hypothesis_id=registered["id"],
            discovery_evidence=discovery_refs,
            question_ids=question_ids,
            change_class=str(proposal.get("change_class") or "research_probe"),
            evidence_class="direct_mechanism" if change.get("kind") == "capability_policy" else None,
        )
        acceptance = list(proposal["acceptance"]) + [
            "the registry entry carries hypothesis_id %s and a causal evidence_class"
            % registered["id"],
            "the probe records supported or falsified via farm.provenance.record_result using validation evidence that is not discovery evidence",
        ]
        order = workorders.submit(
            change, source="research_agent", intent=proposal["intent"],
            acceptance=acceptance, files=proposal["files"], path=queue,
            provenance=lineage,
        )
        if order:
            filed.append(order)
            print("  proposed %s: %s" % (order["id"], str(order["summary"])[:90]))

    write_json(STORE, dict(stored, schema_version=1, last_ts=utcnow(),
                           passes=int(stored.get("passes") or 0) + 1,
                           proposed=sorted(proposed)))

    if reconciled_capabilities:
        print("  active capability policies retired: %s" % ", ".join(
            str(row.get("id")) for row in reconciled_capabilities
        ))
    if settled_superseded_questions:
        print("  superseded questions settled: %s"
              % ", ".join(settled_superseded_questions))
    if settled_capability_questions:
        print("  implemented capability questions settled: %s"
              % ", ".join(settled_capability_questions))
    if not filed:
        if context.get("lead_secure"):
            print("  no proposal: lead is secure and no untested capability remains")
        else:
            print("  no new proposal this pass")
        return 0

    ledger.record("research.proposed", {"count": len(filed),
                                        "ids": [o["id"] for o in filed]})
    # Filing a proposal is successful headless processing, not an attention state.
    return 0


def _model_due(stored: Dict[str, Any]) -> bool:
    last = stored.get("last_model_ts")
    if not last:
        return True
    try:
        when = datetime.strptime(str(last), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - when) >= timedelta(hours=MODEL_HYPOTHESIS_INTERVAL_HOURS)


if __name__ == "__main__":
    raise SystemExit(main())
