"""Status rule, redaction, JSON shape, and /status routing."""

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from wsgiref.simple_server import make_server

import requests
from prometheus_client import CollectorRegistry, Gauge, generate_latest

from app.status_page import (
    ExporterState,
    StatusSnapshot,
    build_device_extras,
    build_status,
    make_status_app,
    render_html,
)
from app.status_rules import (
    HARD_AGE_SECONDS,
    access_note,
    backoff_seconds,
    classify_failure,
    classify_overall,
    format_pt,
    hint_for,
    redact,
)


NOW = datetime(2026, 10, 27, 17, 32, 0, tzinfo=timezone.utc).timestamp()


def _classify(**overrides):
    params = dict(
        now=NOW,
        last_success_at=NOW - 30,
        poll_interval_s=60,
        hard_auth_failure=False,
        upstream_kind="ok",
        process_polling=True,
        uptime_s=3600,
    )
    params.update(overrides)
    return classify_overall(**params)


def test_overall_rule_precedence():
    assert _classify() == "UP"
    # Exactly the healthy window (2 × 60s) is still UP.
    assert _classify(last_success_at=NOW - 120) == "UP"
    # Just outside the window, timeout, still polling → DEGRADED.
    assert _classify(last_success_at=NOW - 121, upstream_kind="timeout") == "DEGRADED"
    # Past the 15 minute hard cutoff, 5xx/timeout while polling stays DEGRADED.
    assert _classify(last_success_at=NOW - 39 * 60, upstream_kind="http_5xx") == "DEGRADED"
    assert _classify(last_success_at=NOW - 39 * 60, upstream_kind="timeout") == "DEGRADED"
    # Auth is DOWN even when the last success is still inside the healthy window.
    assert _classify(hard_auth_failure=True) == "DOWN"
    assert _classify(last_success_at=NOW - 17 * 60, upstream_kind="auth") == "DOWN"
    # 429 and TLS follow the timeout path while the poll thread is alive.
    assert _classify(last_success_at=NOW - 10 * 60, upstream_kind="rate_limit") == "DEGRADED"
    assert _classify(last_success_at=NOW - 10 * 60, upstream_kind="tls_dns") == "DEGRADED"
    # Between the healthy window and the hard cutoff with no fresh error.
    assert _classify(last_success_at=NOW - 10 * 60, upstream_kind="none") == "DEGRADED"
    # Silent stall past 15 minutes is DOWN. Exactly 15 minutes is still inside.
    assert _classify(last_success_at=NOW - HARD_AGE_SECONDS, upstream_kind="none") == "DEGRADED"
    assert _classify(last_success_at=NOW - HARD_AGE_SECONDS - 1, upstream_kind="none") == "DOWN"
    # Transient failure does not keep DEGRADED once the poll thread has stopped.
    assert (
        _classify(
            last_success_at=NOW - 39 * 60,
            upstream_kind="timeout",
            process_polling=False,
        )
        == "DOWN"
    )
    # Startup: no poll finished yet, thread alive, inside the hard cutoff.
    assert _classify(last_success_at=None, upstream_kind="none", uptime_s=5) == "DEGRADED"
    assert _classify(last_success_at=None, upstream_kind="timeout", uptime_s=5) == "DEGRADED"
    assert _classify(last_success_at=None, upstream_kind="auth", uptime_s=5) == "DOWN"
    assert _classify(last_success_at=None, upstream_kind="none", uptime_s=HARD_AGE_SECONDS + 1) == "DOWN"


def test_backoff_caps_at_hard_age():
    assert backoff_seconds(0, 60) == 0
    assert backoff_seconds(1, 60) == 60
    assert backoff_seconds(2, 60) == 120
    assert backoff_seconds(3, 60) == 240
    assert backoff_seconds(6, 60) == HARD_AGE_SECONDS
    assert backoff_seconds(10, 60) == HARD_AGE_SECONDS


def test_pacific_timestamp_label():
    assert format_pt(NOW) == "Oct 27, 2026 10:32:00 PT"


def test_redacts_tokens_emails_and_long_bodies():
    token = "eyJ" + ("a" * 20) + "." + ("b" * 20) + "." + ("c" * 20)
    raw = (
        "failed https://user:supersecretpass@api.example.com/devices"
        "?token=supersecretvalue&email=user@example.com "
        f"Authorization: Bearer {token} "
        "authtoken: raw-header-value "
        + ("y" * 400)
    )
    cleaned = redact(raw, secrets=("supersecretpass",))
    assert "supersecretvalue" not in cleaned
    assert "supersecretpass" not in cleaned
    assert "user@example.com" not in cleaned
    assert token not in cleaned
    assert "raw-header-value" not in cleaned
    assert len(cleaned) <= 200
    assert cleaned.endswith("…")
    assert redact("Casa Main") == "Casa Main"
    assert redact("Shop Subpanel") == "Shop Subpanel"
    assert "hunter2-password" not in redact(
        "rejected hunter2-password",
        secrets=("hunter2-password",),
    )


def test_classify_failure_omits_url_and_body():
    response = requests.Response()
    response.status_code = 403
    response.reason = "Forbidden"
    response.url = "https://api.emporiaenergy.com/devices?token=supersecretvalue"
    response._content = b'{"email":"user@example.com","password":"hunter2-password"}'
    kind, code, message = classify_failure(requests.HTTPError(response=response))
    assert (kind, code) == ("auth", "403")
    assert "supersecretvalue" not in message
    assert "user@example.com" not in message
    assert "hunter2-password" not in message

    kind, code, message = classify_failure(requests.Timeout("timed out token=supersecretvalue"))
    assert kind == "timeout"
    assert "supersecretvalue" not in message

    response = requests.Response()
    response.status_code = 503
    response.reason = "Service Unavailable"
    kind, code, message = classify_failure(requests.HTTPError(response=response))
    assert (kind, code) == ("http_5xx", "503")

    response = requests.Response()
    response.status_code = 429
    response.reason = "Too Many Requests"
    assert classify_failure(requests.HTTPError(response=response))[0] == "rate_limit"


def test_hint_catalogue_wording():
    auth = hint_for(kind="auth", code="401", overall="DOWN", series_count=10, channel_count=4)
    assert "check API token/cred mount" in auth
    assert "do not paste into chat" in auth
    timeout = hint_for(kind="timeout", code="timeout", overall="DEGRADED", series_count=1, channel_count=1)
    assert "check docker3 outbound / upstream API" in timeout
    limited = hint_for(kind="rate_limit", code="429", overall="DEGRADED", series_count=1, channel_count=1)
    assert "rate limit; backoff is intentional" in limited
    tls = hint_for(kind="tls_dns", code="tls", overall="DEGRADED", series_count=1, channel_count=1)
    assert "network path from docker3" in tls
    empty = hint_for(kind="ok", code="200", overall="UP", series_count=0, channel_count=0)
    assert "upstream returned empty" in empty
    assert "not that the scrape is down" in empty


def _snapshot(**overrides) -> StatusSnapshot:
    extras = build_device_extras(
        [
            {"name": "Shop Subpanel", "connected": True, "latitude": 37.7, "zip_code": "94110"},
            {"name": "Casa Main", "connected": False, "longitude": -122.4},
        ],
        32,
    )
    params = dict(
        now=NOW,
        uptime_s=5 * 3600 + 2 * 60,
        poll_interval_s=60,
        last_success_at=NOW - 21,
        consecutive_failures=0,
        backoff_s=0,
        next_attempt_at=NOW + 39,
        hard_auth_failure=False,
        upstream_kind="ok",
        upstream_code="200",
        upstream_message="200 OK · devices",
        last_error_code=None,
        last_error_message=None,
        extras=extras,
        process_polling=True,
        listen_port=10110,
    )
    params.update(overrides)
    return StatusSnapshot(**params)


def test_package_version_matches_pyproject():
    from pathlib import Path

    import app

    text = Path("pyproject.toml").read_text()
    assert f'version = "{app.__version__}"' in text
    assert app.__version__ == "0.2.1"


def test_json_shape_and_emvue_extras():
    payload = build_status(_snapshot(), exporter_up=1, series_count=80)
    assert set(payload) == {
        "exporter",
        "version",
        "build_sha",
        "mode",
        "overall",
        "overall_phrase",
        "last_success_at",
        "last_success_pt",
        "age_seconds",
        "age_phrase",
        "poll_interval_s",
        "next_poll_at",
        "next_poll_pt",
        "next_poll_relative",
        "upstream",
        "failures",
        "backoff_s",
        "backoff",
        "next_retry_at",
        "next_retry_pt",
        "next_retry_relative",
        "next_retry",
        "last_error",
        "hint",
        "exporter_up",
        "series_count",
        "series_stale",
        "uptime_s",
        "uptime_phrase",
        "process_healthy",
        "process",
        "extras",
        "access_note",
    }
    assert payload["overall"] == "UP"
    assert payload["overall_phrase"] == "UP"
    assert payload["exporter"] == "prom_emvue"
    assert payload["version"] == "0.2.1"
    assert payload["mode"] == "Emporia Vue cloud API"
    assert payload["last_success_pt"] == "Oct 27, 2026 10:31:39 PT"
    assert payload["age_phrase"] == "21s ago"
    assert payload["next_retry"] == "n/a"
    assert payload["extras"]["device_count"] == 2
    assert payload["extras"]["channel_count"] == 32
    assert payload["extras"]["last_known"] is False
    assert payload["extras"]["devices"] == [
        {"name": "Casa Main", "connected": False},
        {"name": "Shop Subpanel", "connected": True},
    ]
    assert payload["uptime_phrase"] == "5h 02m"
    assert payload["process"] == "Healthy"
    assert payload["access_note"] == access_note(10110)
    blob = json.dumps(payload)
    for forbidden in ("94110", "37.7", "-122.4", "latitude", "longitude", "zip_code", "password"):
        assert forbidden not in blob


def test_degraded_next_retry_is_absolute_and_relative():
    payload = build_status(
        _snapshot(
            last_success_at=NOW - 47 * 60,
            consecutive_failures=10,
            backoff_s=600,
            next_attempt_at=NOW + 600,
            upstream_kind="timeout",
            upstream_code="timeout",
            upstream_message="Timeout · Emporia",
            last_error_code="timeout",
            last_error_message="Timeout · Emporia user@example.com",
            extras={
                **build_device_extras(
                    [
                        {"name": "Casa Main", "connected": True},
                        {"name": "Shop Subpanel", "connected": True},
                    ],
                    32,
                ),
                "last_known": True,
            },
        ),
        exporter_up=0,
        series_count=80,
    )
    assert payload["overall"] == "DEGRADED"
    assert payload["overall_phrase"] == "DEGRADED — no success in 47m"
    assert payload["next_retry_pt"] == "Oct 27, 2026 10:42:00 PT"
    assert payload["next_retry_relative"] == "~10 min"
    assert payload["next_retry"] == "Oct 27, 2026 10:42:00 PT · ~10 min"
    assert payload["next_poll_relative"] == "held for backoff"
    assert payload["extras"]["last_known"] is True
    assert "user@example.com" not in json.dumps(payload)
    assert payload["last_error"]["message"].startswith("Timeout")
    html = render_html(payload)
    assert payload["access_note"] in html
    assert "docker3" in html
    assert "http://127.0.0.1:10110/status" in html
    assert "no public listen" in html
    assert "Casa Main" in html
    assert "Shop Subpanel" in html
    assert "connected" in html
    assert "Oct 27, 2026 10:42:00 PT · ~10 min" in html
    assert "DEGRADED — no success in 47m" in html
    assert "last: Casa Main (connected), Shop Subpanel (connected)" in html
    assert "last: 32" in html
    assert "<form" not in html.lower()


def test_down_auth_phrase_and_secret_redaction():
    payload = build_status(
        _snapshot(
            last_success_at=NOW - 25 * 60,
            consecutive_failures=6,
            backoff_s=300,
            next_attempt_at=NOW + 300,
            hard_auth_failure=True,
            upstream_kind="auth",
            upstream_code="login",
            upstream_message="Login failed · credentials hunter2-password",
            last_error_code="login",
            last_error_message="Login failed · credentials hunter2-password",
        ),
        exporter_up=0,
        series_count=12,
        secrets=("hunter2-password",),
    )
    assert payload["overall"] == "DOWN"
    assert payload["overall_phrase"] == "DOWN — no success in 25m"
    assert "hunter2-password" not in json.dumps(payload)
    assert "do not paste into chat" in payload["hint"]
    html = render_html(payload)
    assert "hunter2-password" not in html
    assert "do not paste into chat" in html


def test_last_known_survives_failure():
    state = ExporterState(poll_interval_s=60, listen_port=10110)
    state.mark_success(
        NOW,
        build_device_extras(
            [{"name": "Casa Main", "connected": True}, {"name": "Shop Subpanel", "connected": False}],
            32,
        ),
    )
    state.mark_failure(NOW + 30, kind="timeout", code="timeout", message="Timeout · Emporia")
    snap = state.snapshot(now=NOW + 30)
    assert snap.extras["last_known"] is True
    assert snap.extras["channel_count"] == 32
    assert snap.consecutive_failures == 1
    assert snap.backoff_s == 60
    state.mark_success(NOW + 90, build_device_extras([{"name": "Casa Main", "connected": True}], 8))
    snap = state.snapshot(now=NOW + 90)
    assert snap.consecutive_failures == 0
    assert snap.extras["last_known"] is False
    assert snap.extras["channel_count"] == 8
    assert snap.hard_auth_failure is False


def test_metric_names_unchanged():
    from prometheus_client import generate_latest as latest
    from prometheus_client import REGISTRY

    import app.main  # noqa: F401

    text = latest(REGISTRY).decode()
    for name in (
        "emporia_exporter_up",
        "emporia_exporter_last_success_timestamp_seconds",
        "emporia_exporter_poll_duration_seconds",
        "emporia_exporter_errors_total",
        "emporia_device_power_watts",
        "emporia_channel_power_watts",
        "emporia_device_info",
        "emporia_device_connected",
        "emporia_outlet_on",
        "emporia_charger_on",
        "emporia_charger_charging_rate_amps",
        "emporia_vehicle_info",
        "emporia_vehicle_battery_level_percent",
    ):
        assert f"# HELP {name} " in text


def test_walk_usage_counts_channels_without_renaming_labels():
    from app.main import CHANNEL_POWER_WATTS, kwh_per_min_to_watts, walk_usage

    class _Ch:
        def __init__(self, usage, name, nested=None):
            self.usage = usage
            self.name = name
            self.nested_devices = nested

    class _Usage:
        def __init__(self, channels):
            self.channels = channels

    class _Info:
        def __init__(self, name):
            self.device_name = name

    nested = {20: _Usage({"1": _Ch(0.002, "Welder")})}
    usage = {10: _Usage({"1": _Ch(0.001, "Lights", nested=nested)})}
    assert walk_usage(usage, {10: _Info("Casa Main")}) == 2
    assert kwh_per_min_to_watts(0.001) == 60.0
    lights = [
        sample.value
        for metric in CHANNEL_POWER_WATTS.collect()
        for sample in metric.samples
        if sample.name == "emporia_channel_power_watts"
        and sample.labels.get("device_name") == "Casa Main"
        and sample.labels.get("channel_name") == "Lights"
    ]
    assert lights == [60.0]


def test_http_status_and_metrics_routes():
    registry = CollectorRegistry()
    Gauge("emporia_exporter_up", "1 if the last poll succeeded", registry=registry).set(1)
    Gauge(
        "emporia_channel_power_watts",
        "Estimated current channel power in watts",
        ["device_gid", "device_name", "channel_num", "channel_name"],
        registry=registry,
    ).labels(device_gid="1", device_name="Casa Main", channel_num="1", channel_name="Lights").set(12)

    state = ExporterState(poll_interval_s=60, listen_port=10110)
    state.mark_success(
        time.time(),
        build_device_extras(
            [{"name": "Casa Main", "connected": True}, {"name": "Shop Subpanel", "connected": True}],
            32,
        ),
    )
    sleeper = threading.Thread(target=time.sleep, args=(30,), daemon=True)
    sleeper.start()
    state.poll_thread = sleeper
    state.schedule_next(time.time() + 30)

    app = make_status_app(state, registry, listen_port=10110)
    httpd = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/status", timeout=5) as response:
            html = response.read().decode()
            assert "text/html" in response.headers["Content-Type"]
        assert "Reachable on docker3 as http://127.0.0.1:10110/status via lab SSH hop (no public listen)." in html
        assert "Casa Main" in html
        assert "Shop Subpanel" in html
        assert "connected" in html

        with urllib.request.urlopen(base + "/status?format=JSON", timeout=5) as response:
            body = response.read().decode()
            assert response.headers["Content-Type"].startswith("application/json")
        payload = json.loads(body)
        assert payload["overall"] in {"UP", "DEGRADED", "DOWN"}
        assert payload["extras"]["devices"][0]["name"] == "Casa Main"
        assert payload["extras"]["channel_count"] == 32
        assert "next_retry" in payload
        assert payload["access_note"] in html

        with urllib.request.urlopen(base + "/metrics", timeout=5) as response:
            metrics = response.read()
            assert "text/plain" in response.headers["Content-Type"]
        assert metrics == generate_latest(registry)
        assert b"emporia_exporter_up" in metrics
        assert b"emporia_channel_power_watts" in metrics
        assert b"<html" not in metrics.lower()

        with urllib.request.urlopen(base + "/", timeout=5) as response:
            root = response.read()
        assert root == metrics

        request = urllib.request.Request(base + "/status", method="POST", data=b"{}")
        try:
            urllib.request.urlopen(request, timeout=5)
            raise AssertionError("POST /status should not be accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 405
    finally:
        httpd.shutdown()
        httpd.server_close()
