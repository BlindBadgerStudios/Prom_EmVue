"""Build the /status document and serve it beside unchanged /metrics output."""

from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import dataclass
from html import escape
from socketserver import ThreadingMixIn
from typing import Callable
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import make_wsgi_app
from prometheus_client.registry import Collector

from app import __version__
from app.status_rules import (
    absolute_and_relative,
    access_note,
    age_phrase,
    backoff_phrase,
    backoff_seconds,
    classify_overall,
    compact_duration,
    format_iso,
    format_pt,
    future_phrase,
    hint_for,
    overall_phrase,
    configured_secrets,
    read_build_sha,
    redact,
)

EXPORTER_NAME = "prom_emvue"
CONNECTION_MODE = "Emporia Vue cloud API"

HINT_NOTES = (
    "401/403 → check API token/cred mount; do not paste into chat",
    "timeout → check docker3 outbound / upstream API",
    "429 → rate limit; backoff is intentional",
    "TLS/DNS → network path from docker3",
    "zero/absent metrics in daylight with UP → upstream empty, not scrape down",
    "Do not restart-loop",
)


def unknown_extras() -> dict:
    return {
        "device_count": None,
        "devices": [],
        "channel_count": None,
        "last_known": False,
    }


def build_device_extras(devices: list[dict], channel_count: int | None) -> dict:
    """Name and connected flag only. No gid, zip, lat/lon, or account fields."""
    cleaned = []
    for device in devices:
        cleaned.append(
            {
                "name": redact(str(device.get("name") or "unnamed")),
                "connected": bool(device.get("connected")),
            }
        )
    cleaned.sort(key=lambda item: item["name"].casefold())
    return {
        "device_count": len(cleaned),
        "devices": cleaned,
        "channel_count": channel_count,
        "last_known": False,
    }


@dataclass
class StatusSnapshot:
    now: float
    uptime_s: float
    poll_interval_s: int
    last_success_at: float | None
    consecutive_failures: int
    backoff_s: int
    next_attempt_at: float | None
    hard_auth_failure: bool
    upstream_kind: str
    upstream_code: str
    upstream_message: str
    last_error_code: str | None
    last_error_message: str | None
    extras: dict
    process_polling: bool
    listen_port: int


class ExporterState:
    """Thread-safe poll bookkeeping. The status page reads a snapshot."""

    def __init__(self, *, poll_interval_s: int, listen_port: int) -> None:
        self.poll_interval_s = poll_interval_s
        self.listen_port = listen_port
        self.started_monotonic = time.monotonic()
        self._lock = threading.Lock()
        self.last_success_at: float | None = None
        self.consecutive_failures = 0
        self.backoff_s = 0
        self.next_attempt_at: float | None = None
        self.hard_auth_failure = False
        self.upstream_kind = "none"
        self.upstream_code = ""
        self.upstream_message = ""
        self.last_error_code: str | None = None
        self.last_error_message: str | None = None
        self.extras = unknown_extras()
        self.poll_thread: threading.Thread | None = None

    def mark_success(self, now: float, extras: dict) -> int:
        with self._lock:
            self.last_success_at = now
            self.consecutive_failures = 0
            self.backoff_s = 0
            self.hard_auth_failure = False
            self.upstream_kind = "ok"
            self.upstream_code = "200"
            self.upstream_message = "200 OK · devices"
            self.last_error_code = None
            self.last_error_message = None
            self.extras = copy.deepcopy(extras)
            self.extras["last_known"] = False
            return self.poll_interval_s

    def mark_failure(self, _now: float, *, kind: str, code: str, message: str) -> int:
        with self._lock:
            self.consecutive_failures += 1
            self.backoff_s = backoff_seconds(self.consecutive_failures, self.poll_interval_s)
            self.hard_auth_failure = kind == "auth"
            self.upstream_kind = kind
            self.upstream_code = code
            self.upstream_message = message
            self.last_error_code = code
            self.last_error_message = message
            if self.last_success_at is not None:
                self.extras["last_known"] = True
            return self.backoff_s

    def schedule_next(self, next_attempt_at: float) -> None:
        with self._lock:
            self.next_attempt_at = next_attempt_at

    def snapshot(self, *, now: float) -> StatusSnapshot:
        with self._lock:
            thread = self.poll_thread
            polling = thread is not None and thread.is_alive()
            return StatusSnapshot(
                now=now,
                uptime_s=max(0.0, time.monotonic() - self.started_monotonic),
                poll_interval_s=self.poll_interval_s,
                last_success_at=self.last_success_at,
                consecutive_failures=self.consecutive_failures,
                backoff_s=self.backoff_s,
                next_attempt_at=self.next_attempt_at,
                hard_auth_failure=self.hard_auth_failure,
                upstream_kind=self.upstream_kind,
                upstream_code=self.upstream_code,
                upstream_message=self.upstream_message,
                last_error_code=self.last_error_code,
                last_error_message=self.last_error_message,
                extras=copy.deepcopy(self.extras),
                process_polling=polling,
                listen_port=self.listen_port,
            )


def _age_seconds(snapshot: StatusSnapshot) -> float | None:
    if snapshot.last_success_at is None:
        return None
    return max(0.0, snapshot.now - snapshot.last_success_at)


def build_status(
    snapshot: StatusSnapshot,
    *,
    exporter_up: int,
    series_count: int,
    version: str = __version__,
    build_sha: str | None = None,
    secrets: tuple[str, ...] = (),
) -> dict:
    """One document for HTML and JSON. Free text is redacted again here."""
    age = _age_seconds(snapshot)
    auth = snapshot.hard_auth_failure or snapshot.upstream_kind == "auth"
    overall = classify_overall(
        now=snapshot.now,
        last_success_at=snapshot.last_success_at,
        poll_interval_s=snapshot.poll_interval_s,
        hard_auth_failure=snapshot.hard_auth_failure,
        upstream_kind=snapshot.upstream_kind,
        process_polling=snapshot.process_polling,
        uptime_s=snapshot.uptime_s,
    )
    extras = copy.deepcopy(snapshot.extras)
    for device in extras.get("devices") or []:
        device["name"] = redact(str(device.get("name") or ""), secrets=secrets)
        device["connected"] = bool(device.get("connected"))

    if snapshot.upstream_kind == "none":
        upstream = {"code": "", "message": "No poll yet"}
    elif snapshot.upstream_kind == "ok":
        upstream = {"code": "200", "message": "200 OK · devices"}
    else:
        upstream = {
            "code": redact(snapshot.upstream_code, secrets=secrets, limit=40),
            "message": redact(snapshot.upstream_message, secrets=secrets),
        }

    if snapshot.last_error_code is None and snapshot.last_error_message is None:
        last_error = None
    else:
        last_error = {
            "code": redact(snapshot.last_error_code or "", secrets=secrets, limit=40),
            "message": redact(snapshot.last_error_message or "", secrets=secrets),
        }

    retrying = snapshot.consecutive_failures > 0 and snapshot.next_attempt_at is not None
    if snapshot.next_attempt_at is None:
        next_poll_relative = "pending"
        next_poll_at = None
    else:
        next_poll_at = snapshot.next_attempt_at
        if retrying:
            next_poll_relative = "held for backoff"
        else:
            next_poll_relative = future_phrase(snapshot.next_attempt_at - snapshot.now)

    if retrying:
        next_retry_relative = future_phrase(snapshot.next_attempt_at - snapshot.now)
        next_retry_at = snapshot.next_attempt_at
    else:
        next_retry_relative = None
        next_retry_at = None

    next_retry_pt = format_pt(next_retry_at)
    channel_count = extras.get("channel_count")
    if snapshot.process_polling and overall == "UP":
        process = "Healthy"
    elif snapshot.process_polling:
        process = "Running · poll thread alive"
    else:
        process = "Poll thread stopped"

    phrase = overall_phrase(
        overall,
        age,
        auth=auth,
        poll_interval_s=snapshot.poll_interval_s,
    )
    return {
        "exporter": EXPORTER_NAME,
        "version": version,
        "build_sha": build_sha,
        "mode": CONNECTION_MODE,
        "overall": overall,
        "overall_phrase": phrase,
        "last_success_at": format_iso(snapshot.last_success_at),
        "last_success_pt": format_pt(snapshot.last_success_at),
        "age_seconds": None if age is None else int(age),
        "age_phrase": age_phrase(age),
        "poll_interval_s": snapshot.poll_interval_s,
        "next_poll_at": format_iso(next_poll_at),
        "next_poll_pt": format_pt(next_poll_at),
        "next_poll_relative": next_poll_relative,
        "upstream": upstream,
        "failures": snapshot.consecutive_failures,
        "backoff_s": snapshot.backoff_s if snapshot.consecutive_failures else 0,
        "backoff": backoff_phrase(snapshot.backoff_s if snapshot.consecutive_failures else 0),
        "next_retry_at": format_iso(next_retry_at),
        "next_retry_pt": next_retry_pt,
        "next_retry_relative": next_retry_relative,
        "next_retry": absolute_and_relative(next_retry_pt, next_retry_relative),
        "last_error": last_error,
        "hint": hint_for(
            kind=snapshot.upstream_kind,
            code=snapshot.upstream_code,
            overall=overall,
            series_count=series_count,
            channel_count=channel_count if isinstance(channel_count, int) else None,
        ),
        "exporter_up": int(exporter_up),
        "series_count": int(series_count),
        "series_stale": overall != "UP",
        "uptime_s": int(snapshot.uptime_s),
        "uptime_phrase": compact_duration(snapshot.uptime_s),
        "process_healthy": bool(snapshot.process_polling),
        "process": process,
        "extras": extras,
        "access_note": access_note(snapshot.listen_port),
    }


def _badge(phrase: str, overall: str) -> str:
    css = {"UP": "up", "DEGRADED": "degraded", "DOWN": "down"}.get(overall, "down")
    return f'<strong class="{css}">{escape(phrase)}</strong>'


def _time_cell(iso: str | None, pt: str | None, relative: str | None) -> str:
    parts: list[str] = []
    if pt:
        if iso:
            parts.append(f'<time datetime="{escape(iso)}">{escape(pt)}</time>')
        else:
            parts.append(escape(pt))
    if relative:
        parts.append(escape(relative))
    return " · ".join(parts) if parts else "n/a"


def _rows(pairs: list[tuple[str, str]]) -> str:
    body = []
    for label, value in pairs:
        body.append(f"<tr><th>{escape(label)}</th><td>{value}</td></tr>")
    return "<table>" + "".join(body) + "</table>"


def _device_summary(extras: dict) -> str:
    if extras.get("device_count") is None:
        return "unknown until next success"
    devices = extras.get("devices") or []
    if not devices:
        return "0 devices"
    prefix = "last: " if extras.get("last_known") else ""
    parts = []
    for device in devices:
        flag = "connected" if device.get("connected") else "not connected"
        parts.append(f"{device.get('name') or 'unnamed'} ({flag})")
    return prefix + ", ".join(parts)


def _channel_summary(extras: dict) -> str:
    count = extras.get("channel_count")
    if count is None:
        return "unknown"
    if extras.get("last_known"):
        return f"last: {count}"
    return str(count)


def _series_summary(payload: dict) -> str:
    count = payload["series_count"]
    channels = _channel_summary(payload["extras"])
    if payload["series_stale"]:
        return f"stale · ~{count} · channels {channels}"
    return f"~{count} · channels {channels}"


def render_html(payload: dict) -> str:
    extras = payload["extras"]
    error = payload["last_error"]
    error_text = "None" if not error else f"{error.get('code') or ''} · {error.get('message') or ''}".strip(" ·")
    upstream = payload["upstream"]
    upstream_text = f"{upstream.get('code') or ''} {upstream.get('message') or ''}".strip()
    version_bits = f"{payload['exporter']} {payload['version']}"
    if payload.get("build_sha"):
        version_bits += f" · {payload['build_sha']}"
    failures = payload["failures"]
    failure_text = "0" if not failures else f"{failures} consecutive"
    known = "Last-known snapshot from the previous successful poll." if extras.get("last_known") else ""
    device_items = []
    for device in extras.get("devices") or []:
        flag = "connected" if device.get("connected") else "not connected"
        device_items.append(f"<li>{escape(str(device.get('name') or 'unnamed'))} — {escape(flag)}</li>")
    if extras.get("device_count") is None:
        device_html = "<p>unknown until next success</p>"
    elif not device_items:
        device_html = "<p>0 devices</p>"
    else:
        device_html = "<ul>" + "".join(device_items) + "</ul>"
    notes = "".join(f"<li>{escape(note)}</li>" for note in HINT_NOTES)
    overview = _rows(
        [
            ("Overall", _badge(payload["overall_phrase"], payload["overall"])),
            ("Exporter", escape(version_bits)),
            ("Mode", escape(payload["mode"])),
            (
                "Last success",
                _time_cell(payload["last_success_at"], payload["last_success_pt"], payload["age_phrase"]),
            ),
            (
                "Poll",
                escape(f"every {payload['poll_interval_s']}s · next {payload['next_poll_relative']}")
                + (
                    f" · {_time_cell(payload['next_poll_at'], payload['next_poll_pt'], None)}"
                    if payload.get("next_poll_pt")
                    else ""
                ),
            ),
            ("Upstream", escape(upstream_text or "n/a")),
            ("Failures", escape(failure_text)),
            ("Backoff", escape(str(payload["backoff"]))),
            ("Next retry", escape(payload["next_retry"])),
        ]
    )
    error_table = _rows(
        [
            ("Last error", escape(error_text)),
            ("Hint", escape(payload["hint"])),
        ]
    )
    series = _rows(
        [
            ("exporter_up", escape(str(payload["exporter_up"]))),
            ("Series", escape(_series_summary(payload))),
            ("Uptime", escape(str(payload["uptime_phrase"]))),
            ("Process", escape(payload["process"])),
        ]
    )
    extra_table = _rows(
        [
            ("Devices", escape(_device_summary(extras))),
            ("Device count", escape("unknown" if extras.get("device_count") is None else str(extras["device_count"]))),
            ("Channels", escape(_channel_summary(extras))),
        ]
    )
    known_html = f"<p class=\"note\">{escape(known)}</p>" if known else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{escape(payload["exporter"])} /status</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; font: 16px/1.45 "Segoe UI", Helvetica, Arial, sans-serif; color: #1c1c1c; background: #f6f5f2; }}
  main {{ max-width: 980px; margin: 0 auto; padding: 1.5rem 1rem 3rem; }}
  h1 {{ font-size: 1.5rem; margin: 0.4rem 0 0.2rem; }}
  h2 {{ font-size: 1.05rem; margin: 0 0 0.6rem; }}
  a {{ color: #0b4f8a; }}
  .access {{ background: #e7eef5; border-left: 4px solid #0b4f8a; padding: 0.75rem 1rem; margin: 0; }}
  .sub {{ color: #444; margin-top: 0; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; }}
  section {{ background: #fff; border: 1px solid #ddd; padding: 0.9rem 1rem 1rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; vertical-align: top; width: 9rem; color: #444; font-weight: 600; padding: 0.28rem 0.6rem 0.28rem 0; }}
  td {{ padding: 0.28rem 0; }}
  .up {{ color: #146c2e; }}
  .degraded {{ color: #8a5a00; }}
  .down {{ color: #9b1c1c; }}
  .note {{ color: #444; margin: 0.4rem 0 0; }}
  ul {{ margin: 0.3rem 0 0; padding-left: 1.2rem; }}
  @media (max-width: 720px) {{
    .grid {{ grid-template-columns: 1fr; }}
    th {{ width: 7.5rem; }}
  }}
</style>
</head>
<body>
<main>
  <p class="access">{escape(payload["access_note"])}</p>
  <h1>{escape(payload["exporter"])} · /status</h1>
  <p class="sub">{escape(version_bits)} · {escape(payload["mode"])} · read-only</p>
  <p class="sub"><a href="/status?format=json">JSON</a> · <a href="/metrics">metrics</a></p>
  <div class="grid">
    <section>
      <h2>Overview</h2>
      {overview}
    </section>
    <section>
      <h2>Error and hint</h2>
      {error_table}
    </section>
    <section>
      <h2>Series and process</h2>
      {series}
    </section>
    <section>
      <h2>Exporter extras</h2>
      {extra_table}
      {device_html}
      {known_html}
    </section>
  </div>
  <section style="margin-top: 1rem;">
    <h2>Notes</h2>
    <ul>{notes}</ul>
  </section>
</main>
</body>
</html>
"""


def render_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def emporia_series_count(registry: Collector) -> int:
    count = 0
    for metric in registry.collect():
        for sample in metric.samples:
            name = sample.name
            if name.startswith("emporia_") and not name.endswith("_created"):
                count += 1
    return count


def exporter_up_value(registry: Collector) -> int:
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name == "emporia_exporter_up":
                return 1 if float(sample.value) >= 1 else 0
    return 0


def make_status_app(state: ExporterState, registry: Collector, *, listen_port: int) -> Callable:
    """Route GET /status. Every other path is the stock Prometheus WSGI app."""
    metrics_app = make_wsgi_app(registry)

    def app(environ, start_response):
        path = (environ.get("PATH_INFO") or "").rstrip("/") or "/"
        method = environ.get("REQUEST_METHOD", "GET")
        if method == "GET" and path == "/status":
            params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)
            fmt = (params.get("format") or [""])[0].strip().lower()
            snapshot = state.snapshot(now=time.time())
            snapshot.listen_port = listen_port
            payload = build_status(
                snapshot,
                exporter_up=exporter_up_value(registry),
                series_count=emporia_series_count(registry),
                version=__version__,
                build_sha=read_build_sha(),
                secrets=configured_secrets(),
            )
            if fmt == "json":
                body = render_json(payload).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                body = render_html(payload).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            start_response(
                "200 OK",
                [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                    ("X-Content-Type-Options", "nosniff"),
                ],
            )
            return [body]
        return metrics_app(environ, start_response)

    return app


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        return


def serve_forever(port: int, application) -> WSGIServer:
    """Bind the same way start_http_server does: all interfaces, daemon thread."""
    httpd = make_server(
        "0.0.0.0",
        port,
        application,
        server_class=_ThreadingWSGIServer,
        handler_class=_QuietHandler,
    )
    thread = threading.Thread(target=httpd.serve_forever, name="exporter-http", daemon=True)
    thread.start()
    return httpd
