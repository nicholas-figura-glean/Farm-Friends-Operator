"""Minimal JSON-RPC 2.0 / Streamable-HTTP MCP client (stdlib only).

Secret handling: the endpoint URL is a secret. It is read from
$FARM_MCP_URL or ~/.config/farm/endpoint (mode 0600), never passed as a
CLI argument, and scrubbed from every exception message and log line.
"""

import http.client
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import compaction, ledger, probe_guard

DEFAULT_ENDPOINT_FILE = os.path.expanduser("~/.config/farm/endpoint")
DEFAULT_TOOL_CALL_LOG = os.path.join("state", "tool_calls.ndjson")
# list_farm still scales with herd size. feed_animals and collect_produce are now
# constant-time bulk operations, but the read path retains its measured timeout.
TIMEOUT = 120
RETRIES = 3
BACKOFF = 1.5


def _trace_ts() -> str:
    """UTC with milliseconds: fast MCP calls need more than whole seconds."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_arguments(arguments: Dict[str, Any]) -> Any:
    """Bound telemetry without changing or rejecting the actual tool arguments."""
    try:
        raw = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(arguments)
    if len(raw) > 600:
        return {"preview": raw[:599] + "…"}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"preview": raw}


def _write_tool_call(row: Dict[str, Any]) -> None:
    """Best-effort boundary telemetry with actor/run/policy attribution."""
    try:
        for key, value in ledger.tool_context().items():
            row.setdefault(key, value)
        probe_id = os.environ.get("FARM_PROBE_ID")
        if probe_id:
            row.setdefault("probe_id", probe_id)
        path = os.environ.get("FARM_TOOL_CALL_LOG", DEFAULT_TOOL_CALL_LOG)
        compaction.append_json(path, row, strict=False)
    except (OSError, TypeError, ValueError):
        pass


class RateLimiter(object):
    """Process-wide call budget, shared by every Client and worker thread.

    Adoption is parallel and the farm compounds, so call volume grows on its
    own. One global limiter keeps total pressure on the server predictable no
    matter how many workers exist, and can be lowered mid-run after an error.
    """

    def __init__(self, rate: float = 4.0):
        self._rate = float(rate)
        self._lock = threading.Lock()
        self._next = 0.0

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float) -> None:
        with self._lock:
            self._rate = max(0.2, float(rate))

    def acquire(self) -> None:
        """Reserve the next slot exactly once, then sleep until it arrives.

        The reservation must happen in a single critical section. An earlier
        version re-checked the clock in a loop after sleeping, advancing the
        reservation pointer on every iteration, so waiters pushed each other
        further into the future and throughput collapsed to a crawl.
        """
        with self._lock:
            interval = 1.0 / self._rate
            slot = max(time.monotonic(), self._next)
            self._next = slot + interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


LIMITER = RateLimiter()


class McpError(RuntimeError):
    """Transport or protocol failure, with the endpoint scrubbed out."""


class ToolError(RuntimeError):
    """The server returned isError=true for a tool call."""


def _load_endpoint() -> str:
    url = os.environ.get("FARM_MCP_URL", "").strip()
    if not url:
        path = os.environ.get("FARM_MCP_ENDPOINT_FILE", DEFAULT_ENDPOINT_FILE)
        try:
            with open(path, "r") as fh:
                url = fh.read().strip()
        except OSError as exc:
            raise McpError(
                "no endpoint configured: set FARM_MCP_URL or write %s (chmod 600): %s"
                % (path, exc.__class__.__name__)
            )
    if not url.startswith("https://"):
        raise McpError("endpoint must be https")
    return url


class Client(object):
    """One client per process. Stateless POSTs; no session id required."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        timeout: int = TIMEOUT,
        retries: int = RETRIES,
    ):
        # Managed probes never receive the secret endpoint. Their client speaks
        # over inherited pipes to a trusted parent that owns authorization,
        # budgets, retries, and transport.
        brokered = os.environ.get(probe_guard.ENFORCEMENT_ENV) == "1"
        self._endpoint = "https://probe-broker.invalid" if brokered else (endpoint or _load_endpoint())
        self._timeout = max(1, int(timeout))
        self._retries = max(1, int(retries))
        self._id = 0
        self._ctx = ssl.create_default_context()
        self.call_count = 0
        self.transport_errors = 0
        # Same retries, attributed to the tool that caused them. Bulk feed and
        # collection are constant-time now, so their failures are no longer
        # exempted from ordinary transport health handling.
        self.transport_errors_by_tool: Dict[str, int] = {}
        self._current_tool = "rpc"
        # Time spent in the request itself, EXCLUDING the wait for a rate-limiter
        # slot. The distinction is not academic: the courtesy ease-off in
        # cycle.py throttles adoption when the mean adopt call looks slow, and
        # timing the limiter wait made that measurement circular. With W workers
        # sharing a limiter at R calls/s, a worker waits ~W/R for its slot, so at
        # 6 workers and 2.5/s every call "took" 2.4s (observed: 2.42s) against a
        # 1.2s threshold - so the rate was cut, which lengthened the wait, which
        # cut the rate again. The server was never the constraint.
        self.last_service_seconds = 0.0

    @property
    def endpoint(self) -> str:
        """Only for spawning sibling clients in worker threads."""
        return self._endpoint

    # -- redaction ---------------------------------------------------------
    def scrub(self, text: str) -> str:
        """Remove the secret endpoint (and its path token) from any text."""
        if not text:
            return text
        out = text.replace(self._endpoint, "<endpoint>")
        token = self._endpoint.rstrip("/").rsplit("/", 1)[-1]
        if len(token) > 6:
            out = out.replace(token, "<token>")
        host = self._endpoint.split("//", 1)[-1].split("/", 1)[0]
        return out.replace(host, "<host>")

    # -- transport ---------------------------------------------------------
    def _post(
        self,
        payload: Dict[str, Any],
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        timeout_budget = self._timeout if timeout is None else max(1, int(timeout))
        brokered = os.environ.get(probe_guard.ENFORCEMENT_ENV) == "1"
        # A managed probe receives exactly one transport attempt by default. This
        # makes ambiguous mutation failures consume one reservation rather than
        # silently repeating a side effect.
        attempt_budget = (1 if brokered else self._retries) if retries is None else max(1, int(retries))
        if brokered:
            try:
                return probe_guard.broker_post(payload, timeout_budget, attempt_budget)
            except probe_guard.AuthorizationError as exc:
                raise McpError("probe transport denied: %s" % str(exc)[:400]) from exc
        req = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            },
            method="POST",
        )
        last = None
        for attempt in range(attempt_budget):
            try:
                LIMITER.acquire()
                _started = time.time()
                with urllib.request.urlopen(req, timeout=timeout_budget, context=self._ctx) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                self.last_service_seconds = time.time() - _started
                return _decode(raw)
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                OSError,
                ValueError,
            ) as exc:
                last = exc
                self.transport_errors += 1
                self.transport_errors_by_tool[self._current_tool] = (
                    self.transport_errors_by_tool.get(self._current_tool, 0) + 1
                )
                if attempt < attempt_budget - 1:
                    time.sleep(BACKOFF ** (attempt + 1))
        raise McpError(
            self.scrub("transport failure after %d tries: %r" % (attempt_budget, last))
        )

    def rpc(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        data = self._post(payload, timeout=timeout, retries=retries)
        if "error" in data:
            raise McpError(self.scrub("rpc error on %s: %s" % (method, json.dumps(data["error"]))))
        return data.get("result", {})

    def call(
        self,
        tool: str,
        _transport_timeout: Optional[int] = None,
        _transport_retries: Optional[int] = None,
        **arguments: Any,
    ) -> str:
        """Call a tool and record the process boundary as a real trace span.

        A caller may narrow the transport budget for a nonessential read. Tool
        arguments remain separate so the reduced budget can never leak into the
        MCP payload or silently change a mutation request.
        """
        self.call_count += 1
        call_id = "%d-%x-%d" % (os.getpid(), id(self), self.call_count)
        started = time.monotonic()
        safe_arguments = _safe_arguments(arguments)
        try:
            safe_arguments = json.loads(self.scrub(json.dumps(safe_arguments)))
        except (TypeError, ValueError):
            safe_arguments = {"preview": self.scrub(str(safe_arguments))[:600]}
        _write_tool_call({
            "id": call_id,
            "event": "start",
            "ts": _trace_ts(),
            "tool": tool,
            "arguments": safe_arguments,
        })
        try:
            self._current_tool = tool
            params = {"name": tool, "arguments": arguments}
            if _transport_timeout is None and _transport_retries is None:
                # Preserve the long-standing rpc(method, params) extension point
                # for fixtures and sibling clients that override it.
                result = self.rpc("tools/call", params)
            else:
                result = self.rpc(
                    "tools/call",
                    params,
                    timeout=_transport_timeout,
                    retries=_transport_retries,
                )
            text = "\n".join(
                block.get("text", "")
                for block in (result.get("content") or [])
                if block.get("type") == "text"
            )
            if result.get("isError"):
                raise ToolError(self.scrub("%s returned isError: %s" % (tool, text[:400])))
            text = self.scrub(text)
            _write_tool_call({
                "id": call_id,
                "event": "end",
                "ts": _trace_ts(),
                "tool": tool,
                "ok": True,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "result": text[:240],
            })
            return text
        except Exception as exc:
            _write_tool_call({
                "id": call_id,
                "event": "end",
                "ts": _trace_ts(),
                "tool": tool,
                "ok": False,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "error": self.scrub(str(exc))[:240],
            })
            raise

    def tool_names(self) -> list:
        """List tools and trace the MCP handshake as a boundary span too."""
        # Keep call_count's old meaning (tools/call only); release metrics and
        # budgets already depend on it. The upcoming JSON-RPC id is still unique.
        call_id = "rpc-%d-%x-%d" % (os.getpid(), id(self), self._id + 1)
        started = time.monotonic()
        _write_tool_call({"id": call_id, "event": "start", "ts": _trace_ts(),
                          "tool": "tools/list", "arguments": {}})
        try:
            names = sorted(t.get("name", "") for t in (self.rpc("tools/list").get("tools") or []))
            _write_tool_call({"id": call_id, "event": "end", "ts": _trace_ts(),
                              "tool": "tools/list", "ok": True,
                              "duration_ms": round((time.monotonic() - started) * 1000, 1),
                              "result": "%d tools" % len(names)})
            return names
        except Exception as exc:
            _write_tool_call({"id": call_id, "event": "end", "ts": _trace_ts(),
                              "tool": "tools/list", "ok": False,
                              "duration_ms": round((time.monotonic() - started) * 1000, 1),
                              "error": self.scrub(str(exc))[:240]})
            raise


def _decode(raw: str) -> Dict[str, Any]:
    """Accept a bare JSON body or an SSE framed body."""
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                return json.loads(chunk)
    raise ValueError("unrecognized response framing")
