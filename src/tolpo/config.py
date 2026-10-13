"""Runtime configuration: an optional TOML file plus environment overrides."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "~/.config/tolpo/config.toml"


@dataclass(frozen=True)
class Config:
    # Secrets are excluded from repr so they cannot leak through logs or tracebacks.
    email: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    host: str = "127.0.0.1"
    port: int = 8765
    subnets: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    discovery_interval: int = 600
    sweep: bool = True
    cache_ttl: float = 10.0
    cache_file: str = "~/.cache/tolpo/hosts.json"
    allow_write: frozenset[str] = frozenset()
    # The camera account is made in the Tapo app and is not the Tapo login. Pictures stay off for every
    # camera whose name is not listed in `snapshots`.
    camera_username: str = field(default="", repr=False)
    camera_password: str = field(default="", repr=False)
    snapshots: tuple[str, ...] = ()
    ffmpeg: str = ""  # path to ffmpeg; empty means look for it on PATH
    clip_dir: str = ""  # the one folder where saved camera clips go; empty means clips cannot be saved
    tier: int = 1  # the highest level of device method an agent may use; see policy.Tier
    allow_methods: frozenset[str] = frozenset()  # single device methods allowed above `tier`
    areas: tuple[str, ...] = ()  # which area modules offer tools; empty means all of them
    home_layout: str = "~/.config/tolpo/home.json"  # the owner's floor plan for the 3D view at /home; never shipped
    history_file: str = "~/.local/share/tolpo/history.jsonl"  # what `tolpo record` saw door and window sensors do


def _warn_if_shared(path: Path) -> None:
    if path.stat().st_mode & 0o077:
        log.warning("%s holds credentials but other users can read it; run: chmod 600 %s", path, path)


def _names(values) -> tuple[str, ...]:
    """A name list from the file or the environment. A lone string is one name, and blanks are dropped so an
    empty entry can never match."""
    if isinstance(values, str):
        values = [values]
    return tuple(v.strip() for v in values if isinstance(v, str) and v.strip())


def _tier(value) -> int:
    """The level from the file or the environment. A typo must not widen what an agent may do, so anything that is not a
    plain whole number from 0 to 4 stops the server from starting (2.9 and true are not read as 2 and 1)."""
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        value = int(value.strip())
    if type(value) is not int or not 0 <= value <= 4:
        raise ValueError("policy.tier (or TOLPO_TIER) must be a whole number from 0 to 4")
    return value


def load_config(path: str | None = None) -> Config:
    """Read the config file, then let environment variables override it.

    Environment: TAPO_EMAIL, TAPO_PASSWORD, TOLPO_TOKEN, TOLPO_HOST,
    TOLPO_PORT, TOLPO_CONFIG, TOLPO_CREDENTIALS_FILE, TOLPO_CAMERA_USERNAME,
    TOLPO_CAMERA_PASSWORD, TOLPO_SNAPSHOTS (camera names separated by commas), TOLPO_FFMPEG, TOLPO_TIER, TOLPO_AREAS (area names separated by commas),
    TOLPO_HOME_LAYOUT (the floor plan file of the 3D view at /home), TOLPO_HISTORY_FILE (where `tolpo record` saves
    what door and window sensors do).
    """
    cfg_path = Path(path or os.environ.get("TOLPO_CONFIG") or DEFAULT_CONFIG_PATH).expanduser()
    raw: dict = {}
    if cfg_path.is_file():
        raw = tomllib.loads(cfg_path.read_text())
    tapo, server, discovery, policy, cameras = (
        raw.get(k, {}) for k in ("tapo", "server", "discovery", "policy", "cameras")
    )

    email, password = tapo.get("email", ""), tapo.get("password", "")
    token = server.get("token", "")
    if (password or token or cameras.get("password")) and cfg_path.is_file():
        _warn_if_shared(cfg_path)

    # A JSON file {"email": ..., "password": ...} is read in place instead of copying the secret.
    cred_file = os.environ.get("TOLPO_CREDENTIALS_FILE") or tapo.get("credentials_file")
    if cred_file and not (email and password):
        cf = Path(cred_file).expanduser()
        data = json.loads(cf.read_text())
        _warn_if_shared(cf)
        email, password = data.get("email", ""), data.get("password", "")

    snapshots = os.environ["TOLPO_SNAPSHOTS"].split(",") if "TOLPO_SNAPSHOTS" in os.environ else cameras.get("snapshots", ())
    tier = _tier(os.environ.get("TOLPO_TIER", policy.get("tier", 1)))

    return Config(
        email=os.environ.get("TAPO_EMAIL", email),
        password=os.environ.get("TAPO_PASSWORD", password),
        token=os.environ.get("TOLPO_TOKEN", token),
        host=os.environ.get("TOLPO_HOST", server.get("host", "127.0.0.1")),
        port=int(os.environ.get("TOLPO_PORT", server.get("port", 8765))),
        subnets=tuple(discovery.get("subnets", ())),
        hosts=tuple(discovery.get("hosts", ())),
        discovery_interval=int(discovery.get("interval_seconds", 600)),
        sweep=bool(discovery.get("sweep", True)),
        cache_ttl=float(server.get("cache_ttl_seconds", 10.0)),
        cache_file=discovery.get("cache_file", "~/.cache/tolpo/hosts.json"),
        allow_write=frozenset(policy.get("allow_write", ())),
        camera_username=os.environ.get("TOLPO_CAMERA_USERNAME", cameras.get("username", "")),
        camera_password=os.environ.get("TOLPO_CAMERA_PASSWORD", cameras.get("password", "")),
        snapshots=_names(snapshots),
        ffmpeg=os.environ.get("TOLPO_FFMPEG", cameras.get("ffmpeg", "")),
        clip_dir=os.environ.get("TOLPO_CLIP_DIR", cameras.get("clip_dir", "")),
        tier=tier,
        allow_methods=frozenset(policy.get("allow_methods", ())),
        areas=_names(os.environ["TOLPO_AREAS"].split(",") if "TOLPO_AREAS" in os.environ else server.get("areas", ())),
        home_layout=os.environ.get("TOLPO_HOME_LAYOUT", server.get("home_layout", "~/.config/tolpo/home.json")),
        history_file=os.environ.get("TOLPO_HISTORY_FILE", server.get("history_file", "~/.local/share/tolpo/history.jsonl")),
    )


def local_subnets() -> list[str]:
    """The /24 of the interface that holds the default route. Override with `discovery.subnets`."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # A UDP connect only selects a route; no packet is sent.
        probe.connect(("192.0.2.1", 9))
        ip = probe.getsockname()[0]
    except OSError:
        return []
    finally:
        probe.close()
    return [str(ipaddress.ip_network(f"{ip}/24", strict=False))]


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
