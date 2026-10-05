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


async def start_simulators(configs: list[ModuleConfig], keys: list[str], faults: list[str],
                           endpoint: str, speed: float = 1.0) -> Any:
    """Start the simulated modules on one endpoint, on the running event loop."""
    from sim.fake_module import SimulatedServer

    module_profiles = [config.profile for config in configs if config.profile.key in keys]
    if not module_profiles:
        return None
    server = SimulatedServer(endpoint, module_profiles)
    for module in server.modules.values():
        module.faults.update(faults)
        module.speed = speed
    try:
        await server.start()
    except OSError as exc:
        raise SystemExit(
            f"the simulator failed to start on {endpoint}: {exc} "
            f"(use --sim-endpoint opc.tcp://127.0.0.1:<free port>)"
        ) from exc
    return server


async def serve(args: argparse.Namespace) -> None:
    """The simulator, the OPC UA links and the web server, all on this one event loop."""
    import uvicorn

    configs = build_configs(args)
    keys = simulated_keys(args)
    simulator = None
    if keys:
        for index, config in enumerate(configs):
            if config.profile.key in keys:
                configs[index] = replace(config, endpoint=args.sim_endpoint)
        simulator = await start_simulators(configs, keys, args.fault, args.sim_endpoint, args.sim_speed)
    service = HmiService(configs, sampling_ms=args.sampling_ms)
    app = create_app(service)
    for config in configs:
        LOGGER.info(
            "module %-11s -> %s%s", config.profile.key, config.endpoint,
            " (simulated)" if config.profile.key in keys else "",
        )
    LOGGER.info("HMI on http://%s:%s", args.host, args.port)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning",
                                           ws_ping_interval=20))
    try:
        await server.serve()
    finally:
        if simulator is not None:
            await simulator.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        LOGGER.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
