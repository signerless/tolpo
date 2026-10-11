"""Command line: `serve` runs the HTTP API, `mcp` the MCP server, `scan` prints what it finds, `snapshot` saves one
picture from a camera so you can check the setup yourself, `watch` opens a live window of a camera, and
`automation` shows, stops or resumes the automation rules."""

from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from .config import Config, is_loopback, load_config
from .registry import NotEnabled, Registry, RegistryError
from .snapshot import RAW_VIDEO_INPUT, GrabError, elementary_video, pump, scrub
from .views import clean

NO_CREDENTIALS = (
    "No Tapo account configured. Set TAPO_EMAIL and TAPO_PASSWORD, or put them in\n"
    "~/.config/tolpo/config.toml under [tapo] (see README), then chmod 600 that file."
)


def check(cfg: Config) -> str | None:
    """Return a reason the server must not start, or None."""
    if not (cfg.email and cfg.password):
        return NO_CREDENTIALS
    if not is_loopback(cfg.host) and not cfg.token:
        return (
            f"Refusing to listen on {cfg.host} without a token: anyone on the network could control the house.\n"
            "Set TOLPO_TOKEN (or [server] token in the config), or bind to 127.0.0.1."
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


NOT_ENABLED = (
    "To allow pictures from this camera, put its name under [cameras] in ~/.config/tolpo/config.toml:\n"
    '    snapshots = ["<the camera name as shown in the Tapo app>"]\n'
    "plus the camera account (username and password) that you create in the Tapo app. See the README."
)


def _complain(exc: RegistryError) -> int:
    """Tell the owner what went wrong, with more detail than an agent gets."""
    print(f"{exc}.", file=sys.stderr)
    if exc.detail:
        print(f"ffmpeg said:\n{exc.detail}", file=sys.stderr)
    if isinstance(exc, NotEnabled):
        print(NOT_ENABLED, file=sys.stderr)
    return 1


async def _snapshot(cfg: Config, ref: str, output: str, quality: str) -> int:
    """Save one picture to a file only you can read. The picture is not printed or sent anywhere."""
    registry = Registry(cfg)
    try:
        await registry.discover()
        picture = await registry.snapshot(registry.resolve_one(ref), quality)
    except RegistryError as exc:
        return _complain(exc)
    finally:
        await registry.stop()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(picture.data)
    print(f"{output}: {picture.width}x{picture.height}, {len(picture.data) // 1024} KB")
    return 0


def _find_ffplay(cfg: Config) -> str | None:
    if cfg.ffmpeg and (sibling := Path(cfg.ffmpeg).with_name("ffplay")).is_file():
        return str(sibling)
    return shutil.which("ffplay")


async def _tail(stream: asyncio.StreamReader, keep: collections.deque) -> None:
    while line := await stream.readline():
        keep.append(line.decode("utf-8", "replace").rstrip())


async def _watch(cfg: Config, ref: str, quality: str, seconds: int | None = None, ffplay: str | None = None) -> int:
    """Open a live window of a camera on this screen. Nothing is recorded and nothing goes to an agent."""
    registry = Registry(cfg)
    proc = None
    tasks: list[asyncio.Task] = []
    try:
        await registry.discover()
        entry = registry.resolve_one(ref)
        player = ffplay or _find_ffplay(cfg)
        if player is None:
            print("ffplay was not found. It comes with ffmpeg: install a full ffmpeg, or set [cameras] ffmpeg.", file=sys.stderr)
            return 1
        url, feed, secret = await registry.live_view(entry, quality)
        argv = [player, "-hide_banner", "-loglevel", "warning", "-window_title", clean(entry.device.alias or entry.slug)]
        argv += ["-t", str(seconds)] if seconds else []
        if url:
            argv += ["-rtsp_transport", "tcp", "-i", url]
        else:  # the player gets the raw video only: it cannot make sense of the camera's extra track either
            try:
                fmt, feed = await elementary_video(feed, 20.0)
            except GrabError as exc:
                print(f"The camera stopped the video: {exc}.", file=sys.stderr)
                return 1
            argv += [*RAW_VIDEO_INPUT, "-f", fmt, "-i", "pipe:0"]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if feed is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        said: collections.deque = collections.deque(maxlen=20)
        tasks.append(asyncio.create_task(_tail(proc.stderr, said)))
        complaint = asyncio.create_task(pump(feed, proc.stdin)) if feed is not None else None
        if complaint is not None:
            tasks.append(complaint)
        print(f"Live view of {entry.slug}. Close the window to stop.", file=sys.stderr)
        await proc.wait()
        if complaint is not None and complaint.done() and isinstance(complaint.result(), GrabError):
            print(f"The camera stopped the video: {complaint.result()}.", file=sys.stderr)
            return 1
        if proc.returncode:
            print(scrub("\n".join(said), (secret,)), file=sys.stderr)
        return 1 if proc.returncode else 0
    except RegistryError as exc:
        return _complain(exc)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        await registry.stop()


def _automation(action: str) -> int:
    """The owner's switch for the automation engine. An agent can stop the rules; only the owner can start them again
    from here, and an agent needs level 3 and a phrase to do it through the tool."""
    from .automation.store import Store

    store = Store()
    try:
        state = store.load()
        if action in ("kill", "resume"):
            state.settings.killed = action == "kill"
            store.save(state)
    except (OSError, ValueError) as exc:
        print(f"The automation state could not be used: {type(exc).__name__}", file=sys.stderr)
        return 1
    enabled = sum(rule.enabled for rule in state.rules)
    print(f"{len(state.rules)} rule(s), {enabled} enabled. Automation is {'STOPPED' if state.settings.killed else 'running'}.")
    if action == "resume":
        print("A tolpo server that is already running picks this up when it restarts.")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tolpo", description=__doc__)
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    sub.add_parser("scan", help="discover devices and print them")
    sub.add_parser("mcp", help="run as an MCP server over stdio, for Claude Code and other agents")
    snap = sub.add_parser("snapshot", help="save one still picture from a camera you enabled, to check the setup")
    snap.add_argument("camera", help="camera id or name")
    snap.add_argument("-o", "--output", default="snapshot.jpg", help="file to write (default: snapshot.jpg)")
    snap.add_argument("--quality", choices=("low", "high"), default="low")
    watch = sub.add_parser("watch", help="open a live window of a camera you enabled (needs ffplay, which comes with ffmpeg)")
    watch.add_argument("camera", help="camera id or name")
    watch.add_argument("--quality", choices=("low", "high"), default="low")
    watch.add_argument("--seconds", type=int, help="close the window after this many seconds")
    auto = sub.add_parser("automation", help="show, stop or resume the automation rules (no Tapo account needed)")
    auto.add_argument("action", choices=("status", "kill", "resume"))
    args = parser.parse_args(argv)
    if args.command == "automation":
        sys.exit(_automation(args.action))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.command == "serve":
        cfg = replace(cfg, host=args.host or cfg.host, port=args.port or cfg.port)
    if problem := check(cfg):
        sys.exit(problem)

    if args.command == "scan":
        asyncio.run(_scan(cfg))
        return
    if args.command == "watch":
        sys.exit(asyncio.run(_watch(cfg, args.camera, args.quality, args.seconds)))
    if args.command == "snapshot":
        sys.exit(asyncio.run(_snapshot(cfg, args.camera, args.output, args.quality)))
    if args.command == "mcp":
        from .mcp_server import run

        run(cfg)
        return

    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
