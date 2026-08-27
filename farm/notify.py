"""Bounded Slack delivery for unattended Farm Friends reports.

The webhook is a secret and never belongs in the repository, launchd plist, process
arguments, or state ledgers. It is read from ``$FARM_SLACK_WEBHOOK_URL`` or
``~/.config/farm/slack_webhook``. The file must not be accessible by group/other.

This module only delivers text that a caller has already decided to send. Incident
detection and report composition remain pure, independently testable code.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

DEFAULT_WEBHOOK_FILE = Path("~/.config/farm/slack_webhook").expanduser()
SLACK_WEBHOOK_HOSTS = frozenset({"hooks.slack.com", "hooks.slack-gov.com"})
TIMEOUT_SECONDS = 15
ATTRIBUTION = "— sent via Glean Desktop"


class NotificationConfigError(RuntimeError):
    """Local delivery configuration is missing or unsafe."""


class NotificationDeliveryError(RuntimeError):
    """Slack did not confirm delivery."""


def _webhook_file() -> Path:
    return Path(os.environ.get("FARM_SLACK_WEBHOOK_FILE", str(DEFAULT_WEBHOOK_FILE))).expanduser()


def _validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in SLACK_WEBHOOK_HOSTS:
        raise NotificationConfigError("Slack webhook must use an approved HTTPS Slack host")
    if not parsed.path.startswith("/services/") or len(parsed.path.split("/")) < 5:
        raise NotificationConfigError("Slack webhook path is not an incoming-webhook URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise NotificationConfigError("Slack webhook URL contains unsupported components")
    return value


def load_webhook() -> str:
    """Load and validate the secret without ever returning it in an exception."""
    value = os.environ.get("FARM_SLACK_WEBHOOK_URL", "").strip()
    if value:
        return _validate_url(value)

    path = _webhook_file()
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise NotificationConfigError(
            "no Slack webhook configured; write %s with mode 0600" % path
        ) from exc
    if mode & 0o077:
        raise NotificationConfigError("Slack webhook file must have mode 0600")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise NotificationConfigError("Slack webhook file is unreadable") from exc
    return _validate_url(value)


def configured() -> Dict[str, Any]:
    """Safe configuration status for health checks and operator output."""
    try:
        load_webhook()
    except NotificationConfigError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": "Slack incoming webhook configured"}


def with_attribution(message: str) -> str:
    body = str(message or "").strip()
    if body.endswith(ATTRIBUTION):
        return body
    return "%s\n\n%s" % (body, ATTRIBUTION)


def send(
    message: str,
    *,
    webhook: Optional[str] = None,
    opener: Optional[Callable[..., Any]] = None,
    timeout: int = TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Deliver one Slack message; no retries because webhooks are not idempotent."""
    url = _validate_url(webhook) if webhook else load_webhook()
    body = with_attribution(message)
    payload = json.dumps({"text": body}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace").strip()
            status = int(getattr(response, "status", 200) or 200)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        raise NotificationDeliveryError(
            "Slack delivery failed: %s" % exc.__class__.__name__
        ) from exc
    if status < 200 or status >= 300 or raw.lower() != "ok":
        raise NotificationDeliveryError("Slack delivery was not acknowledged")
    return {"ok": True, "status": status, "bytes": len(payload)}
