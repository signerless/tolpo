"""Command line: `serve` runs the HTTP API, `mcp` the MCP server, `scan` prints what it finds."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace

from .config import Config, is_loopback, load_config
from .registry import Registry

NO_CREDENTIALS = (
    "No Tapo account configured. Set TAPO_EMAIL and TAPO_PASSWORD, or put them in\n"
    "~/.config/agentic-home/config.toml under [tapo] (see README), then chmod 600 that file."
)


def check(cfg: Config) -> str | None:
    """Return a reason the server must not start, or None."""
    if not (cfg.email and cfg.password):
        return NO_CREDENTIALS
    if not is_loopback(cfg.host) and not cfg.token:
        return (
            f"Refusing to listen on {cfg.host} without a token: anyone on the network could control the house.\n"
            "Set AGENTIC_HOME_TOKEN (or [server] token in the config), or bind to 127.0.0.1."
        )
    return None


async def _scan(cfg: Config) -> None:
    registry = Registry(cfg)
    try:
        await registry.discover()
        for entry in registry.entries():
            dev = entry.device
            where = f"  (via {entry.root.slug})" if entry.root else f"  {dev.host}"
            print(f"{entry.slug:24} {dev.model:8} {entry.kind:9}{where}")
        for host, problem in registry.problems.items():
            print(f"! {host}: {problem}", file=sys.stderr)
    finally:
        await registry.stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentic-home", description=__doc__)
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    sub.add_parser("scan", help="discover devices and print them")
    sub.add_parser("mcp", help="run as an MCP server over stdio, for Claude Code and other agents")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.command == "serve":
        cfg = replace(cfg, host=args.host or cfg.host, port=args.port or cfg.port)
    if problem := check(cfg):
        sys.exit(problem)

    if args.command == "scan":
        asyncio.run(_scan(cfg))
        return
    if args.command == "mcp":
        from .mcp_server import run

        run(cfg)
        return

    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
