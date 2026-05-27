# Pigmax · 位置控制

> A local web UI on `localhost:3000` to spoof your iPhone's GPS location for LBS games like **Pikmin Bloom**, via USB and `pymobiledevice3`.

![status](https://img.shields.io/badge/iOS-17%2B-blue) ![python](https://img.shields.io/badge/Python-3.10%2B-green) ![platform](https://img.shields.io/badge/platform-macOS-lightgrey)

---

## ✨ Features

- 🗺️ **Click-to-teleport** — click anywhere on a Leaflet map to spoof your iPhone's GPS to that point
- 🚶 **Walk simulation** — generate a GPX route with adjustable speed (1–12 km/h) and play it back so step counters, route trackers, and Pikmin flower trails register naturally
- 📍 **Persistent location lock** — a dedicated `hold_location.py` script holds the DVT session open so iOS doesn't revert after ~60 s
- 🛡️ **Watchdog** — auto-restarts the hold process if it dies
- ⭐ **Saved waypoints** — bookmark and one-click jump to your common spots
- 🎯 **Direct coordinate input** — paste `lat, lon` and hit enter
- 🔁 **Return-to-real-GPS** — single button stops simulation and reverts to physical location

---

## 📦 Requirements

| Tool | Version | Notes |
|---|---|---|
| macOS | 12+ | `usbmuxd` and CoreDevice are macOS-only |
| Python | 3.10+ | **Must be OpenSSL-backed** (Homebrew Python, not system LibreSSL) |
| iPhone | iOS 17+ | Developer Mode enabled |
| Lightning / USB-C cable | — | Trusted connection required |

> ⚠️ The macOS system Python ships with LibreSSL which fails the QUIC handshake. Use Homebrew Python: `brew install python3`.

---

## 🚀 Setup

```bash
# 1. Clone
git clone https://github.com/raki1268/pb_positioncontrol.git
cd pb_positioncontrol

# 2. Virtual environment with Homebrew Python
/opt/homebrew/bin/python3 -m venv venv
venv/bin/pip install pymobiledevice3 fastapi uvicorn

# 3. Copy waypoint template (optional)
cp locations.example.json locations.json

# 4. Enable Developer Mode on your iPhone
#    Settings → Privacy & Security → Developer Mode → ON → reboot
```

---

## 🎮 Usage

### Terminal 1 — start the web server

```bash
sudo venv/bin/python3 server.py
```

Open `http://127.0.0.1:3000` in your browser.

### Terminal 2 — start the RSD tunnel (required for iOS 17+)

```bash
sudo venv/bin/python3 -m pymobiledevice3 remote start-tunnel --connection-type usb
```

Look for output like:

```
RSD Address: fdXX:XXXX:XXXX::1
RSD Port: 5XXXX
```

> Both the address and port are randomly generated each time you start the tunnel — your values will differ. **Keep this terminal open** — closing it kills the tunnel.

### In the browser

1. Click **① 挂载镜像** (Mount Image) — one-time per device boot
2. Paste the **RSD Address** + **Port** from Terminal 2 into the input fields, click **② 连接 Tunnel**
3. Click on the map (or paste coordinates in the **坐标直达** box) → action card appears
4. Click **⚡ 瞬移** (Teleport) or **🚶 步行前往** (Walk)
5. To stop simulation: click **📍 回到出发点 (停止模拟)**

---

## 🏗️ Architecture

```
┌──────────────────┐        HTTP        ┌─────────────────┐
│  Browser (3000)  │ ◀──────────────▶   │  FastAPI server │
│  Leaflet + JS    │                    │   (server.py)   │
└──────────────────┘                    └────────┬────────┘
                                                 │ subprocess
                                                 ▼
                                       ┌────────────────────┐
                                       │ hold_location.py   │
                                       │ (asyncio DVT loop) │
                                       └────────┬───────────┘
                                                │ RSD/TCP
                                                ▼
                                       ┌────────────────────┐
                                       │ pymobiledevice3    │
                                       │   remote tunnel    │
                                       └────────┬───────────┘
                                                │ USB
                                                ▼
                                       ┌────────────────────┐
                                       │     iPhone         │
                                       │  (DVT Location)    │
                                       └────────────────────┘
```

### Why a separate `hold_location.py`?

iOS only keeps the simulated GPS as long as the DVT session is **alive**. The CLI's `simulate-location set` blocks on `signal.sigwait(...)` to keep the session open — kill the process and iOS reverts within ~60 s.

`hold_location.py` does the same via the Python API, prints `LOCATION_SET` so the server knows the location actually applied, then blocks on a signal until the server terminates it.

A watchdog in `server.py` polls every 10 s and respawns it if it dies (tunnel hiccup, USB reseat, etc.).

---

## 🐛 Troubleshooting

### "Encountered a QUIC protocol error" when starting the tunnel
You're using system Python with LibreSSL. Use Homebrew Python:
```bash
brew install python3
/opt/homebrew/bin/python3 -m venv venv
venv/bin/pip install pymobiledevice3
```

### "Tunnel 不可达"
The `start-tunnel` terminal died or was Ctrl+C'd. Restart it — and note the **new port** (it changes every restart). Paste the new HOST/PORT in the browser.

### Teleport works but reverts after ~60 s
The hold process died. Check Terminal 1 (`[hold:err]` lines) for the exception. Most commonly the tunnel went stale — restart Terminal 2.

### "No such option: --tunnel-type"
You're on an older `pymobiledevice3`. The flag is `--connection-type usb` in 9.x.

---

## 📁 File map

| File | Purpose |
|---|---|
| `server.py` | FastAPI backend — REST API, hold/walk lifecycle, watchdog |
| `hold_location.py` | Standalone async script that keeps the DVT session alive |
| `static/index.html` | Single-page Leaflet UI |
| `locations.json` | Your personal waypoints (gitignored) |
| `locations.example.json` | Template — copy to `locations.json` to start |

---

## ⚠️ Disclaimer

For personal debugging and educational use only. Spoofing your GPS in third-party apps may violate their Terms of Service. Use at your own risk.
