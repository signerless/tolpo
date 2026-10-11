"""Device registry: discovery, state cache, lookup by name, and the few control operations."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

import kasa
from kasa import Device, DeviceType, Feature, Module
from kasa.exceptions import AuthenticationError, KasaException

from . import compat, policy
from .config import Config, local_subnets

log = logging.getLogger(__name__)

UPDATE_TIMEOUT = 20.0
SWEEP_MAX_HOSTS = 1024  # do not unicast-sweep anything larger than a /22
BOOT_GRACE = 15.0  # at startup, stop waiting for slow devices after this long and let them finish in the background
RETRY_DELAYS = (5.0, 15.0, 45.0)  # seconds between retries of hosts we know but could not reach
CACHE_MAX_AGE = 30 * 24 * 3600  # forget a remembered host only after this long without seeing it


class RegistryError(Exception):
    status_code = 400

    def __init__(self, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


class NotFound(RegistryError):
    status_code = 404


class Ambiguous(RegistryError):
    status_code = 409


class Forbidden(RegistryError):
    status_code = 403


class BadRequest(RegistryError):
    status_code = 400


class DeviceUnavailable(RegistryError):
    status_code = 502


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
    last_update: float = 0.0
    online: bool = True
    error: str | None = None

    @property
    def top(self) -> Entry:
        return self.root or self

    @property
    def kind(self) -> str:
        return _KIND.get(self.device.device_type, self.device.device_type.name.lower())


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
        self.retry_delays = RETRY_DELAYS
        self.boot_grace = BOOT_GRACE
        self._connecting: set[str] = set()  # hosts with a connection attempt in flight
        self._stragglers: set[asyncio.Task] = set()  # attempts that outlived the startup grace period
        self._task: asyncio.Task | None = None
        self.ready = asyncio.Event()  # set once the first discovery has finished, successfully or not
        self._discovery_lock = asyncio.Lock()

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

    def _save_cache(self) -> None:
        if not self._cache:
            return
        cutoff = time.time() - CACHE_MAX_AGE
        self._known = {h: i for h, i in self._known.items() if i.get("seen", 0) >= cutoff}
        try:
            self._cache.parent.mkdir(parents=True, exist_ok=True)
            self._cache.write_text(json.dumps({"version": 2, "hosts": self._known}))
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
        return entry

    @staticmethod
    async def _quiet_disconnect(device: Device) -> None:
        with contextlib.suppress(Exception):
            await device.disconnect()

    def _unique_slug(self, device: Device) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", (device.alias or device.model or "device").lower()).strip("-") or "device"
        taken = {e.slug for e in self._entries.values()}
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base}-{n}", n + 1
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
        for e in items:
            if e.slug == low:
                return [e]
        by_name = [e for e in items if (e.device.alias or "").lower() == low]
        if by_name:
            return by_name
        if any(c in ref for c in "*?["):
            return [
                e for e in items if fnmatch.fnmatchcase(e.slug, low) or fnmatch.fnmatchcase((e.device.alias or "").lower(), low)
            ]
        return []

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
        for child in top.device.children or []:
            self._upsert(child, top)
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
            if fid in feats and policy.can_write(kind, fid, allow):
                caps.append(cap)
        if "current_consumption" in feats:
            caps.append("energy")
        if kind == "sensor":
            caps.append("sensor")
        return caps

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
        if not policy.can_write(entry.kind, feature_id, self.cfg.allow_write):
            raise Forbidden(f"{feature_id!r} cannot be changed on {entry.kind} devices: the owner's policy blocks it")
        return feat

    async def _run(self, entry: Entry, op: Callable[[], Awaitable[Any]]) -> None:
        top = entry.top
        async with top.lock:
            try:
                await asyncio.wait_for(op(), UPDATE_TIMEOUT)
            except AuthenticationError as exc:
                raise DeviceUnavailable(f"{entry.slug} rejected the account credentials") from exc
            except TimeoutError as exc:
                raise DeviceUnavailable(f"{entry.slug} did not answer in time") from exc
            except KasaException as exc:
                raise DeviceUnavailable(f"{entry.slug} reported an error: {exc}") from exc
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
