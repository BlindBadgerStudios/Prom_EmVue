"""Overall state rule for the energy exporters.

This is the shared helper, copied into each exporter repo so the rule cannot
drift. prom_emvue, prom_tigo, and prom_solark must keep the same precedence.

Precedence, first match wins:

1. DOWN — a hard auth failure is latched (HTTP 401/403, login/credential
   rejection, or missing EMPORIA_USERNAME / EMPORIA_PASSWORD). Waiting does
   not clear an auth failure.

2. UP — the last successful poll is within the healthy window and no hard
   auth failure is latched. The healthy window is 2× the poll interval
   (``HEALTHY_WINDOW_MULTIPLIER``). At the default 60s poll that is 2 minutes.

3. DEGRADED — the poll thread is still running and the latest upstream result
   is a transient failure: HTTP 5xx, HTTP 429, timeout, TLS/DNS/connection,
   or any other non-auth error. Backoff is intentional. This stays DEGRADED
   past the hard age cutoff while the process keeps polling. The age phrase
   still reports how long it has been since the last success.

4. DEGRADED — no poll has finished yet, the poll thread is running, and
   process uptime is still inside the hard age cutoff. Startup is not a
   failure.

5. DOWN — there has never been a success and the hard age cutoff has passed
   with no transient failure in progress, or the last success is older than
   the hard age cutoff and the current failure is not a transient one while
   polling. The hard age cutoff is 15 minutes (``HARD_AGE_SECONDS``), not
   3× the poll interval. Three poll intervals at 60s would be 3 minutes and
   would mark a short upstream blip DOWN.

6. DEGRADED — the last success is older than the healthy window but still
   inside the hard age cutoff (the process is between the two windows).

Absolute timestamps on the status page are America/Los_Angeles, labeled PT.
``next_retry`` is always an absolute PT timestamp plus a relative phrase
when a retry is scheduled.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

HEALTHY_WINDOW_MULTIPLIER = 2
HARD_AGE_SECONDS = 15 * 60
BACKOFF_CAP_SECONDS = HARD_AGE_SECONDS
MESSAGE_LIMIT = 200

OVERALL_UP = "UP"
OVERALL_DEGRADED = "DEGRADED"
OVERALL_DOWN = "DOWN"

# Non-auth failures while the poll thread is alive. 429, TLS, and DNS are
# grouped with timeout/5xx: the process should keep backing off, not flip to
# DOWN and invite a restart loop.
TRANSIENT_KINDS = frozenset({"timeout", "http_5xx", "rate_limit", "tls_dns", "other"})

_PACIFIC = ZoneInfo("America/Los_Angeles")

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|authtoken|proxy-authorization|cookie|set-cookie|x-api-key)\b"
    r"\s*[:=]\s*\S+"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access_token|id_token|refresh_token|token|password|passwd|"
    r"api_key|apikey|secret|auth|authtoken|authorization|username|email)=)[^&\s#]+"
)
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")
_USERINFO = re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@")
_JSON_SECRET = re.compile(
    r"(?i)(\"(?:access_token|id_token|refresh_token|password|passwd|api_key|"
    r"authtoken|authorization|secret|username|email)\"\s*:\s*\")([^\"]*)"
)
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")


class HardAuthFailure(Exception):
    """Credential or login rejection. Latches overall state to DOWN."""

    def __init__(self, message: str, code: str = "auth") -> None:
        super().__init__(message)
        self.code = code


def healthy_window_seconds(poll_interval_s: int) -> int:
    return HEALTHY_WINDOW_MULTIPLIER * max(int(poll_interval_s), 0)


def backoff_seconds(consecutive_failures: int, poll_interval_s: int) -> int:
    """Sleep after a failed poll. The first failure waits one poll interval.

    Later failures double until ``BACKOFF_CAP_SECONDS`` (15 minutes). A
    successful poll uses the normal poll interval and does not call this.
    """
    if consecutive_failures <= 0:
        return 0
    interval = max(int(poll_interval_s), 0)
    shift = min(consecutive_failures - 1, 16)
    return int(min(interval * (2**shift), BACKOFF_CAP_SECONDS))


def classify_overall(
    *,
    now: float,
    last_success_at: float | None,
    poll_interval_s: int,
    hard_auth_failure: bool,
    upstream_kind: str,
    process_polling: bool,
    uptime_s: float,
) -> str:
    """Return UP, DEGRADED, or DOWN. See the module docstring for precedence."""
    age = None if last_success_at is None else now - last_success_at
    auth = hard_auth_failure or upstream_kind == "auth"
    if auth:
        return OVERALL_DOWN

    healthy = healthy_window_seconds(poll_interval_s)
    if last_success_at is not None and age is not None and age <= healthy:
        return OVERALL_UP

    if upstream_kind in TRANSIENT_KINDS and process_polling:
        return OVERALL_DEGRADED

    if (
        last_success_at is None
        and process_polling
        and upstream_kind == "none"
        and uptime_s <= HARD_AGE_SECONDS
    ):
        return OVERALL_DEGRADED

    if last_success_at is None or (age is not None and age > HARD_AGE_SECONDS):
        return OVERALL_DOWN

    return OVERALL_DEGRADED


def compact_duration(seconds: float) -> str:
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m"
    hours, rem = divmod(whole, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m"


def age_phrase(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    return f"{compact_duration(seconds)} ago"


def future_phrase(seconds: float) -> str:
    whole = max(0, int(round(seconds)))
    if whole < 60:
        return f"~{whole}s"
    minutes = int(round(whole / 60))
    if minutes < 60:
        return f"~{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return f"~{hours}h"
    return f"~{hours}h {minutes:02d}m"


def backoff_phrase(seconds: int) -> str:
    if seconds <= 0:
        return "None"
    return future_phrase(seconds).removeprefix("~")


def overall_phrase(
    state: str,
    age_seconds: float | None,
    *,
    auth: bool,
    poll_interval_s: int,
) -> str:
    if state == OVERALL_UP:
        return OVERALL_UP
    healthy = healthy_window_seconds(poll_interval_s)
    recent_auth = auth and (age_seconds is None or age_seconds <= healthy)
    if state == OVERALL_DOWN and recent_auth:
        return "DOWN — auth failure"
    if age_seconds is None:
        return f"{state} — no success yet"
    return f"{state} — no success in {compact_duration(age_seconds)}"


def format_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_pt(timestamp: float | None) -> str | None:
    """Absolute Pacific Time, labeled PT (PST or PDT as the calendar requires)."""
    if timestamp is None:
        return None
    local = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(_PACIFIC)
    return f"{local.strftime('%b')} {local.day}, {local.strftime('%Y %H:%M:%S')} PT"


def absolute_and_relative(absolute: str | None, relative: str | None) -> str:
    if not absolute:
        return "n/a"
    if relative:
        return f"{absolute} · {relative}"
    return absolute


def redact(text: str | None, *, secrets: tuple[str, ...] = (), limit: int = MESSAGE_LIMIT) -> str:
    """Drop tokens, auth headers, emails, and query secrets. Truncate long bodies.

    ``secrets`` are extra literal values (a password, a username) that must
    not survive into status or logs. Values shorter than 4 characters are
    not substring-replaced; callers must not interpolate those into messages.
    """
    if not text:
        return ""
    cleaned = str(text)
    cleaned = _USERINFO.sub(r"\1", cleaned)
    cleaned = _QUERY_SECRET.sub(r"\1[redacted]", cleaned)
    cleaned = _URL_QUERY.sub(r"\1", cleaned)
    cleaned = _JSON_SECRET.sub(r"\1[redacted]", cleaned)
    cleaned = _AUTH_HEADER.sub(lambda match: f"{match.group(1)}: [redacted]", cleaned)
    cleaned = _BEARER.sub("Bearer [redacted]", cleaned)
    cleaned = _JWT.sub("[redacted]", cleaned)
    cleaned = _EMAIL.sub("[redacted-email]", cleaned)
    for secret in secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[redacted]")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def _network_kind(text: str) -> tuple[str, str, str] | None:
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout", "timeout", "Timeout · Emporia"
    if any(
        token in lowered
        for token in (
            "certificate",
            "tls",
            "ssl",
            "name or service not known",
            "temporary failure in name resolution",
            "nodename nor servname",
            "gaierror",
            "name resolution",
        )
    ):
        return "tls_dns", "tls", "TLS or DNS error · Emporia"
    return None


def classify_failure(exc: BaseException) -> tuple[str, str, str]:
    """Map an exception to ``(kind, code, message)`` with no response body.

    Messages are short fixed strings whenever the raw exception could contain
    a URL, token, or account email. Callers still run ``redact`` before display.
    """
    if isinstance(exc, HardAuthFailure):
        return "auth", exc.code, str(exc)

    type_name = type(exc).__name__
    if type_name == "NotAuthorizedException":
        return "auth", "login", "Login failed · credentials"
    if type_name == "TokenVerificationException":
        return "other", "token_time", "Token is not yet valid; check host clock (NTP)"

    if isinstance(exc, requests.HTTPError):
        response = exc.response
        status = int(response.status_code) if response is not None else 0
        reason = (response.reason or "") if response is not None else ""
        summary = f"{status} {reason}".strip()
        if status in (401, 403):
            return "auth", str(status), summary or "Login failed · credentials"
        if status == 429:
            return "rate_limit", "429", "429 Too Many Requests"
        if 500 <= status <= 599:
            return "http_5xx", str(status), summary or "Upstream server error"
        return "other", str(status) if status else "http", summary or "HTTP error"

    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return "timeout", "timeout", "Timeout · Emporia"
    if isinstance(exc, requests.exceptions.SSLError):
        return "tls_dns", "tls", "TLS error · Emporia"
    if isinstance(exc, requests.ConnectionError):
        detected = _network_kind(str(exc))
        if detected is not None and detected[0] == "tls_dns":
            return detected
        return "timeout", "connect", "Connection failed · Emporia"

    text = str(exc)
    lowered = text.lower()
    if any(
        token in lowered
        for token in (
            "not authorized",
            "incorrect username",
            "not authenticated",
            "invalid credentials",
        )
    ):
        return "auth", "login", "Login failed · credentials"

    detected = _network_kind(text)
    if detected is not None:
        return detected

    safe = redact(f"{type_name}: {text}")
    return "other", type_name or "error", safe or type_name or "error"


def hint_for(
    *,
    kind: str,
    code: str,
    overall: str,
    series_count: int,
    channel_count: int | None,
) -> str:
    """Operator hint. Wording follows the signed-off catalogue where it applies."""
    if kind == "auth" or str(code) in {"401", "403", "config", "login"}:
        return (
            "401/403: check API token/cred mount (EMPORIA_USERNAME / EMPORIA_PASSWORD); "
            "do not paste into chat. Restart once after fixing env — not repeatedly."
        )
    if kind == "timeout":
        return "Timeout: check docker3 outbound / upstream API. Do not restart-loop."
    if kind == "rate_limit" or str(code) == "429":
        return "429: rate limit; backoff is intentional. Do not restart-loop."
    if kind == "tls_dns":
        return "TLS/DNS: check the network path from docker3. Do not restart-loop."
    if kind == "other" and code == "token_time":
        return (
            "Token time check failed. Check host NTP sync. "
            "Do not paste tokens into chat. Do not restart-loop."
        )
    if overall == OVERALL_UP and (series_count == 0 or channel_count == 0):
        return (
            "Zero or absent metrics in daylight with UP means the upstream returned empty, "
            "not that the scrape is down."
        )
    if overall == OVERALL_UP:
        return "No action needed."
    if kind == "none":
        return "Waiting for the first poll. Do not restart-loop."
    return "Upstream error while polling. Do not restart-loop; backoff is intentional."


def configured_secrets() -> tuple[str, ...]:
    """Credential values that must never be copied into status output or logs."""
    import os

    values = []
    for key in ("EMPORIA_USERNAME", "EMPORIA_PASSWORD"):
        value = os.getenv(key)
        if value and len(value) >= 4:
            values.append(value)
    return tuple(values)


def read_build_sha() -> str | None:
    """Return a stamped git SHA if the image already provides one. Otherwise omit it."""
    import os

    for key in ("GIT_SHA", "BUILD_SHA", "SOURCE_COMMIT"):
        value = os.getenv(key, "").strip()
        if _SHA.fullmatch(value):
            return value
    return None


def access_note(port: int) -> str:
    return (
        f"Reachable on docker3 as http://127.0.0.1:{int(port)}/status "
        "via lab SSH hop (no public listen)."
    )
