"""
hold_location.py  —  Keep simulate-location set alive via Python API.
Usage: python3 hold_location.py <host> <port> <lat> <lon>
Prints "LOCATION_SET" when the location is applied, then blocks until killed.
"""
import asyncio
import signal
import sys


async def main():
    if len(sys.argv) != 5:
        print("usage: hold_location.py HOST PORT LAT LON", file=sys.stderr)
        sys.exit(1)

    host = sys.argv[1]
    port = int(sys.argv[2])
    lat  = float(sys.argv[3])
    lon  = float(sys.argv[4])

    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with RemoteServiceDiscoveryService((host, port)) as rsd:
        async with DvtProvider(rsd) as dvt:
            async with LocationSimulation(dvt) as ls:
                await ls.set(lat, lon)
                print("LOCATION_SET", flush=True)  # server reads this to confirm
                await stop.wait()                   # block until killed


if __name__ == "__main__":
    asyncio.run(main())
