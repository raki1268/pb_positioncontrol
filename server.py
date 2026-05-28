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
_walk_start_time: Optional[float] = None  # unix timestamp when walk began
_walk_total_sec:  Optional[float] = None
_walk_start_pos:  Optional[tuple] = None  # (lat, lon) at walk start
_walk_seg_id:     int = 0               # incremented on each new walk segment

current_lat: float = 49.22491703456117
current_lon: float = -122.96817654417664

# ── Route state ───────────────────────────────────────────────────────────────
_route_queue:   list  = []      # remaining waypoints [(lat, lon), ...] after current segment
_route_speed:   float = 4.5
_route_active:  bool  = False

# ── Location hold ─────────────────────────────────────────────────────────────
_PROJ_DIR = Path(__file__).parent.resolve()
VENV_PY     = str(_PROJ_DIR / "venv" / "bin" / "python3")
HOLD_SCRIPT = str(_PROJ_DIR / "hold_location.py")
WALK_SCRIPT = str(_PROJ_DIR / "walk_location.py")

_hold_proc: Optional[subprocess.Popen] = None
_hold_lat:  Optional[float] = None
_hold_lon:  Optional[float] = None

# Tunnel health — actively probed by watchdog instead of trusting "was once set"
_tunnel_alive: bool = False
_tunnel_last_check: float = 0.0


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


def _launch_walk_segment(slat: float, slon: float, elat: float, elon: float, speed: float) -> dict:
    """Blocking: launch walk subprocess and wait for WALK_START handshake.
    Updates global walk state on success. Caller must NOT hold _lock."""
    global walk_proc, walk_eta, walk_target
    global _walk_start_time, _walk_total_sec, _walk_start_pos, _walk_seg_id
    global _hold_proc, _hold_lat, _hold_lon

    dist    = haversine_m(slat, slon, elat, elon)
    eta_sec = dist / (max(0.05, speed) * 1000 / 3600)

    try: WALK_LAST_FILE.unlink()
    except FileNotFoundError: pass

    cmd = [VENV_PY, WALK_SCRIPT, rsd_host, str(rsd_port),
           str(slat), str(slon), str(elat), str(elon),
           str(speed), "1.0", str(WALK_LAST_FILE)]
    print(f"[walk] launching: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)

    started    = threading.Event()
    stderr_buf = []

    def _stdout_reader():
        for line in iter(proc.stdout.readline, ''):
            line = line.rstrip()
            if not line: continue
            print(f"[walk:out] {line}")
            if "WALK_START" in line:
                started.set()

    def _stderr_reader():
        for line in iter(proc.stderr.readline, ''):
            line = line.rstrip()
            if line:
                print(f"[walk:err] {line}")
                stderr_buf.append(line)

    threading.Thread(target=_stdout_reader, daemon=True).start()
    threading.Thread(target=_stderr_reader, daemon=True).start()

    deadline = time.time() + 20
    while time.time() < deadline:
        if started.is_set():
            break
        if proc.poll() is not None:
            err = _clean("\n".join(stderr_buf))
            return {"ok": False,
                    "msg": err[:300] or f"步行进程退出 (code {proc.returncode})"}
        time.sleep(0.1)
    else:
        proc.terminate()
        err = _clean("\n".join(stderr_buf))
        return {"ok": False, "msg": f"步行启动超时（20 s）。stderr: {err[:200]}"}

    with _lock:
        walk_proc        = proc
        _hold_proc       = proc
        _hold_lat        = None
        _hold_lon        = None
        walk_eta         = time.time() + eta_sec
        walk_target      = (elat, elon)
        _walk_start_time = time.time()
        _walk_total_sec  = eta_sec
        _walk_start_pos  = (slat, slon)
        _walk_seg_id    += 1

    return {"ok": True, "eta": int(eta_sec), "dist": int(dist)}


def _watchdog_loop():
    """Tick every 5 s: probe tunnel; recover crashed walks; advance route queue;
    sample walking path; restart dead holds."""
    global _tunnel_alive, _tunnel_last_check
    global current_lat, current_lon, _hold_lat, _hold_lon
    global _route_active, _route_queue
    while True:
        time.sleep(5)

        # Tunnel health probe — outside lock since it's a blocking TCP call
        if rsd_host and rsd_port:
            _tunnel_alive = (_check_rsd_reachable(rsd_host, rsd_port, timeout=2.0) is None)
        else:
            _tunnel_alive = False
        _tunnel_last_check = time.time()

        action = None
        with _lock:
            if walk_proc and walk_proc.poll() is not None and walk_target:
                # Walk subprocess died unexpectedly
                last = _read_walk_last() or _interp_pos()
                _cancel_walk_state()
                current_lat, current_lon = last

                if _route_active and _route_queue:
                    next_wp = _route_queue.pop(0)
                    action = ('route_next', (last, next_wp))
                else:
                    _route_active = False
                    _route_queue = []
                    _hold_lat, _hold_lon = last
                    action = ('recover', last)

            elif walk_proc and walk_proc.poll() is None and walk_target:
                # Check if current segment has finished (ETA passed)
                if _route_active and walk_eta and time.time() > walk_eta + 1.0:
                    arrived = walk_target
                    _cancel_walk_state()
                    current_lat, current_lon = arrived[0], arrived[1]

                    if _route_queue:
                        next_wp = _route_queue.pop(0)
                        action = ('route_next', (arrived, next_wp))
                    else:
                        _route_active = False
                        _hold_lat, _hold_lon = arrived
                        action = ('hold_arrived', arrived)
                else:
                    action = ('sample', _interp_pos())

            elif _hold_lat is not None and (_hold_proc is None or _hold_proc.poll() is not None):
                action = ('rehold', (_hold_lat, _hold_lon))

        if not action: continue
        kind, pos = action[0], action[1]

        if kind == 'recover':
            print(f"[watchdog] walk died, holding at last-known {pos}")
            _sample_path(*pos)
            _launch_hold_proc(*pos)

        elif kind == 'sample':
            _sample_path(*pos)

        elif kind == 'rehold':
            print(f"[watchdog] hold proc died, restarting at {pos}")
            _launch_hold_proc(*pos)

        elif kind == 'route_next':
            (slat, slon), (elat, elon) = pos
            print(f"[watchdog] route advancing: {slat:.5f},{slon:.5f} → {elat:.5f},{elon:.5f}")
            _sample_path(slat, slon)
            r = _launch_walk_segment(slat, slon, elat, elon, _route_speed)
            if not r['ok']:
                print(f"[watchdog] route_next failed: {r['msg']} — stopping route")
                with _lock:
                    _route_active = False
                    _route_queue = []
                    _hold_lat, _hold_lon = slat, slon
                _launch_hold_proc(slat, slon)

        elif kind == 'hold_arrived':
            print(f"[watchdog] route complete, holding at {pos}")
            _launch_hold_proc(*pos)


def start_location_hold(lat: float, lon: float) -> dict:
    global _hold_lat, _hold_lon
    result = _launch_hold_proc(lat, lon)
    if result["ok"]:
        _hold_lat, _hold_lon = lat, lon
    return result


def stop_location_hold():
    global _hold_lat, _hold_lon
    _hold_lat = _hold_lon = None
    _kill_hold()
    run_cmd(["developer", "dvt", "simulate-location", "clear"] + rsd_args())


# Start watchdog background thread
threading.Thread(target=_watchdog_loop, daemon=True).start()

LOCATIONS_FILE = Path(__file__).parent / "locations.json"
PATH_FILE      = Path(__file__).parent / "walk_path.json"
WALK_LAST_FILE = Path("/tmp/pikmin_walk_last")
PATH_MIN_DIST_M = 50

walk_path: list = []

def _load_path():
    global walk_path
    if PATH_FILE.exists():
        try: walk_path = json.loads(PATH_FILE.read_text())
        except Exception: walk_path = []

def _save_path():
    try: PATH_FILE.write_text(json.dumps(walk_path, ensure_ascii=False))
    except Exception as e: print(f"[path] save failed: {e}")

_load_path()


# ── Utilities ─────────────────────────────────────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _sample_path(lat: float, lon: float) -> bool:
    if walk_path:
        last = walk_path[-1]
        if haversine_m(last['lat'], last['lon'], lat, lon) < PATH_MIN_DIST_M:
            return False
    walk_path.append({'lat': lat, 'lon': lon, 'ts': int(time.time())})
    _save_path()
    return True


def _read_walk_last():
    try:
        a, b = WALK_LAST_FILE.read_text().split()
        return float(a), float(b)
    except Exception:
        return None


_NOISE = ("NotOpenSSLWarning", "urllib3", "LibreSSL", "warnings.warn",
          "NotOpenSSL", "site-packages/urllib")

def _clean(text: str) -> str:
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
        if walking:
            current_lat, current_lon = _interp_pos()
        elif walk_target is not None:
            current_lat, current_lon = walk_target
            walk_target = None
            walk_eta = None
        eta_remaining = round(max(0.0, walk_eta - time.time()), 1) if walk_eta and walking else None
        holding = _hold_lat is not None
        hold_alive = holding and _hold_proc is not None and _hold_proc.poll() is None
        return {
            "tunnel": rsd_host is not None,
            "tunnel_alive": _tunnel_alive,
            "rsd": f"{rsd_host}:{rsd_port}" if rsd_host else None,
            "holding": holding,
            "hold_alive": hold_alive,
            "walking": walking,
            "eta": eta_remaining,
            "lat": current_lat,
            "lon": current_lon,
            # Walk segment info (for client animation restart on route advance)
            "walk_seg_id":   _walk_seg_id,
            "walk_from":     list(_walk_start_pos) if _walk_start_pos else None,
            "walk_to":       list(walk_target) if walk_target else None,
            "walk_total_sec": _walk_total_sec,
            # Route info
            "route_active":    _route_active,
            "route_remaining": len(_route_queue),
            "route_speed":     _route_speed,
            "route_queue":     [{"lat": lat, "lon": lon} for lat, lon in _route_queue[:5]],
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
            mh = re.search(r'(?:RSD Address|Tunnel address|address)\s*[=:]\s*([0-9a-fA-F:.]+)', line, re.I)
            mp = re.search(r'(?:RSD Port|Tunnel port|port)\s*[=:]\s*(\d+)', line, re.I)
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
    global rsd_host, rsd_port, _tunnel_alive
    with _lock:
        rsd_host = req.host
        rsd_port = req.port
    _tunnel_alive = (_check_rsd_reachable(rsd_host, rsd_port, timeout=2.0) is None)
    return {"ok": True, "rsd": f"{rsd_host}:{rsd_port}", "tunnel_alive": _tunnel_alive}


class LocReq(BaseModel):
    lat: float
    lon: float
    speed: float = 4.5


def _cancel_walk_state():
    """Kill the walk subprocess and clear walk-related state. Caller holds _lock."""
    global walk_proc, walk_eta, walk_target
    global _walk_start_time, _walk_total_sec, _walk_start_pos
    if walk_proc and walk_proc.poll() is None:
        walk_proc.kill()
    walk_proc = None
    walk_eta = None
    walk_target = None
    _walk_start_time = None
    _walk_total_sec = None
    _walk_start_pos = None


def _interp_pos() -> tuple:
    """Estimate where the phone is right now based on walk progress. Caller holds _lock."""
    if (_walk_start_pos and walk_target and _walk_start_time and _walk_total_sec):
        t = min(1.0, max(0.0, (time.time() - _walk_start_time) / _walk_total_sec))
        slat, slon = _walk_start_pos
        elat, elon = walk_target
        return (slat + (elat - slat) * t, slon + (elon - slon) * t)
    return (current_lat, current_lon)


@app.post("/api/teleport")
def api_teleport(req: LocReq):
    global current_lat, current_lon, _route_active, _route_queue
    with _lock:
        _cancel_walk_state()
        _route_active = False
        _route_queue = []
    r = start_location_hold(req.lat, req.lon)
    if r["ok"]:
        with _lock:
            current_lat, current_lon = req.lat, req.lon
    return r


@app.post("/api/walk")
def api_walk(req: LocReq):
    global current_lat, current_lon, _route_active, _route_queue
    if not (rsd_host and rsd_port):
        return {"ok": False, "msg": "Tunnel 未连接，请先填写 RSD HOST/PORT"}
    reach_err = _check_rsd_reachable(rsd_host, rsd_port)
    if reach_err:
        return {"ok": False, "msg": reach_err}

    with _lock:
        slat, slon = current_lat, current_lon
        _cancel_walk_state()
        _kill_hold()
        _route_active = False
        _route_queue = []

    result = _launch_walk_segment(slat, slon, req.lat, req.lon, req.speed)
    if result["ok"]:
        _sample_path(slat, slon)
    return result


@app.post("/api/stop")
def api_stop():
    """Stop walking at the interpolated current position."""
    global current_lat, current_lon, _route_active, _route_queue
    with _lock:
        lat, lon = _interp_pos()
        _cancel_walk_state()
        _route_active = False
        _route_queue = []
        current_lat, current_lon = lat, lon
    _sample_path(lat, lon)
    start_location_hold(lat, lon)
    return {"ok": True}


@app.post("/api/clear")
def api_clear():
    global _route_active, _route_queue
    with _lock:
        _cancel_walk_state()
        _route_active = False
        _route_queue = []
    stop_location_hold()
    return {"ok": True}


# ── Route API ─────────────────────────────────────────────────────────────────

class RouteReq(BaseModel):
    waypoints: list   # [{"lat": float, "lon": float}, ...]
    speed: float = 4.5
    laps: int = 1


@app.post("/api/route")
def api_start_route(req: RouteReq):
    global current_lat, current_lon, _route_queue, _route_speed, _route_active

    if not (rsd_host and rsd_port):
        return {"ok": False, "msg": "Tunnel 未连接"}
    reach_err = _check_rsd_reachable(rsd_host, rsd_port)
    if reach_err:
        return {"ok": False, "msg": reach_err}

    try:
        wps = [(float(w["lat"]), float(w["lon"])) for w in req.waypoints]
    except (KeyError, TypeError, ValueError) as e:
        return {"ok": False, "msg": f"路线格式错误: {e}"}
    if not wps:
        return {"ok": False, "msg": "路线为空"}

    # Expand waypoints for laps
    expanded = wps * max(1, int(req.laps))

    with _lock:
        slat, slon = current_lat, current_lon
        _cancel_walk_state()
        _kill_hold()
        _route_queue  = list(expanded[1:])
        _route_speed  = req.speed
        _route_active = True

    first = expanded[0]
    _sample_path(slat, slon)
    result = _launch_walk_segment(slat, slon, first[0], first[1], req.speed)
    if not result["ok"]:
        with _lock:
            _route_active = False
            _route_queue  = []
        return result

    return {"ok": True, "total": len(expanded),
            "eta": result["eta"], "dist": result["dist"]}


@app.delete("/api/route")
def api_cancel_route():
    global _route_active, _route_queue, current_lat, current_lon
    with _lock:
        lat, lon = _interp_pos()
        _cancel_walk_state()
        _route_active = False
        _route_queue  = []
        current_lat, current_lon = lat, lon
    _sample_path(lat, lon)
    start_location_hold(lat, lon)
    return {"ok": True}


class RouteUpdateReq(BaseModel):
    speed: Optional[float] = None
    remove_idx: Optional[int] = None   # 0-based index into _route_queue


@app.patch("/api/route")
def api_update_route(req: RouteUpdateReq):
    global _route_speed, _route_queue, current_lat, current_lon

    if req.remove_idx is not None:
        with _lock:
            idx = req.remove_idx
            if 0 <= idx < len(_route_queue):
                removed = _route_queue.pop(idx)
                return {"ok": True, "removed": {"lat": removed[0], "lon": removed[1]}}
        return {"ok": False, "msg": "索引越界"}

    if req.speed is not None:
        # Restart current segment from current interpolated position with new speed
        with _lock:
            _route_speed = req.speed
            slat, slon   = _interp_pos()
            target       = walk_target
            _cancel_walk_state()
            _kill_hold()
            current_lat, current_lon = slat, slon

        if target:
            _sample_path(slat, slon)
            result = _launch_walk_segment(slat, slon, target[0], target[1], req.speed)
            return result

    return {"ok": True}


# ── Saved locations ────────────────────────────────────────────────────────────

@app.get("/api/saved")
def api_list_saved():
    return load_locs()


class SaveReq(BaseModel):
    name: str
    lat: float
    lon: float
    folder: Optional[str] = None


@app.post("/api/saved")
def api_save(req: SaveReq):
    locs = load_locs()
    prev = next((l for l in locs if l["name"] == req.name), None)
    folder = req.folder if req.folder is not None else (prev or {}).get("folder")
    locs = [l for l in locs if l["name"] != req.name]
    entry = {"name": req.name, "lat": req.lat, "lon": req.lon}
    if folder:
        entry["folder"] = folder
    locs.append(entry)
    save_locs(locs)
    return {"ok": True}


@app.delete("/api/saved/{name}")
def api_del_saved(name: str):
    save_locs([l for l in load_locs() if l["name"] != name])
    return {"ok": True}


# ── Walk path overlay ─────────────────────────────────────────────────────────

@app.get("/api/path")
def api_get_path():
    return walk_path


@app.delete("/api/path")
def api_clear_path():
    global walk_path
    walk_path = []
    _save_path()
    return {"ok": True}


class MoveReq(BaseModel):
    folder: Optional[str] = None


@app.patch("/api/saved/{name}")
def api_move_saved(name: str, req: MoveReq):
    locs = load_locs()
    found = False
    for l in locs:
        if l["name"] == name:
            found = True
            if req.folder:
                l["folder"] = req.folder
            else:
                l.pop("folder", None)
            break
    if not found:
        return {"ok": False, "msg": "not found"}
    save_locs(locs)
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
