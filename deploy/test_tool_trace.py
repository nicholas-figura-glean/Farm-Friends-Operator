#!/usr/bin/env python3
"""Checks for best-effort MCP boundary tracing.

No network is used. Fake clients drive Client.call()/tool_names() through success,
tool error and transport error paths, then the NDJSON is inspected. The contract
is stronger than "a row was written": telemetry must pair start/end records,
scrub secrets, remain bounded, preserve call_count semantics and never break a
successful tool call when its own file cannot be written.
"""

import http.client
import json
import os
import tempfile
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402
from farm import ledger, mcp  # noqa: E402
from farm.mcp import Client, McpError, ToolError  # noqa: E402


class FakeClient(Client):
    def __init__(self, response: Optional[Dict[str, Any]] = None, error: Optional[Exception] = None):
        super().__init__("https://farm.invalid/secret-token-123")
        self.response = response or {}
        self.fake_error = error

    def rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.fake_error:
            raise self.fake_error
        if method == "tools/list":
            return {"tools": [{"name": "sell"}, {"name": "list_farm"}]}
        return self.response


def rows(path: str):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    checks = 0
    failures = []

    def check(condition: bool, label: str, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        print("  %-4s %s%s" % ("ok" if condition else "FAIL", label,
                                 ("  [%s]" % detail) if detail and not condition else ""))
        if not condition:
            failures.append(label + ((" [%s]" % detail) if detail else ""))

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "calls.ndjson")
        old = os.environ.get("FARM_TOOL_CALL_LOG")
        os.environ["FARM_TOOL_CALL_LOG"] = path
        try:
            success = FakeClient({"content": [{"type": "text", "text": "sold 4 eggs"}]})
            value = success.call("sell", item="egg", qty=4)
            data = rows(path)
            check(value == "sold 4 eggs", "successful call returns its normal result")
            check(len(data) == 2 and [r["event"] for r in data] == ["start", "end"],
                  "successful call writes one paired span", repr(data))
            check(data[0]["id"] == data[1]["id"], "span pair shares an id")
            check(data[0]["tool"] == "sell" and data[0]["arguments"] == {"item": "egg", "qty": 4},
                  "start span records tool and arguments", repr(data[0]))
            check(data[1]["ok"] is True and data[1]["duration_ms"] >= 0,
                  "end span records success and duration", repr(data[1]))
            check(data[1]["result"] == "sold 4 eggs", "bounded result preview is retained")
            check(success.call_count == 1, "tools/call count keeps its historical meaning")

            # The monitor pairs those rows and attaches them to the measured step;
            # this is the exact schema the browser consumes.
            old_calls = monitor.TOOL_CALLS
            monitor.TOOL_CALLS = Path(path)
            try:
                pipeline = {
                    "started_ts": data[0]["ts"], "updated_ts": data[1]["ts"],
                    "steps": [{"name": "sell", "started_ts": data[0]["ts"],
                               "ended_ts": data[1]["ts"]}],
                }
                graph = {"nodes": [{"kind": "tool", "label": "sell", "steps": ["sell"]}]}
                paired = monitor._boundary_calls(pipeline, graph)
            finally:
                monitor.TOOL_CALLS = old_calls
            check(len(paired) == 1, "monitor pairs one start/end record into one span", repr(paired))
            check(paired[0]["step"] == "sell" and paired[0]["status"] == "ok",
                  "monitor assigns span to its measured parent step", repr(paired[0]))
            check(paired[0]["arguments"]["qty"] == 4 and paired[0]["result"] == "sold 4 eggs",
                  "paired browser payload preserves input and output", repr(paired[0]))

            with ledger.bind(
                actor="cycle", run=42, step="sell", policy_id="pol-test",
                claim_registry_version=7,
            ):
                contextual = FakeClient({"content": [{"type": "text", "text": "context ok"}]})
                contextual.call("sell", item="egg", qty=1)
            context_rows = rows(path)[-2:]
            check(
                all(row.get("actor") == "cycle" and row.get("run") == 42 for row in context_rows),
                "MCP span pair inherits actor and run context", repr(context_rows),
            )
            check(
                all(row.get("policy_id") == "pol-test" and row.get("claim_registry_version") == 7
                    for row in context_rows),
                "MCP span pair inherits policy and claim identities", repr(context_rows),
            )

            names = FakeClient()
            check(names.tool_names() == ["list_farm", "sell"], "tools/list result is unchanged")
            name_rows = rows(path)[-2:]
            check(name_rows[0]["tool"] == "tools/list" and name_rows[1]["ok"] is True,
                  "MCP handshake is traced as a boundary span", repr(name_rows))
            check(names.call_count == 0, "tools/list does not inflate tools/call count")

            bounded = FakeClient({"content": [{"type": "text", "text": "ok"}]})
            bounded.call("sell", payload="x" * 900)
            bounded_start = rows(path)[-2]
            check("preview" in bounded_start["arguments"] and len(bounded_start["arguments"]["preview"]) <= 600,
                  "large arguments are bounded", repr(bounded_start["arguments"]))
            bounded.call("sell", callback="https://farm.invalid/secret-token-123")
            secret_start = rows(path)[-2]
            check("secret-token-123" not in json.dumps(secret_start["arguments"])
                  and "farm.invalid" not in json.dumps(secret_start["arguments"]),
                  "arguments cannot leak the secret endpoint", repr(secret_start["arguments"]))

            bad_tool = FakeClient({"isError": True, "content": [{"type": "text", "text": "no sale"}]})
            raised = False
            try:
                bad_tool.call("sell", qty=1)
            except ToolError:
                raised = True
            error_rows = rows(path)[-2:]
            check(raised, "tool error still reaches the caller")
            check(error_rows[1]["ok"] is False and "no sale" in error_rows[1]["error"],
                  "tool error closes the span as error", repr(error_rows[1]))

            transport = FakeClient(error=McpError("https://farm.invalid/secret-token-123 failed"))
            try:
                transport.call("list_farm")
            except McpError:
                pass
            transport_end = rows(path)[-1]
            check("secret-token-123" not in transport_end["error"] and "farm.invalid" not in transport_end["error"],
                  "telemetry scrubs endpoint and token", transport_end["error"])

            # Large list_farm responses occasionally lose the final HTTP chunk.
            # That is transport noise and must be retried inside the call rather
            # than crashing the whole cycle and waiting for scheduler recovery.
            saved_urlopen = mcp.urllib.request.urlopen
            saved_retries, saved_backoff = mcp.RETRIES, mcp.BACKOFF
            attempts = []

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'

            def flaky_urlopen(*args, **kwargs):
                attempts.append(1)
                if len(attempts) == 1:
                    raise http.client.IncompleteRead(b'{"partial":')
                return Response()

            try:
                mcp.urllib.request.urlopen = flaky_urlopen
                mcp.RETRIES, mcp.BACKOFF = 2, 0
                retried = Client("https://farm.invalid/secret-token-123")
                result = retried._post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            finally:
                mcp.urllib.request.urlopen = saved_urlopen
                mcp.RETRIES, mcp.BACKOFF = saved_retries, saved_backoff
            check(result.get("result", {}).get("ok") is True and len(attempts) == 2,
                  "an incomplete chunk is retried inside the MCP call", repr(result))
            check(retried.transport_errors == 1,
                  "the recovered chunk failure remains transport telemetry")

            # A broken telemetry destination must cost visibility, never a cycle.
            os.environ["FARM_TOOL_CALL_LOG"] = os.path.join(tmp, "not-a-dir", "calls.ndjson")
            with open(os.path.join(tmp, "not-a-dir"), "w", encoding="utf-8") as handle:
                handle.write("x")
            invisible = FakeClient({"content": [{"type": "text", "text": "still works"}]})
            check(invisible.call("sell") == "still works",
                  "unwritable telemetry cannot break a successful tool call")
        finally:
            if old is None:
                os.environ.pop("FARM_TOOL_CALL_LOG", None)
            else:
                os.environ["FARM_TOOL_CALL_LOG"] = old

    print()
    if failures:
        print("TOOL TRACE TEST FAILED: %d of %d checks" % (len(failures), checks))
        for failure in failures:
            print("  - " + failure)
        return 1
    print("TOOL TRACE TEST PASSED: %d checks" % checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
