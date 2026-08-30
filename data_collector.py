"""
SmartDorm - Standalone TinyTuya LAN Data Collector

Purpose:
    Poll the two Tuya smart meters locally over Wi-Fi and save telemetry to
    smartdorm.db. This program intentionally contains NO Flask/web server.

Run:
    python data_collector.py

Stop:
    Ctrl+C

The website is a separate program:
    python smartdorm.py
"""

import json
import signal
import time
from pathlib import Path
from datetime import datetime, timezone

import tinytuya

from database import init_db, insert_reading

BASE_DIR = Path(__file__).resolve().parent
DEVICES_FILE = BASE_DIR / "devices.json"
INTERVAL_SECONDS = 10

# Device IDs only. Local keys and current IPs are read from devices.json.
KNOWN_NAMES = {
    "bfbdf64a96838294f4tabw": "Room F",
    "bf12b5a475e54ca3068byh": "Room K",
}

running = True


def stop_handler(signum, frame):
    global running
    running = False
    print("\n[COLLECTOR] Stopping safely...")


def load_devices():
    if not DEVICES_FILE.exists():
        raise FileNotFoundError(
            f"{DEVICES_FILE} not found. Run TinyTuya scan/wizard first."
        )

    with DEVICES_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    raw = data.get("devices", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError("Unsupported devices.json format.")

    devices = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        dev_id = item.get("id") or item.get("device_id")
        key = item.get("key") or item.get("local_key")
        ip = item.get("ip") or item.get("address")

        if not dev_id or not key or not ip:
            continue

        name = KNOWN_NAMES.get(dev_id) or item.get("name") or f"Device {dev_id[-4:]}"
        devices.append({"id": dev_id, "name": name, "ip": ip, "key": key})

    if not devices:
        raise RuntimeError("No usable Tuya devices were found in devices.json.")

    return devices


def convert_dps(dps):
    """Convert the verified SY1-TSW DPS values into normal units."""
    return {
        "voltage": round(float(dps.get("20", 0)) / 10.0, 1),
        "current": round(float(dps.get("18", 0)) / 1000.0, 3),
        "power": round(float(dps.get("19", 0)) / 10.0, 1),
        "energy": round(float(dps.get("17", 0)) / 1000.0, 3),
        "online": str(dps.get("66", "offline")).lower() == "online"
                   or bool(dps.get("1", False)),
        "switch": bool(dps.get("1", False)),
        "fault": dps.get("26", 0),
    }


def read_device(device_info):
    dev_id = device_info["id"]
    name = device_info["name"]
    ip = device_info["ip"]

    try:
        device = tinytuya.Device(dev_id, ip, device_info["key"])
        device.set_version(3.5)
        status = device.status()

        print(f"[TINYTUYA RAW] {name}: {status}")

        if not isinstance(status, dict) or "dps" not in status:
            print(f"[TINYTUYA ERROR] {name}: no DPS payload received; not saved.")
            return None

        dps = status.get("dps", {})
        print(f"[TINYTUYA DPS] {name}: {dps}")

        values = convert_dps(dps)
        return {
            "id": dev_id,
            "name": name,
            "ip": ip,
            **values,
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        print(f"[TINYTUYA ERROR] {name} ({ip}): {type(exc).__name__}: {exc}")
        return None


def collect_once(devices):
    saved = 0

    for device in devices:
        reading = read_device(device)
        if reading is None:
            continue

        insert_reading(reading)
        saved += 1

    print(f"[COLLECTOR] Saved {saved} reading(s).")
    return saved


def main():
    global running

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    init_db()
    devices = load_devices()

    print(f"[TINYTUYA] Loaded {len(devices)} local device(s).")
    print(f"[COLLECTOR] Started: every {INTERVAL_SECONDS}s")
    print("[COLLECTOR] Storage: smartdorm.db")
    print("[COLLECTOR] Transport: TinyTuya -> Tuya device LAN (NO TUYA CLOUD)")
    print("[COLLECTOR] Press Ctrl+C to stop.\n")

    while running:
        cycle_start = time.monotonic()
        collect_once(devices)

        remaining = max(0.0, INTERVAL_SECONDS - (time.monotonic() - cycle_start))
        while running and remaining > 0:
            time.sleep(min(0.5, remaining))
            remaining = max(0.0, INTERVAL_SECONDS - (time.monotonic() - cycle_start))

    print("[COLLECTOR] Stopped.")


if __name__ == "__main__":
    main()
