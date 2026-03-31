#!/usr/bin/env python3
"""Weii Scale add-on — aiohttp server + Wii Balance Board measurement logic."""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from aiohttp import web, ClientSession

OPTIONS_FILE = Path("/data/options.json")
MEASUREMENTS_FILE = Path("/data/measurements.json")
PORT = 8765
DISCOVERY_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_options() -> dict:
    if OPTIONS_FILE.exists():
        return json.loads(OPTIONS_FILE.read_text())
    return {"address": "", "adjust": 0.0}


# ---------------------------------------------------------------------------
# Measurement state (single global — only one measurement runs at a time)
# ---------------------------------------------------------------------------

class State:
    status: str = "idle"          # idle | scanning | measuring | timed_out | error
    log: list[str] = []
    weight: Optional[float] = None
    measuring: bool = False

state = State()


# ---------------------------------------------------------------------------
# Persistent measurement store
# ---------------------------------------------------------------------------

def load_measurements() -> list[dict]:
    if MEASUREMENTS_FILE.exists():
        try:
            return json.loads(MEASUREMENTS_FILE.read_text())
        except Exception:
            return []
    return []


def save_measurement(weight: float) -> None:
    measurements = load_measurements()
    measurements.append({
        "timestamp": datetime.now().isoformat(),
        "weight": weight,
    })
    MEASUREMENTS_FILE.write_text(json.dumps(measurements, indent=2))


def daily_averages(measurements: list[dict]) -> list[dict]:
    """Return [{date, weight}] with one averaged point per calendar day."""
    by_day: dict[str, list[float]] = {}
    for m in measurements:
        day = m["timestamp"][:10]  # YYYY-MM-DD
        by_day.setdefault(day, []).append(m["weight"])
    return [
        {"date": d, "weight": round(sum(w) / len(w), 1)}
        for d, w in sorted(by_day.items())
    ]


# ---------------------------------------------------------------------------
# Push current weight to HA via Supervisor API
# ---------------------------------------------------------------------------

async def push_to_ha(weight: float) -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    try:
        async with ClientSession() as session:
            await session.post(
                "http://supervisor/core/api/states/sensor.weii_scale_weight",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "state": f"{weight:.1f}",
                    "attributes": {
                        "unit_of_measurement": "kg",
                        "device_class": "weight",
                        "friendly_name": "Weii Scale Weight",
                        "icon": "mdi:scale-bathroom",
                    },
                },
            )
    except Exception as exc:
        print(f"[weii] Warning: could not push to HA: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Measurement logic (runs in a thread-pool executor)
# ---------------------------------------------------------------------------

def _measure_blocking(address: str, adjust: float) -> Optional[float]:
    """Synchronous measurement — called via run_in_executor."""
    import evdev
    from evdev import ecodes

    def log(msg: str) -> None:
        state.log.append(msg)
        print(msg, flush=True)

    # --- Discovery (10-second timeout) ---
    log("Waiting for balance board...")
    deadline = time.monotonic() + DISCOVERY_TIMEOUT
    board = None
    while time.monotonic() < deadline:
        matches = [
            path for path in evdev.list_devices()
            if evdev.InputDevice(path).name == "Nintendo Wii Remote Balance Board"
        ]
        if matches:
            board = evdev.InputDevice(matches[0])
            break
        time.sleep(0.5)

    if board is None:
        log("Timed out: balance board not found.")
        return None

    log("Balance board found, please step on.")
    state.status = "measuring"

    # --- Read samples ---
    def get_raw() -> float:
        data: list[Optional[float]] = [None] * 4
        while True:
            event = board.read_one()
            if event is None:
                continue
            if event.code == ecodes.ABS_HAT1X:
                data[0] = event.value / 100
            elif event.code == ecodes.ABS_HAT0X:
                data[1] = event.value / 100
            elif event.code == ecodes.ABS_HAT0Y:
                data[2] = event.value / 100
            elif event.code == ecodes.ABS_HAT1Y:
                data[3] = event.value / 100
            elif event.code == ecodes.BTN_A:
                raise RuntimeError("Board button pressed during measurement, aborting.")
            elif event.code == ecodes.SYN_REPORT and event.value == 0:
                if None not in data:
                    return sum(data)  # type: ignore[arg-type]
                data = [None] * 4

    samples: list[float] = []
    while True:
        m = get_raw()
        if samples and m < 20:
            log("User stepped off.")
            break
        if not samples and m < 20:
            continue
        samples.append(m)
        if len(samples) == 1:
            log("Measurement started, please wait...")
        if len(samples) > 200:
            break

    board.close()

    final = statistics.median(samples) + adjust
    log(f"Done, weight: {final:.1f}.")

    # --- Disconnect ---
    log("Disconnecting...")
    subprocess.run(
        ["/usr/bin/env", "bluetoothctl", "disconnect", address],
        capture_output=True,
    )

    return final


async def do_measure(address: str, adjust: float) -> None:
    if state.measuring:
        return
    state.measuring = True
    state.status = "scanning"
    state.log = []

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _measure_blocking, address, adjust)
        if result is not None:
            state.weight = result
            state.status = "idle"
            save_measurement(result)
            await push_to_ha(result)
        else:
            state.status = "timed_out"
    except Exception as exc:
        state.log.append(f"Error: {exc}")
        state.status = "error"
    finally:
        state.measuring = False


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_index(request: web.Request) -> web.Response:
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    averages = daily_averages(load_measurements())
    html = INDEX_HTML.replace("__INGRESS_PATH__", ingress_path)
    html = html.replace("__MEASUREMENTS_JSON__", json.dumps(averages))
    return web.Response(text=html, content_type="text/html")


async def handle_measure(request: web.Request) -> web.Response:
    if state.measuring:
        return web.json_response({"error": "Already measuring"}, status=409)
    options = load_options()
    address = options.get("address", "").strip()
    if not address or address == "AA:BB:CC:DD:EE:FF":
        return web.json_response({"error": "No address configured in add-on options"}, status=400)
    adjust = float(options.get("adjust", 0.0))
    asyncio.ensure_future(do_measure(address, adjust))
    return web.json_response({"status": "started"}, status=202)


async def handle_status(request: web.Request) -> web.Response:
    return web.json_response({
        "status": state.status,
        "log": state.log,
        "weight": state.weight,
    })


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Weii Scale</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #111318;
      color: #e4e6eb;
      padding: 24px;
      max-width: 900px;
      margin: 0 auto;
    }
    h1 { font-size: 1.5rem; font-weight: 600; margin-bottom: 24px; color: #fff; }
    .card {
      background: #1e2128;
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
    }
    .card h2 { font-size: 0.85rem; font-weight: 500; color: #8b8fa8; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
    .controls { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
    #measure-btn {
      background: #3b82f6;
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: 10px 24px;
      font-size: 0.95rem;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s, opacity 0.15s;
    }
    #measure-btn:hover:not(:disabled) { background: #2563eb; }
    #measure-btn:disabled { opacity: 0.45; cursor: not-allowed; }
    #status-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 0.9rem;
      color: #9ca3af;
    }
    #status-dot {
      width: 10px; height: 10px;
      border-radius: 50%;
      background: #4b5563;
      transition: background 0.3s;
    }
    #status-dot.idle     { background: #22c55e; }
    #status-dot.scanning { background: #f59e0b; animation: pulse 1s infinite; }
    #status-dot.measuring{ background: #3b82f6; animation: pulse 0.7s infinite; }
    #status-dot.timed_out{ background: #ef4444; }
    #status-dot.error    { background: #ef4444; }
    @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.35; } }
    #weight-display {
      font-size: 2.5rem;
      font-weight: 700;
      color: #fff;
      letter-spacing: -0.02em;
    }
    #weight-unit { font-size: 1.1rem; color: #6b7280; margin-left: 4px; }
    #log {
      font-family: "SF Mono", "Fira Code", monospace;
      font-size: 0.8rem;
      color: #9ca3af;
      background: #13151a;
      border-radius: 8px;
      padding: 12px;
      min-height: 60px;
      max-height: 180px;
      overflow-y: auto;
      white-space: pre-wrap;
      line-height: 1.6;
    }
    .chart-wrap { position: relative; height: 300px; }
    .no-data { color: #4b5563; font-size: 0.9rem; text-align: center; padding: 60px 0; }
  </style>
</head>
<body>
  <h1>Weii Scale</h1>

  <div class="card">
    <h2>Measurement</h2>
    <div class="controls">
      <button id="measure-btn" onclick="measure()">Measure</button>
      <span id="status-badge">
        <span id="status-dot"></span>
        <span id="status-text">idle</span>
      </span>
    </div>
  </div>

  <div class="card">
    <h2>Last Weight</h2>
    <div id="weight-display">—<span id="weight-unit"> kg</span></div>
  </div>

  <div class="card">
    <h2>Log</h2>
    <div id="log">No measurement taken yet.</div>
  </div>

  <div class="card">
    <h2>Weight History (daily average)</h2>
    <div class="chart-wrap">
      <canvas id="chart"></canvas>
      <div class="no-data" id="no-data" style="display:none">No measurements recorded yet.</div>
    </div>
  </div>

  <script>
    const BASE = "__INGRESS_PATH__";
    let chart = null;
    let pollTimer = null;

    // --- Initial data injected server-side ---
    const initialData = __MEASUREMENTS_JSON__;

    // --- Chart ---
    function buildChart(data) {
      const ctx = document.getElementById("chart").getContext("2d");
      if (data.length === 0) {
        document.getElementById("no-data").style.display = "block";
        return;
      }
      document.getElementById("no-data").style.display = "none";

      const points = data.map(d => ({ x: d.date, y: d.weight }));

      // Determine y-axis range with a small margin around the data
      const weights = data.map(d => d.weight);
      const minW = Math.min(...weights);
      const maxW = Math.max(...weights);
      const margin = Math.max((maxW - minW) * 0.15, 1);

      if (chart) chart.destroy();
      chart = new Chart(ctx, {
        type: "line",
        data: {
          datasets: [{
            label: "Weight (kg)",
            data: points,
            borderColor: "#3b82f6",
            backgroundColor: "rgba(59,130,246,0.12)",
            pointBackgroundColor: "#3b82f6",
            pointRadius: 5,
            pointHoverRadius: 7,
            tension: 0.25,
            fill: true,
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: {
              type: "time",
              time: { unit: "day", tooltipFormat: "PPP", displayFormats: { day: "MMM d" } },
              grid: { color: "rgba(255,255,255,0.05)" },
              ticks: { color: "#6b7280", maxTicksLimit: 10 },
              title: { display: true, text: "Date", color: "#6b7280" },
            },
            y: {
              min: Math.floor(minW - margin),
              max: Math.ceil(maxW + margin),
              grid: { color: "rgba(255,255,255,0.05)" },
              ticks: { color: "#6b7280", callback: v => v + " kg" },
              title: { display: true, text: "Weight (kg)", color: "#6b7280" },
            },
          },
          plugins: {
            legend: { labels: { color: "#9ca3af" } },
            tooltip: {
              callbacks: {
                label: ctx => ` ${ctx.parsed.y.toFixed(1)} kg`
              }
            }
          },
        }
      });
    }

    // --- Status polling ---
    function startPolling() {
      if (pollTimer) return;
      pollTimer = setInterval(pollStatus, 1000);
    }

    function stopPolling() {
      clearInterval(pollTimer);
      pollTimer = null;
    }

    async function pollStatus() {
      try {
        const res = await fetch(BASE + "/status");
        const data = await res.json();
        updateUI(data);
        if (data.status === "idle" || data.status === "timed_out" || data.status === "error") {
          stopPolling();
          // Refresh the graph with the latest measurements
          refreshChart();
        }
      } catch (e) { /* ignore network blips during polling */ }
    }

    async function refreshChart() {
      try {
        const res = await fetch(BASE + "/status");
        const data = await res.json();
        // Re-fetch measurements by reloading — simpler than a separate endpoint
        // since the page already has initial data; just update in-place from status
        if (data.weight !== null) {
          const today = new Date().toISOString().slice(0, 10);
          // Find and update today's point, or append it
          const existing = initialData.find(d => d.date === today);
          if (existing) {
            existing.weight = data.weight;
          } else {
            initialData.push({ date: today, weight: data.weight });
          }
          buildChart(initialData);
        }
      } catch (e) {}
    }

    function updateUI(data) {
      const dot = document.getElementById("status-dot");
      const text = document.getElementById("status-text");
      const btn = document.getElementById("measure-btn");
      const logEl = document.getElementById("log");
      const weightEl = document.getElementById("weight-display");

      dot.className = data.status;
      text.textContent = {
        idle: "Idle",
        scanning: "Scanning for board...",
        measuring: "Measuring...",
        timed_out: "Timed out — board not found",
        error: "Error",
      }[data.status] || data.status;

      btn.disabled = (data.status === "scanning" || data.status === "measuring");

      if (data.log && data.log.length > 0) {
        logEl.textContent = data.log.join("\n");
        logEl.scrollTop = logEl.scrollHeight;
      }

      if (data.weight !== null) {
        const node = weightEl.firstChild;
        node.textContent = data.weight.toFixed(1);
      }
    }

    // --- Trigger measurement ---
    async function measure() {
      document.getElementById("measure-btn").disabled = true;
      document.getElementById("log").textContent = "";
      try {
        const res = await fetch(BASE + "/measure", { method: "POST" });
        if (!res.ok) {
          const err = await res.json();
          document.getElementById("log").textContent = "Error: " + (err.error || res.status);
          document.getElementById("measure-btn").disabled = false;
          return;
        }
        startPolling();
      } catch (e) {
        document.getElementById("log").textContent = "Error: " + e;
        document.getElementById("measure-btn").disabled = false;
      }
    }

    // --- Init ---
    buildChart(initialData);
    // Sync current state on load (in case a measurement was in progress)
    fetch(BASE + "/status").then(r => r.json()).then(d => {
      updateUI(d);
      if (d.status === "scanning" || d.status === "measuring") startPolling();
    }).catch(() => {});
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_post("/measure", handle_measure)
    app.router.add_get("/status", handle_status)
    print(f"[weii] Starting on port {PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
