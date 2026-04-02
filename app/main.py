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
            # Try common fallbacks and call the API without `unit` if none found.
            unit_const = None
            for candidate in ("WATTS", "WATT", "W"):
                if hasattr(Unit, candidate):
                    unit_const = getattr(Unit, candidate)
                    logging.debug("Using Unit.%s for usage calls", candidate)
                    break

            usage_kwargs = {
                "deviceGids": gids,
                "instant": None,
                "scale": Scale.MINUTE,
            }
            if unit_const is not None:
                usage_kwargs["unit"] = unit_const

            logging.debug("Fetching usage for gids=%s", gids)
            usage = {}
            try:
                usage_result = vue.get_device_list_usage(**usage_kwargs)
                if isinstance(usage_result, dict):
                    usage = usage_result
                elif isinstance(usage_result, list):
                    usage = {int(getattr(dev, "device_gid", 0)): dev for dev in usage_result}
                else:
                    usage = {}
                logging.debug("Initial usage keys=%s", list(usage.keys()))
            except Exception:
                logging.exception("Failed initial usage query")
                usage = {}

            # ensure we have all devices; some API versions only return one set when using all gids
            missing = [g for g in gids if int(g) not in usage]
            if missing:
                logging.debug("Missing usage for gids=%s, fetching individually", missing)
                for missing_gid in missing:
                    try:
                        one_usage = vue.get_device_list_usage(
                            deviceGids=[missing_gid], instant=None, scale=Scale.MINUTE, **({"unit": unit_const} if unit_const is not None else {})
                        )
                        if isinstance(one_usage, dict):
                            usage.update(one_usage)
                        elif isinstance(one_usage, list):
                            for dev in one_usage:
                                usage[int(getattr(dev, "device_gid", 0))] = dev
                    except Exception:
                        logging.exception("Failed usage query for gid %s", missing_gid)
            logging.debug("Final usage keys=%s", list(usage.keys()))

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

            # Recursively process usage data to export all circuits (channels)
            def process_usage_device(dev):
                try:
                    dg = int(getattr(dev, "device_gid", 0) or 0)
                except Exception:
                    dg = 0
                mapped_dev = device_map.get(dg)
                device_name = (
                    str(getattr(mapped_dev, "device_name", ""))
                    if mapped_dev and getattr(mapped_dev, "device_name", "")
                    else str(getattr(dev, "device_name", "unknown"))
                )

                total = 0.0
                channels = getattr(dev, "channels", {}) or {}
                # channels is a dict of channel_num -> VueDeviceChannelUsage
                for channel_num, channel in channels.items():
                    try:
                        channel_usage = getattr(channel, "usage", None)
                        usage_val = float(channel_usage) if channel_usage is not None else None
                    except Exception:
                        usage_val = None

                    channel_name = str(getattr(channel, "name", channel_num))
                    if usage_val is not None:
                        total += usage_val
                        CHANNEL_POWER_WATTS.labels(
                            device_gid=str(dg),
                            device_name=device_name,
                            channel_num=str(channel_num),
                            channel_name=channel_name,
                        ).set(usage_val)

                    # handle nested devices attached to this channel
                    nested = getattr(channel, "nested_devices", {}) or {}
                    for _, nested_dev in (nested.items() if hasattr(nested, "items") else enumerate(nested)):
                        # nested_dev is a VueUsageDevice
                        nested_total = process_usage_device(nested_dev)
                        # Do not double-count nested usage into parent total here; nested devices are separate devices

                # Set device-level metric for this device
                try:
                    DEVICE_POWER_WATTS.labels(
                        device_gid=str(dg),
                        device_name=device_name,
                    ).set(total)
                except Exception:
                    logging.debug("Failed to set DEVICE_POWER_WATTS for %s", dg, exc_info=True)

                # Also export static info if available
                if mapped_dev:
                    try:
                        DEVICE_INFO.labels(
                            device_gid=str(getattr(mapped_dev, "device_gid", "")),
                            device_name=str(getattr(mapped_dev, "device_name", "")),
                            display_name=str(getattr(mapped_dev, "display_name", "")),
                            model=str(getattr(mapped_dev, "model", "")),
                            firmware=str(getattr(mapped_dev, "firmware", "")),
                            manufacturer_id=str(getattr(mapped_dev, "manufacturer_id", "")),
                            zip_code=str(getattr(mapped_dev, "zip_code", "")),
                            time_zone=str(getattr(mapped_dev, "time_zone", "")),
                            solar=str(getattr(mapped_dev, "solar", "")),
                        ).set(1)
                    except Exception:
                        logging.debug("Failed to set DEVICE_INFO for %s", mapped_dev, exc_info=True)
                    DEVICE_CONNECTED.labels(
                        device_gid=str(getattr(mapped_dev, "device_gid", "")),
                        device_name=str(getattr(mapped_dev, "device_name", "")),
                    ).set(1 if getattr(mapped_dev, "connected", False) else 0)

                return total

            for _, device in usage.items():
                try:
                    process_usage_device(device)
                except Exception:
                    logging.debug("Failed to process usage for device %s", getattr(device, "device_gid", "?"), exc_info=True)

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