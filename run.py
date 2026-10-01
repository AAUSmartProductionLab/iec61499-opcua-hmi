"""Command line entry point for the OPC UA HMI.

Examples:
    python run.py
    python run.py --modules filling --endpoint-filling opc.tcp://192.168.0.191:4840
    python run.py --simulate filling,stoppering
    python run.py --simulate --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading
from dataclasses import replace
from typing import Any

from hmi import profiles as prof
from hmi.app import create_app
from hmi.service import HmiService, ModuleConfig

LOGGER = logging.getLogger("hmi.run")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OPC UA HMI for IEC 61499 modules")
    parser.add_argument("--modules", default="filling,stoppering", help="comma separated module keys")
    parser.add_argument("--endpoint", action="append", default=[], metavar="KEY=URL",
                        help="endpoint of a module, repeatable (e.g. filling=opc.tcp://host:4840)")
    parser.add_argument("--simulate", nargs="?", const="", default=None, metavar="KEYS",
                        help="start the simulator for these modules (default: all)")
    parser.add_argument("--sim-endpoint", default="opc.tcp://127.0.0.1:4841",
                        help="endpoint the simulated modules serve on")
    parser.add_argument("--fault", action="append", default=[], choices=["sensor", "invariant"],
                        help="inject a fault into the simulator, repeatable")
    parser.add_argument("--sim-speed", type=float, default=1.0,
                        help="slow the simulator down: 0.25 is quarter speed, handy for demos")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--sampling-ms", type=int, default=200, help="subscription sampling interval")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def build_configs(args: argparse.Namespace) -> list[ModuleConfig]:
    keys = [key.strip() for key in args.modules.split(",") if key.strip()]
    overrides = {}
    for item in args.endpoint:
        key, _, url = item.partition("=")
        if not key or not url:
            raise SystemExit(f"--endpoint expects KEY=URL, got '{item}'")
        overrides[key.strip()] = url.strip()
    configs = []
    for key in keys:
        profile = prof.get_profile(key)
        endpoint = overrides.get(key, profile.default_endpoint)
        configs.append(ModuleConfig(profile=profile, endpoint=endpoint))
    return configs


def simulated_keys(args: argparse.Namespace) -> list[str]:
    if args.simulate is None:
        return []
    if not args.simulate:
        return [key.strip() for key in args.modules.split(",") if key.strip()]
    return [key.strip() for key in args.simulate.split(",") if key.strip()]


def run_simulators(configs: list[ModuleConfig], keys: list[str], faults: list[str],
                   endpoint: str, speed: float = 1.0) -> list[Any]:
    """Start the simulated modules on one endpoint, in a background event loop."""
    from sim.fake_module import SimulatedServer

    module_profiles = [config.profile for config in configs if config.profile.key in keys]
    if not module_profiles:
        return []
    ready = threading.Event()
    failures: list[BaseException] = []
    servers: list[Any] = []
    loop = asyncio.new_event_loop()

    def worker() -> None:
        asyncio.set_event_loop(loop)

        async def main() -> None:
            server = SimulatedServer(endpoint, module_profiles)
            for module in server.modules.values():
                module.faults.update(faults)
                module.speed = speed
            await server.start()
            servers.append(server)
            ready.set()
            while True:
                await asyncio.sleep(3600)

        try:
            loop.run_until_complete(main())
        except (asyncio.CancelledError, RuntimeError):
            pass
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
            ready.set()

    thread = threading.Thread(target=worker, name="simulator", daemon=True)
    thread.start()
    if not ready.wait(timeout=20):
        raise SystemExit("the simulator did not start")
    if failures:
        raise SystemExit(
            f"the simulator failed to start on {endpoint}: {failures[0]} "
            f"(use --sim-endpoint opc.tcp://127.0.0.1:<free port>)"
        )
    return servers


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    configs = build_configs(args)
    keys = simulated_keys(args)
    if keys:
        for index, config in enumerate(configs):
            if config.profile.key in keys:
                configs[index] = replace(config, endpoint=args.sim_endpoint)
        run_simulators(configs, keys, args.fault, args.sim_endpoint, args.sim_speed)
    service = HmiService(configs, sampling_ms=args.sampling_ms)
    service.start()
    app = create_app(service)
    for config in configs:
        LOGGER.info(
            "module %-11s -> %s%s", config.profile.key, config.endpoint,
            " (simulated)" if config.profile.key in keys else "",
        )
    LOGGER.info("HMI on http://%s:%s", args.host, args.port)
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        LOGGER.info("stopping")
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())