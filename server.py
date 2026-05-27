#!/usr/bin/env python3
"""Pikmin Bloom GPS Location Controller — FastAPI backend
Usage: sudo venv/bin/python3 server.py (run from project root)
"""

import json
import math
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

# ── Shared state ──────────────────────────────────────────────────────────────
_lock = threading.Lock()

tunnel_proc: Optional[subprocess.Popen] = None
rsd_host: Optional[str] = None
rsd_port: Optional[int] = None

walk_proc: Optional[subprocess.Popen] = None
walk_eta: Optional[float] = None        # unix timestamp when walk ends
walk_target: Optional[tuple] = None     # (lat, lon) destination

current_lat: float = 49.22491703456117
current_lon: float = -122.96817654417664

# ── Location hold ─────────────────────────────────────────────────────────────
_PROJ_DIR = Path(__file__).parent.resolve()
VENV_PY     = str(_PROJ_DIR / "venv" / "bin" / "python3")
HOLD_SCRIPT = str(_PROJ_DIR / "hold_location.py")

_hold_proc: Optional[subprocess.Popen] = None
_hold_lat:  Optional[float] = None
_hold_lon:  Optional[float] = None


def _kill_hold():
    global _hold_proc
    if _hold_proc and _hold_proc.poll() is None:
        _hold_proc.terminate()
        try:
            _hold_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _hold_proc.kill()
    _hold_proc = None


def _check_rsd_reachable(host: str, port: int, timeout: float = 3.0) -> Optional[str]:
    """Return None if reachable, else an error string."""
    import socket
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return f"DNS/地址解析失败：{e}"
    for family, type_, proto, _, addr in infos:
        s = socket.socket(family, type_, proto)
        s.settimeout(timeout)
        try:
            s.connect(addr)
            s.close()
            return None
        except (socket.timeout, OSError) as e:
            last_err = e
        finally:
            try: s.close()
            except Exception: pass
    return f"Tunnel 不可达 ({host}:{port}) — 请重启 start-tunnel 并填新地址"


def _launch_hold_proc(lat: float, lon: float) -> dict:
    """Spawn hold_location.py and wait for LOCATION_SET confirmation."""
    global _hold_proc
    _kill_hold()
    if not (rsd_host and rsd_port):
        return {"ok": False, "msg": "Tunnel 未连接，请先填写 RSD HOST/PORT"}
    # Fast pre-flight TCP check — fail in 3s instead of hanging 75s
    reach_err = _check_rsd_reachable(rsd_host, rsd_port)
    if reach_err:
        return {"ok": False, "msg": reach_err}
    cmd = [VENV_PY, HOLD_SCRIPT, rsd_host, str(rsd_port), str(lat), str(lon)]
    print(f"[hold] launching: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)

    found = threading.Event()
    stderr_buf = []

    def _stdout_reader():
        try:
            for line in iter(proc.stdout.readline, ''):
                line = line.rstrip()
                print(f"[hold:out] {line}")
                if "LOCATION_SET" in line:
                    found.set()
        except Exception as e:
            print(f"[hold] stdout reader err: {e}")

    def _stderr_reader():
        try:
            for line in iter(proc.stderr.readline, ''):
                line = line.rstrip()
                if line:
                    print(f"[hold:err] {line}")
                    stderr_buf.append(line)
        except Exception as e:
            print(f"[hold] stderr reader err: {e}")

    threading.Thread(target=_stdout_reader, daemon=True).start()
    threading.Thread(target=_stderr_reader, daemon=True).start()

    # Wait up to 25 s for LOCATION_SET marker, or early exit
    deadline = time.time() + 25
    while time.time() < deadline:
        if found.is_set():
            _hold_proc = proc
            return {"ok": True}
        if proc.poll() is not None:
            err = _clean("\n".join(stderr_buf))
            return {"ok": False,
                    "msg": err[:300] or f"hold 进程退出（code {proc.returncode}）"}
        time.sleep(0.1)

    proc.terminate()
    err = _clean("\n".join(stderr_buf))
    return {"ok": False, "msg": f"位置设置超时（25 s）。stderr: {err[:200]}"}


def _watchdog_loop():
    """Restart hold process if it dies while a location is locked."""
    while True:
        time.sleep(10)
        if _hold_lat is None:
            continue                # no active hold, skip
        if _hold_proc is None or _hold_proc.poll() is not None:
            print(f"[watchdog] hold proc died, restarting at {_hold_lat},{_hold_lon}")
            _launch_hold_proc(_hold_lat, _hold_lon)


def start_location_hold(lat: float, lon: float) -> dict:
    global _hold_lat, _hold_lon
    result = _launch_hold_proc(lat, lon)
    if result["ok"]:
        _hold_lat, _hold_lon = lat, lon
    return result


def stop_location_hold():
    global _hold_lat, _hold_lon
    _hold_lat = _hold_lon = None   # watchdog will see None and skip restart
    _kill_hold()
    run_cmd(["developer", "dvt", "simulate-location", "clear"] + rsd_args())


# Start watchdog background thread (runs for the lifetime of the server)
threading.Thread(target=_watchdog_loop, daemon=True).start()

LOCATIONS_FILE = Path(__file__).parent / "locations.json"
GPX_TMP = Path("/tmp/pikmin_route.gpx")


# ── Utilities ─────────────────────────────────────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def generate_gpx(slat, slon, elat, elon, speed_kmh=4.5) -> str:
    dist = haversine_m(slat, slon, elat, elon)
    speed_ms = speed_kmh * 1000 / 3600
    total_sec = max(1.0, dist / speed_ms)
    steps = max(2, int(total_sec / 3))  # one point per ~3 seconds
    now = datetime.now(timezone.utc)
    pts = []
    for i in range(steps + 1):
        t = i / steps
        lat = slat + (elat - slat) * t
        lon = slon + (elon - slon) * t
        ts = (now + timedelta(seconds=t * total_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pts.append(f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"><time>{ts}</time></trkpt>')
    body = "\n".join(pts)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="PikminBloomUI">
  <trk><name>route</name><trkseg>
{body}
  </trkseg></trk>
</gpx>"""


_NOISE = ("NotOpenSSLWarning", "urllib3", "LibreSSL", "warnings.warn",
          "NotOpenSSL", "site-packages/urllib")

def _clean(text: str) -> str:
    """Strip noisy urllib3 / SSL warning lines from command output."""
    lines = [l for l in text.splitlines()
             if not any(kw in l for kw in _NOISE)]
    return "\n".join(lines).strip()

def run_cmd(args: list, timeout=45) -> dict:
    cmd = [VENV_PY, "-m", "pymobiledevice3"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "out": _clean(r.stdout), "err": _clean(r.stderr)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "err": "command timed out"}
    except Exception as e:
        return {"ok": False, "out": "", "err": str(e)}


def rsd_args() -> list:
    if rsd_host and rsd_port:
        return ["--rsd", rsd_host, str(rsd_port)]
    return []


def load_locs() -> list:
    if LOCATIONS_FILE.exists():
        try:
            return json.loads(LOCATIONS_FILE.read_text())
        except Exception:
            return []
    return []


def save_locs(locs: list):
    LOCATIONS_FILE.write_text(json.dumps(locs, indent=2, ensure_ascii=False))


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    global walk_proc, walk_eta, walk_target, current_lat, current_lon
    with _lock:
        walking = walk_proc is not None and walk_proc.poll() is None
        if not walking and walk_target is not None:
            # walk just finished — commit position
            current_lat, current_lon = walk_target
            walk_target = None
            walk_eta = None
        eta_remaining = round(max(0.0, walk_eta - time.time()), 1) if walk_eta and walking else None
        return {
            "tunnel": rsd_host is not None,
            "rsd": f"{rsd_host}:{rsd_port}" if rsd_host else None,
            "walking": walking,
            "eta": eta_remaining,
            "lat": current_lat,
            "lon": current_lon,
        }


@app.post("/api/setup/mount")
def api_mount():
    r = run_cmd(["mounter", "auto-mount"], timeout=90)
    return {"ok": r["ok"], "msg": (r["out"] or r["err"])[:300]}


@app.post("/api/setup/tunnel")
def api_tunnel():
    global tunnel_proc, rsd_host, rsd_port
    with _lock:
        if tunnel_proc and tunnel_proc.poll() is None:
            return {"ok": True, "msg": "already running", "rsd": f"{rsd_host}:{rsd_port}"}

    cmd = [VENV_PY, "-m", "pymobiledevice3", "remote", "start-tunnel", "--connection-type", "usb"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    found = threading.Event()
    h, p = [None], [None]
    captured = []

    def _reader():
        for line in proc.stdout:
            line = line.rstrip()
            captured.append(line)
            print("[tunnel]", line)
            # Pattern A: "RSD Address: fd7c:..."  +  "RSD Port: 60106"
            mh = re.search(r'(?:RSD Address|Tunnel address|address)\s*[=:]\s*([0-9a-fA-F:.]+)', line, re.I)
            mp = re.search(r'(?:RSD Port|Tunnel port|port)\s*[=:]\s*(\d+)', line, re.I)
            # Pattern B: "--rsd fd7c:... 60106"  (single line in newer versions)
            m_rsd = re.search(r'--rsd\s+([0-9a-fA-F:.]+)\s+(\d+)', line)
            if m_rsd:
                h[0], p[0] = m_rsd.group(1), int(m_rsd.group(2))
            if mh:
                h[0] = mh.group(1)
            if mp:
                p[0] = int(mp.group(1))
            if h[0] and p[0]:
                found.set()

    threading.Thread(target=_reader, daemon=True).start()

    if not found.wait(timeout=30):
        proc.kill()
        last = " | ".join(_clean(l) for l in captured[-6:] if l.strip())
        return {"ok": False, "msg": f"Tunnel 超时。原始输出：{last or '(无输出，请确认手机已连接并信任此 Mac)'}"}

    with _lock:
        tunnel_proc = proc
        rsd_host = h[0]
        rsd_port = p[0]

    return {"ok": True, "rsd": f"{rsd_host}:{rsd_port}"}


class RsdReq(BaseModel):
    host: str
    port: int

@app.post("/api/setup/rsd")
def api_set_rsd(req: RsdReq):
    global rsd_host, rsd_port
    with _lock:
        rsd_host = req.host
        rsd_port = req.port
    return {"ok": True, "rsd": f"{rsd_host}:{rsd_port}"}


class LocReq(BaseModel):
    lat: float
    lon: float
    speed: float = 4.5  # km/h


@app.post("/api/teleport")
def api_teleport(req: LocReq):
    global current_lat, current_lon
    r = start_location_hold(req.lat, req.lon)
    if r["ok"]:
        with _lock:
            current_lat, current_lon = req.lat, req.lon
    return r


@app.post("/api/walk")
def api_walk(req: LocReq):
    global walk_proc, walk_eta, walk_target, current_lat, current_lon
    with _lock:
        if walk_proc and walk_proc.poll() is None:
            walk_proc.kill()

        gpx = generate_gpx(current_lat, current_lon, req.lat, req.lon, req.speed)
        GPX_TMP.write_text(gpx)

        dist = haversine_m(current_lat, current_lon, req.lat, req.lon)
        eta_sec = dist / (req.speed * 1000 / 3600)

        cmd = ([VENV_PY, "-m", "pymobiledevice3", "developer", "dvt",
                "simulate-location", "play"]
               + rsd_args()
               + ["--timing-randomness", "3", str(GPX_TMP)])
        walk_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        walk_eta = time.time() + eta_sec
        walk_target = (req.lat, req.lon)

    # After walk finishes, lock position with 'set' so iOS doesn't revert
    target_lat, target_lon = req.lat, req.lon
    def _lock_after_walk():
        global current_lat, current_lon
        walk_proc.wait()
        r = start_location_hold(target_lat, target_lon)
        if r["ok"]:
            with _lock:
                current_lat, current_lon = target_lat, target_lon
    threading.Thread(target=_lock_after_walk, daemon=True).start()

    return {"ok": True, "eta": int(eta_sec), "dist": int(dist)}


@app.post("/api/stop")
def api_stop():
    global walk_proc, walk_eta, walk_target
    with _lock:
        if walk_proc and walk_proc.poll() is None:
            walk_proc.kill()
        walk_proc = None
        walk_eta = None
        walk_target = None
    return {"ok": True}


@app.post("/api/clear")
def api_clear():
    stop_location_hold()
    return {"ok": True}


@app.get("/api/saved")
def api_list_saved():
    return load_locs()


class SaveReq(BaseModel):
    name: str
    lat: float
    lon: float


@app.post("/api/saved")
def api_save(req: SaveReq):
    locs = load_locs()
    locs = [l for l in locs if l["name"] != req.name]
    locs.append({"name": req.name, "lat": req.lat, "lon": req.lon})
    save_locs(locs)
    return {"ok": True}


@app.delete("/api/saved/{name}")
def api_del_saved(name: str):
    save_locs([l for l in load_locs() if l["name"] != name])
    return {"ok": True}


# ── Static files ──────────────────────────────────────────────────────────────
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


def _cleanup(*_):
    _kill_hold()
    if tunnel_proc and tunnel_proc.poll() is None:
        tunnel_proc.kill()
    if walk_proc and walk_proc.poll() is None:
        walk_proc.kill()
    sys.exit(0)


signal.signal(signal.SIGINT, _cleanup)
signal.signal(signal.SIGTERM, _cleanup)

if __name__ == "__main__":
    print("🌱  Pikmin Bloom Location UI  →  http://127.0.0.1:3000")
    uvicorn.run(app, host="127.0.0.1", port=3000, log_level="warning")
