"""Command line entry point."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .config import Config
from .demo import DemoSource


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unifi-dashboard", description="UniFi status dashboard server")
    parser.add_argument("-c", "--config", help="path to config.toml")
    parser.add_argument("--host", help="bind address (overrides config)")
    parser.add_argument("--port", type=int, help="bind port (overrides config)")
    parser.add_argument("--demo", action="store_true", help="serve synthetic data, no controller needed")
    parser.add_argument(
        "--demo-fault",
        # Taken from the source itself, so a new fault is offered here the
        # moment it exists rather than whenever someone remembers this list.
        choices=DemoSource.FAULTS,
        default="none",
        help="with --demo, inject a fault or traffic profile for testing",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = Config.load(args.config)
    if args.demo or args.demo_fault != "none":
        cfg.demo = True
    # Only override when the flag was actually given: assigning the argparse
    # default unconditionally would silently discard the config file's value.
    if args.demo_fault != "none":
        cfg.demo_fault = args.demo_fault
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port

    if not cfg.demo and cfg.unifi.auth_mode == "none":
        parser.error(
            "no controller credentials configured. Set unifi.api_key (or username/password) "
            "in config.toml, or run with --demo."
        )

    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
