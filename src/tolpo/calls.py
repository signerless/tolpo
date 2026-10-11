"""The one way area modules send a method to a device.

Every method is declared first (policy.method), with a level of risk. The caller refuses what is not declared, what
is above the owner's level, and what needs a confirmation phrase that was not given. Only then does it touch the wire.
Tests use a subclass that replaces `_send`, so the policy runs for real while no device is involved.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from . import policy
from .registry import Entry, Forbidden, Registry, RegistryError


class NeedsConfirmation(RegistryError):
    """A level 3 or 4 action was asked for without the confirmation phrase that names it and the device."""

    status_code = 428


class DeviceCaller(Protocol):
    def plan(self, entry: Entry, method: str) -> dict[str, Any]: ...

    async def call(
        self, entry: Entry, method: str, params: Mapping[str, Any] | None = None, *, confirm: str | None = None
    ) -> Any: ...

    async def call_many(
        self, entry: Entry, calls: Mapping[str, Mapping[str, Any] | None], *, confirm: str | None = None
    ) -> dict[str, Any]: ...


class Caller:
    def __init__(self, registry: Registry):
        from . import areas  # the area modules declare their methods; load them once

        areas.declare_methods()
        self.registry = registry

    # ---- policy ----------------------------------------------------------------------------

    def rule(self, entry: Entry, method: str) -> policy.MethodRule:
        found = policy.rule_for(entry.kind, method)
        if found is None:
            raise Forbidden(f"{method!r} is not a method tolpo sends to a {entry.kind}")
        return found

    def check(self, entry: Entry, method: str, confirm: str | None = None) -> policy.MethodRule:
        rule = self.rule(entry, method)
        cfg = self.registry.cfg
        if not policy.permitted(rule, cfg.tier, cfg.allow_methods):
            # Say what it is, never how to widen the permission.
            raise Forbidden(f"{method!r} is a level {int(rule.tier)} action ({rule.summary}) and the owner's policy blocks it")
        phrase = policy.confirmation(rule, entry.slug)
        if rule.needs_confirmation and confirm != phrase:
            raise NeedsConfirmation(
                f"{method!r} is a level {int(rule.tier)} action: {rule.summary} To go ahead, call again with confirm={phrase!r}"
            )
        return rule

    def plan(self, entry: Entry, method: str) -> dict[str, Any]:
        """What asking for this method would mean, without doing it."""
        rule = self.rule(entry, method)
        cfg = self.registry.cfg
        allowed = policy.permitted(rule, cfg.tier, cfg.allow_methods)
        out: dict[str, Any] = {"method": method, "level": int(rule.tier), "what": rule.summary, "allowed": allowed}
        if allowed and rule.needs_confirmation:
            out["confirm"] = policy.confirmation(rule, entry.slug)
        return out

    # ---- the wire --------------------------------------------------------------------------

    async def call(
        self, entry: Entry, method: str, params: Mapping[str, Any] | None = None, *, confirm: str | None = None
    ) -> Any:
        self.check(entry, method, confirm)
        result = await self.registry.guarded(entry, lambda: self._send(entry, {method: params}))
        return result.get(method, result) if isinstance(result, dict) else result

    async def call_many(
        self, entry: Entry, calls: Mapping[str, Mapping[str, Any] | None], *, confirm: str | None = None
    ) -> dict[str, Any]:
        """Several methods in one request. Every one is checked before any is sent."""
        for method in calls:
            self.check(entry, method, confirm)
        return await self.registry.guarded(entry, lambda: self._send(entry, dict(calls)))

    async def _send(self, entry: Entry, request: dict[str, Any]) -> dict[str, Any]:
        """The request is `{method: params}`, which python-kasa sends as is for SMART and SMARTCAM devices
        (a hub's children go through the hub). Tests replace this method."""
        return await entry.device.protocol.query({m: (dict(p) if p else None) for m, p in request.items()})
