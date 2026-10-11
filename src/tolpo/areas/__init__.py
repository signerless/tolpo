"""Area modules: one file per part of what the Tapo app does (lights, plugs, cameras, ...).

Adding an area means adding a file here. Nothing else is edited, so many people can work at once. A module may define:

    METHODS = [policy.method(name, tier, kinds, summary), ...]   # every device method it sends, with its level
    def tools(ctx): ...                                           # register MCP tools with @ctx.tool
    def routes(ctx): ...                                          # register HTTP routes with @ctx.route

The module that defines MCP tools must not use `from __future__ import annotations`, because the SDK reads the
signatures at runtime. See docs/areas/README.md for the rules and a template.
"""

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
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


def load(ctx: AreaContext, *, strict: bool = False) -> None:
    """Register the tools and routes of the enabled areas (all of them unless `areas` is set in the config).
    `strict` re-raises, which the tests use. The result is kept in `ctx.catalog`."""
    declare_methods()
    wanted = set(ctx.cfg.areas)
    for module in modules():
        name = area_name(module)
        enabled = not wanted or name in wanted
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
        ctx.catalog[name] = {"enabled": enabled, "summary": summary, "tools": list(ctx._names.get(name, []))}
    ctx.current = ""
