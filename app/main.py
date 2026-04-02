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

# --- Exporter health metrics ---
EXPORTER_UP = Gauge("emporia_exporter_up", "1 if the last poll succeeded")
LAST_SUCCESS = Gauge("emporia_exporter_last_success_timestamp_seconds", "Unix timestamp of last successful poll")
POLL_DURATION = Gauge("emporia_exporter_poll_duration_seconds", "Duration of the last poll in seconds")
ERRORS_TOTAL = Counter("emporia_exporter_errors_total", "Total Emporia polling errors")

# --- Energy metrics ---
DEVICE_POWER_WATTS = Gauge(
    "emporia_device_power_watts", "Estimated current device power in watts",
    ["device_gid", "device_name"],
)
CHANNEL_POWER_WATTS = Gauge(
    "emporia_channel_power_watts", "Estimated current channel power in watts",
    ["device_gid", "device_name", "channel_num", "channel_name"],
)

# --- Device metadata ---
DEVICE_INFO = Gauge(
    "emporia_device_info", "Static device info (value always 1)",
    ["device_gid", "device_name", "model", "firmware", "zip_code", "time_zone"],
)
DEVICE_CONNECTED = Gauge(
    "emporia_device_connected", "1 if device is connected",
    ["device_gid", "device_name"],
)

# --- Outlets and chargers ---
OUTLET_ON = Gauge("emporia_outlet_on", "1 if outlet is on", ["device_gid", "load_gid"])
CHARGER_ON = Gauge("emporia_charger_on", "1 if charger is on", ["device_gid", "load_gid"])
CHARGER_RATE = Gauge("emporia_charger_charging_rate_amps", "Charger rate in amps", ["device_gid", "load_gid"])

# --- Vehicles ---
VEHICLE_INFO = Gauge(
    "emporia_vehicle_info", "Vehicle static info (value always 1)",
    ["vehicle_gid", "display_name", "make", "model", "year"],
)
VEHICLE_BATTERY = Gauge(
    "emporia_vehicle_battery_level_percent", "Vehicle battery level",
    ["vehicle_gid", "display_name", "charging_state"],
)


def kwh_per_min_to_watts(kwh):
    """Convert kWh (over 1 minute sample) to average watts."""
    return kwh * 60 * 1000


def walk_usage(usage_dict, device_info, parent_gid=None, parent_name=None):
    """
    Recursively walk usage_dict (which may contain nested_devices on channels)
    and emit CHANNEL_POWER_WATTS and DEVICE_POWER_WATTS metrics.
    """
    for gid, usage_device in usage_dict.items():
        gid = int(gid)
        info = device_info.get(gid)
        device_name = (
            (info.device_name if info else None)
            or parent_name
            or f"device_{gid}"
        )

        total_watts = 0.0
        channels = getattr(usage_device, "channels", {}) or {}

        for ch_num, ch in channels.items():
            if ch is None:
                continue
            raw = getattr(ch, "usage", None)
            if raw is None:
                continue
            watts = kwh_per_min_to_watts(float(raw))
            total_watts += watts
            ch_name = str(getattr(ch, "name", ch_num) or ch_num)

            CHANNEL_POWER_WATTS.labels(
                device_gid=str(gid),
                device_name=device_name,
                channel_num=str(ch_num),
                channel_name=ch_name,
            ).set(watts)

            # Recurse into nested devices (subpanels, smart plugs on circuit)
            nested = getattr(ch, "nested_devices", None)
            if nested:
                walk_usage(nested, device_info, parent_gid=gid, parent_name=device_name)

        DEVICE_POWER_WATTS.labels(
            device_gid=str(gid),
            device_name=device_name,
        ).set(total_watts)


def collect_loop(vue: PyEmVue):
    while True:
        start = time.time()
        try:
            # get_devices() returns a list of VueDevice objects
            devices = vue.get_devices()

            device_info = {}
            device_gids = []
            for device in devices:
                gid = device.device_gid
                if gid not in device_gids:
                    device_gids.append(gid)
                # Merge channels if the same GID appears more than once
                if gid in device_info:
                    device_info[gid].channels += device.channels
                else:
                    device_info[gid] = device

            # Populate name/location properties for each device
            for gid, device in device_info.items():
                try:
                    vue.populate_device_properties(device)
                except Exception:
                    logging.debug("Could not populate properties for %s", gid, exc_info=True)

                device_name = device.device_name or f"device_{gid}"

                try:
                    DEVICE_INFO.labels(
                        device_gid=str(gid),
                        device_name=device_name,
                        model=str(getattr(device, "model", "")),
                        firmware=str(getattr(device, "firmware", "")),
                        zip_code=str(getattr(device, "zip_code", "")),
                        time_zone=str(getattr(device, "time_zone", "")),
                    ).set(1)
                except Exception:
                    logging.debug("Could not set DEVICE_INFO for %s", gid, exc_info=True)

                DEVICE_CONNECTED.labels(
                    device_gid=str(gid),
                    device_name=device_name,
                ).set(1 if getattr(device, "connected", False) else 0)

            # Fetch usage for all devices in one call
            usage_dict = vue.get_device_list_usage(
                deviceGids=device_gids,
                instant=None,
                scale=Scale.MINUTE.value,
                unit=Unit.KWH.value,
            )

            # Walk usage recursively (handles subpanels / nested devices)
            walk_usage(usage_dict, device_info)

            # Outlets
            try:
                for outlet in vue.get_outlets():
                    OUTLET_ON.labels(
                        device_gid=str(outlet.device_gid),
                        load_gid=str(outlet.load_gid),
                    ).set(1 if outlet.outlet_on else 0)
            except Exception:
                logging.debug("Could not fetch outlets", exc_info=True)

            # EV Chargers
            try:
                for charger in vue.get_chargers():
                    labels = dict(device_gid=str(charger.device_gid), load_gid=str(charger.load_gid))
                    CHARGER_ON.labels(**labels).set(1 if charger.charger_on else 0)
                    CHARGER_RATE.labels(**labels).set(float(getattr(charger, "charging_rate", 0)))
            except Exception:
                logging.debug("Could not fetch chargers", exc_info=True)

            # Vehicles
            try:
                for vehicle in vue.get_vehicles():
                    vid = str(vehicle.vehicle_gid)
                    dname = str(vehicle.display_name)
                    VEHICLE_INFO.labels(
                        vehicle_gid=vid,
                        display_name=dname,
                        make=str(getattr(vehicle, "make", "")),
                        model=str(getattr(vehicle, "model", "")),
                        year=str(getattr(vehicle, "year", "")),
                    ).set(1)
                    try:
                        vs = vue.get_vehicle_status(vehicle)
                        if vs:
                            VEHICLE_BATTERY.labels(
                                vehicle_gid=vid,
                                display_name=dname,
                                charging_state=str(getattr(vs, "charging_state", "")),
                            ).set(float(getattr(vs, "battery_level", 0)))
                    except Exception:
                        logging.debug("Could not get vehicle status for %s", vid, exc_info=True)
            except Exception:
                logging.debug("Could not fetch vehicles", exc_info=True)

            EXPORTER_UP.set(1)
            LAST_SUCCESS.set(time.time())
            logging.info("Poll succeeded in %.1fs", time.time() - start)

        except Exception:
            logging.exception("Polling cycle failed")
            ERRORS_TOTAL.inc()
            EXPORTER_UP.set(0)

        finally:
            POLL_DURATION.set(time.time() - start)

        time.sleep(POLL_INTERVAL)


def main():
    if not USERNAME or not PASSWORD:
        raise RuntimeError("EMPORIA_USERNAME and EMPORIA_PASSWORD must be set")

    # Login once — token refresh is handled automatically by the library
    vue = PyEmVue()
    vue.login(username=USERNAME, password=PASSWORD, token_storage_file=None)
    logging.info("Logged in to Emporia. Starting exporter on port %d", LISTEN_PORT)

    start_http_server(LISTEN_PORT)

    thread = threading.Thread(target=collect_loop, args=(vue,), daemon=True)
    thread.start()

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()