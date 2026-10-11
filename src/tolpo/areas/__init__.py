"""Area modules: one file per part of what the Tapo app does (lights, plugs, cameras, ...).

Adding an area means adding a file here. Nothing else is edited, so many people can work at once. A module may define:

    METHODS = [policy.method(name, tier, kinds, summary), ...]   # every device method it sends, with its level
    def tools(ctx): ...                                           # register MCP tools with @ctx.tool
    def routes(ctx): ...                                          # register HTTP routes with @ctx.route

The module that defines MCP tools must not use `from __future__ import annotations`, because the SDK reads the
signatures at runtime. See docs/areas/README.md for the rules and a template.
"""

import importlib
import json
import logging
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

from mcp.types import ToolAnnotations

from .. import policy
from ..config import Config
from ..registry import Registry

log = logging.getLogger(__name__)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
RISKY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


def annotations_for(tier: policy.Tier) -> ToolAnnotations:
    """MCP clients use these hints to decide when to ask the person first."""
    if tier <= policy.Tier.READ:
        return READ
    return WRITE if tier <= policy.Tier.SETTING else RISKY


@dataclass
class AreaContext:
    registry: Registry
    caller: Any  # a calls.DeviceCaller
    server: Any = None  # the MCPServer, when tools are being registered
    api: Any = None  # the FastAPI app, when routes are being registered
    auth: list = field(default_factory=list)  # FastAPI dependencies that every route must carry
    ready_for: Callable[[list[str]], Awaitable[None]] | None = None  # waits until the named devices are known
    translated: Callable[[], Any] | None = None  # context manager: registry errors become tool errors
    catalog: dict = field(default_factory=dict)  # area name -> {"enabled", "summary", "tools"}, filled by load()
    current: str = ""  # the area being loaded
    _names: dict = field(default_factory=dict)

    @property
    def cfg(self) -> Config:
        return self.registry.cfg

    def tool(self, *, title: str, tier: policy.Tier = policy.Tier.READ, **options: Any):
        """Register an MCP tool. Its docstring is what the agent reads. Does nothing when no server is being built."""

        def decorate(fn):
            if self.server is not None:
                self.server.tool(title=title, annotations=annotations_for(tier), **options)(fn)
                self._names.setdefault(self.current, []).append(options.get("name") or fn.__name__)
            return fn

        return decorate

    def route(self, path: str, *, method: str = "get", **options: Any):
        """Register an HTTP route behind the same token check as the others. Does nothing when no app is being built."""

        def decorate(fn):
            if self.api is not None:
                getattr(self.api, method)(path, dependencies=self.auth, **options)(fn)
            return fn

        return decorate


def modules() -> list[ModuleType]:
    """Every area module. One that fails to import is logged and skipped, so one bad area cannot take the rest down."""
    found = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            found.append(importlib.import_module(f"{__name__}.{info.name}"))
        except Exception:
            log.exception("area %s could not be imported; the rest of tolpo keeps working", info.name)
    return found


def declare_methods() -> None:
    """Put every area's declared device methods in the policy catalog. Safe to call again."""
    for module in modules():
        policy.register_methods(getattr(module, "METHODS", ()))


def area_name(module: ModuleType) -> str:
    return module.__name__.rsplit(".", 1)[-1]


# An area tied to a kind of device is on by default only if the owner has such a device. Areas not listed here
# (automation, network, pairing, device admin and so on) are always on. The owner can override all of this with `areas`.
AREA_KINDS: dict[str, set[str]] = {
    "lights": {"light"},
    "plugs": {"plug", "switch"},
    "hub": {"hub"},
    "doorbell": {"hub", "doorbell", "camera"},
    "camera_detection": {"camera"},
    "camera_system": {"camera"},
    "camera_media": {"camera"},
    "vacuum": {"vacuum"},
    "lock": {"lock"},
}
ALWAYS_ON = {"overview"}
# What a hub brings with it: its sensors, valves, chimes and doorbells are not hosts of their own, so they are not remembered.
HUB_BRINGS = {"sensor", "valve", "thermostat", "chime", "doorbell"}


def kind_of_model(model: str) -> str | None:
    """A rough kind from a Tapo model name (L530 -> light). None when the name is not recognized."""
    m = model.upper().strip()
    for prefix, kind in (("RV", "vacuum"), ("DL", "lock"), ("KE", "valve"), ("TC", "camera"), ("C", "camera"), ("D", "doorbell"),
                         ("H", "hub"), ("L", "light"), ("P", "plug"), ("S", "switch"), ("T", "sensor")):
        if m.startswith(prefix):
            return kind
    return None


def kinds_present(cfg: Config) -> set[str] | None:
    """The kinds of devices known from earlier runs, or None when nothing is remembered (then every area is on)."""
    if not cfg.cache_file:
        return None
    try:
        hosts = json.loads(Path(cfg.cache_file).expanduser().read_text()).get("hosts", {})
    except (OSError, ValueError, AttributeError):
        return None
    models = [str(info.get("model") or "") for info in hosts.values() if isinstance(info, dict)]
    kinds = {kind_of_model(m) for m in models if m}
    if not models or None in kinds:  # nothing known, or a model we do not recognize: do not hide anything
        return None
    return kinds | (HUB_BRINGS if "hub" in kinds else set())


def enabled_areas(names: list[str], cfg: Config) -> dict[str, tuple[bool, str]]:
    """For each area: is it on, and why. `areas` in the config wins; otherwise the devices the owner has decide."""
    wanted = set(cfg.areas)
    present = None if wanted else kinds_present(cfg)
    out = {}
    for name in names:
        if name in ALWAYS_ON:
            out[name] = (True, "always on")
        elif wanted:
            out[name] = (name in wanted or "all" in wanted, "chosen in the config")
        elif present is None or name not in AREA_KINDS:
            out[name] = (True, "on by default")
        else:
            needed = AREA_KINDS[name]
            out[name] = (bool(needed & present), f"on because a {sorted(needed & present)[0]} is known" if needed & present else f"off: no {'/'.join(sorted(needed))} known")
    return out


def load(ctx: AreaContext, *, strict: bool = False) -> None:
    """Register the tools and routes of the enabled areas (all of them unless `areas` is set in the config).
    `strict` re-raises, which the tests use. The result is kept in `ctx.catalog`."""
    declare_methods()
    found = modules()
    decisions = enabled_areas([area_name(m) for m in found], ctx.cfg)
    for module in found:
        name = area_name(module)
        enabled, why = decisions[name]
        ctx.current = name
        try:
            if enabled and ctx.server is not None and hasattr(module, "tools"):
                module.tools(ctx)
            if enabled and ctx.api is not None and hasattr(module, "routes"):
                module.routes(ctx)
        except Exception:
            if strict:
                raise
            log.exception("area %s could not be loaded; the rest of tolpo keeps working", module.__name__)
        summary = (module.__doc__ or "").strip().splitlines()[0] if module.__doc__ else ""
        ctx.catalog[name] = {"enabled": enabled, "why": why, "summary": summary, "tools": list(ctx._names.get(name, []))}
    ctx.current = ""
