"""
hold_location.py  —  Keep simulate-location set alive via Python API.
Usage: python3 hold_location.py HOST PORT LAT LON [ALT] [H_ACC] [V_ACC] [SPEED] [COURSE]
Prints "LOCATION_SET_FULL" (advanced) or "LOCATION_SET" (basic fallback) on success,
then blocks until killed.
"""
import asyncio
import signal
import sys


async def main():
    if len(sys.argv) < 5:
        print("usage: hold_location.py HOST PORT LAT LON [ALT] [H_ACC] [V_ACC] [SPEED] [COURSE]",
              file=sys.stderr)
        sys.exit(1)

    host     = sys.argv[1]
    port     = int(sys.argv[2])
    lat      = float(sys.argv[3])
    lon      = float(sys.argv[4])
    altitude = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
    h_acc    = float(sys.argv[6]) if len(sys.argv) > 6 else 5.0
    v_acc    = float(sys.argv[7]) if len(sys.argv) > 7 else 5.0
    speed    = float(sys.argv[8]) if len(sys.argv) > 8 else 0.0
    course   = float(sys.argv[9]) if len(sys.argv) > 9 else -1.0

    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.dtx import dtx_method
    from pymobiledevice3.dtx_service import DtxService as _DtxSvcWrapper
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulationService
    from pymobiledevice3.services.dvt.instruments.location_simulation_base import LocationSimulationBase

    # Extended service class — adds the 7-parameter ObjC selector on top of the
    # existing 2-parameter one.  Same channel identifier is inherited, so the
    # same DVT service on the device handles both methods.
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

    async with RemoteServiceDiscoveryService((host, port)) as rsd:
        async with DvtProvider(rsd) as dvt:
            async with AdvancedLocationSimulation(dvt) as ls:
                try:
                    await ls.set_full(lat, lon, altitude, speed, course, h_acc, v_acc)
                    print("LOCATION_SET_FULL", flush=True)
                except Exception as e:
                    print(f"[hold] 7-param set failed ({e}), falling back to basic",
                          file=sys.stderr)
                    await ls.set(lat, lon)
                    print("LOCATION_SET", flush=True)
                await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
