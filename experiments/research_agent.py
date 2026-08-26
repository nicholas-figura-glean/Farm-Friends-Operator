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
    analysis, canary, claims, contract, journal, ledger, llm, policy, provenance,
    questions, research, rules, workorders,
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
    for name in sorted(set(tools) - set(rely)):
        if name in IGNORED_CAPABILITIES:
            continue
        spec = tools[name] or {}
        out.append({
            "capability": name,
            "description": str(spec.get("description") or "")[:300],
            "required": spec.get("required") or [],
            "args": sorted((spec.get("args") or {}).keys()),
        })
    return out


def capability_proposal(entry: Dict[str, Any]) -> Dict[str, Any]:
    """A work order proposing a bounded probe for one unused capability."""
    name = entry["capability"]
    change = {
        "id": "research-capability-%s" % name,
        "severity": "opportunity",
        "kind": "unused_capability",
        "tool": name,
        "summary": "%s is exposed by the server but never called: %s"
                   % (name, entry["description"][:120]),
        "we_use_it": False,
        "sites": [],
        "detail": entry,
    }
    intent = (
        "The server exposes `%s` (%s) and our code has never called it, so we have no "
        "evidence about whether it helps. Add a bounded probe under experiments/ that "
        "measures its effect on lifetime produce, and register it in "
        "experiments/registry.py so the probe scheduler can run it. Do NOT wire it into "
        "farm/cycle.py: an unmeasured capability on the live path is exactly the kind of "
        "plausible change POSTMORTEM-run377 documents losing ground. If the probe must "
        "mutate state, give it a hard budget and mark it non-autonomous so it only runs "
        "when explicitly invoked." % (name, entry["description"][:160])
    )
    acceptance = [
        "a probe for %s exists under experiments/ and is registered" % name,
        "the probe declares a budget and its read_only/autonomous flags",
        "farm/cycle.py is unchanged by this order",
        "the probe records its outcome so a later pass can read the result",
    ]
    return {"change": change, "intent": intent, "acceptance": acceptance,
            "files": ["experiments/registry.py"]}


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
You are a research analyst for an automated farming game agent that is currently
in first place and must stay there. You do not write production code. You propose
falsifiable experiments.

Rules:

* Reply with a JSON array only. No prose, no markdown fences.
* Each element must be an object with exactly these keys:
    "title"      short imperative name
    "hypothesis" what you believe is true, stated so it can be wrong
    "falsifier"  the observation that would disprove it
    "probe"      a bounded experiment design, naming the tools it would call
    "metric"     the single number that decides it, e.g. produce per coin
    "risk"       what it could cost if the hypothesis is wrong
* Propose at most 3, ordered by expected value.
* Prefer experiments that are read-only or cheaply reversible.
* Do not propose anything that would slow the 180s cycle or add server load
  proportional to herd size.
* Do not propose changes to feeding cadence or the growth gate unless the evidence
  given to you specifically contradicts the current claim; both have prior
  incidents.
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
    required = ("title", "hypothesis", "falsifier", "probe", "metric")
    out = []
    for item in parsed[:3]:
        if isinstance(item, dict) and all(isinstance(item.get(k), str) and item.get(k) for k in required):
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
        "Build a bounded probe to test this hypothesis. Do not change live strategy.\n\n"
        "Hypothesis: %s\n\nFalsifier: %s\n\nProbe design: %s\n\nDeciding metric: %s\n\n"
        "Implement it under experiments/ and register it in experiments/registry.py with "
        "an explicit budget. The probe must record enough for a later pass to decide the "
        "hypothesis without re-running it."
        % (item["hypothesis"], item["falsifier"], item["probe"], item["metric"])
    )
    acceptance = [
        "a registered probe tests the stated hypothesis",
        "the probe records the deciding metric (%s)" % item["metric"][:80],
        "farm/cycle.py is unchanged by this order",
        "the probe has an explicit budget",
    ]
    return {"change": change, "intent": intent, "acceptance": acceptance,
            "files": ["experiments/registry.py"]}


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
                subject=item["parameter"],
            )
        except Exception as exc:  # noqa: BLE001
            print("  could not open question for %s: %s" % (item["parameter"], str(exc)[:80]))

    proposals: List[Dict[str, Any]] = []

    # Cheapest source first: capabilities we have never tried.
    for entry in capabilities:
        key = "capability:%s" % entry["capability"]
        if key in proposed:
            continue
        proposals.append((capability_proposal(entry), key))
        if len(proposals) >= MAX_PROPOSALS_PER_PASS:
            break

    # Only pay for model hypotheses when the free sources are exhausted, and at
    # most once a day.
    if not proposals and _model_due(stored):
        availability = llm.availability()
        if availability.get("available"):
            payload = {
                "production": context,
                "unused_capabilities": capabilities,
                "sensitive_parameters": sensitive[:8],
                "open_questions": [
                    {"class": q.get("class"), "question": str(q.get("question"))[:200]}
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
                    if len(proposals) >= MAX_PROPOSALS_PER_PASS:
                        break
                stored["last_model_ts"] = utcnow()
            except llm.Dormant as exc:
                print("  model dormant: %s" % exc)
            except llm.GatewayError as exc:
                print("  gateway error: %s" % str(exc)[:120])
        else:
            print("  model dormant: %s" % availability.get("reason"))

    filed = []
    discovery_refs = [
        "history.ndjson#run=%s" % context.get("run"),
        "policy.json#%s" % runtime.get("policy_id"),
    ]
    question_ids = [str(item.get("id")) for item in questions.open_questions() if item.get("id")]
    for proposal, key in proposals:
        change = proposal["change"]
        detail = change.get("detail") or {}
        if change.get("kind") == "strategy_hypothesis":
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
                "hypothesis": "A bounded use of %s improves lifetime-produce efficiency."
                              % capability,
                "null_hypothesis": "%s produces no measurable lifetime-produce benefit."
                                   % capability,
                "falsifier": "The bounded probe shows no gain or violates a declared safety budget.",
                "primary_metric": "lifetime produce gained per bounded call or coin",
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
        lineage = dict(spec, hypothesis_id=registered["id"],
                       discovery_evidence=discovery_refs,
                       change_class="research_probe")
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
