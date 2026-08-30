import sqlite3
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_FILE = Path(__file__).resolve().parent / "smartdorm.db"

@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            device_id TEXT NOT NULL,
            device_name TEXT NOT NULL,
            voltage REAL NOT NULL DEFAULT 0,
            current REAL NOT NULL DEFAULT 0,
            power REAL NOT NULL DEFAULT 0,
            energy_kwh REAL NOT NULL DEFAULT 0,
            online INTEGER NOT NULL DEFAULT 0,
            switch_on INTEGER NOT NULL DEFAULT 0,
            fault TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_device_time ON readings(device_id, timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_time ON readings(timestamp)")

def insert_reading(r):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO readings
        (timestamp, device_id, device_name, voltage, current, power, energy_kwh, online, switch_on, fault)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r.get("timestamp_iso", datetime.now(timezone.utc).isoformat()),
            r["id"], r["name"], float(r.get("voltage", 0)),
            float(r.get("current", 0)), float(r.get("power", 0)),
            float(r.get("energy", 0)), int(bool(r.get("online"))),
            int(bool(r.get("switch"))),
            str(r.get("fault")) if r.get("fault") is not None else None
        ))

def insert_readings(readings):
    for r in readings:
        insert_reading(r)

def get_recent_readings(device_id=None, minutes=60, limit=1000):
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with get_connection() as conn:
        if device_id:
            rows = conn.execute("""SELECT * FROM readings
                WHERE device_id=? AND timestamp>=?
                ORDER BY timestamp ASC LIMIT ?""",
                (device_id, since.isoformat(), limit)).fetchall()
        else:
            rows = conn.execute("""SELECT * FROM readings
                WHERE timestamp>=?
                ORDER BY timestamp ASC LIMIT ?""",
                (since.isoformat(), limit)).fetchall()
    return [dict(r) for r in rows]

def get_latest_readings():
    with get_connection() as conn:
        rows = conn.execute("""
        SELECT r.* FROM readings r
        JOIN (SELECT device_id, MAX(timestamp) latest FROM readings GROUP BY device_id) x
        ON r.device_id=x.device_id AND r.timestamp=x.latest
        ORDER BY r.device_name
        """).fetchall()
    return [dict(r) for r in rows]

def get_daily_summary(device_id=None, days=7):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    where = "WHERE timestamp>=?"
    params = [since.isoformat()]
    if device_id:
        where += " AND device_id=?"
        params.append(device_id)
    with get_connection() as conn:
        rows = conn.execute(f"""
        SELECT device_id, device_name, substr(timestamp,1,10) day,
               AVG(power) average_power_w, MAX(power) peak_power_w,
               MIN(power) minimum_power_w, AVG(voltage) average_voltage_v,
               MAX(energy_kwh) latest_energy, MIN(energy_kwh) first_energy,
               COUNT(*) samples
        FROM readings {where}
        GROUP BY device_id, device_name, day
        ORDER BY day, device_name
        """, params).fetchall()
    result=[]
    for r in rows:
        x=dict(r)
        x["energy_used_kwh"]=round(max(0,(x.pop("latest_energy") or 0)-(x.pop("first_energy") or 0)),6)
        for k in ("average_power_w","peak_power_w","minimum_power_w","average_voltage_v"):
            x[k]=round(x[k] or 0,2)
        result.append(x)
    return result

def get_energy_summary(device_id=None, days=180):
    """Per-device totals over the given window, built from cumulative meter
    readings already stored in smartdorm.db (no live LAN access needed).
    energy_used_kwh = latest cumulative reading - earliest cumulative reading
    in the window, which is the correct way to measure consumption from a
    running kWh counter."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    where = "WHERE timestamp>=?"
    params = [since.isoformat()]
    if device_id:
        where += " AND device_id=?"
        params.append(device_id)
    with get_connection() as conn:
        rows = conn.execute(f"""
        SELECT device_id, device_name,
               MIN(energy_kwh) first_energy, MAX(energy_kwh) latest_energy,
               AVG(power) avg_power_w, MAX(power) peak_power_w,
               AVG(voltage) avg_voltage_v, COUNT(*) samples,
               MAX(timestamp) last_seen
        FROM readings {where}
        GROUP BY device_id, device_name
        ORDER BY device_name
        """, params).fetchall()
    result = []
    for r in rows:
        x = dict(r)
        x["energy_used_kwh"] = round(max(0, (x.pop("latest_energy") or 0) - (x.pop("first_energy") or 0)), 3)
        x["avg_power_w"] = round(x["avg_power_w"] or 0, 1)
        x["peak_power_w"] = round(x["peak_power_w"] or 0, 1)
        x["avg_voltage_v"] = round(x["avg_voltage_v"] or 0, 1)
        result.append(x)
    return result


def get_database_stats():
    with get_connection() as conn:
        r=conn.execute("""SELECT COUNT(*) total_readings,
        COUNT(DISTINCT device_id) device_count, MIN(timestamp) first_reading,
        MAX(timestamp) last_reading FROM readings""").fetchone()
    return dict(r)
