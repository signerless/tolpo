"""Device registry: discovery, state cache, lookup by name, and the few control operations."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import hashlib
import ipaddress
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import kasa
from kasa import Device, DeviceType, Feature, Module
from kasa.exceptions import AuthenticationError, KasaException

from . import compat, policy
from .config import Config, local_subnets
from .media import live_chunks
from .snapshot import QUALITIES, GrabError, Picture, grab, live_jpegs, rtsp_url

log = logging.getLogger(__name__)

UPDATE_TIMEOUT = 20.0
LIVE_STREAMS = 2  # cameras that may send live video at the same time
LIVE_SECONDS = 900  # longest live view of a wired camera; the viewer can start a new one
LIVE_BATTERY_SECONDS = 120  # a battery camera stays awake while it streams, so its live view is short
STOP_GRACE = 10.0  # how long a live view that did not start may take to close its video before the camera is given back
SWEEP_MAX_HOSTS = 1024  # do not unicast-sweep anything larger than a /22
BOOT_GRACE = 15.0  # at startup, stop waiting for slow devices after this long and let them finish in the background
RETRY_DELAYS = (5.0, 15.0, 45.0)  # seconds between retries of hosts we know but could not reach
CACHE_MAX_AGE = 30 * 24 * 3600  # forget a remembered host only after this long without seeing it
LOGIN_COOLDOWN = 300.0  # after a camera rejects the login, leave it alone this long: repeated failures can lock it out


_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SLUGS_MAX = 4096  # remembered slugs: a house has dozens of devices, so more than this in the file is damage


def device_key(device: Device) -> str:
    """A short name for a device that is safe to store in a file: a hash of its id, never the id itself."""
    return hashlib.sha256(str(device.device_id).encode()).hexdigest()[:16]


def describe_failure(exc: KasaException) -> str:
    """What went wrong, in words that are safe to show an agent. The library's own text can carry the device's
    address or text the device chose, so only the device's error code or the kind of failure is used."""
    code = getattr(exc, "error_code", None)
    if code is not None:
        return f"error code {getattr(code, 'name', code)}"
    kind = type(exc).__name__.lstrip("_")
    return {"ConnectionError": "the connection failed", "UnsupportedDeviceError": "this device is not supported"}.get(kind, kind)


def failure_code(exc: BaseException) -> str:
    """The name of the device's error code behind a DeviceUnavailable, for example PARAMS_ERROR, or '' when there is none.
    It is a name from python-kasa's list, so it is safe to show an agent, and it tells the owner what the device said."""
    code = getattr(exc.__cause__, "error_code", None)
    return str(getattr(code, "name", "") or "")


class RegistryError(Exception):
    status_code = 400
    detail = ""  # for the owner at a terminal only; never put in an API or tool response

    def __init__(self, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


class NotFound(RegistryError):
    status_code = 404


class Ambiguous(RegistryError):
    status_code = 409


class Forbidden(RegistryError):
    status_code = 403


class NotEnabled(Forbidden):
    """The owner has not allowed pictures from this camera."""


class BadRequest(RegistryError):
    status_code = 400


class DeviceUnavailable(RegistryError):
    status_code = 502


class Busy(RegistryError):
    status_code = 409


class LiveStream:
    """The pictures of one live view. `aclose()` always gives the camera back, also when nobody read a single picture
    (a plain generator would skip its cleanup then), and it may be called more than once."""

    def __init__(self, first: bytes, frames: AsyncIterator[bytes], release: Callable[[], None], slug: str):
        self._first, self._frames, self._release, self._slug = first, frames, release, slug
        self._closed = False

    def __aiter__(self) -> "LiveStream":
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        if self._first is not None:
            picture, self._first = self._first, None
            return picture
        try:
            return await self._frames.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise
        except GrabError as exc:
            log.warning("live video from %s ended: %s", self._slug, exc.reason)
            await self.aclose()
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._frames.aclose()
        finally:
            self._release()


class Unsupported(RegistryError):
    """tolpo cannot do this on this machine, for example because ffmpeg is missing."""

    status_code = 501


_KIND = {
    DeviceType.Bulb: "light",
    DeviceType.LightStrip: "light",
    DeviceType.Dimmer: "light",
    DeviceType.Plug: "plug",
    DeviceType.Strip: "plug",
    DeviceType.StripSocket: "plug",
    DeviceType.WallSwitch: "switch",
    DeviceType.Sensor: "sensor",
    DeviceType.Hub: "hub",
    DeviceType.Camera: "camera",
    DeviceType.Doorbell: "doorbell",
}


def jsonable(value: Any) -> Any:
    """Features return datetimes, tuples and enums; turn them into plain JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return str(value)


@dataclass(eq=False)
class Entry:
    device: Device
    slug: str
    root: Entry | None = None  # the hub, for a hub child
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    picture_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # a camera serves one of our streams at a time
    last_update: float = 0.0
    online: bool = True
    error: str | None = None
    known_model: str = ""  # the model when the device was adopted, for when the library cannot read it

    @property
    def top(self) -> Entry:
        return self.root or self

    @property
    def kind(self) -> str:
        return _KIND.get(self.device.device_type, self.device.device_type.name.lower())


def model_of(entry: Entry) -> str:
    """The device's model. The library reads it from the device's last answer, which after a failed update can be an
    error code instead of the device's info, and then raises: the model seen when the device was adopted is used."""
    try:
        return entry.device.model
    except Exception:
        return entry.known_model


class Registry:
    def __init__(self, cfg: Config):
        compat.allow_ecdsa_cameras()
        self.cfg = cfg
        self._entries: dict[str, Entry] = {}  # keyed by the device's own id
        self.problems: dict[str, str] = {}
        self._failed_models: dict[str, str] = {}  # hosts that answered discovery but would not connect
        self.last_discovery: float | None = None
        self._cache = Path(cfg.cache_file).expanduser() if cfg.cache_file else None
        # host -> {"seen", "name", "model"}. Never shrinks because one scan missed a device.
        self._known: dict[str, dict[str, Any]] = self._load_cache()
        # device key -> slug. A device keeps the short id it was first given, also after a rename or a restart.
        self._slugs: dict[str, str] = {}
        self._slug_seen: dict[str, float] = {}  # device key -> when a device with that key was last here
        self._load_slugs()
        self._missing: dict[str, int] = {}  # hub child id -> updates in a row in which its hub did not list it
        self.retry_delays = RETRY_DELAYS
        self.boot_grace = BOOT_GRACE
        self._connecting: set[str] = set()  # hosts with a connection attempt in flight
        self._stragglers: set[asyncio.Task] = set()  # attempts that outlived the startup grace period
        self._task: asyncio.Task | None = None
        self.ready = asyncio.Event()  # set once the first discovery has finished, successfully or not
        self._discovery_lock = asyncio.Lock()
        self.grabs = asyncio.Semaphore(2)  # ffmpeg decodes video; do not run a pile of them at once
        self.lives = asyncio.Semaphore(LIVE_STREAMS)  # live video runs ffmpeg for minutes; keep the number small
        self._rejected: dict[str, float] = {}  # slug -> when the camera last refused our login for video

    # ---- lifecycle -------------------------------------------------------

    async def start(self, *, discover: bool = True, wait: bool = True) -> None:
        """Begin discovery. With wait=False return at once and let the first scan run in the background."""
        if not discover:
            self.ready.set()
            return
        self._task = asyncio.create_task(self._bootstrap())
        if wait:
            await self.ready.wait()

    async def _bootstrap(self) -> None:
        try:
            if self._known or self.cfg.hosts:
                # Known hosts answer in well under a second, so the first requests need not wait
                # for the 10 s broadcast; the full sweep below still finds anything new or moved.
                await self.discover(quick=True, grace=self.boot_grace)
                self.ready.set()
                await self.discover()
            else:
                await self.discover(grace=self.boot_grace)  # first run ever: nothing is remembered yet
            self.ready.set()
            await self._settle()
        except Exception:
            log.exception("initial discovery failed")
        finally:
            self.ready.set()
        await self._rescan_loop()

    async def wait_ready(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for straggler in list(self._stragglers):
            straggler.cancel()
        await asyncio.gather(*self._stragglers, return_exceptions=True)
        for entry in list(self._entries.values()):
            if entry.root is None:
                with contextlib.suppress(Exception):
                    await entry.device.disconnect()

    async def _rescan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.discovery_interval)
            try:
                await self.discover()
                await self._settle()
            except Exception:
                log.exception("periodic discovery failed")

    async def _settle(self) -> None:
        """Retry hosts we know but could not reach (a camera that is busy or asleep) soon, not at the next full scan."""
        for delay in self.retry_delays:
            if not self.not_connected():
                return
            await asyncio.sleep(delay)
            await self.discover(quick=True)

    def not_connected(self) -> list[dict[str, str]]:
        """Devices remembered from earlier runs that are not reachable right now."""
        reachable = {e.device.host for e in self._entries.values() if e.root is None and e.online}
        missing = [
            {
                "host": host,
                "name": str(info.get("name") or host),
                "model": str(info.get("model") or ""),
                "reason": self.problems.get(host, "connecting" if host in self._connecting else "not reached yet"),
            }
            for host, info in sorted(self._known.items())
            if host not in reachable
        ]
        # Devices that answered discovery but never connected are not remembered, yet they exist.
        for host, reason in sorted(self.problems.items()):
            if host not in self._known and host not in reachable:
                model = self._failed_models.get(host, "")
                missing.append({"host": host, "name": model or "unknown device", "model": model, "reason": reason})
        return missing

    # ---- discovery -------------------------------------------------------

    async def discover(self, *, quick: bool = False, grace: float | None = None) -> int:
        """Find devices. `quick` skips the slow UDP broadcast and asks only hosts we already know.
        With `grace`, return after that many seconds even if some devices are still connecting; they finish
        in the background and are reported as `connecting` meanwhile."""
        async with self._discovery_lock:
            return await self._discover(quick=quick, grace=grace)

    async def _discover(self, *, quick: bool, grace: float | None) -> int:
        creds = kasa.Credentials(self.cfg.email, self.cfg.password)

        async def on_unsupported(exc: Exception) -> None:
            self.problems[getattr(exc, "host", "?")] = "unsupported device type"

        subnets = [] if quick else (list(self.cfg.subnets) or local_subnets())
        if not quick and not subnets and not self.cfg.hosts:
            log.warning("no subnet to scan: set discovery.subnets or discovery.hosts")

        found: dict[str, Device] = {}
        sweeps = await asyncio.gather(
            *(
                kasa.Discover.discover(
                    target=str(ipaddress.ip_network(s, strict=False).broadcast_address),
                    credentials=creds,
                    discovery_timeout=10,
                    discovery_packets=5,
                    on_unsupported=on_unsupported,
                )
                for s in subnets
            ),
            return_exceptions=True,
        )
        for sweep in sweeps:
            if isinstance(sweep, dict):
                found.update(sweep)
            else:
                log.warning("subnet scan failed: %s", type(sweep).__name__)
        known_online = {e.device.host for e in self._entries.values() if e.root is None and e.online}

        # UDP broadcast is lossy and battery cameras answer it unevenly, so ask remembered and configured
        # hosts directly. Each one is connected the moment it answers; a silent device must not delay the rest.
        remembered = set(self._known) | set(self.cfg.hosts) | {e.device.host for e in self._entries.values() if e.root is None}
        silent = sorted(remembered - found.keys() - known_online - self._connecting)

        async def bring_up(host: str, device: Device) -> None:
            if host in known_online or host in self._connecting:
                with contextlib.suppress(Exception):
                    await device.disconnect()
                return
            self._connecting.add(host)
            try:
                await asyncio.wait_for(device.update(), UPDATE_TIMEOUT)
                self._adopt(device)
                self._save_cache()
            except Exception as exc:
                self.problems[host] = type(exc).__name__
                self._failed_models[host] = str(getattr(device, "model", "") or "")
                with contextlib.suppress(Exception):
                    await device.disconnect()
            finally:
                self._connecting.discard(host)

        async def probe_then_bring_up(host: str) -> None:
            try:
                device = await kasa.Discover.discover_single(host, credentials=creds, discovery_timeout=3)
            except Exception:
                device = None
            if device is None:
                self.problems[host] = "remembered host did not answer"
                return
            await bring_up(host, device)

        tasks = [asyncio.create_task(bring_up(h, d)) for h, d in found.items()]
        tasks += [asyncio.create_task(probe_then_bring_up(h)) for h in silent]
        if not quick and self.cfg.sweep:
            swept: dict[str, Device] = {}
            await self._sweep(subnets, creds, swept, skip=found.keys() | known_online | self._connecting | set(silent))
            tasks += [asyncio.create_task(bring_up(h, d)) for h, d in swept.items()]
        if tasks and grace is not None:
            _, pending = await asyncio.wait(tasks, timeout=grace)
            for task in pending:  # keep a reference so they are neither garbage collected nor forgotten
                self._stragglers.add(task)
                task.add_done_callback(self._stragglers.discard)
            if pending:
                log.info("%d device(s) still connecting after %.0f s; continuing in the background", len(pending), grace)
        elif tasks:
            await asyncio.gather(*tasks)
        self.last_discovery = time.time()
        self._save_cache()
        total = len(self._entries)
        log.info("discovery done: %d devices, %d not connected", total, len(self.not_connected()))
        return total

    async def _sweep(self, subnets: list[str], creds: kasa.Credentials, found: dict[str, Device], skip: set[str]) -> None:
        """Ask every address in the subnet directly. UDP broadcast is lossy, and battery devices such as
        the C610 camera ignore it at times yet always answer a direct question."""
        sem = asyncio.Semaphore(64)

        async def one(ip: str) -> None:
            async with sem:
                try:
                    device = await kasa.Discover.discover_single(ip, credentials=creds, discovery_timeout=2)
                except Exception:
                    return
                if device is not None:
                    found.setdefault(ip, device)

        candidates: list[str] = []
        for subnet in subnets:
            network = ipaddress.ip_network(subnet, strict=False)
            if network.num_addresses > SWEEP_MAX_HOSTS:
                log.info("not sweeping %s: more than %d addresses", subnet, SWEEP_MAX_HOSTS)
                continue
            candidates += [str(ip) for ip in network.hosts() if str(ip) not in skip]
        await asyncio.gather(*(one(ip) for ip in candidates))

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self._cache:
            return {}
        try:
            raw = json.loads(self._cache.read_text()).get("hosts", {})
        except (OSError, ValueError, AttributeError):
            return {}
        if isinstance(raw, list):  # the format 0.1.0 wrote: only addresses
            return {h: {"seen": time.time()} for h in raw if isinstance(h, str)}
        if not isinstance(raw, dict):
            return {}
        return {h: info for h, info in raw.items() if isinstance(info, dict)}

    def _read_slugs(self) -> tuple[dict[str, str], dict[str, float]]:
        """The slugs in the cache file and when each was last seen. Anything that does not look right is ignored."""
        if not self._cache:
            return {}, {}
        try:
            data = json.loads(self._cache.read_text())
            raw, seen = data.get("slugs", {}), data.get("slug_seen", {})
        except (OSError, ValueError, AttributeError):
            return {}, {}
        if not isinstance(raw, dict):
            return {}, {}
        seen = seen if isinstance(seen, dict) else {}
        out, when, used, now = {}, {}, set(), time.time()
        # newest first, so that a file with too many entries keeps the devices that are still around
        ranked = sorted(raw.items(), key=lambda kv: -(seen.get(kv[0]) if isinstance(seen.get(kv[0]), (int, float)) else now))
        for key, slug in ranked[:SLUGS_MAX]:
            if isinstance(key, str) and isinstance(slug, str) and _SLUG.fullmatch(slug) and len(slug) <= 64 and slug not in used:
                out[key] = slug
                used.add(slug)
                stamp = seen.get(key)
                when[key] = float(stamp) if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else now
        return out, when

    def _load_slugs(self) -> None:
        self._slugs, self._slug_seen = self._read_slugs()

    def _merge_slugs_from_disk(self) -> None:
        """Another tolpo process may have given short ids since this one started. Take what this one does not know, so
        that two processes that share the cache give a device the same short id."""
        disk, when = self._read_slugs()
        used = set(self._slugs.values())
        for key, slug in disk.items():
            if key not in self._slugs and slug not in used:
                self._slugs[key], self._slug_seen[key] = slug, when[key]
                used.add(slug)
            elif key in self._slugs:
                self._slug_seen[key] = max(self._slug_seen.get(key, 0.0), when.get(key, 0.0))

    def _save_cache(self) -> None:
        if not self._cache:
            return
        cutoff = time.time() - CACHE_MAX_AGE
        self._known = {h: i for h, i in self._known.items() if i.get("seen", 0) >= cutoff}
        self._merge_slugs_from_disk()
        here = {device_key(e.device) for e in self._entries.values()}
        # A short id is given up for good only when its device has been away as long as a host is remembered.
        keep = [k for k in self._slugs if k in here or self._slug_seen.get(k, 0.0) >= cutoff]
        keep = sorted(keep, key=lambda k: -self._slug_seen.get(k, 0.0))[:SLUGS_MAX]
        for key in set(self._slugs) - set(keep):
            self._slugs.pop(key, None)
            self._slug_seen.pop(key, None)
        document = json.dumps(
            {"version": 2, "hosts": self._known, "slugs": self._slugs, "slug_seen": {k: self._slug_seen[k] for k in self._slugs if k in self._slug_seen}}
        )
        try:
            self._cache.parent.mkdir(parents=True, exist_ok=True)
            # Write a whole new file and move it into place, so that a crash cannot leave half a cache behind.
            fd, tmp = tempfile.mkstemp(prefix=".hosts-", dir=self._cache.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(document)
                os.replace(tmp, self._cache)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            log.warning("could not save the host cache: %s", exc)

    def _adopt(self, device: Device) -> None:
        top = self._upsert(device, None)
        self._known[device.host] = {"seen": time.time(), "name": device.alias, "model": device.model}
        self.problems.pop(device.host, None)
        self._failed_models.pop(device.host, None)
        for child in device.children or []:
            self._upsert(child, top)

    def _upsert(self, device: Device, root: Entry | None) -> Entry:
        did = device.device_id
        entry = self._entries.get(did)
        if entry is None:
            entry = Entry(device=device, slug=self._unique_slug(device), root=root)
            self._entries[did] = entry
        else:
            old = entry.device
            entry.device, entry.root = device, root
            if root is None and old is not device:
                asyncio.ensure_future(self._quiet_disconnect(old))
        entry.last_update = time.monotonic()
        entry.online, entry.error = True, None
        entry.known_model = model_of(entry) or entry.known_model
        self._slug_seen[device_key(device)] = time.time()
        return entry

    @staticmethod
    async def _quiet_disconnect(device: Device) -> None:
        with contextlib.suppress(Exception):
            await device.disconnect()

    def _unique_slug(self, device: Device) -> str:
        key, in_use = device_key(device), {e.slug for e in self._entries.values()}
        kept = self._slugs.get(key)
        if kept and kept not in in_use:
            return kept
        # A device not seen yet this run may still own a slug from an earlier run: leave those for their owners.
        taken = in_use | set(self._slugs.values())
        base = re.sub(r"[^a-z0-9]+", "-", (device.alias or device.model or "device").lower()).strip("-") or "device"
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
        self._slugs[key] = slug
        return slug

    # ---- lookup ----------------------------------------------------------

    def entries(self, *, kind: str | None = None, q: str | None = None) -> list[Entry]:
        items = list(self._entries.values())
        if kind:
            items = [e for e in items if e.kind == kind]
        if q:
            needle = q.lower()
            items = [e for e in items if needle in e.slug or needle in (e.device.alias or "").lower()]
        return sorted(items, key=lambda e: (e.top.slug, e.root is not None, e.slug))

    def resolve(self, ref: str) -> list[Entry]:
        """Match by device id, slug, case-insensitive name, or a glob such as `office*`."""
        if ref in self._entries:
            return [self._entries[ref]]
        low = ref.lower()
        items = list(self._entries.values())
        # A short id outlives a rename, so it can name one device while a different device now has that exact name.
        # Both match then, and the caller is asked to be specific instead of the older id winning.
        by_slug = [e for e in items if e.slug == low]
        found = by_slug + [e for e in items if (e.device.alias or "").lower() == low and e not in by_slug]
        if found:
            return found
        if any(c in ref for c in "*?["):
            return [
                e for e in items if fnmatch.fnmatchcase(e.slug, low) or fnmatch.fnmatchcase((e.device.alias or "").lower(), low)
            ]
        return []

    def forget(self, entry: Entry) -> None:
        """Drop a device that is known to be gone, for example a hub child that was just removed. It keeps its short id for
        the day it is paired again."""
        self._entries.pop(entry.device.device_id, None)
        self._missing.pop(entry.device.device_id, None)

    def by_slug(self, slug: str) -> Entry:
        """The device that has exactly this short id. Rules and rooms point at short ids, so a device that is missing is an
        error, never another device with a similar name."""
        for entry in self._entries.values():
            if entry.slug == slug:
                return entry
        raise NotFound(f"no device has the short id {slug!r}")

    def resolve_one(self, ref: str) -> Entry:
        found = self.resolve(ref)
        if not found:
            raise NotFound(f"no device matches {ref!r}")
        if len(found) > 1:
            raise Ambiguous(f"{ref!r} matches {len(found)} devices", [e.slug for e in found])
        return found[0]

    # ---- state -----------------------------------------------------------

    async def refresh(self, entry: Entry, *, force: bool = False) -> None:
        top = entry.top
        if not force and time.monotonic() - top.last_update < self.cfg.cache_ttl:
            return
        async with top.lock:
            if not force and time.monotonic() - top.last_update < self.cfg.cache_ttl:
                return
            await self._update_locked(top)

    async def refresh_all(self, entries: list[Entry], *, force: bool = False) -> None:
        await asyncio.gather(*(self.refresh(t, force=force) for t in {e.top for e in entries}))

    async def _update_locked(self, top: Entry) -> None:
        try:
            await asyncio.wait_for(top.device.update(), UPDATE_TIMEOUT)
        except Exception as exc:
            self._mark(top, online=False, error=type(exc).__name__)
            log.warning("update failed for %s: %s", top.slug, type(exc).__name__)
            return
        children = top.device.children or []
        for child in children:
            self._upsert(child, top)
        # A child that was unpaired is no longer in the hub's list. One list can be partial (a later page that did not come
        # back is not an error to the library), so a child goes only after two updates in a row that leave it out.
        present = {child.device_id for child in children}
        for did, entry in list(self._entries.items()):
            if entry.root is not top:
                continue
            if did in present:
                self._missing.pop(did, None)
            elif self._missing.get(did, 0) + 1 >= 2:
                self.forget(entry)
            else:
                self._missing[did] = self._missing.get(did, 0) + 1
        self._mark(top, online=True)

    def _mark(self, top: Entry, *, online: bool, error: str | None = None) -> None:
        top.last_update = time.monotonic()
        for e in self._entries.values():
            if e.top is top:
                e.online, e.error = online, error

    # ---- views -----------------------------------------------------------

    @staticmethod
    def feature_value(feature: Feature) -> Any:
        try:
            return jsonable(feature.value)
        except Exception:
            return None

    def capabilities(self, entry: Entry) -> list[str]:
        feats, kind, allow = entry.device.features, entry.kind, self.cfg.allow_write
        caps = []
        for cap, fid in (("power", "state"), ("brightness", "brightness"), ("color_temp", "color_temperature"), ("color", "hsv")):
            if fid in feats and policy.can_write(kind, fid, allow, self.cfg.tier):
                caps.append(cap)
        if "current_consumption" in feats:
            caps.append("energy")
        if kind == "sensor":
            caps.append("sensor")
        if self.pictures_enabled(entry):
            caps.append("snapshot")
        return caps

    def pictures_enabled(self, entry: Entry) -> bool:
        """Whether the owner has enabled pictures for this camera, by listing its name."""
        return (entry.root is None or entry.root.kind == "hub") and policy.can_snapshot(entry.kind, entry.device.alias, self.cfg.snapshots)

    def readings(self, entry: Entry) -> tuple[dict[str, Any], dict[str, str]]:
        values, units = {}, {}
        for fid, feat in entry.device.features.items():
            if fid in policy.HIDDEN:
                continue
            if feat.category is Feature.Category.Primary and feat.type in (Feature.Type.Sensor, Feature.Type.BinarySensor):
                values[fid] = self.feature_value(feat)
                if feat.unit:
                    units[fid] = str(feat.unit)
        return values, units

    # ---- control ---------------------------------------------------------

    def _require_write(self, entry: Entry, feature_id: str) -> Feature:
        feat = entry.device.features.get(feature_id)
        if feat is None or feature_id in policy.HIDDEN:
            raise NotFound(f"{entry.slug} has no feature {feature_id!r}")
        if not policy.can_write(entry.kind, feature_id, self.cfg.allow_write, self.cfg.tier):
            raise Forbidden(f"{feature_id!r} cannot be changed on {entry.kind} devices: the owner's policy blocks it")
        return feat

    async def _call_locked(self, entry: Entry, op: Callable[[], Awaitable[Any]]) -> Any:
        """Run `op` with the device's lock already held, and turn the library's failures into ours."""
        try:
            return await asyncio.wait_for(op(), UPDATE_TIMEOUT)
        except AuthenticationError as exc:
            raise DeviceUnavailable(f"{entry.slug} rejected the account credentials") from exc
        except TimeoutError as exc:
            raise DeviceUnavailable(f"{entry.slug} did not answer in time") from exc
        except KasaException as exc:
            raise DeviceUnavailable(f"{entry.slug} reported an error: {describe_failure(exc)}") from exc

    async def guarded(self, entry: Entry, op: Callable[[], Awaitable[Any]]) -> Any:
        """Run `op` on the device's connection, one at a time per hub or device, and return what it returns."""
        async with entry.top.lock:
            return await self._call_locked(entry, op)

    async def _run(self, entry: Entry, op: Callable[[], Awaitable[Any]]) -> None:
        top = entry.top
        async with top.lock:
            await self._call_locked(entry, op)
            # The command already succeeded; a failed refresh only means stale readings.
            await self._update_locked(top)

    async def set_power(self, entry: Entry, on: bool) -> None:
        self._require_write(entry, "state")
        dev = entry.device
        await self._run(entry, dev.turn_on if on else dev.turn_off)

    async def toggle(self, entry: Entry) -> bool:
        self._require_write(entry, "state")
        await self.refresh(entry, force=True)
        target = not bool(entry.device.is_on)
        await self.set_power(entry, target)
        return target

    async def set_light(
        self,
        entry: Entry,
        *,
        on: bool | None = None,
        brightness: int | None = None,
        color_temp: int | None = None,
        hue: int | None = None,
        saturation: int | None = None,
    ) -> None:
        light = entry.device.modules.get(Module.Light)
        if light is None:
            raise BadRequest(f"{entry.slug} is not a light")
        if (hue is None) != (saturation is None):
            raise BadRequest("hue and saturation must be given together")
        if color_temp is not None and hue is not None:
            raise BadRequest("choose either color_temp or hue/saturation, not both")
        changes = brightness is not None or color_temp is not None or hue is not None
        if on is False and changes:
            raise BadRequest("cannot combine on=false with other settings")
        if on is None and not changes:
            raise BadRequest("nothing to change")

        if on is not None:
            self._require_write(entry, "state")  # switching on or off is the same level 1 action as set_power
        for fid, val in (("brightness", brightness), ("color_temperature", color_temp), ("hsv", hue)):
            if val is not None:
                feat = self._require_write(entry, fid)
                if fid != "hsv" and not (feat.minimum_value <= val <= feat.maximum_value):
                    raise BadRequest(f"{fid} must be between {feat.minimum_value} and {feat.maximum_value}")

        dev = entry.device

        async def op() -> None:
            if on:
                await dev.turn_on()
            if hue is not None:
                await light.set_hsv(hue, saturation, brightness)
            elif color_temp is not None:
                await light.set_color_temp(color_temp, brightness=brightness)
            elif brightness is not None:
                await light.set_brightness(brightness)
            if on is False:
                await dev.turn_off()

        await self._run(entry, op)

    async def set_feature(self, entry: Entry, feature_id: str, value: Any) -> None:
        feat = self._require_write(entry, feature_id)
        kind = feat.type
        if kind is Feature.Type.Switch:
            if not isinstance(value, bool):
                raise BadRequest(f"{feature_id} takes true or false")
        elif kind is Feature.Type.Number:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BadRequest(f"{feature_id} takes a number")
            if not (feat.minimum_value <= value <= feat.maximum_value):
                raise BadRequest(f"{feature_id} must be between {feat.minimum_value} and {feat.maximum_value}")
        elif kind is Feature.Type.Choice:
            if value not in (feat.choices or []):
                raise BadRequest(f"{feature_id} must be one of {feat.choices}")
        elif kind is Feature.Type.Action:
            value = None
        else:
            raise BadRequest(f"{feature_id} cannot be set directly")
        await self._run(entry, lambda: feat.set_value(value))

    async def check_picture(self, entry: Entry, quality: str) -> None:
        """Everything that must hold before any video is opened. Raises an error an agent may read."""
        self._picture_rules(entry, quality)
        await self._lens_open(entry)

    def _picture_rules(self, entry: Entry, quality: str) -> None:
        """The checks that need no answer from the camera."""
        if quality not in QUALITIES:
            raise BadRequest(f"quality must be one of {', '.join(QUALITIES)}")
        if entry.kind not in policy.SNAPSHOT_KINDS:
            raise BadRequest(f"{entry.slug} is a {entry.kind}, not a camera")
        if not policy.can_snapshot(entry.kind, entry.device.alias, self.cfg.snapshots):
            raise NotEnabled(f"pictures cannot be taken with {entry.slug}: the owner's policy blocks it")
        if entry.root is not None and entry.root.kind != "hub":
            raise BadRequest(f"{entry.slug} is reached through {entry.root.slug}, which cannot send its video")
        if (waited := time.monotonic() - self._rejected.get(entry.slug, float("-inf"))) < LOGIN_COOLDOWN:
            raise DeviceUnavailable(
                f"{entry.slug} refused the login for video a moment ago; not trying again for {LOGIN_COOLDOWN - waited:.0f} seconds"
            )

    async def _lens_open(self, entry: Entry) -> None:
        """The privacy switch, read fresh. If the camera cannot be asked, assume nothing about the lens."""
        await self.refresh(entry, force=True)
        if not entry.online:
            raise DeviceUnavailable(f"{entry.slug} is not reachable right now")
        state = entry.device.features.get("state")
        if state is not None and self.feature_value(state) is False:
            raise Forbidden(f"{entry.slug} has its privacy mode on, so its lens is covered")

    def _stream(self, entry: Entry, quality: str) -> tuple[str | None, AsyncIterator[bytes] | None, str]:
        """Where the video comes from: an RTSP address with a camera account, or the app's channel without one,
        which needs only the Tapo account tolpo already has. The third value is the secret to scrub from output."""
        if entry.root is not None:
            # A camera or doorbell behind a hub has no address of its own: the hub sends its video on the app's
            # channel when asked by the device's id. RTSP cannot reach it.
            return None, live_chunks(entry.root.device.host, self.cfg.password, quality, child=entry.device.device_id), self.cfg.password
        if self.cfg.camera_username and self.cfg.camera_password:
            url = rtsp_url(entry.device.host, self.cfg.camera_username, self.cfg.camera_password, quality)
            return url, None, self.cfg.camera_password
        return None, live_chunks(entry.device.host, self.cfg.password, quality), self.cfg.password

    def note_login_rejected(self, entry: Entry) -> None:
        """A camera refused our login for video: leave it alone for a while, repeated failures can lock it out."""
        self._rejected[entry.slug] = time.monotonic()

    def picture_failure(self, entry: Entry, exc: GrabError) -> RegistryError:
        log.warning("no picture from %s: %s", entry.slug, exc.reason)
        if exc.reason == "auth":
            self._rejected[entry.slug] = time.monotonic()
        failure = (Unsupported if exc.reason == "missing" else DeviceUnavailable)(f"no picture from {entry.slug}: {exc}")
        failure.detail = exc.detail
        return failure

    async def snapshot(self, entry: Entry, quality: str = "low") -> Picture:
        """One still picture from a camera the owner has enabled. Every error message is fit for an agent to read."""
        await self.check_picture(entry, quality)
        url, feed, secret = self._stream(entry, quality)
        async with entry.picture_lock, self.grabs:
            started = time.monotonic()
            try:
                picture = await grab(url, feed=feed, ffmpeg=self.cfg.ffmpeg or "ffmpeg", secrets=(secret,))
            except GrabError as exc:
                raise self.picture_failure(entry, exc) from None
        log.info(
            "picture from %s (%s, %s): %dx%d, %d KB, %.1f s",
            entry.slug, quality, "app channel" if feed is not None else "rtsp",
            picture.width, picture.height, len(picture.data) // 1024, time.monotonic() - started,
        )
        return picture

    async def live_view(self, entry: Entry, quality: str = "low") -> tuple[str | None, AsyncIterator[bytes] | None, str]:
        """The same checks as a picture, then the stream itself for a player on the owner's own screen:
        an RTSP address, or the app's channel as MPEG-TS chunks. Returns (url, feed, secret to scrub)."""
        await self.check_picture(entry, quality)
        log.info("live view of %s (%s)", entry.slug, quality)
        return self._stream(entry, quality)

    async def live_pictures(self, entry: Entry, quality: str = "low") -> AsyncIterator[bytes]:
        """Live video from a camera the owner has enabled, as JPEG pictures one after the other, for a viewer on the
        owner's own screen. The same checks as a picture. One stream per camera, LIVE_STREAMS cameras at once, and a
        time limit that is shorter for a battery camera, because live video keeps it awake and empties its battery.
        The privacy switch is read while the video starts, which saves a second, and no picture is handed out before
        the switch says the lens is open."""
        self._picture_rules(entry, quality)
        if self.lives.locked():
            raise Busy(f"{LIVE_STREAMS} cameras are already sending live video; close one first")
        try:
            await asyncio.wait_for(entry.picture_lock.acquire(), 3)
        except TimeoutError:
            raise Busy(f"{entry.slug} is already sending video") from None
        await self.lives.acquire()
        battery = "battery_level" in entry.device.features
        seconds = LIVE_BATTERY_SECONDS if battery else LIVE_SECONDS
        url, feed, _ = self._stream(entry, quality)
        frames = live_jpegs(url, feed=feed, quality=quality, seconds=seconds, ffmpeg=self.cfg.ffmpeg or "ffmpeg")

        async def first_picture() -> bytes:
            return await frames.__anext__()

        started = time.monotonic()
        starting = asyncio.create_task(first_picture())
        try:
            await self._lens_open(entry)
            first = await starting
        except BaseException as exc:
            log.info("live video from %s did not start after %.1f s: %s", entry.slug, time.monotonic() - started, type(exc).__name__)
            starting.cancel()
            try:
                # Close the camera's video, which may have started before the answer came. Bounded, and the camera
                # is given back whatever happens here.
                await asyncio.wait({starting}, timeout=STOP_GRACE)
                if starting.done():
                    await frames.aclose()
            finally:
                self.lives.release()
                entry.picture_lock.release()
            if isinstance(exc, GrabError):
                raise self.picture_failure(entry, exc) from None
            if isinstance(exc, StopAsyncIteration):
                raise DeviceUnavailable(f"no video from {entry.slug}") from None
            raise
        log.info("live video from %s (%s), at most %d s", entry.slug, quality, seconds)

        def release() -> None:
            self.lives.release()
            entry.picture_lock.release()
            log.info("live video from %s closed", entry.slug)

        return LiveStream(first, frames, release, entry.slug)

    def health(self) -> dict[str, Any]:
        tops = [e for e in self._entries.values() if e.root is None]
        missing = self.not_connected()
        return {
            "devices": len(self._entries),
            "hosts": len(tops),
            "complete": not missing,
            "not_connected": missing,
            "offline": sorted(e.slug for e in self._entries.values() if not e.online),
            "problems": self.problems,
            "last_discovery": self.last_discovery,
        }
