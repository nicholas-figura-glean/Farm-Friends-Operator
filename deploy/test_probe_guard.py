#!/usr/bin/env python3
"""Failure-injection checks for pre-transport probe authority."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from farm import mcp, probe_guard, probes, questions  # noqa: E402


class Suite:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: List[str] = []

    def check(self, condition: bool, label: str, detail: Any = "") -> None:
        self.checks += 1
        passed = bool(condition)
        print("  %-4s %s%s" % ("ok" if passed else "FAIL", label,
                                 ("  [%s]" % detail) if detail and not passed else ""))
        if not passed:
            self.failures.append(label + ((" [%s]" % detail) if detail else ""))

    def raises(self, exc_type, fn, label: str) -> None:
        try:
            fn()
        except exc_type:
            self.check(True, label)
        except Exception as exc:
            self.check(False, label, "%s: %s" % (type(exc).__name__, exc))
        else:
            self.check(False, label, "did not raise")


def spec(calls: int = 1, coins: int = 0) -> Dict[str, Any]:
    return {
        "command": ["run.py", "--sweep"],
        "read_only": True,
        "autonomous": False,
        "budget": {"calls": calls, "coins": coins, "wall_seconds": 30},
        "tools": {"list_farm": {"max_calls": calls, "arguments": {}}} if calls else {},
    }


def main() -> int:
    suite = Suite()

    validated = probe_guard.validate_spec("read", spec())
    suite.check(validated["tools"]["list_farm"]["read_only"],
                "protected profiles classify the read capability")
    registry = probes._registry()
    suite.check(
        all(
            probe_guard.spec_fingerprint(name, item) == probe_guard.PINNED_AUTONOMOUS.get(name)
            for name, item in registry.items() if item.get("autonomous")
        ),
        "every autonomous probe spec and executable bytes are pinned",
    )
    from farm import control
    suite.check(
        all(
            control.is_protected(str((item.get("command") or [""])[0]))
            for item in registry.values() if item.get("autonomous")
        ),
        "every autonomous probe executable is inside the trusted boundary",
    )
    suite.raises(
        probe_guard.AuthorizationError,
        lambda: probe_guard.validate_spec("read", dict(spec(), autonomous=True)),
        "an unpinned registry edit cannot become autonomous",
    )
    adjudicating = dict(registry["activity_replay"], hypothesis_id="hyp-untrusted")
    suite.raises(
        probe_guard.AuthorizationError,
        lambda: probe_guard.validate_spec("activity_replay", adjudicating),
        "autonomous worker cannot self-adjudicate a hypothesis",
    )
    suite.raises(
        probe_guard.AuthorizationError,
        lambda: probe_guard.validate_spec("escape", dict(spec(), command=["../escape.py"])),
        "registry command traversal is rejected",
    )
    suite.raises(
        probe_guard.AuthorizationError,
        lambda: probe_guard.validate_spec(
            "unknown", dict(spec(), tools={"invented_tool": {"max_calls": 1, "arguments": {}}}),
        ),
        "registry cannot invent a capability",
    )
    mutating_read = dict(spec(), budget={"calls": 1, "coins": 4, "wall_seconds": 30})
    mutating_read["tools"] = {
        "plant": {
            "max_calls": 1,
            "arguments": {"kind": {"required": True, "equals": "wheat"}},
        }
    }
    suite.raises(
        probe_guard.AuthorizationError,
        lambda: probe_guard.validate_spec("false-read", mutating_read),
        "a read-only label cannot grant a mutation",
    )
    feed_grant = probe_guard.new_grant("feed_economics", registry["feed_economics"])
    suite.raises(
        probe_guard.AuthorizationError,
        lambda: probe_guard.authorize(
            feed_grant,
            {"method": "tools/call", "params": {
                "name": "feed_animals", "arguments": {"animal_id": "all"},
            }},
            retries=1,
        ),
        "single-animal feed probe cannot consume the whole-herd reserve",
    )

    plant_spec = {
        "command": ["run.py", "--sweep"],
        "read_only": False,
        "autonomous": False,
        "budget": {"calls": 1, "coins": 4, "wall_seconds": 30},
        "tools": {
            "plant": {
                "max_calls": 1,
                "arguments": {
                    "kind": {"required": True, "equals": "wheat"},
                    "qty": {"integer": True, "min": 1, "max": 1, "default": 1},
                },
            }
        },
    }
    plant = probe_guard.new_grant("plant", plant_spec)
    payload = {"method": "tools/call", "params": {"name": "plant", "arguments": {"kind": "wheat"}}}
    reserved = probe_guard.authorize(plant, payload, retries=1)
    suite.check(reserved["reserved_coins"] == 4 and probe_guard.usage(plant)["calls"] == 1,
                "coin exposure is reserved before a mutation")
    suite.raises(
        probe_guard.AuthorizationError,
        lambda: probe_guard.authorize(plant, payload, retries=1),
        "call N+1 is denied before transport",
    )
    retry_grant = probe_guard.new_grant("plant", plant_spec)
    suite.raises(
        probe_guard.AuthorizationError,
        lambda: probe_guard.authorize(retry_grant, payload, retries=2),
        "mutating probes cannot ambiguously retry",
    )

    concurrent_spec = spec(calls=3)
    concurrent = probe_guard.new_grant("concurrent", concurrent_spec)
    successes: List[int] = []
    failures: List[int] = []

    def reserve(index: int) -> None:
        try:
            probe_guard.authorize(
                concurrent,
                {"method": "tools/call", "params": {"name": "list_farm", "arguments": {}}},
                retries=1,
            )
            successes.append(index)
        except probe_guard.AuthorizationError:
            failures.append(index)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    suite.check(len(successes) == 3 and len(failures) == 9,
                "concurrent reservations cannot oversubscribe the grant",
                {"successes": len(successes), "failures": len(failures)})

    # End-to-end client/broker proof: the second call is denied in the trusted
    # parent and the fake transport sees exactly one request.
    with tempfile.TemporaryDirectory() as tmp:
        grant = probe_guard.new_grant("brokered", spec())
        request_read, request_write = os.pipe()
        response_read, response_write = os.pipe()
        transported: List[Dict[str, Any]] = []

        def transport(request: Dict[str, Any], timeout: int, retries: int) -> Dict[str, Any]:
            transported.append(request)
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"content": [{"type": "text", "text": "farm snapshot"}]},
            }

        broker = threading.Thread(
            target=probe_guard.serve,
            args=(request_read, response_write, grant, transport),
            daemon=True,
        )
        broker.start()
        prior = dict(os.environ)
        try:
            os.environ[probe_guard.ENFORCEMENT_ENV] = "1"
            os.environ[probe_guard.REQUEST_FD_ENV] = str(request_write)
            os.environ[probe_guard.RESPONSE_FD_ENV] = str(response_read)
            os.environ["FARM_TOOL_CALL_LOG"] = str(Path(tmp) / "tool_calls.ndjson")
            client = mcp.Client()
            suite.check(client.call("list_farm") == "farm snapshot",
                        "managed client reaches transport through the broker")
            suite.raises(mcp.McpError, lambda: client.call("list_farm"),
                         "broker denies the over-budget client call")
        finally:
            os.environ.clear()
            os.environ.update(prior)
            os.close(request_write)
            os.close(response_read)
            broker.join(timeout=2)
        usage = probe_guard.usage(grant)
        suite.check(len(transported) == 1 and usage["calls"] == 1 and usage["denials"] == 1,
                    "denied authority creates no second transport effect",
                    {"transported": len(transported), "usage": usage})

    # Full runner path: the child has no endpoint, the parent fake sees one
    # authorized request, and an attempted second call is classified as a
    # capability violation even if worker code would otherwise catch the error.
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state"
        state.mkdir()
        worker = Path(tmp) / "worker.py"
        runner_spec = dict(spec(), outputs=[])
        transported = []
        transport_delay = [0.0]

        class ParentClient:
            def _post(self, request, timeout=None, retries=None):
                transported.append(request)
                if transport_delay[0]:
                    time.sleep(transport_delay[0])
                return {
                    "jsonrpc": "2.0", "id": request.get("id"),
                    "result": {"content": [{"type": "text", "text": "runner snapshot"}]},
                }

        saved_registry, saved_command, saved_client = probes._registry, probes._command, probes.mcp.Client
        prior = dict(os.environ)
        try:
            os.environ["FARM_STATE_DIR"] = str(state)
            os.environ["FARM_EXPERIMENT_LOG"] = str(state / "experiments.ndjson")
            probes._registry = lambda: {"runner": runner_spec}
            probes._command = lambda unused: [sys.executable, str(worker)]
            probes.mcp.Client = ParentClient
            worker.write_text(
                "import sys\nsys.path.insert(0, %r)\nfrom farm.mcp import Client\n"
                "print(Client().call('list_farm'))\n" % str(PROJECT),
                encoding="utf-8",
            )
            passed = probes.run_probe("runner", explicit=True, run=1)
            suite.check(passed["status"] == "passed" and passed["calls"] == 1
                        and passed["coins_reserved"] == 0,
                        "sandboxed runner uses the parent-owned capability broker", passed)
            trace_rows = [
                json.loads(line)
                for line in (state / "tool_calls.ndjson").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            suite.check(
                len(trace_rows) == 2
                and all(row.get("authoritative") is True for row in trace_rows)
                and [row.get("event") for row in trace_rows] == ["start", "end"],
                "parent broker persists one authoritative trace pair",
                trace_rows,
            )
            transported.clear()
            worker.write_text(
                "import sys\nsys.path.insert(0, %r)\nfrom farm.mcp import Client\n"
                "c=Client()\nc.call('list_farm')\nc.call('list_farm')\n" % str(PROJECT),
                encoding="utf-8",
            )
            denied = probes.run_probe("runner", explicit=True, run=2)
            suite.check(denied["status"] == "capability_violation"
                        and denied["calls"] == 1 and denied["capability_denials"] == 1
                        and len(transported) == 1,
                        "runner denies call N+1 before the fake transport", denied)

            transported.clear()
            transport_delay[0] = 1.2
            worker.write_text(
                "import os, sys, time\nsys.path.insert(0, %r)\nfrom farm import probe_guard\n"
                "payload={'jsonrpc':'2.0','id':7,'method':'tools/call','params':"
                "{'name':'list_farm','arguments':{}}}\n"
                "probe_guard._write_frame(int(os.environ[probe_guard.REQUEST_FD_ENV]), "
                "{'payload':payload,'timeout':3,'retries':1})\ntime.sleep(.2)\n" % str(PROJECT),
                encoding="utf-8",
            )
            started = time.monotonic()
            delayed = probes.run_probe("runner", explicit=True, run=3)
            elapsed = time.monotonic() - started
            suite.check(delayed["status"] == "passed" and elapsed >= 1.0
                        and len(transported) == 1,
                        "runner holds the farm boundary until an in-flight transport finishes",
                        {"result": delayed, "elapsed": elapsed})

            transport_delay[0] = 0.0
            worker.write_text(
                "import os, subprocess, sys\n"
                "fds=(int(os.environ['FARM_MCP_BROKER_REQUEST_FD']), "
                "int(os.environ['FARM_MCP_BROKER_RESPONSE_FD']))\n"
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'], pass_fds=fds)\n",
                encoding="utf-8",
            )
            started = time.monotonic()
            descendant = probes.run_probe("runner", explicit=True, run=4)
            elapsed = time.monotonic() - started
            suite.check(descendant["status"] in {"passed", "failed"} and elapsed < 3.0,
                        "descendant cannot retain broker pipes and wedge the farm lock",
                        {"result": descendant, "elapsed": elapsed})

            question = questions.open_or_update(
                "model_drift", "MODEL DRIFT: broker setup fixture",
                item={"run": 5}, subject="broker setup fixture",
            )["question"]

            class FailingClient:
                def __init__(self):
                    raise mcp.McpError("endpoint unavailable")

            probes.mcp.Client = FailingClient
            setup_failed = False
            try:
                probes.run_probe(
                    "runner", explicit=True, run=5, question_ids=[question["id"]],
                )
            except mcp.McpError:
                setup_failed = True
            after = next(row for row in questions.load_all() if row["id"] == question["id"])
            suite.check(setup_failed and after["status"] == "open"
                        and not after.get("active_probe_id"),
                        "broker setup failure leaves its question open and unclaimed", after)
        finally:
            probes._registry, probes._command, probes.mcp.Client = saved_registry, saved_command, saved_client
            os.environ.clear()
            os.environ.update(prior)

    print()
    if suite.failures:
        print("PROBE GUARD TEST FAILED: %d of %d checks" % (len(suite.failures), suite.checks))
        for failure in suite.failures:
            print("  - " + failure)
        return 1
    print("PROBE GUARD TEST PASSED: %d checks" % suite.checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
