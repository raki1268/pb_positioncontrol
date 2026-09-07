"""
walk_location.py — Smoothly walk the simulated location from start to end at a
constant speed, then hold the final position until killed.

Usage:
  walk_location.py HOST PORT SLAT SLON ELAT ELON SPEED_KMH [STEP_SEC] [LAST_POS_FILE]
                   [ALT] [H_ACC] [V_ACC]

Prints "WALK_START" once connected, "WALK_PROGRESS <pct>" periodically, then
"WALK_DONE" upon arrival.  After WALK_DONE the process blocks and re-asserts the
final position every 30 s to prevent drift, until SIGTERM/SIGINT.

ALT / H_ACC / V_ACC enable the 7-parameter DVT selector for enhanced location
injection; falls back to the basic 2-parameter selector if the device does not
support it.

If LAST_POS_FILE is given, writes "<lat> <lon>" to that file after every
successful set so the parent process can recover at the last known point if we
die unexpectedly.
"""
import asyncio
import math
import signal
import sys


def haversine_m(la1, lo1, la2, lo2):
    R = 6371000
    f1, f2 = math.radians(la1), math.radians(la2)
    df = math.radians(la2 - la1)
    dl = math.radians(lo2 - lo1)
    a = math.sin(df / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def main():
    if len(sys.argv) < 8:
        print(
            "usage: walk_location.py HOST PORT SLAT SLON ELAT ELON SPEED_KMH "
            "[STEP_SEC] [LAST_POS_FILE] [ALT] [H_ACC] [V_ACC]",
            file=sys.stderr,
        )
        sys.exit(1)

    host       = sys.argv[1]
    port       = int(sys.argv[2])
    slat, slon = float(sys.argv[3]), float(sys.argv[4])
    elat, elon = float(sys.argv[5]), float(sys.argv[6])
    speed_kmh  = float(sys.argv[7])
    step_sec   = float(sys.argv[8]) if len(sys.argv) > 8 else 1.0
    last_file  = sys.argv[9]        if len(sys.argv) > 9 else None
    altitude   = float(sys.argv[10]) if len(sys.argv) > 10 else 0.0
    h_acc      = float(sys.argv[11]) if len(sys.argv) > 11 else 5.0
    v_acc      = float(sys.argv[12]) if len(sys.argv) > 12 else 5.0

    def _write_last(la, lo):
        if not last_file:
            return
        try:
            with open(last_file, "w") as f:
                f.write(f"{la} {lo}\n")
        except Exception:
            pass

    speed_ms  = max(0.05, speed_kmh) * 1000 / 3600
    dist      = haversine_m(slat, slon, elat, elon)
    total_sec = max(step_sec, dist / speed_ms)
    steps     = max(2, int(round(total_sec / step_sec)))

    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.dtx import dtx_method
    from pymobiledevice3.dtx_service import DtxService as _DtxSvcWrapper
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulationService
    from pymobiledevice3.services.dvt.instruments.location_simulation_base import LocationSimulationBase

    class AdvancedLocationSimulationService(LocationSimulationService):
        @dtx_method(
            "simulateLocationWithLatitude:longitude:altitude:"
            "speed:course:horizontalAccuracy:verticalAccuracy:"
        )
        async def simulate_location_full_(
            self,
            latitude: float,
            longitude: float,
            altitude: float,
            speed: float,
            course: float,
            horizontal_accuracy: float,
            vertical_accuracy: float,
        ) -> None: ...

    class AdvancedLocationSimulation(
        _DtxSvcWrapper[AdvancedLocationSimulationService], LocationSimulationBase
    ):
        def __init__(self, dvt):
            _DtxSvcWrapper.__init__(self, dvt)
            LocationSimulationBase.__init__(self)

        async def set(self, lat_: float, lon_: float) -> None:
            await self.service.simulate_location_with_latitude_longitude_(lat_, lon_)

        async def set_full(
            self,
            lat_: float, lon_: float,
            alt_: float, spd_: float, crs_: float,
            hacc_: float, vacc_: float,
        ) -> None:
            await self.service.simulate_location_full_(
                lat_, lon_, alt_, spd_, crs_, hacc_, vacc_
            )

        async def clear(self) -> None:
            await self.service.stop_location_simulation()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def sleep_or_stop(t):
        try:
            await asyncio.wait_for(stop.wait(), timeout=t)
            return True
        except asyncio.TimeoutError:
            return False

    # Probe whether device supports the 7-param selector on first use.
    _use_full = True

    async def inject(ls, la, lo, spd=-1.0, crs=-1.0):
        """Try advanced injection; fall back to basic on first failure."""
        nonlocal _use_full
        if _use_full:
            try:
                await ls.set_full(la, lo, altitude, spd, crs, h_acc, v_acc)
                return
            except Exception as e:
                print(f"[walk] 7-param set failed ({e}), switching to basic",
                      file=sys.stderr, flush=True)
                _use_full = False
        await ls.set(la, lo)

    async with RemoteServiceDiscoveryService((host, port)) as rsd:
        async with DvtProvider(rsd) as dvt:
            async with AdvancedLocationSimulation(dvt) as ls:
                await inject(ls, slat, slon)
                _write_last(slat, slon)
                print("WALK_START", flush=True)

                last_pct = -10
                for i in range(1, steps + 1):
                    t = i / steps
                    lat = slat + (elat - slat) * t
                    lon = slon + (elon - slon) * t
                    try:
                        await inject(ls, lat, lon)
                        _write_last(lat, lon)
                    except Exception as e:
                        print(f"WALK_ERR set: {e}", file=sys.stderr, flush=True)
                    pct = int(t * 100)
                    if pct - last_pct >= 5 or i == steps:
                        print(f"WALK_PROGRESS {pct}", flush=True)
                        last_pct = pct
                    if i < steps:
                        if await sleep_or_stop(step_sec):
                            return

                print("WALK_DONE", flush=True)
                # Hold final position, re-asserting periodically to prevent drift.
                while not stop.is_set():
                    if await sleep_or_stop(30):
                        return
                    try:
                        await inject(ls, elat, elon, spd=0.0, crs=-1.0)
                        _write_last(elat, elon)
                    except Exception as e:
                        print(f"WALK_ERR rehold: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
