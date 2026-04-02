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

# Additional metrics
DEVICE_INFO = Gauge(
    "emporia_device_info",
    "Static device info (value is always 1)",
    [
        "device_gid",
        "device_name",
        "display_name",
        "model",
        "firmware",
        "manufacturer_id",
        "zip_code",
        "time_zone",
        "solar",
    ],
)

DEVICE_CONNECTED = Gauge(
    "emporia_device_connected",
    "1 if device currently connected",
    ["device_gid", "device_name"],
)

OUTLET_ON = Gauge(
    "emporia_outlet_on",
    "1 if an outlet is on",
    ["device_gid", "load_gid"],
)

CHARGER_ON = Gauge(
    "emporia_charger_on",
    "1 if a charger is on",
    ["device_gid", "load_gid"],
)

CHARGER_CHARGING_RATE = Gauge(
    "emporia_charger_charging_rate",
    "Current charger charging rate",
    ["device_gid", "load_gid"],
)

CHANNEL_TYPE_INFO = Gauge(
    "emporia_channel_type_info",
    "Channel type metadata (value is always 1)",
    ["channel_type_gid", "description", "selectable"],
)

VEHICLE_INFO = Gauge(
    "emporia_vehicle_info",
    "Vehicle static info (value is always 1)",
    ["vehicle_gid", "display_name", "vendor", "make", "model", "year"],
)

VEHICLE_STATUS = Gauge(
    "emporia_vehicle_status",
    "Vehicle runtime status metrics",
    ["vehicle_gid", "display_name", "charging_state"],
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

            # Build a device map from the device list so we can export static
            # info and connectivity status. The public pyemvue API returns
            # a list of `VueDevice` objects from `get_devices()`.
            device_map = {}
            try:
                for d in device_iter:
                    dg = getattr(d, "device_gid", None) or getattr(d, "gid", None)
                    if dg:
                        device_map[int(dg)] = d
            except Exception:
                device_map = {}
            # Some versions of `pyemvue.enums.Unit` may not expose `WATTS`.
            # Try common fallbacks and use KWH if available (to match old code style).
            unit_const = None
            for candidate in ("KWH", "WATTS", "WATT", "W"):
                if hasattr(Unit, candidate):
                    unit_const = getattr(Unit, candidate)
                    logging.debug("Using Unit.%s for usage calls", candidate)
                    break

            # Update device metadata and collect per-device usage, including subpanels and nested circuits.
            for device in device_map.values():
                try:
                    # Populate location/name metadata if available
                    vue.populate_device_properties(device)
                except Exception:
                    logging.debug("Unable to populate properties for device %s", getattr(device, "device_gid", "?"), exc_info=True)

                device_gid = getattr(device, "device_gid", None) or getattr(device, "gid", None)
                if not device_gid:
                    continue

                device_name = (
                    str(getattr(device, "device_name", ""))
                    or str(getattr(device, "display_name", ""))
                    or f"device_{device_gid}"
                )

                try:
                    usage_result = vue.get_device_list_usage(
                        deviceGids=[device_gid],
                        instant=None,
                        scale=Scale.MINUTE,
                        **({"unit": unit_const} if unit_const is not None else {}),
                    )
                except Exception:
                    logging.exception("Failed to fetch usage for device %s", device_gid)
                    continue

                # Normalize usage result to dictionary by gid
                usage_dict = {}
                if isinstance(usage_result, dict):
                    usage_dict = usage_result
                elif isinstance(usage_result, list):
                    for u in usage_result:
                        key = int(getattr(u, "device_gid", 0) or 0)
                        if key:
                            usage_dict[key] = u
                else:
                    continue

                usage_device = usage_dict.get(int(device_gid))
                if not usage_device:
                    continue

                # Per device circuit power
                total_power = 0.0
                channels = getattr(usage_device, "channels", {}) or {}
                for ch_num, ch in channels.items():
                    if ch is None:
                        continue
                    ch_name = str(getattr(ch, "name", ch_num))
                    ch_usage = getattr(ch, "usage", None)
                    if ch_usage is None:
                        continue
                    ch_u = float(ch_usage)
                    total_power += ch_u

                    CHANNEL_POWER_WATTS.labels(
                        device_gid=str(device_gid),
                        device_name=device_name,
                        channel_num=str(ch_num),
                        channel_name=ch_name,
                    ).set(ch_u)

                DEVICE_POWER_WATTS.labels(
                    device_gid=str(device_gid),
                    device_name=device_name,
                ).set(total_power)

                # Static metadata metrics
                try:
                    DEVICE_INFO.labels(
                        device_gid=str(device_gid),
                        device_name=device_name,
                        display_name=str(getattr(device, "display_name", "")),
                        model=str(getattr(device, "model", "")),
                        firmware=str(getattr(device, "firmware", "")),
                        manufacturer_id=str(getattr(device, "manufacturer_id", "")),
                        zip_code=str(getattr(device, "zip_code", "")),
                        time_zone=str(getattr(device, "time_zone", "")),
                        solar=str(getattr(device, "solar", "")),
                    ).set(1)
                except Exception:
                    logging.debug("Failed to set DEVICE_INFO for %s", device_gid, exc_info=True)

                DEVICE_CONNECTED.labels(
                    device_gid=str(device_gid),
                    device_name=device_name,
                ).set(1 if getattr(device, "connected", False) else 0)

            # Export channel types
            try:
                channel_types = vue.get_channel_types()
                for ct in channel_types:
                    CHANNEL_TYPE_INFO.labels(
                        channel_type_gid=str(getattr(ct, "channel_type_gid", "")),
                        description=str(getattr(ct, "description", "")),
                        selectable=str(getattr(ct, "selectable", "")),
                    ).set(1)
            except Exception:
                logging.debug("Failed to fetch channel types", exc_info=True)

            # Export vehicles and their status
            try:
                vehicles = vue.get_vehicles()
                for v in vehicles:
                    VEHICLE_INFO.labels(
                        vehicle_gid=str(getattr(v, "vehicle_gid", "")),
                        display_name=str(getattr(v, "display_name", "")),
                        vendor=str(getattr(v, "vendor", "")),
                        make=str(getattr(v, "make", "")),
                        model=str(getattr(v, "model", "")),
                        year=str(getattr(v, "year", "")),
                    ).set(1)
                    try:
                        vs = vue.get_vehicle_status(getattr(v, "vehicle_gid", 0))
                        if vs:
                            VEHICLE_STATUS.labels(
                                vehicle_gid=str(getattr(vs, "vehicle_gid", "")),
                                display_name=str(getattr(v, "display_name", "")),
                                charging_state=str(getattr(vs, "charging_state", "")),
                            ).set(float(getattr(vs, "battery_level", 0)))
                    except Exception:
                        logging.debug("Failed to fetch vehicle status for %s", v, exc_info=True)
            except Exception:
                logging.debug("Failed to fetch vehicles", exc_info=True)

            # Export outlets/chargers and connectivity
            try:
                outlets, chargers = vue.get_devices_status(device_list=list(device_map.values()) if device_map else None)
                for outlet in (outlets or []):
                    OUTLET_ON.labels(
                        device_gid=str(getattr(outlet, "device_gid", "")),
                        load_gid=str(getattr(outlet, "load_gid", "")),
                    ).set(1 if getattr(outlet, "outlet_on", False) else 0)
                for charger in (chargers or []):
                    CHARGER_ON.labels(
                        device_gid=str(getattr(charger, "device_gid", "")),
                        load_gid=str(getattr(charger, "load_gid", "")),
                    ).set(1 if getattr(charger, "charger_on", False) else 0)
                    CHARGER_CHARGING_RATE.labels(
                        device_gid=str(getattr(charger, "device_gid", "")),
                        load_gid=str(getattr(charger, "load_gid", "")),
                    ).set(float(getattr(charger, "charging_rate", 0)))
            except Exception:
                logging.debug("Failed to fetch outlets/chargers", exc_info=True)

            # Per-device usage and metrics already emitted above, so no extra recursive pass is needed.

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