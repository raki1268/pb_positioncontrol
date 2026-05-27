# Pigmax · 位置控制

> A local web UI on `localhost:3000` that lets an iOS device politely reconsider where it thinks it is — useful for testing LBS apps, debugging map-based features, or letting the cat enjoy a digital walk from the windowsill.

![status](https://img.shields.io/badge/iOS-17%2B-blue) ![python](https://img.shields.io/badge/Python-3.10%2B-green) ![platform](https://img.shields.io/badge/platform-macOS-lightgrey)

---

## ✨ Features

- 🗺️ **Click-to-relocate** — pick any point on a Leaflet map and the device's reported coordinates jump there instantly
- 🚶 **Walk simulation** — generate a GPX route with adjustable speed (1–12 km/h) so step counters, route trackers and distance-based UI register a natural-looking journey
- 📍 **Persistent location lock** — a dedicated `hold_location.py` process keeps the DVT session alive so the system doesn't quietly revert after ~60 s
- 🛡️ **Watchdog** — monitors the hold process and respawns it automatically if it exits
- ⭐ **Saved waypoints** — bookmark frequent destinations and recall them in one click
- 🎯 **Direct coordinate input** — paste a `lat, lon` pair and press Enter
- 🔁 **One-click restore** — a single button stops the simulation and returns the device to its real GPS reading

---

## 📦 Requirements

| Component | Version | Notes |
|---|---|---|
| macOS | 12+ | `usbmuxd` and CoreDevice are macOS-only |
| Python | 3.10+ | Must be OpenSSL-backed (Homebrew Python; **not** the system LibreSSL build) |
| iPhone | iOS 17+ | Developer Mode enabled |
| Cable | Lightning / USB-C | Wired connection required, trusted on first plug-in |

> ℹ️ The macOS system Python ships with LibreSSL, which fails the QUIC handshake required by the new tunnel protocol. Install Homebrew Python: `brew install python3`.

---

## 🚀 Setup

```bash
# 1. Clone
git clone https://github.com/raki1268/pb_positioncontrol.git
cd pb_positioncontrol

# 2. Create a virtual environment with Homebrew Python
/opt/homebrew/bin/python3 -m venv venv
venv/bin/pip install pymobiledevice3 fastapi uvicorn

# 3. Copy waypoint template (optional)
cp locations.example.json locations.json

# 4. Enable Developer Mode on the iPhone
#    Settings → Privacy & Security → Developer Mode → ON → reboot
```

---

## 🎮 Usage

### Terminal 1 — start the web server

```bash
sudo venv/bin/python3 server.py
```

Open `http://127.0.0.1:3000` in a browser.

### Terminal 2 — start the RSD tunnel (required for iOS 17+)

```bash
sudo venv/bin/python3 -m pymobiledevice3 remote start-tunnel --connection-type usb
```

Output will look like:

```
RSD Address: fdXX:XXXX:XXXX::1
RSD Port: 5XXXX
```

> Both values are randomly generated each time the tunnel starts; the actual output will differ. Leave this terminal open — closing it kills the tunnel.

### In the browser

1. Click **① 挂载镜像** (Mount Image) — one-time per device boot
2. Paste the **RSD Address** + **Port** from Terminal 2 into the input fields, click **② 连接 Tunnel**
3. Click on the map (or paste coordinates in the **坐标直达** box) → an action card appears
4. Click **⚡ 瞬移** (Teleport) or **🚶 步行前往** (Walk)
5. To end the session: click **📍 回到出发点 (停止模拟)**

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

iOS retains the simulated coordinate only while the DVT session is **alive**. The bundled CLI's `simulate-location set` blocks on `signal.sigwait(...)` to keep the session open — once the process exits, the OS reverts within ~60 s.

`hold_location.py` reproduces this behaviour through the Python API and emits a `LOCATION_SET` marker on stdout, letting the server confirm that the coordinate has actually been applied before returning success. It then blocks on a signal until the server terminates it.

A watchdog in `server.py` polls every 10 seconds and respawns the hold process if it exits (tunnel hiccup, USB reseat, etc.).

---

## 🐛 Troubleshooting

### `Encountered a QUIC protocol error` during tunnel startup
System Python's LibreSSL backend cannot complete the QUIC handshake. Use Homebrew Python:
```bash
brew install python3
/opt/homebrew/bin/python3 -m venv venv
venv/bin/pip install pymobiledevice3
```

### `Tunnel 不可达` (Tunnel unreachable)
The `start-tunnel` process is no longer running (closed terminal, Ctrl+C, USB reseat, etc.). Restart it. The new port differs from the old one and must be re-entered in the browser.

### Location applies, then reverts after ~60 s
The hold process died. Inspect Terminal 1 for `[hold:err]` lines — the most common cause is a stale tunnel; restart Terminal 2.

### `No such option: --tunnel-type`
Older `pymobiledevice3` releases used a different flag. In 9.x the option is `--connection-type usb`.

### Browser shows `Failed to load resource: 3000/favicon.ico` 404
Cosmetic only; can be ignored.

---

## 📁 File map

| File | Purpose |
|---|---|
| `server.py` | FastAPI backend — REST API, hold/walk lifecycle, watchdog |
| `hold_location.py` | Standalone async script that keeps the DVT session alive |
| `static/index.html` | Single-page Leaflet UI |
| `locations.json` | Personal waypoints (gitignored) |
| `locations.example.json` | Template — copy to `locations.json` on first run |

---

## ⚠️ Disclaimer

Intended for personal debugging, local map-feature testing and educational purposes. Applying a simulated location while interacting with third-party services may conflict with their terms of service; users are responsible for their own usage.
