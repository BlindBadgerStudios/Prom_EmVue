import logging
import os
import threading
import time

from prometheus_client import Gauge, Counter, start_http_server
from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "10110"))
USERNAME = os.getenv("EMPORIA_USERNAME")
PASSWORD = os.getenv("EMPORIA_PASSWORD")

EXPORTER_UP = Gauge("emporia_exporter_up", "1 if the last poll succeeded")
LAST_SUCCESS = Gauge(
    "emporia_exporter_last_success_timestamp_seconds",
    "Unix timestamp of last successful poll",
)
POLL_DURATION = Gauge(
    "emporia_exporter_poll_duration_seconds",
    "Duration of the last poll",
)
ERRORS_TOTAL = Counter(
    "emporia_exporter_errors_total",
    "Total Emporia polling errors",
)

DEVICE_POWER_WATTS = Gauge(
    "emporia_device_power_watts",
    "Current device power",
    ["device_gid", "device_name"],
)

CHANNEL_POWER_WATTS = Gauge(
    "emporia_channel_power_watts",
    "Current channel power",
    ["device_gid", "device_name", "channel_num", "channel_name"],
)


def collect_loop():
    while True:
        start = time.time()
        try:
            vue = PyEmVue()
            vue.login(username=USERNAME, password=PASSWORD, token_storage_file=None)

            devices = vue.get_devices()
            # `vue.get_devices()` may return a dict or a list depending on
            # library version. Support either shape and defensively extract
            # device GIDs.
            if isinstance(devices, dict):
                device_iter = devices.values()
            elif isinstance(devices, list):
                device_iter = devices
            else:
                try:
                    device_iter = list(devices)
                except Exception:
                    device_iter = []

            gids = []
            for device in device_iter:
                gid = getattr(device, "device_gid", None) or getattr(
                    device, "gid", None
                )
                if gid:
                    gids.append(gid)
            usage = vue.get_device_list_usage(
                deviceGids=gids,
                instant=None,
                scale=Scale.MINUTE,
                unit=Unit.WATTS,
            )

            for _, device in usage.items():
                device_gid = str(getattr(device, "device_gid", "unknown"))
                device_name = str(getattr(device, "device_name", "unknown"))

                device_usage = getattr(device, "usage", None)
                if device_usage is not None:
                    DEVICE_POWER_WATTS.labels(
                        device_gid=device_gid,
                        device_name=device_name,
                    ).set(float(device_usage))

                channels = getattr(device, "channels", {}) or {}
                for channel_num, channel in channels.items():
                    channel_name = str(getattr(channel, "name", channel_num))
                    channel_usage = getattr(channel, "usage", None)
                    if channel_usage is None:
                        continue

                    CHANNEL_POWER_WATTS.labels(
                        device_gid=device_gid,
                        device_name=device_name,
                        channel_num=str(channel_num),
                        channel_name=channel_name,
                    ).set(float(channel_usage))

            EXPORTER_UP.set(1)
            LAST_SUCCESS.set(time.time())

        except Exception:
            logging.exception("Polling failed")
            ERRORS_TOTAL.inc()
            EXPORTER_UP.set(0)

        finally:
            POLL_DURATION.set(time.time() - start)

        time.sleep(POLL_INTERVAL)


def main():
    if not USERNAME or not PASSWORD:
        raise RuntimeError("EMPORIA_USERNAME and EMPORIA_PASSWORD are required")

    start_http_server(LISTEN_PORT)
    thread = threading.Thread(target=collect_loop, daemon=True)
    thread.start()

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()