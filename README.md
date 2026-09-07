# Pigmax · 位置控制

> A local web UI on `localhost:3000` that lets an iOS device politely reconsider where it thinks it is — useful for testing LBS apps, debugging map-based features, or letting the cat enjoy a digital walk from the windowsill.

![status](https://img.shields.io/badge/iOS-17%2B-blue) ![python](https://img.shields.io/badge/Python-3.10%2B-green) ![platform](https://img.shields.io/badge/platform-macOS-lightgrey)

---

## ✨ Features

- 🗺️ **Click-to-relocate** — pick any point on a Leaflet map and the device's reported coordinates jump there instantly
- 🚶 **Walk simulation** — generate a smooth route with adjustable speed (1–12 km/h) so step counters, route trackers and distance-based UI register a natural-looking journey
- 🛤️ **Multi-point routing** — queue multiple waypoints before starting; the device walks each segment in sequence automatically
  - Confirm with total distance + ETA before executing
  - Adjust speed at any time mid-route (current segment auto-restarts from current position)
  - Cancel any remaining waypoint individually; route replans around the rest
  - Loop detection: if the last waypoint is within 300 m of the first, choose ×1 / ×2 / ×3 / ×5 / ×10 laps
- 💾 **Saved routes** — name and store a planned route; it persists in `routes.json` across restarts
  - Single-click a route in the sidebar to preview it on the map (waypoints, distance, ETA); clicking another route swaps the preview
  - Double-click to restore it into the route planner, then hit **✓ 确认出发** to run it as-is
  - Rename, overwrite or delete saved routes; the running route's name shows in the progress bar
  - Save a route **after** it already started — the progress panel carries its own name field, so a route that was never saved (or failed to save) can still be captured mid-run
- 📚 **Tabbed library** — the sidebar keeps saved locations and saved routes on two tabs with live counts; the last tab you used is remembered
- 🟡 **Walk trail overlay** — a 300 m-radius exploration fog records everywhere the device has been; 70% transparent, flat and uniform (no opacity banding on overlapping areas)
- 📍 **Persistent location lock** — a dedicated hold process keeps the DVT session alive so the system doesn't quietly revert after ~60 s
- 🛡️ **Watchdog** — monitors hold/walk processes and respawns automatically on crash or tunnel hiccup
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

If port 3000 is taken, the server prints the port it picked instead. To force a
specific one, use `--port` or `PORT` (note `sudo` drops the env var unless you
set it inline):

```bash
sudo venv/bin/python3 server.py --port 3100
```

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
2. Paste the **RSD Address** + **Port** from Terminal 2, click **② 连接 Tunnel**
3. Click on the map (or paste coordinates in the **坐标直达** box) → an action card appears
4. **Single destination**: click **⚡ 瞬移** (Teleport) or **🚶 步行前往** (Walk)
5. **Multi-point route**: click **+ 加入路线** for each waypoint, then **✓ 确认路线** — the device walks each segment in order
   - If start ≈ end (≤ 300 m apart), a lap selector appears (×1 / ×2 / ×3 / ×5 / ×10)
   - Drag the speed slider at any time to change pace; the current segment restarts from the current position
   - Click **×** next to a queued waypoint to skip it
6. **Save a route**: with waypoints queued, type a name in the plan card and click **💾 保存路线**
   - Later, single-click the entry under **保存的路线** to preview it, double-click to load it back, then **✓ 确认出发**
7. To end the session: click **📍 回到出发点 (停止模拟)**

---

## 🏗️ Architecture

```
┌──────────────────┐        HTTP        ┌─────────────────┐
│  Browser (3000)  │ ◀──────────────▶   │  FastAPI server │
│  Leaflet + JS    │                    │   (server.py)   │
└──────────────────┘                    └────────┬────────┘
                                                 │ subprocess
                                    ┌────────────┴────────────┐
                                    ▼                         ▼
                           ┌─────────────────┐    ┌──────────────────┐
                           │ hold_location.py│    │ walk_location.py │
                           │ (hold position) │    │ (walk + advance) │
                           └────────┬────────┘    └────────┬─────────┘
                                    └──────────┬───────────┘
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

### Process lifecycle

- **hold_location.py** — keeps a DVT session alive at a fixed coordinate; emits `LOCATION_SET` on stdout so the server can confirm success before returning. Blocks on a signal until terminated.
- **walk_location.py** — walks the device from start to end at a constant speed using haversine interpolation. Prints `WALK_START` once connected, `WALK_PROGRESS <pct>` periodically, and `WALK_DONE` on arrival. After arrival it holds the final position and re-asserts every 30 s to prevent drift, until SIGTERM/SIGINT.
- **Watchdog** (thread in `server.py`) — ticks every 5 s. Detects ETA expiry to auto-advance route segments, samples the interpolated position for the trail overlay, restarts the hold process on crash, and recovers from tunnel hiccups.
- **Route auto-advance** — when `time.time() > walk_eta + 1 s` and a route is active, the watchdog kills the current walk process and immediately launches the next segment. No client coordination needed.

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
The `start-tunnel` process is no longer running. Restart it. The new port differs from the old one and must be re-entered in the browser.

### Location applies, then reverts after ~60 s
The hold process died. Inspect server logs for `[hold:err]` lines — the most common cause is a stale tunnel; restart Terminal 2.

### `No such option: --tunnel-type`
Older `pymobiledevice3` releases used a different flag. In 9.x the option is `--connection-type usb`.

### Browser shows `Failed to load resource: 3000/favicon.ico` 404
Cosmetic only; can be ignored.

### `[Errno 48] address already in use`
Another process holds the port. Find it with `lsof -nP -iTCP:3000 -sTCP:LISTEN`,
then either stop it or start the server on a different port (see above).

---

## 📁 File map

| File | Purpose |
|---|---|
| `server.py` | FastAPI backend — REST API, hold/walk/route lifecycle, watchdog |
| `hold_location.py` | Async script that holds a fixed DVT location until terminated |
| `walk_location.py` | Async script that walks the device between two coordinates at constant speed |
| `static/index.html` | Single-page Leaflet UI (click-to-set, walk, multi-point routing, trail overlay) |
| `locations.json` | Personal saved waypoints (gitignored) |
| `locations.example.json` | Template — copy to `locations.json` on first run |
| `routes.json` | Personal saved routes (gitignored, auto-created) |
| `walk_path.json` | Auto-generated trail recording (gitignored) |

---

## ⚠️ Disclaimer

Intended for personal debugging, local map-feature testing and educational purposes. Applying a simulated location while interacting with third-party services may conflict with their terms of service; users are responsible for their own usage.
