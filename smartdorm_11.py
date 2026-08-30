# ==============================================================================
# IoT-Based Smart Sub-Metering System for Bangladeshi Student Dormitories
# Local Flask + TinyTuya LAN Version
# ==============================================================================

import os
import sys
import time
import random
import json
import re
import ast
import threading
import subprocess
from datetime import datetime, timedelta
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, render_template_string, jsonify, request
import tinytuya
from database import (
    init_db,
    get_recent_readings,
    get_latest_readings,
    get_daily_summary,
    get_database_stats,
    get_energy_summary,
)
import pandas as pd
import numpy as np

# Project directory.
BASE_DIR = Path(__file__).resolve().parent
DEVICES_FILE = BASE_DIR / "devices.json"

DEFAULT_ELECTRICITY_RATE = 12.5
DEFAULT_CARBON_FACTOR = 0.65
DEFAULT_REFRESH_INTERVAL = 10
# Trailing window used to estimate the bill from metered consumption.
# Kept in sync with the Overview KPI (/api/summary?days=30) so the
# Billing page's starting figure agrees with the rest of the dashboard.
DEFAULT_BILLING_DAYS = 30

DEVICE_CONFIG = {
    "bfbdf64a96838294f4tabw": {
        "name": "Room F",
        "ip": "192.168.0.101",
    },
    "bf12b5a475e54ca3068byh": {
        "name": "Room K",
        "ip": "192.168.0.100",
    },
}


# ------------------------------------------------------------------------------
# LOCAL TINYTUYA LAN DATA LAYER
# ------------------------------------------------------------------------------
class TuyaEnergyManager:
    """Reads Tuya smart-meter data directly over local LAN using TinyTuya."""

    def __init__(self):
        self.devices = {}
        self.load_devices()

    def load_devices(self):
        if not DEVICES_FILE.exists():
            print(f"[ERROR] {DEVICES_FILE} was not found.")
            return

        try:
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            raw_devices = data.get("devices", []) if isinstance(data, dict) else data
            if not isinstance(raw_devices, list):
                raise ValueError("Unsupported devices.json structure.")

            for item in raw_devices:
                if not isinstance(item, dict):
                    continue

                dev_id = item.get("id") or item.get("device_id")
                local_key = item.get("key") or item.get("local_key")
                ip = item.get("ip") or item.get("address")

                if not dev_id or not local_key:
                    continue

                known = DEVICE_CONFIG.get(dev_id, {})
                ip = ip or known.get("ip")
                name = item.get("name") or known.get("name") or f"Device {dev_id[-4:]}"

                if not ip:
                    print(f"[WARNING] No IP address found for {name} ({dev_id}).")
                    continue

                self.devices[dev_id] = {
                    "name": name,
                    "ip": ip,
                    "key": local_key,
                }

            for dev_id, known in DEVICE_CONFIG.items():
                if dev_id not in self.devices:
                    print(f"[WARNING] Device not loaded from devices.json: {known['name']}")

            print(f"[TINYTUYA] Loaded {len(self.devices)} local device(s).")

        except Exception as e:
            print(f"[ERROR] Could not load devices.json: {e}")

    @staticmethod
    def convert_dps(dps):
        return {
            "voltage": round(float(dps.get("20", 0)) / 10.0, 1),
            "current": round(float(dps.get("18", 0)) / 1000.0, 3),
            "power": round(float(dps.get("19", 0)) / 10.0, 1),
            "energy": round(float(dps.get("17", 0)) / 1000.0, 3),
            "online": str(dps.get("66", "offline")).lower() == "online" or bool(dps.get("1", False)),
            "switch": bool(dps.get("1", False)),
            "fault": dps.get("26", 0),
        }

    def read_device(self, dev_id, info):
        try:
            device = tinytuya.Device(
                dev_id,
                info["ip"],
                info["key"],
            )
            device.set_version(3.5)
            status = device.status()
            dps = status.get("dps", {}) if isinstance(status, dict) else {}
            values = self.convert_dps(dps)

            return {
                "id": dev_id,
                "name": info["name"],
                "ip": info["ip"],
                **values,
                "timestamp": time.strftime("%H:%M:%S"),
            }
        except Exception as e:
            return {
                "id": dev_id,
                "name": info["name"],
                "ip": info["ip"],
                "voltage": 0.0,
                "current": 0.0,
                "power": 0.0,
                "energy": 0.0,
                "online": False,
                "switch": False,
                "fault": None,
                "timestamp": time.strftime("%H:%M:%S"),
                "error": str(e),
            }

    def fetch_room_devices(self):
        devices_data = []
        for dev_id, info in self.devices.items():
            devices_data.append(self.read_device(dev_id, info))
        return devices_data

    def control_device(self, dev_id, switch_state):
        """Control the main Tuya switch (DPS 1) over local LAN."""
        if dev_id not in self.devices:
            return {"success": False, "error": "Unknown device ID"}

        info = self.devices[dev_id]
        target_state = bool(switch_state)

        try:
            device = tinytuya.Device(
                dev_id,
                info["ip"],
                info["key"],
            )
            device.set_version(3.5)

            result = device.set_status(
                target_state,
                switch=1,
                nowait=False
            )

            success = result is not False

            print(
                f"[TINYTUYA CONTROL] {info['name']} -> "
                f"{'ON' if target_state else 'OFF'} | result={result!r}"
            )

            return {
                "success": bool(success),
                "device_id": dev_id,
                "name": info["name"],
                "switch": target_state,
                "result": result,
            }

        except Exception as e:
            print(f"[TINYTUYA CONTROL ERROR] {info['name']}: {e}")
            return {
                "success": False,
                "device_id": dev_id,
                "name": info["name"],
                "switch": target_state,
                "error": str(e),
            }


init_db()
tuya_manager = TuyaEnergyManager()

COLLECTOR_SCRIPT = BASE_DIR / "data_collector.py"


class CollectorController:
    """Runs data_collector.py as a subprocess and captures its stdout so the
    dashboard can show a live, graphical view of what it's doing (per-device
    readings, save events) instead of just a running/stopped badge."""

    LOG_PATTERN = re.compile(r"^\[TINYTUYA DPS\]\s*(.+?):\s*(\{.*\})\s*$")
    SAVE_PATTERN = re.compile(r"^\[COLLECTOR\]\s*Saved\s+(\d+)\s+reading")
    ERROR_PATTERN = re.compile(r"error", re.IGNORECASE)

    def __init__(self):
        self.process = None
        self.reader_thread = None
        self.lock = threading.Lock()
        self.log_lines = deque(maxlen=300)
        self.device_status = {}
        self.saved_total = 0
        self.last_saved_at = None
        self.started_at = None

    def status(self):
        running = self.process is not None and self.process.poll() is None
        with self.lock:
            uptime_s = round(time.time() - self.started_at, 0) if (running and self.started_at) else 0
            return {
                "running": running,
                "pid": self.process.pid if running else None,
                "uptime_s": uptime_s,
                "saved_total": self.saved_total,
                "last_saved_at": self.last_saved_at,
                "devices": self.device_status,
                "log": list(self.log_lines)[-100:],
            }

    def _reader_loop(self, proc):
        try:
            for raw_line in iter(proc.stdout.readline, ''):
                line = raw_line.rstrip('\n')
                if not line:
                    continue
                self._ingest_line(line)
        except Exception:
            pass

    def _ingest_line(self, line):
        tag = "info"
        if self.ERROR_PATTERN.search(line):
            tag = "error"
        elif line.startswith("[COLLECTOR]"):
            tag = "collector"
        elif line.startswith("[TINYTUYA"):
            tag = "tuya"

        with self.lock:
            self.log_lines.append({"text": line, "ts": time.strftime("%H:%M:%S"), "tag": tag})

            dps_match = self.LOG_PATTERN.match(line)
            if dps_match:
                name = dps_match.group(1).strip()
                try:
                    dps = ast.literal_eval(dps_match.group(2))
                    values = TuyaEnergyManager.convert_dps(dps)
                    values["timestamp"] = time.strftime("%H:%M:%S")
                    self.device_status[name] = values
                except Exception:
                    pass
                return

            save_match = self.SAVE_PATTERN.match(line)
            if save_match:
                self.saved_total += int(save_match.group(1))
                self.last_saved_at = time.strftime("%H:%M:%S")

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return {"status": "already_running", "pid": self.process.pid}
        if not COLLECTOR_SCRIPT.exists():
            return {"status": "error", "message": "data_collector.py not found next to smartdorm.py"}

        with self.lock:
            self.log_lines.clear()
            self.device_status = {}
            self.saved_total = 0
            self.last_saved_at = None

        self.process = subprocess.Popen(
            [sys.executable, "-u", str(COLLECTOR_SCRIPT)],
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.started_at = time.time()
        self.reader_thread = threading.Thread(target=self._reader_loop, args=(self.process,), daemon=True)
        self.reader_thread.start()
        return {"status": "started", "pid": self.process.pid}

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return {"status": "not_running"}
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.started_at = None
        return {"status": "stopped"}


collector = CollectorController()

# ------------------------------------------------------------------------------
# FLASK APPLICATION & TEMPLATES
# ------------------------------------------------------------------------------
app = Flask(__name__)

BASE_LAYOUT = """
<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SmartDorm — Energy Management & MCB Control System</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4"></script>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{
 --bg:#07110d; --panel:rgba(15,29,23,.78); --panel2:rgba(19,40,31,.62);
 --line:rgba(163,230,193,.13); --text:#effff5; --muted:#91a99b;
 --green:#45e39a; --lime:#b8f26b; --cyan:#52d7e8; --amber:#ffc857; --red:#ff6b7a;
}
*{box-sizing:border-box}
body{margin:0;color:var(--text);font-family:'DM Sans',sans-serif;background:
 radial-gradient(circle at 10% 0%,rgba(69,227,154,.14),transparent 30%),
 radial-gradient(circle at 95% 10%,rgba(82,215,232,.10),transparent 25%),
 linear-gradient(135deg,#06100c,#0a1712 55%,#07110d);min-height:100vh}
h1,h2,h3,h4,h5,h6,.brand{font-family:'Space Grotesk',sans-serif}
.navbar{background:rgba(5,15,10,.82)!important;backdrop-filter:blur(20px);border-bottom:1px solid var(--line)}
.brand{letter-spacing:-.03em}.brand-mark{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;background:linear-gradient(135deg,var(--green),var(--cyan));color:#052015;box-shadow:0 0 30px rgba(69,227,154,.25)}
.nav-link{color:#9fb5a8!important;border-radius:12px;padding:.55rem .85rem!important}.nav-link:hover,.nav-link.active{color:#fff!important;background:rgba(69,227,154,.09)}
.shell{max-width:1400px}.glass{background:linear-gradient(145deg,rgba(20,43,33,.82),rgba(8,20,14,.72));border:1px solid var(--line);border-radius:24px;box-shadow:0 18px 60px rgba(0,0,0,.22);backdrop-filter:blur(18px)}
.hero{padding:2.5rem;border-radius:30px;background:linear-gradient(135deg,rgba(69,227,154,.12),rgba(82,215,232,.05));border:1px solid rgba(69,227,154,.16)}
.eyebrow{color:var(--lime);font-size:.78rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
.kpi{padding:1.25rem}.kpi-label{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.08em}.kpi-value{font:700 2rem 'Space Grotesk';letter-spacing:-.04em}.kpi-icon{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;background:rgba(69,227,154,.09);color:var(--green)}
.btn-eco{background:linear-gradient(135deg,var(--green),var(--cyan));color:#04150d;border:0;font-weight:700;border-radius:13px;padding:.7rem 1rem}.btn-eco:hover{color:#04150d;filter:brightness(1.08)}
.btn-ghost{border:1px solid var(--line);color:#e8f5ed;background:rgba(255,255,255,.025);border-radius:13px}.btn-ghost:hover{background:rgba(255,255,255,.06);color:#fff}
.status-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}.online{background:var(--green);box-shadow:0 0 12px var(--green)}.offline{background:var(--red)}
.pipeline-step{border:1px solid var(--line);border-radius:12px;padding:.5rem .75rem;background:rgba(255,255,255,.02);font-size:.78rem;text-align:center}
.form-control, .form-select { background-color: rgba(10, 23, 18, 0.8) !important; border-color: var(--line) !important; color: var(--text) !important; }
.form-control:focus, .form-select:focus { border-color: var(--green) !important; box-shadow: 0 0 0 0.25rem rgba(69,227,154,.25) !important; }
footer{color:#6f877a}
</style>
</head>
<body>
<nav class="navbar navbar-expand-lg sticky-top">
<div class="container shell py-2">
<a class="navbar-brand brand text-white d-flex align-items-center gap-2" href="/">
 <span class="brand-mark"><i class="bi bi-cpu-fill"></i></span>
 <span>SmartDorm <small class="text-success">IoT OS</small></span>
</a>
<button class="navbar-toggler bg-dark" data-bs-toggle="collapse" data-bs-target="#nav"><span class="navbar-toggler-icon"></span></button>
<div class="collapse navbar-collapse" id="nav"><ul class="navbar-nav ms-auto gap-1">
<li class="nav-item"><a class="nav-link {% if active_page=='dashboard' %}active{% endif %}" href="/dashboard"><i class="bi bi-speedometer2 me-1"></i>Overview</a></li>
<li class="nav-item"><a class="nav-link {% if active_page=='analytics' %}active{% endif %}" href="/analytics"><i class="bi bi-graph-up-arrow me-1"></i>Analytics</a></li>
<li class="nav-item"><a class="nav-link {% if active_page=='billing' %}active{% endif %}" href="/billing"><i class="bi bi-receipt me-1"></i>Billing</a></li>
<li class="nav-item"><a class="nav-link {% if active_page=='carbon' %}active{% endif %}" href="/carbon"><i class="bi bi-tree-fill me-1"></i>Carbon</a></li>
<li class="nav-item"><a class="nav-link {% if active_page=='history' %}active{% endif %}" href="/history"><i class="bi bi-clock-history me-1"></i>History</a></li>
<li class="nav-item"><a class="nav-link {% if active_page=='control' %}active{% endif %}" href="/control"><i class="bi bi-toggle2-on me-1"></i>Control Panel</a></li>
<li class="nav-item"><a class="nav-link {% if active_page=='settings' %}active{% endif %}" href="/settings"><i class="bi bi-sliders2 me-1"></i>System</a></li>
</ul></div>
</div></nav>
<main class="container shell py-4">{% block content %}{% endblock %}</main>
<footer class="container shell text-center py-5">
 <div class="row g-2 justify-content-center mb-3">
  <div class="col-auto"><span class="pipeline-step text-success"><i class="bi bi-check-circle me-1"></i>1. Measure</span></div>
  <div class="col-auto"><span class="pipeline-step text-success"><i class="bi bi-check-circle me-1"></i>2. Collect</span></div>
  <div class="col-auto"><span class="pipeline-step text-success"><i class="bi bi-check-circle me-1"></i>3. Store</span></div>
  <div class="col-auto"><span class="pipeline-step text-success"><i class="bi bi-check-circle me-1"></i>4. Calculate</span></div>
  <div class="col-auto"><span class="pipeline-step text-success"><i class="bi bi-check-circle me-1"></i>5. Display</span></div>
  <div class="col-auto"><span class="pipeline-step text-success"><i class="bi bi-check-circle me-1"></i>6. Control</span></div>
  <div class="col-auto"><span class="pipeline-step text-success"><i class="bi bi-check-circle me-1"></i>7. Business & Carbon Impact</span></div>
 </div>
 <small>SmartDorm System · IoT Sub-metering Architecture · Built for Bangladeshi Student Dormitories</small>
</footer>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body></html>
"""

LANDING_TEMPLATE = BASE_LAYOUT.replace("{% block content %}{% endblock %}", """
<div class="row align-items-center py-5 my-3">
    <div class="col-lg-7">
        <span class="badge bg-success bg-opacity-20 text-success border border-success border-opacity-25 mb-3 px-3 py-2 rounded-pill">
            <i class="bi bi-lightning-charge-fill me-1"></i> Measure → Collect → Store → Calculate → Display → Control
        </span>
        <h1 class="display-4 fw-bold lh-sm mb-3">
            Smart Sub-Metering System for <span class="text-success">Bangladeshi Dormitories</span>
        </h1>
        <p class="lead text-muted mb-4">
            End-to-end IoT platform featuring TinyTuya MCB hardware integration, accurate data storage, sub-billing allocation, and robust direct power breaker control.
        </p>
        <div class="d-flex gap-3">
            <a href="/dashboard" class="btn btn-eco btn-lg d-flex align-items-center gap-2">
                <i class="bi bi-speedometer2"></i> Open Command Center
            </a>
            <a href="/analytics" class="btn btn-ghost btn-lg rounded-3">
                View Impact Analysis
            </a>
        </div>
    </div>
    <div class="col-lg-5 mt-5 mt-lg-0">
        <div class="glass p-4 text-center">
            <i class="bi bi-shield-check text-success display-1 mb-3"></i>
            <h4>Integrated MCB Control</h4>
            <p class="text-muted small">Standardized control interface ensuring complete ON/OFF command reliability for dormitory sub-metering breakers over local LAN.</p>
        </div>
    </div>
</div>
"""
)

DASHBOARD_TEMPLATE = BASE_LAYOUT.replace("{% block content %}{% endblock %}", """
<div class="hero mb-4">
 <div class="row align-items-center g-4">
  <div class="col-lg-8">
   <div class="eyebrow mb-2"><i class="bi bi-broadcast me-1"></i> Real-time Energy & Control Dashboard</div>
   <h1 class="display-5 fw-bold mb-2">Smart Dormitory Energy OS</h1>
   <p class="lead text-muted mb-4">Monitor sub-meter telemetry, control individual MCB circuit breakers reliably, and track cost and environmental metrics.</p>
   <div class="d-flex flex-wrap gap-2">
    <a href="/analytics" class="btn btn-eco"><i class="bi bi-bar-chart-line-fill me-1"></i>Analytics & Trends</a>
    <button class="btn btn-ghost text-danger border-danger" onclick="allOff()"><i class="bi bi-power me-1"></i>Emergency Cutoff (All Off)</button>
   </div>
  </div>
  <div class="col-lg-4">
   <div class="glass p-4">
    <div class="d-flex justify-content-between"><span class="text-muted">Eco Efficiency Score</span><strong id="eco-score">—</strong></div>
    <div class="progress my-3" style="height: 10px; background: rgba(255,255,255,0.05);"><div class="progress-bar bg-success" id="eco-bar" style="width:0%"></div></div>
    <small class="text-muted">Calculated dynamically based on real-time room power loads.</small>
   </div>
  </div>
 </div>
</div>

<div class="row g-3 mb-4">
 {% for id,label,icon,sub in [
 ('kpi-power','Live Total Load','bi-lightning-charge-fill','Active Power (Watts)'),
 ('kpi-energy','Total Energy','bi-battery-charging','Tracked Meter Value'),
 ('kpi-cost','Estimated Expense','bi-currency-exchange','Rate: ৳12.5 / kWh'),
 ('kpi-carbon','Carbon Footprint','bi-cloud-haze2','Factor: 0.65 kg/kWh')
 ] %}
 <div class="col-6 col-xl-3"><div class="glass kpi h-100"><div class="d-flex justify-content-between align-items-start"><div><div class="kpi-label">{{label}}</div><div class="kpi-value mt-2" id="{{id}}">—</div><div class="text-muted small">{{sub}}</div></div><div class="kpi-icon"><i class="bi {{icon}}"></i></div></div></div></div>
 {% endfor %}
</div>

<!-- SIDE-BY-SIDE SECTION: OVERVIEW CHART + CONTROL PANEL -->
<div class="row g-4 mb-4">
 <!-- Live Overview Chart Section -->
 <div class="col-lg-7">
  <div class="glass p-4 h-100">
   <div class="d-flex justify-content-between align-items-center mb-3">
    <div>
     <h5 class="mb-0">Live Room Load Overview (W)</h5>
     <div class="text-muted small">Real-time electrical consumption graph</div>
    </div>
   </div>
   <canvas id="powerChart" height="180"></canvas>
  </div>
 </div>

 <!-- Control Panel Section -->
 <div class="col-lg-5">
  <div class="glass p-4 h-100">
   <div class="d-flex justify-content-between align-items-center mb-3">
    <div>
     <h5 class="mb-0"><i class="bi bi-toggle2-on text-success me-2"></i>Control Panel</h5>
     <div class="text-muted small">Manage individual switches & circuit breakers</div>
    </div>
    <span class="badge rounded-pill bg-dark border border-secondary" id="last-sync">Syncing…</span>
   </div>
   <div class="row g-3" id="rooms"></div>
  </div>
 </div>
</div>

<div class="row g-4">
 <div class="col-12">
  <div class="glass p-4">
   <h5>System Diagnostics</h5>
   <div class="row g-3 mt-2">
    <div class="col-md-4">
     <div class="d-flex justify-content-between p-2 rounded bg-dark bg-opacity-25"><span class="text-muted">Online MCBs</span><strong id="online-count">—</strong></div>
    </div>
    <div class="col-md-4">
     <div class="d-flex justify-content-between p-2 rounded bg-dark bg-opacity-25"><span class="text-muted">Highest Load Room</span><strong id="peak-room">—</strong></div>
    </div>
    <div class="col-md-4">
     <div class="d-flex justify-content-between p-2 rounded bg-dark bg-opacity-25"><span class="text-muted">Potential Carbon Saving</span><strong class="text-success" id="saving">—</strong></div>
    </div>
   </div>
   <hr style="border-color:var(--line)">
   <div class="p-3 rounded-3 bg-dark bg-opacity-50 border border-secondary small"><i class="bi bi-lightbulb-fill text-warning me-1"></i><span id="eco-tip">Loading recommendation…</span></div>
  </div>
 </div>
</div>

<script>
let powerChart;
function money(v){return '৳ '+Number(v||0).toLocaleString(undefined,{maximumFractionDigits:2});}

function init(){
  powerChart=new Chart(document.getElementById('powerChart'),{
    type:'bar',
    data:{labels:[],datasets:[{label:'Power (Watts)',data:[],backgroundColor:'#45e39a',borderRadius:8}]},
    options:{responsive:true,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true,grid:{color:'rgba(255,255,255,.06)'}},x:{grid:{display:false}}}}
  });
}

async function load(){
 const [summary,rooms]=await Promise.all([fetch('/api/summary?days=30').then(r=>r.json()),fetch('/api/rooms').then(r=>r.json())]);
 document.getElementById('kpi-power').textContent=summary.total_power_w+' W';
 document.getElementById('kpi-energy').textContent=summary.total_energy_kwh+' kWh';
 document.getElementById('kpi-cost').textContent=money(summary.total_cost_bdt);
 document.getElementById('kpi-carbon').textContent=summary.total_carbon_kg+' kg';
 document.getElementById('online-count').textContent=summary.online_count+' / '+rooms.length;
 
 const peak=[...rooms].sort((a,b)=>b.power-a.power)[0];
 document.getElementById('peak-room').textContent=peak?peak.name:'—';
 
 const score=Math.max(0,Math.min(100,Math.round(100-Math.min(100,(summary.total_power_w/(rooms.length*1000||1))*100))));
 document.getElementById('eco-score').textContent=score+'/100'; 
 document.getElementById('eco-bar').style.width=score+'%';
 document.getElementById('saving').textContent='≈ '+Math.max(0,Math.round(summary.total_carbon_kg*0.18))+' kg CO₂/mo';
 document.getElementById('eco-tip').textContent=peak&&peak.power>500?peak.name+' has the highest active load. Consider shedding idle loads.':'All sub-meters operating within normal load parameters.';
 document.getElementById('last-sync').textContent='Updated '+new Date().toLocaleTimeString();
 
 const c=document.getElementById('rooms'); 
 c.innerHTML='';
 rooms.forEach(r=>{
   const statusBadge = r.online ? '<span class="badge bg-success bg-opacity-20 text-success border border-success"><i class="status-dot online"></i>ONLINE</span>' : '<span class="badge bg-danger bg-opacity-20 text-danger border border-danger"><i class="status-dot offline"></i>OFFLINE</span>';
   const switchBadge = r.switch ? '<span class="badge bg-success"><i class="bi bi-power me-1"></i>MCB ON</span>' : '<span class="badge bg-secondary"><i class="bi bi-slash-circle me-1"></i>MCB OFF</span>';
   
   c.innerHTML+=`
   <div class="col-12">
     <div class="glass p-3 border border-secondary border-opacity-25">
       <div class="d-flex justify-content-between align-items-center mb-2">
         <div>
           <h6 class="mb-0 fw-bold">${r.name}</h6>
           <small class="text-muted">ID: ${r.id}</small>
         </div>
         <div class="d-flex gap-2">
           ${statusBadge}
           ${switchBadge}
         </div>
       </div>
       
       <div class="row g-2 my-2 p-2 bg-dark bg-opacity-50 rounded border border-secondary border-opacity-10 text-center">
         <div class="col-3"><div class="text-muted small" style="font-size: 0.65rem;">VOLTAGE</div><strong class="text-info small">${r.voltage} V</strong></div>
         <div class="col-3"><div class="text-muted small" style="font-size: 0.65rem;">CURRENT</div><strong class="text-info small">${r.current} A</strong></div>
         <div class="col-3"><div class="text-muted small" style="font-size: 0.65rem;">POWER</div><strong class="text-warning small">${r.power} W</strong></div>
         <div class="col-3"><div class="text-muted small" style="font-size: 0.65rem;">ENERGY</div><strong class="text-success small">${r.energy} kWh</strong></div>
       </div>

       <!-- FORM-BASED MCB CONTROL SYSTEM -->
       <form onsubmit="submitMcbForm(event, '${r.id}')" class="mt-2">
         <div class="input-group input-group-sm">
           <select id="select-mcb-${r.id}" class="form-select form-select-sm">
             <option value="true" ${r.switch ? 'selected' : ''}>TURN ON</option>
             <option value="false" ${!r.switch ? 'selected' : ''}>TURN OFF</option>
           </select>
           <button type="submit" id="btn-mcb-${r.id}" class="btn btn-sm btn-eco px-3">
             <i class="bi bi-send-fill me-1"></i> Apply
           </button>
         </div>
         <div id="mcb-msg-${r.id}" class="form-text text-muted mt-1 small"></div>
       </form>
     </div>
   </div>`;
 });
 powerChart.data.labels=rooms.map(r=>r.name); 
 powerChart.data.datasets[0].data=rooms.map(r=>r.power); 
 powerChart.update();
}

async function submitMcbForm(event, devId){
 event.preventDefault();
 const selectElem = document.getElementById('select-mcb-' + devId);
 const btnElem = document.getElementById('btn-mcb-' + devId);
 const msgElem = document.getElementById('mcb-msg-' + devId);
 
 const targetState = selectElem.value === 'true';
 btnElem.disabled = true;
 msgElem.innerHTML = '<span class="text-info"><i class="bi bi-hourglass-split me-1"></i>Sending command over LAN...</span>';

 try {
  const res = await fetch('/api/control/' + encodeURIComponent(devId), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({switch: targetState})
  });
  const data = await res.json();
  if(data.success){
    msgElem.innerHTML = '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>MCB state changed successfully!</span>';
    setTimeout(load, 1000);
  } else {
    msgElem.innerHTML = '<span class="text-danger"><i class="bi bi-exclamation-triangle-fill me-1"></i>Error: ' + (data.error || 'Control failed') + '</span>';
  }
 } catch(e) {
    msgElem.innerHTML = '<span class="text-danger"><i class="bi bi-exclamation-triangle-fill me-1"></i>Network Error: ' + e + '</span>';
 }
 btnElem.disabled = false;
}

async function allOff(){
 if(!confirm('Emergency Action: Turn OFF all dormitory room circuit breakers?')) return;
 const res=await fetch('/api/control/all-off',{method:'POST'}); 
 const data=await res.json();
 if(data.failed&&data.failed.length) alert('Failed for rooms: '+data.failed.join(', ')); 
 await load();
}

document.addEventListener('DOMContentLoaded',()=>{init();load();setInterval(load,10000);});
</script>
""")

CONTROL_PANEL_TEMPLATE = BASE_LAYOUT.replace("{% block content %}{% endblock %}", """
<div class="d-flex justify-content-between align-items-center mb-4">
    <div>
        <div class="eyebrow"><i class="bi bi-toggle2-on me-1"></i> Sub-Meter Hardware Management</div>
        <h3 class="fw-bold mb-0">Control Panel</h3>
        <p class="text-muted small mb-0">Manage circuit breaker switches and hardware controls directly over local LAN.</p>
    </div>
    <div>
        <button class="btn btn-danger btn-sm px-3" onclick="allOff()"><i class="bi bi-power me-1"></i> Emergency All Off</button>
    </div>
</div>

<div class="row g-4" id="control-grid">
    <div class="col-12 text-center text-muted py-5">
        <div class="spinner-border text-success mb-2" role="status"></div>
        <div>Loading device statuses...</div>
    </div>
</div>

<script>
async function loadControlPanel(){
    try {
        const rooms = await fetch('/api/rooms').then(r => r.json());
        const grid = document.getElementById('control-grid');
        grid.innerHTML = '';

        rooms.forEach(r => {
            const statusBadge = r.online 
                ? '<span class="badge bg-success bg-opacity-20 text-success border border-success"><i class="status-dot online"></i>ONLINE</span>' 
                : '<span class="badge bg-danger bg-opacity-20 text-danger border border-danger"><i class="status-dot offline"></i>OFFLINE</span>';
            const switchBadge = r.switch 
                ? '<span class="badge bg-success"><i class="bi bi-power me-1"></i>MCB ON</span>' 
                : '<span class="badge bg-secondary"><i class="bi bi-slash-circle me-1"></i>MCB OFF</span>';

            grid.innerHTML += `
            <div class="col-md-6 col-lg-4">
                <div class="glass p-4 h-100">
                    <div class="d-flex justify-content-between align-items-center mb-3">
                        <div>
                            <h5 class="fw-bold mb-0">${r.name}</h5>
                            <small class="text-muted">ID: ${r.id}</small>
                        </div>
                        <div class="d-flex gap-1 flex-column align-items-end">
                            ${statusBadge}
                            ${switchBadge}
                        </div>
                    </div>

                    <div class="row g-2 my-3 p-2 bg-dark bg-opacity-50 rounded border border-secondary border-opacity-10 text-center">
                        <div class="col-6 mb-2"><div class="text-muted small">VOLTAGE</div><strong class="text-info">${r.voltage} V</strong></div>
                        <div class="col-6 mb-2"><div class="text-muted small">CURRENT</div><strong class="text-info">${r.current} A</strong></div>
                        <div class="col-6"><div class="text-muted small">POWER</div><strong class="text-warning">${r.power} W</strong></div>
                        <div class="col-6"><div class="text-muted small">ENERGY</div><strong class="text-success">${r.energy} kWh</strong></div>
                    </div>

                    <form onsubmit="submitMcbForm(event, '${r.id}')" class="mt-3">
                        <label class="form-label text-muted small">Circuit Breaker Power Toggle</label>
                        <div class="input-group">
                            <select id="select-mcb-${r.id}" class="form-select">
                                <option value="true" ${r.switch ? 'selected' : ''}>TURN ON</option>
                                <option value="false" ${!r.switch ? 'selected' : ''}>TURN OFF</option>
                            </select>
                            <button type="submit" id="btn-mcb-${r.id}" class="btn btn-eco px-3">
                                <i class="bi bi-send-fill me-1"></i> Apply
                            </button>
                        </div>
                        <div id="mcb-msg-${r.id}" class="form-text text-muted mt-2 small"></div>
                    </form>
                </div>
            </div>`;
        });
    } catch(e) {
        console.error('Failed to load control panel devices', e);
    }
}

async function submitMcbForm(event, devId){
    event.preventDefault();
    const selectElem = document.getElementById('select-mcb-' + devId);
    const btnElem = document.getElementById('btn-mcb-' + devId);
    const msgElem = document.getElementById('mcb-msg-' + devId);
    
    const targetState = selectElem.value === 'true';
    btnElem.disabled = true;
    msgElem.innerHTML = '<span class="text-info"><i class="bi bi-hourglass-split me-1"></i>Sending command over LAN...</span>';

    try {
        const res = await fetch('/api/control/' + encodeURIComponent(devId), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({switch: targetState})
        });
        const data = await res.json();
        if(data.success){
            msgElem.innerHTML = '<span class="text-success"><i class="bi bi-check-circle-fill me-1"></i>MCB state changed successfully!</span>';
            setTimeout(loadControlPanel, 1000);
        } else {
            msgElem.innerHTML = '<span class="text-danger"><i class="bi bi-exclamation-triangle-fill me-1"></i>Error: ' + (data.error || 'Control failed') + '</span>';
        }
    } catch(e) {
        msgElem.innerHTML = '<span class="text-danger"><i class="bi bi-exclamation-triangle-fill me-1"></i>Network Error: ' + e + '</span>';
    }
    btnElem.disabled = false;
}

async function allOff(){
    if(!confirm('Emergency Action: Turn OFF all dormitory room circuit breakers?')) return;
    const res = await fetch('/api/control/all-off',{method:'POST'}); 
    const data = await res.json();
    if(data.failed&&data.failed.length) alert('Failed for rooms: '+data.failed.join(', ')); 
    await loadControlPanel();
}

document.addEventListener('DOMContentLoaded', () => {
    loadControlPanel();
    setInterval(loadControlPanel, 10000);
});
</script>
"""
)

ANALYTICS_TEMPLATE = BASE_LAYOUT.replace("{% block content %}{% endblock %}", """
<div class="d-flex justify-content-between align-items-end flex-wrap gap-3 mb-4">
 <div><div class="eyebrow"><i class="bi bi-graph-up me-1"></i>Analytical Data Processing</div><h2 class="mb-1">Energy & Cost Analytics</h2><div class="text-muted">Aggregated sub-metering trends mined directly from stored database history.</div></div>
 <div class="btn-group" id="period-seg">
  <button class="btn btn-outline-success active" data-p="daily" onclick="setPeriod('daily',this)">Daily</button>
  <button class="btn btn-outline-success" data-p="monthly" onclick="setPeriod('monthly',this)">Monthly</button>
  <button class="btn btn-outline-success" data-p="yearly" onclick="setPeriod('yearly',this)">Yearly</button>
 </div>
</div>

<div class="row g-3 mb-4">
 <div class="col-6 col-lg-3"><div class="glass kpi h-100"><div class="kpi-label">Consumption</div><div class="kpi-value text-success" id="a-energy">—</div></div></div>
 <div class="col-6 col-lg-3"><div class="glass kpi h-100"><div class="kpi-label">Estimated Bill</div><div class="kpi-value text-info" id="a-cost">—</div></div></div>
 <div class="col-6 col-lg-3"><div class="glass kpi h-100"><div class="kpi-label">CO₂ Emission</div><div class="kpi-value text-warning" id="a-carbon">—</div></div></div>
 <div class="col-6 col-lg-3"><div class="glass kpi h-100"><div class="kpi-label">Peak Demand</div><div class="kpi-value text-danger" id="a-peak">—</div></div></div>
</div>

<div class="glass p-4 mb-4">
 <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-2">
  <div><h5 class="mb-0" id="chart-title">Energy Consumption Trend</h5><div class="text-muted small">kWh tracked over selected timeframe</div></div>
  <div class="text-muted small" id="chart-total">—</div>
 </div>
 <div class="chart-wrap"><canvas id="trendChart" height="95"></canvas></div>
</div>

<div class="row g-4 mb-4">
 <div class="col-lg-5">
  <div class="glass p-4 h-100">
   <h5 class="mb-0">Dorm Load Distribution</h5><div class="text-muted small mb-3">Share of energy consumption by room</div>
   <div class="chart-wrap" style="max-width:260px;margin:0 auto"><canvas id="shareChart" height="230"></canvas></div>
  </div>
 </div>
 <div class="col-lg-7">
  <div class="glass p-4 h-100">
   <h5 class="mb-0">Room Sub-Meter Comparison</h5><div class="text-muted small mb-3">Absolute consumption per room</div>
   <canvas id="roomChart" height="150"></canvas>
  </div>
 </div>
</div>

<script>
let period='daily',trend,roomChart,shareChart;
const PALETTE=['#45e39a','#52d7e8','#ffc857','#ff6b7a','#b8f26b'];

function money(v){return '৳ '+Number(v||0).toLocaleString(undefined,{maximumFractionDigits:0});}

function setPeriod(p,b){
 period=p;
 document.querySelectorAll('#period-seg button').forEach(x=>x.classList.remove('active'));
 b.classList.add('active');
 loadAnalytics();
}

function initCharts(){
 const tctx=document.getElementById('trendChart').getContext('2d');
 trend=new Chart(tctx,{type:'line',data:{labels:[],datasets:[{label:'kWh',data:[],borderColor:'#45e39a',backgroundColor:'rgba(69,227,154,0.1)',fill:true,tension:0.3}]},options:{responsive:true,scales:{y:{beginAtZero:true,grid:{color:'rgba(255,255,255,.06)'}},x:{grid:{display:false}}}}});
 roomChart=new Chart(document.getElementById('roomChart'),{type:'bar',data:{labels:[],datasets:[{label:'kWh',data:[],backgroundColor:'#52d7e8',borderRadius:8}]},options:{responsive:true,scales:{y:{beginAtZero:true,grid:{color:'rgba(255,255,255,.06)'}},x:{grid:{display:false}}}}});
 shareChart=new Chart(document.getElementById('shareChart'),{type:'doughnut',data:{labels:[],datasets:[{data:[],backgroundColor:PALETTE}]},options:{responsive:true,cutout:'65%'}});
}

async function loadAnalytics(){
 const d=await fetch('/api/analytics?period='+period).then(r=>r.json());
 document.getElementById('a-energy').textContent=d.total_energy_kwh.toFixed(2)+' kWh';
 document.getElementById('a-cost').textContent=money(d.cost_bdt);
 document.getElementById('a-carbon').textContent=d.carbon_kg.toFixed(2)+' kg';
 document.getElementById('a-peak').textContent=d.peak_power_w.toFixed(0)+' W';
 document.getElementById('chart-title').textContent=(period[0].toUpperCase()+period.slice(1))+' Energy Consumption';
 document.getElementById('chart-total').textContent=d.total_energy_kwh.toFixed(1)+' kWh aggregate';
 
 trend.data.labels=d.labels; trend.data.datasets[0].data=d.energy_values; trend.update();
 roomChart.data.labels=d.room_labels; roomChart.data.datasets[0].data=d.room_values; roomChart.update();
 shareChart.data.labels=d.room_labels; shareChart.data.datasets[0].data=d.room_values; shareChart.update();
}

document.addEventListener('DOMContentLoaded',()=>{initCharts();loadAnalytics();});
</script>
""")

BILLING_TEMPLATE = BASE_LAYOUT.replace("{% block content %}{% endblock %}", """
<div class="mb-4">
    <div class="eyebrow"><i class="bi bi-calculator me-1"></i> Business & Financial Module</div>
    <h3 class="fw-bold mb-0">Fair Sub-Billing Allocation Engine</h3>
    <p class="text-muted small">Automated division of utility bills calculated from actual sub-meter telemetry vs traditional flat-rate splitting.</p>
</div>

<div class="row g-4">
    <div class="col-lg-4">
        <div class="glass p-4">
            <h5 class="fw-bold mb-3">Master Utility Invoice</h5>
            <form id="billing-form" onsubmit="event.preventDefault(); calculateBilling();">
                <div class="mb-3">
                    <label class="form-label small text-muted">Total Dormitory Monthly Bill (BDT)</label>
                    <div class="input-group">
                        <span class="input-group-text bg-dark border-secondary text-white">৳</span>
                        <input type="number" id="master-bill-input" class="form-control" placeholder="Loading estimate..." step="50">
                    </div>
                    <div class="form-text text-muted" id="bill-source-note">Pre-filled from metered consumption. Replace with the real DESCO/utility invoice total when it arrives, then recalculate.</div>
                </div>
                <button type="submit" class="btn btn-eco w-100">
                    <i class="bi bi-calculator me-1"></i> Calculate Fair Splits
                </button>
            </form>
        </div>
    </div>
    <div class="col-lg-8">
        <div class="glass p-4">
            <h5 class="fw-bold mb-3">Room Discrepancy Matrix</h5>
            <div class="table-responsive">
                <table class="table align-middle text-white">
                    <thead>
                        <tr class="text-muted small">
                            <th>ROOM</th>
                            <th>CONSUMPTION</th>
                            <th>FLAT SPLIT</th>
                            <th>SUB-METERED</th>
                            <th>VARIANCE</th>
                        </tr>
                    </thead>
                    <tbody id="billing-table-body">
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</div>

<script>
    // On first load (or whenever the input is left blank) we don't pass
    // total_bill at all, so the backend fills in the metered estimate
    // (same rate/window as Overview & Analytics) and tells us via
    // is_estimate. Once the user types their own figure and submits,
    // that override is sent and used instead.
    function calculateBilling() {
        const raw = document.getElementById('master-bill-input').value;
        const url = raw ? '/api/billing?total_bill=' + encodeURIComponent(raw) : '/api/billing';
        fetch(url)
            .then(res => res.json())
            .then(data => {
                const input = document.getElementById('master-bill-input');
                const note = document.getElementById('bill-source-note');
                if (data.is_estimate) {
                    input.value = data.total_bill;
                    note.textContent = 'Estimated from metered consumption (৳' + data.estimated_bill_bdt + '). Replace with the real utility invoice total when it arrives, then recalculate.';
                } else {
                    note.textContent = 'Using your entered invoice total. Metered estimate for comparison: ৳' + data.estimated_bill_bdt + '.';
                }

                const tbody = document.getElementById('billing-table-body');
                tbody.innerHTML = '';
                data.rooms.forEach(r => {
                    const diffClass = r.difference > 0 ? 'text-danger' : 'text-success';
                    const diffSign = r.difference > 0 ? '+' : '';
                    tbody.innerHTML += `
                        <tr>
                            <td class="fw-bold">${r.name}</td>
                            <td>${r.energy_kwh} kWh (${r.share_percent}%)</td>
                            <td>৳ ${r.equal_split}</td>
                            <td class="fw-bold text-success">৳ ${r.consumption_split}</td>
                            <td class="${diffClass} fw-bold">${diffSign}৳ ${r.difference}</td>
                        </tr>
                    `;
                });
            });
    }
    document.addEventListener('DOMContentLoaded', calculateBilling);
</script>
"""
)

CARBON_TEMPLATE = BASE_LAYOUT.replace("{% block content %}{% endblock %}", """
<div class="mb-4">
    <div class="eyebrow"><i class="bi bi-tree me-1"></i> Environmental Module</div>
    <h3 class="fw-bold mb-0">Environmental Footprint Analysis</h3>
    <p class="text-muted small">Calculated using Bangladesh Grid Carbon Factor (0.65 kg CO₂ / kWh)</p>
</div>

<div class="row g-4 mb-4">
    <div class="col-md-6">
        <div class="glass p-4 text-center">
            <i class="bi bi-cloud-haze2 text-danger display-4 mb-2"></i>
            <h6 class="text-muted">Highest Emitting Sub-Unit</h6>
            <h2 class="fw-bold text-danger my-2" id="highest-room">-</h2>
            <p class="text-muted small mb-0">Flagged for potential load shedding advisory</p>
        </div>
    </div>
    <div class="col-md-6">
        <div class="glass p-4 text-center">
            <i class="bi bi-leaf text-success display-4 mb-2"></i>
            <h6 class="text-muted">Eco-Champion Sub-Unit</h6>
            <h2 class="fw-bold text-success my-2" id="lowest-room">-</h2>
            <p class="text-muted small mb-0">Lowest relative energy intensity</p>
        </div>
    </div>
</div>

<div class="glass p-4">
    <h5 class="fw-bold mb-3">Sub-Room CO₂ Generation (kg)</h5>
    <canvas id="chart-carbon" style="max-height: 300px;"></canvas>
</div>

<script>
    document.addEventListener('DOMContentLoaded', () => {
        fetch('/api/carbon')
            .then(res => res.json())
            .then(data => {
                document.getElementById('highest-room').innerText = data.highest_room;
                document.getElementById('lowest-room').innerText = data.lowest_room;

                const ctx = document.getElementById('chart-carbon').getContext('2d');
                new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: data.details.map(d => d.name),
                        datasets: [{
                            label: 'CO2 Emissions (kg)',
                            data: data.details.map(d => d.carbon_kg),
                            backgroundColor: '#45e39a',
                            borderRadius: 8
                        }]
                    },
                    options: { responsive: true, scales: { y: { grid: { color: 'rgba(255,255,255,0.05)' } } } }
                });
            });
    });
</script>
"""
)

HISTORY_TEMPLATE = BASE_LAYOUT.replace("{% block content %}{% endblock %}", """
<div class="d-flex justify-content-between align-items-center mb-4">
    <div>
        <div class="eyebrow"><i class="bi bi-database me-1"></i> Data Storage Layer</div>
        <h3 class="fw-bold mb-0">Historical Consumption Log</h3>
        <p class="text-muted small mb-0">Stored telemetry logs queried from smartdorm.db</p>
    </div>
    <div>
        <select id="days-select" class="form-select form-select-sm" onchange="loadHistory()" style="width: auto;">
            <option value="7">Last 7 days</option>
            <option value="30" selected>Last 30 days</option>
            <option value="180">Last 180 days</option>
        </select>
    </div>
</div>

<div class="row g-3 mb-4" id="db-stats-row">
    <div class="col-6 col-lg-3"><div class="glass p-3"><span class="text-muted small">Total Readings</span><div class="h3 fw-bold text-success my-1" id="stat-total">-</div></div></div>
    <div class="col-6 col-lg-3"><div class="glass p-3"><span class="text-muted small">Devices Tracked</span><div class="h3 fw-bold text-info my-1" id="stat-devices">-</div></div></div>
    <div class="col-6 col-lg-3"><div class="glass p-3"><span class="text-muted small">First Reading</span><div class="fw-bold text-white my-1" style="font-size:0.95rem;" id="stat-first">-</div></div></div>
    <div class="col-6 col-lg-3"><div class="glass p-3"><span class="text-muted small">Last Reading</span><div class="fw-bold text-white my-1" style="font-size:0.95rem;" id="stat-last">-</div></div></div>
</div>

<div class="glass p-4 mb-4">
    <h5 class="fw-bold mb-3">Daily Average Power (Watts)</h5>
    <canvas id="chart-daily" style="max-height: 320px;"></canvas>
</div>

<div class="glass p-4">
    <h5 class="fw-bold mb-3">Stored Database Records</h5>
    <div class="table-responsive">
        <table class="table align-middle text-white">
            <thead>
                <tr class="text-muted small">
                    <th>DATE</th><th>ROOM</th><th>AVG POWER</th><th>PEAK POWER</th><th>AVG VOLTAGE</th><th>ENERGY USED</th><th>SAMPLES</th>
                </tr>
            </thead>
            <tbody id="history-table-body"></tbody>
        </table>
    </div>
</div>

<script>
    let dailyChart;

    function initDailyChart() {
        const ctx = document.getElementById('chart-daily').getContext('2d');
        dailyChart = new Chart(ctx, {
            type: 'line',
            data: { datasets: [] },
            options: { responsive: true, scales: { y: { grid: { color: 'rgba(255,255,255,0.05)' } }, x: { grid: { display: false } } } }
        });
    }

    function loadHistory() {
        const days = document.getElementById('days-select').value;
        fetch('/api/database')
            .then(res => res.json())
            .then(stats => {
                document.getElementById('stat-total').innerText = stats.total_readings ?? 0;
                document.getElementById('stat-devices').innerText = stats.device_count ?? 0;
                document.getElementById('stat-first').innerText = stats.first_reading ? stats.first_reading.substring(0, 16).replace('T', ' ') : '-';
                document.getElementById('stat-last').innerText = stats.last_reading ? stats.last_reading.substring(0, 16).replace('T', ' ') : '-';
            });

        fetch('/api/history/daily?days=' + days)
            .then(res => res.json())
            .then(rows => {
                const tbody = document.getElementById('history-table-body');
                tbody.innerHTML = '';
                const byRoom = {};
                const allDays = [];

                rows.forEach(r => {
                    if (!byRoom[r.device_name]) byRoom[r.device_name] = {};
                    byRoom[r.device_name][r.day] = r.average_power_w;
                    if (!allDays.includes(r.day)) allDays.push(r.day);

                    tbody.innerHTML += `
                        <tr>
                            <td>${r.day}</td>
                            <td class="fw-bold">${r.device_name}</td>
                            <td>${r.average_power_w} W</td>
                            <td class="text-warning">${r.peak_power_w} W</td>
                            <td>${r.average_voltage_v} V</td>
                            <td class="text-success">${r.energy_used_kwh} kWh</td>
                            <td class="text-muted">${r.samples}</td>
                        </tr>
                    `;
                });

                allDays.sort();
                const colors = ['#45e39a', '#52d7e8', '#ffc857', '#ff6b7a'];
                const datasets = Object.keys(byRoom).map((room, i) => ({
                    label: room,
                    data: allDays.map(d => byRoom[room][d] ?? null),
                    borderColor: colors[i % colors.length],
                    backgroundColor: colors[i % colors.length],
                    tension: 0.3,
                }));

                dailyChart.data.labels = allDays;
                dailyChart.data.datasets = datasets;
                dailyChart.update();
            });
    }

    document.addEventListener('DOMContentLoaded', () => {
        initDailyChart();
        loadHistory();
    });
</script>
"""
)

SETTINGS_TEMPLATE = BASE_LAYOUT.replace("{% block content %}{% endblock %}", """
<div class="mb-4">
    <div class="eyebrow"><i class="bi bi-gear me-1"></i> System Operations</div>
    <h3 class="fw-bold mb-0">System Configuration & Data Collector Control</h3>
    <p class="text-muted small">Manage rate parameters and local telemetry daemon process.</p>
</div>

<div class="row g-4">
    <div class="col-lg-6">
        <div class="glass p-4">
            <h5 class="fw-bold mb-3">Tariff & Environmental Constants</h5>
            <form onsubmit="event.preventDefault(); alert('Parameters saved successfully!');">
                <div class="mb-3">
                    <label class="form-label text-muted small">Electricity Tariff Rate (BDT / kWh)</label>
                    <input type="number" class="form-control" value="12.5" step="0.1">
                </div>
                <div class="mb-3">
                    <label class="form-label text-muted small">Grid Carbon Conversion Factor (kg CO₂ / kWh)</label>
                    <input type="number" class="form-control" value="0.65" step="0.01">
                </div>
                <div class="mb-3">
                    <label class="form-label text-muted small">Telemetry Refresh Interval (Seconds)</label>
                    <input type="number" class="form-control" value="10">
                </div>
                <button type="submit" class="btn btn-eco">Save System Parameters</button>
            </form>
        </div>
    </div>
    <div class="col-lg-6">
        <div class="glass p-4 mb-4">
            <h5 class="fw-bold mb-3">Background Data Collector Daemon</h5>
            <p class="text-muted small">Controls <code>data_collector.py</code> background process for real-time polling and SQLite DB insertion.</p>
            <div class="p-3 bg-dark bg-opacity-50 rounded-3 mb-3">
                <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
                    <div class="d-flex align-items-center gap-2">
                        <span id="collector-pulse" class="rounded-circle" style="width:10px;height:10px;background:#6c757d;display:inline-block;"></span>
                        <span id="collector-status-badge" class="badge bg-secondary">Checking...</span>
                    </div>
                    <div class="text-muted small" id="collector-meta">—</div>
                </div>
            </div>
            <div class="d-flex gap-2 mb-4">
                <button type="button" class="btn btn-eco flex-fill" onclick="startCollector()"><i class="bi bi-play-fill me-1"></i>Start Collection Process</button>
                <button type="button" class="btn btn-outline-danger flex-fill" onclick="stopCollector()"><i class="bi bi-stop-fill me-1"></i>Stop Process</button>
            </div>

            <h6 class="fw-bold text-muted small mb-2">LIVE PER-ROOM TELEMETRY</h6>
            <div class="row g-3 mb-4" id="collector-device-cards">
                <div class="col-12 text-muted small">No device data yet — start the collector to see live readings.</div>
            </div>

            <h6 class="fw-bold text-muted small mb-2">COLLECTOR LOG</h6>
            <div id="collector-console" style="background:#0b0f10;border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:10px 12px;height:220px;overflow-y:auto;font-family:'Courier New',monospace;font-size:12px;line-height:1.5;"></div>
        </div>
    </div>
</div>

<script>
    const COLLECTOR_TAG_COLOR = { tuya: '#52d7e8', collector: '#45e39a', error: '#ff6b6b', info: '#9aa0a6' };

    function renderDeviceCards(devices) {
        const wrap = document.getElementById('collector-device-cards');
        const names = Object.keys(devices || {});
        if (!names.length) {
            wrap.innerHTML = '<div class="col-12 text-muted small">No device data yet — start the collector to see live readings.</div>';
            return;
        }
        wrap.innerHTML = names.map(name => {
            const d = devices[name];
            const onlineColor = d.online ? '#45e39a' : '#6c757d';
            const switchLabel = d.switch ? 'ON' : 'OFF';
            const switchColor = d.switch ? '#45e39a' : '#ff6b6b';
            const powerPct = Math.max(0, Math.min(100, (d.power / 20)));
            return `
                <div class="col-md-6">
                    <div class="p-3 rounded-3" style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span class="fw-bold small"><span class="rounded-circle d-inline-block me-1" style="width:8px;height:8px;background:${onlineColor};"></span>${name}</span>
                            <span class="badge" style="background:${switchColor};color:#0b0f10;">${switchLabel}</span>
                        </div>
                        <div class="d-flex justify-content-between small text-muted"><span>Power</span><span class="text-white">${d.power.toFixed(1)} W</span></div>
                        <div class="progress mb-2" style="height:5px;background:rgba(255,255,255,.08);">
                            <div class="progress-bar" style="width:${powerPct}%;background:#52d7e8;"></div>
                        </div>
                        <div class="row small text-muted g-1">
                            <div class="col-4">V <span class="text-white">${d.voltage}</span></div>
                            <div class="col-4">A <span class="text-white">${d.current}</span></div>
                            <div class="col-4">kWh <span class="text-white">${d.energy}</span></div>
                        </div>
                        <div class="text-muted small mt-1">Updated ${d.timestamp || '—'}</div>
                    </div>
                </div>
            `;
        }).join('');
    }

    function renderConsole(lines) {
        const el = document.getElementById('collector-console');
        const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
        el.innerHTML = (lines || []).map(l => {
            const color = COLLECTOR_TAG_COLOR[l.tag] || '#9aa0a6';
            return `<div><span style="color:#6c757d;">${l.ts}</span> <span style="color:${color};">${l.text.replace(/</g,'&lt;')}</span></div>`;
        }).join('');
        if (nearBottom) el.scrollTop = el.scrollHeight;
    }

    function refreshCollectorStatus() {
        fetch('/api/collector/status')
            .then(res => res.json())
            .then(data => {
                const badge = document.getElementById('collector-status-badge');
                const pulse = document.getElementById('collector-pulse');
                const meta = document.getElementById('collector-meta');
                if (data.running) {
                    badge.textContent = 'RUNNING (PID ' + data.pid + ')';
                    badge.className = 'badge bg-success';
                    pulse.style.background = '#45e39a';
                    pulse.style.boxShadow = '0 0 8px #45e39a';
                } else {
                    badge.textContent = 'STOPPED';
                    badge.className = 'badge bg-secondary';
                    pulse.style.background = '#6c757d';
                    pulse.style.boxShadow = 'none';
                }
                const savedBit = data.saved_total ? (data.saved_total + ' readings saved' + (data.last_saved_at ? ' · last ' + data.last_saved_at : '')) : 'No readings saved yet';
                meta.textContent = savedBit;

                renderDeviceCards(data.devices);
                renderConsole(data.log);
            });
    }

    function startCollector() {
        fetch('/api/collector/start', { method: 'POST' }).then(() => refreshCollectorStatus());
    }

    function stopCollector() {
        fetch('/api/collector/stop', { method: 'POST' }).then(() => refreshCollectorStatus());
    }

    document.addEventListener('DOMContentLoaded', () => {
        refreshCollectorStatus();
        setInterval(refreshCollectorStatus, 2000);
    });
</script>
"""
)

# ------------------------------------------------------------------------------
# FLASK ROUTING & API ENDPOINTS
# ------------------------------------------------------------------------------
@app.route('/')
def route_landing():
    return render_template_string(LANDING_TEMPLATE, active_page='landing')

@app.route('/dashboard')
def route_dashboard():
    return render_template_string(DASHBOARD_TEMPLATE, active_page='dashboard')

@app.route('/history')
def route_history():
    return render_template_string(HISTORY_TEMPLATE, active_page='history')

@app.route('/control')
def route_control():
    return render_template_string(CONTROL_PANEL_TEMPLATE, active_page='control')

@app.route('/analytics')
def route_analytics():
    return render_template_string(ANALYTICS_TEMPLATE, active_page='analytics')

@app.route('/billing')
def route_billing():
    return render_template_string(BILLING_TEMPLATE, active_page='billing')

@app.route('/carbon')
def route_carbon():
    return render_template_string(CARBON_TEMPLATE, active_page='carbon')

@app.route('/settings')
def route_settings():
    return render_template_string(SETTINGS_TEMPLATE, active_page='settings')

# --- REST APIs ---
@app.route('/api/control/<dev_id>', methods=['POST'])
def api_control(dev_id):
    payload = request.get_json(silent=True) or {}
    if 'switch' not in payload:
        return jsonify({"success": False, "error": "Missing 'switch' parameter"}), 400
    state = payload.get('switch')
    if not isinstance(state, bool):
        return jsonify({"success": False, "error": "'switch' must be boolean"}), 400
    result = tuya_manager.control_device(dev_id, state)
    return jsonify(result), (200 if result.get("success") else 502)

@app.route('/api/control/all-off', methods=['POST'])
def api_control_all_off():
    failed = []
    for dev_id in tuya_manager.devices:
        result = tuya_manager.control_device(dev_id, False)
        if not result.get("success"):
            failed.append(tuya_manager.devices[dev_id]["name"])
    return jsonify({"success": not failed, "failed": failed})

@app.route('/api/rooms')
def api_rooms():
    latest = {row['device_id']: row for row in get_latest_readings()}
    rooms = []
    for dev_id, cfg in DEVICE_CONFIG.items():
        row = latest.get(dev_id)
        if row:
            rooms.append({
                'id': dev_id,
                'name': row['device_name'],
                'voltage': row['voltage'],
                'current': row['current'],
                'power': row['power'],
                'energy': row['energy_kwh'],
                'online': bool(row['online']),
                'switch': bool(row['switch_on']),
                'timestamp': row['timestamp'],
            })
        else:
            rooms.append({
                'id': dev_id, 'name': cfg['name'], 'voltage': 0.0, 'current': 0.0,
                'power': 0.0, 'energy': 0.0, 'online': False, 'switch': False, 'timestamp': 'No data yet',
            })
    return jsonify(rooms)

@app.route('/api/summary')
def api_summary():
    days = max(1, min(int(request.args.get('days', 180)), 3650))
    latest = get_latest_readings()
    totals = get_energy_summary(days=days)

    total_power = sum(r['power'] for r in latest if r['online'])
    total_energy = sum(t['energy_used_kwh'] for t in totals)
    total_cost = round(total_energy * DEFAULT_ELECTRICITY_RATE, 2)
    total_carbon = round(total_energy * DEFAULT_CARBON_FACTOR, 2)

    return jsonify({
        'total_power_w': round(total_power, 1),
        'total_energy_kwh': round(total_energy, 2),
        'total_cost_bdt': total_cost,
        'total_carbon_kg': total_carbon,
        'online_count': len([r for r in latest if r['online']]),
        'offline_count': len([r for r in latest if not r['online']]),
    })

@app.route('/api/analytics')
def api_analytics():
    period = request.args.get('period', 'daily').lower()
    # Real trailing windows per period, instead of always summing all
    # stored history. 'daily' uses the same 30-day window as the Overview
    # KPI so the two pages report the same totals.
    PERIOD_WINDOW_DAYS = {'daily': 30, 'monthly': 365, 'yearly': 3650}
    if period not in PERIOD_WINDOW_DAYS:
        period = 'daily'
    window_days = PERIOD_WINDOW_DAYS[period]

    # Top-line KPIs: same function + same window as /api/summary so the
    # numbers agree with the Overview page instead of drifting apart.
    totals = get_energy_summary(days=window_days)
    total_energy = sum(t['energy_used_kwh'] for t in totals)
    total_cost = round(total_energy * DEFAULT_ELECTRICITY_RATE, 2)
    carbon = round(total_energy * DEFAULT_CARBON_FACTOR, 2)

    # Trend chart / per-room breakdown, scoped to the same window (rather
    # than always pulling all 3650 days of history regardless of period).
    rows = get_daily_summary(days=window_days)
    buckets = defaultdict(lambda: {"energy": 0.0, "peak": 0.0, "rooms": defaultdict(float)})
    for r in rows:
        day = str(r.get('day', ''))[:10]
        if not day:
            continue
        try:
            dt = datetime.fromisoformat(day)
        except ValueError:
            continue
        key = dt.strftime('%Y-%m-%d' if period == 'daily' else '%Y-%m' if period == 'monthly' else '%Y')
        energy = float(r.get('energy_used_kwh') or 0)
        peak = float(r.get('peak_power_w') or 0)
        buckets[key]["energy"] += energy
        buckets[key]["peak"] = max(buckets[key]["peak"], peak)
        buckets[key]["rooms"][r.get('device_name', 'Unknown')] += energy

    labels = sorted(buckets.keys())
    energy_values = [round(buckets[k]["energy"], 3) for k in labels]
    peak = max((buckets[k]["peak"] for k in labels), default=0)
    all_room = defaultdict(float)
    for k in labels:
        for room, value in buckets[k]["rooms"].items():
            all_room[room] += value
    room_items = sorted(all_room.items(), key=lambda x: x[1], reverse=True)

    intensity = total_energy / max(1, len(labels))
    
    return jsonify({
        "period": period,
        "window_days": window_days,
        "labels": labels,
        "energy_values": energy_values,
        "total_energy_kwh": round(total_energy, 2),
        "cost_bdt": total_cost,
        "carbon_kg": carbon,
        "peak_power_w": peak,
        "room_labels": [x[0] for x in room_items],
        "room_values": [round(x[1], 3) for x in room_items],
        "intensity": intensity,
    })

@app.route('/api/billing')
def api_billing():
    days = max(1, min(int(request.args.get('days', DEFAULT_BILLING_DAYS)), 3650))
    totals = get_energy_summary(days=days)

    total_kwh = sum(t['energy_used_kwh'] for t in totals) or 1.0

    # Estimated bill from actual sub-meter telemetry, using the same rate
    # and (by default) the same trailing window as the Overview/Analytics
    # cost figures, so this page agrees with the rest of the dashboard
    # until the user overrides it with a real utility invoice amount.
    estimated_bill = round(total_kwh * DEFAULT_ELECTRICITY_RATE, 2)

    # 'total_bill' is optional: if the caller doesn't supply it (e.g. first
    # load of the Billing page), fall back to the metered estimate above
    # rather than a hardcoded figure. Once the user types in the actual
    # invoice total and resubmits, that value is used and split instead.
    total_bill_param = request.args.get('total_bill')
    total_bill = float(total_bill_param) if total_bill_param not in (None, '') else estimated_bill

    num_rooms = len(totals) or 1
    equal_split = round(total_bill / num_rooms, 2)
    billing_data = []

    for room in totals:
        share_percent = (room['energy_used_kwh'] / total_kwh)
        consumption_split = round(total_bill * share_percent, 2)
        diff = round(consumption_split - equal_split, 2)

        billing_data.append({
            'name': room['device_name'],
            'energy_kwh': room['energy_used_kwh'],
            'share_percent': round(share_percent * 100, 1),
            'equal_split': equal_split,
            'consumption_split': consumption_split,
            'difference': diff,
        })

    return jsonify({
        'total_bill': total_bill,
        'estimated_bill_bdt': estimated_bill,
        'is_estimate': total_bill_param in (None, ''),
        'days': days,
        'rooms': billing_data,
    })

@app.route('/api/carbon')
def api_carbon():
    days = max(1, min(int(request.args.get('days', 180)), 3650))
    totals = get_energy_summary(days=days)
    carbon_details = []

    for r in totals:
        c_kg = round(r['energy_used_kwh'] * DEFAULT_CARBON_FACTOR, 2)
        carbon_details.append({'name': r['device_name'], 'carbon_kg': c_kg, 'energy_kwh': r['energy_used_kwh']})

    sorted_rooms = sorted(carbon_details, key=lambda x: x['carbon_kg'], reverse=True)

    return jsonify({
        'highest_room': sorted_rooms[0]['name'] if sorted_rooms else 'N/A',
        'lowest_room': sorted_rooms[-1]['name'] if sorted_rooms else 'N/A',
        'total_carbon_kg': round(sum(c['carbon_kg'] for c in carbon_details), 2),
        'days': days,
        'details': carbon_details,
    })

@app.route('/api/collector/status')
def api_collector_status():
    return jsonify(collector.status())

@app.route('/api/collector/start', methods=['POST'])
def api_collector_start():
    return jsonify(collector.start())

@app.route('/api/collector/stop', methods=['POST'])
def api_collector_stop():
    return jsonify(collector.stop())

@app.route('/api/history/daily')
def api_history_daily():
    try:
        days = max(1, min(int(request.args.get('days', 7)), 365))
        device_id = request.args.get('device_id') or None
        return jsonify(get_daily_summary(device_id=device_id, days=days))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/database')
def api_database():
    try:
        return jsonify(get_database_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

SERVER_PORT = 5000


def start_ngrok_tunnel(port):
    """Optionally expose the local server publicly via ngrok.

    The authtoken is never hardcoded here. Provide it either as the
    NGROK_AUTHTOKEN environment variable, or in a local
    'ngrok_authtoken.txt' file next to this script (already created for
    you — keep it out of git / don't share it).
    """
    authtoken = os.environ.get("NGROK_AUTHTOKEN")
    token_file = BASE_DIR / "ngrok_authtoken.txt"
    if not authtoken and token_file.exists():
        authtoken = token_file.read_text(encoding="utf-8").strip()

    if not authtoken:
        print("[NGROK] No token found (NGROK_AUTHTOKEN env var or ngrok_authtoken.txt) — skipping tunnel.")
        return None

    try:
        from pyngrok import ngrok, conf
        conf.get_default().auth_token = authtoken
        tunnel = ngrok.connect(port, "http")
        print(f"[NGROK] Public URL: {tunnel.public_url}  ->  http://127.0.0.1:{port}")
        return tunnel
    except ImportError:
        print("[NGROK] pyngrok isn't installed. Run: pip install pyngrok")
    except Exception as e:
        print(f"[NGROK] Failed to start tunnel: {e}")
    return None


if __name__ == "__main__":
    print("\n" + "=" * 75)
    print("       SMARTDORM LOCAL LAN SERVER")
    print("=" * 75)
    print("  Local PC:      http://127.0.0.1:5000")
    print("  LAN access:    http://<YOUR-PC-IP>:5000")
    print("  Pipeline:      Measure -> Collect -> Store -> Calculate -> Display -> Control")
    print("=" * 75 + "\n")

    start_ngrok_tunnel(SERVER_PORT)

    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, use_reloader=False)