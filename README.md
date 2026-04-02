# Prom_EmVue 
Prometheus Client exporter using PyEmVue to pull data from Emporia's cloud API.  This uses PyEmVue to login to Emporia's cloud service to pull data and then expose it out with the prometheus client for prometheus to scrape and pull into it's time-series data and be used by whatever you interface with it (e.g. grafana).

## Prometheus scrape config example

Add the following to your `prometheus.yml`:

```yaml
- job_name: emporia
  scrape_interval: 60s
  scrape_timeout: 15s
  static_configs:
    - targets:
        - 192.168.192.52:10110
      labels:
        site: home
        environment: prod
        role: energy
        service: emporia
        source: cloud
```

## Docker Compose configuration notes

In `docker-compose.yml`, the exporter service is configured with:

- `EMPORIA_USERNAME` and `EMPORIA_PASSWORD` environment variables (required)
- `POLL_INTERVAL_SECONDS` default `60` (controls polling cadence)
- `LISTEN_PORT` default `10110` (service metrics endpoint)
- `LOG_LEVEL` (e.g. `INFO`, `DEBUG` for troubleshooting)
- `read_only: true` and `tmpfs: /tmp` for container hardening
- `cap_drop: [ALL]` and `no-new-privileges: true` for security

Adjust these values as needed for your deployment environment.
