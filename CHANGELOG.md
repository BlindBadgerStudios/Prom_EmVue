# Changelog

## 0.2.1 - 2026-10-27

### Added
- `GET /status` returns a read-only HTML page for operators. `GET /status?format=json` returns the same fields for scripts.
- Overall state is `UP`, `DEGRADED`, or `DOWN`, using the rule in `app/status_rules.py`. The healthy window is 2× the poll interval. The hard age cutoff is 15 minutes.
- EmVue extras: device names, device count, connected flags, and channel count. After a failed poll the page keeps the last successful snapshot.
- `next_retry` is an absolute Pacific Time timestamp plus a relative phrase. Upstream errors are a code and a short message; tokens, auth headers, account emails, and long bodies are redacted.
- The HTML page notes that the exporter is reachable on docker3 as `http://127.0.0.1:<port>/status` via the lab SSH hop.
- `tzdata` so absolute times render in Pacific Time in the slim image.

### Changed
- A failed login or missing `EMPORIA_USERNAME` / `EMPORIA_PASSWORD` keeps the process up and backs off, so `/status` can report `DOWN` instead of the container restart-looping. The first failure still waits one poll interval. Later failures double up to 15 minutes. Successful polls stay on `POLL_INTERVAL_SECONDS`.

### Unchanged
- Prometheus metric names and the `/metrics` response. Grafana scrape config does not need to change. Do not point Prometheus at `/status`.

## 0.2.0 - 2026-10-27

Initial versioned release for the Emporia Vue Prometheus exporter (no prior
semver in `pyproject.toml`; only a legacy `v1` git tag existed).

### Changed
- Dependency bump: `prometheus-client` 0.24.1 → 0.26.0.
- Set project version to 0.2.0 in `pyproject.toml`.
