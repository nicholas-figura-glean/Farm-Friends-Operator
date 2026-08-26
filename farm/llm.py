"""Headless model access via the Glean Desktop llm_proxy gateway.

This is the ONLY module in the project that talks to a language model, and it
exists so the author and research agents can rewrite code without a human at a
keyboard. Everything else stays deterministic Python.

Why this gateway and not a CLI: Glean Desktop already holds an OAuth token for
this machine with an `llm_proxy` scope and an OpenAI-compatible endpoint. Reusing
it means no second credential to provision, rotate, or leak, and no Node runtime.
The alternative considered and rejected was installing `cursor-agent`, which
would have needed its own API key and brought its own agentic loop.

Secret handling follows farm/mcp.py: the bearer token is a secret. It is read
from the auth file at call time, never cached to our own state, never logged, and
scrubbed from every exception message.

DORMANCY IS NOT AN ERROR. The token's `refresh` field is `__desktop_managed__`,
so only Glean Desktop can renew it. If Desktop stays closed past expiry the
gateway becomes unavailable, and that is a normal operating state: callers must
degrade to deterministic behaviour and escalate, never crash the farm. A farm
that stops feeding because a model was unreachable would lose the game, which is
the one outcome this whole system exists to prevent.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import rules, tokens

DEFAULT_AUTH_FILE = os.path.expanduser("~/.glean/agent/auth.json")
REQUIRED_SCOPE = "llm_proxy"
TIMEOUT = 180
RETRIES = 3
BACKOFF = 2.0
# Refuse to start work that cannot finish: a rewrite needs enough remaining token
# lifetime to also run the gate matrix and publish, or it will strand a half-built
# release. 10 minutes is generous against a ~4 minute author pass.
MIN_TOKEN_LIFETIME_SECONDS = 600


class Dormant(RuntimeError):
    """The gateway is unavailable for a benign, expected reason.

    Callers must treat this as "do nothing this pass", not as a failure.
    """


class GatewayError(RuntimeError):
    """The gateway was reachable but the request failed, endpoint scrubbed."""


def _auth_path() -> str:
    return os.environ.get("FARM_LLM_AUTH_FILE", DEFAULT_AUTH_FILE)


def _scrub(text: str, secret: str = "") -> str:
    """Remove the bearer token and host from any text before it is surfaced."""
    if not text:
        return text
    out = str(text)
    if secret and len(secret) > 12:
        out = out.replace(secret, "<token>")
        # A truncated token in a traceback is still a leak.
        out = out.replace(secret[:40], "<token>")
    return out


def _read_auth() -> Dict[str, Any]:
    """Load the Glean credential block, or explain why we are dormant."""
    path = _auth_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        raise Dormant("no Glean auth file at %s; is Desktop installed?" % path)
    except (OSError, ValueError) as exc:
        raise Dormant("unreadable Glean auth file: %s" % exc.__class__.__name__)

    block = (payload or {}).get("glean")
    if not isinstance(block, dict):
        raise Dormant("auth file has no glean credential block")

    access = str(block.get("access") or "").strip()
    base = str(block.get("gatewayBaseUrl") or "").strip()
    if not access:
        raise Dormant("no access token; sign in to Glean Desktop")
    if not base:
        raise Dormant("no gatewayBaseUrl in the credential block")
    if not base.startswith("https://"):
        raise Dormant("gateway must be https")

    scopes = str(block.get("scopes") or "").split()
    if REQUIRED_SCOPE not in scopes:
        raise Dormant("token lacks the %s scope (has: %s)" % (REQUIRED_SCOPE, " ".join(scopes) or "none"))

    remaining = _remaining_seconds(block.get("expires"))
    if remaining is not None and remaining <= 0:
        raise Dormant("token expired; open Glean Desktop to refresh it")

    return {
        "access": access,
        "base": base.rstrip("/"),
        "expires": block.get("expires"),
        "remaining_seconds": remaining,
        "scopes": scopes,
    }


def _remaining_seconds(expires: Any) -> Optional[float]:
    """Milliseconds-since-epoch to seconds remaining, or None if unstated."""
    if not isinstance(expires, (int, float)):
        return None
    when = datetime.fromtimestamp(float(expires) / 1000.0, timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


def availability() -> Dict[str, Any]:
    """Non-raising probe of whether authoring can run this pass.

    Makes no network call: this is consulted every supervisor pass and must stay
    free. A reachable-but-broken gateway surfaces later, on the real request.
    """
    try:
        auth = _read_auth()
    except Dormant as exc:
        return {"available": False, "reason": str(exc), "dormant": True}

    remaining = auth.get("remaining_seconds")
    if remaining is not None and remaining < MIN_TOKEN_LIFETIME_SECONDS:
        return {
            "available": False,
            "dormant": True,
            "reason": "token expires in %.0fs, below the %ds working floor"
            % (remaining, MIN_TOKEN_LIFETIME_SECONDS),
            "remaining_seconds": round(remaining, 1),
        }
    return {
        "available": True,
        "dormant": False,
        "reason": "ok",
        "remaining_seconds": round(remaining, 1) if remaining is not None else None,
        "expires_ts": (
            datetime.fromtimestamp(float(auth["expires"]) / 1000.0, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if isinstance(auth.get("expires"), (int, float))
            else None
        ),
    }


def _post(auth: Dict[str, Any], path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST JSON to the gateway with bounded retries and a scrubbed failure."""
    url = "%s/%s" % (auth["base"], path.lstrip("/"))
    body = json.dumps(payload).encode("utf-8")
    context = ssl.create_default_context()
    secret = auth["access"]
    last: Optional[BaseException] = None

    for attempt in range(RETRIES):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": "Bearer " + secret,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            # An auth rejection is dormancy, not a retryable fault: the token is
            # Desktop-managed and will not improve by trying again.
            if exc.code in (401, 403):
                raise Dormant(
                    _scrub("gateway rejected the token (HTTP %d); reopen Glean Desktop" % exc.code, secret)
                )
            last = exc
            # 4xx other than rate limiting is our bug; do not burn retries on it.
            if 400 <= exc.code < 500 and exc.code != 429:
                raise GatewayError(_scrub("gateway HTTP %d: %s" % (exc.code, detail), secret))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
        if attempt < RETRIES - 1:
            time.sleep(BACKOFF ** (attempt + 1))

    raise GatewayError(_scrub("gateway unreachable after %d tries: %r" % (RETRIES, last), secret))


def models() -> List[str]:
    """List gateway model ids. Used by --llm-status to prove reachability."""
    auth = _read_auth()
    url = "%s/models" % auth["base"]
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + auth["access"], "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=ssl.create_default_context()) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise Dormant("gateway rejected the token (HTTP %d)" % exc.code)
        raise GatewayError(_scrub("model list failed: HTTP %d" % exc.code, auth["access"]))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GatewayError(_scrub("model list failed: %r" % exc, auth["access"]))
    return sorted(str(entry.get("id") or "") for entry in (payload.get("data") or []))


def pick_model(preferred: Optional[str] = None) -> str:
    """Resolve a usable model id, falling back down the configured chain.

    The gateway's roster changes without notice, so a hard-coded id is a latent
    outage. Verify against the live list and degrade in preference order.
    """
    wanted = [preferred] if preferred else []
    wanted += list(rules.LLM_AUTHOR_MODELS)
    available = set(models())
    for candidate in wanted:
        if candidate and candidate in available:
            return candidate
    if not available:
        raise GatewayError("gateway advertised no models")
    # Nothing preferred is present: refuse rather than silently authoring code
    # with an unvetted model.
    raise GatewayError(
        "no preferred model available; gateway offers %d others" % len(available)
    )


def complete(
    system: str,
    user: str,
    model: Optional[str] = None,
    max_output_tokens: int = 32_000,
    run: Optional[int] = None,
    note: str = "",
    actor: str = "author",
    purpose: str = "reasoning",
) -> Dict[str, Any]:
    """One turn against the gateway, with real usage booked to the token ledger.

    The gateway speaks the OpenAI **Responses** API, not Chat Completions:
    `/chat/completions` and `/completions` both return 404. So `system` maps to
    `instructions` and `user` to `input`.

    Two measured properties of this endpoint drive the shape of this function:

    1. **Temperature is not ours to set.** The gpt-5.x reasoning models pin it to
       1.0 and echo that back regardless of what we send, so we do not send it.
       Reproducibility comes from a tightly specified prompt and from the gate
       matrix that verifies the output, never from sampling settings.
    2. **Reasoning tokens consume the output budget.** A trivial "OK" reply spent
       16 reasoning tokens against 23 output tokens. A patch budget that only
       counts the visible diff will truncate mid-file, so `max_output_tokens`
       defaults high and `truncated` is always checked by callers.
    """
    auth = _read_auth()
    chosen = model or pick_model()
    payload = {
        "model": chosen,
        "instructions": system,
        "input": user,
        "max_output_tokens": int(max_output_tokens),
    }
    started = time.monotonic()
    response = _post(auth, "responses", payload)

    status = response.get("status")
    error = response.get("error")
    if error:
        raise GatewayError(_scrub("gateway reported an error: %s" % json.dumps(error)[:300], auth["access"]))

    text = _output_text(response)
    # `incomplete` means the budget ran out mid-answer. A half-written patch is
    # more dangerous than no patch, so surface it rather than letting a caller
    # apply a truncated diff.
    truncated = status == "incomplete"
    incomplete_reason = (response.get("incomplete_details") or {}).get("reason")

    usage = response.get("usage") or {}
    tokens_in = int(usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output_tokens") or 0)
    reasoning = int((usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
    estimated = False
    if not tokens_in:
        tokens_in = tokens.estimate_tokens(system + user)
        estimated = True
    if not tokens_out:
        tokens_out = tokens.estimate_tokens(text)
        estimated = True

    ledger_kind = actor if actor in {"author", "research", "test"} else "other_llm"
    tokens.record(
        ledger_kind,
        run,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        note=(
            "%s purpose=%s model=%s reasoning=%d%s%s"
            % (note, purpose, chosen, reasoning, " estimated" if estimated else "", " TRUNCATED" if truncated else "")
        )[:200],
    )

    return {
        "text": text,
        "model": chosen,
        "status": status,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "reasoning_tokens": reasoning,
        "estimated_usage": estimated,
        "duration_seconds": round(time.monotonic() - started, 2),
        "truncated": truncated,
        "incomplete_reason": incomplete_reason,
        "actor": ledger_kind,
        "purpose": purpose,
    }


def _output_text(response: Dict[str, Any]) -> str:
    """Concatenate assistant text from a Responses payload.

    The convenience `output_text` field is an SDK-side helper and comes back null
    from this gateway, so the output list has to be walked. Reasoning items carry
    no `content` and are skipped: only `output_text` blocks are real answer text.
    """
    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    chunks: List[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "output_text":
                chunks.append(str(block.get("text") or ""))
    return "".join(chunks)
