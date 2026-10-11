"""What an agent may read and write. Deny by default; widen with `allow_write` and `snapshots` in the config."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable

# Never returned by the API: Matter and HomeKit setup codes would let anyone add the device
# to another home, and the SSID leaks the home network name.
HIDDEN = frozenset({"ssid", "matter_setup_code", "matter_setup_payload", "homekit_setup_code", "homekit_setup_payload"})

# Destructive on the hub: removing a sensor means re-pairing it by hand. Not overridable.
NEVER_WRITABLE = frozenset({"pair", "unpair"})

POWER_KINDS = frozenset({"light", "plug", "switch"})

# Writable out of the box. Everything else (reboot, alarms, camera privacy, pan/tilt,
# detection toggles...) needs an explicit entry in `allow_write`.
DEFAULT_WRITABLE = frozenset({"state", "brightness", "color_temperature", "hsv", "light_preset"})


def can_write(kind: str, feature_id: str, extra: frozenset[str] = frozenset()) -> bool:
    """`extra` holds plain ids ("pan_left") or kind-qualified ones ("camera.state")."""
    if feature_id in HIDDEN or feature_id in NEVER_WRITABLE:
        return False
    if feature_id in extra or f"{kind}.{feature_id}" in extra:
        return True
    if feature_id == "state":
        # On cameras and doorbells `state` is the lens/privacy switch, not a power relay.
        return kind in POWER_KINDS
    return feature_id in DEFAULT_WRITABLE


# A picture leaves the house: it goes to whatever model the agent runs on. So it is opt-in, camera by camera.
SNAPSHOT_KINDS = frozenset({"camera", "doorbell"})


def _plain(name: str) -> str:
    return " ".join(name.split()).casefold()


def can_snapshot(kind: str, name: str | None, enabled: Iterable[str]) -> bool:
    """`enabled` holds camera names as the owner typed them. Case and spacing do not matter; nothing else is guessed."""
    wanted = _plain(name or "")
    return kind in SNAPSHOT_KINDS and bool(wanted) and wanted in {_plain(n) for n in enabled}


# ---- levels of risk for raw device methods ----------------------------------------------------------------------
#
# Every device method an area module sends is declared with a level. The owner chooses the highest level that is
# allowed (`policy.tier`, 1 by default) and can allow single methods above it (`policy.allow_methods`). Methods that
# are not declared are refused. Levels 3 and 4 also need a confirmation phrase on every call.


class Tier(IntEnum):
    READ = 0  # look at something
    CONTROL = 1  # reversible control: power, brightness, color, modes
    SETTING = 2  # changes behavior: schedules, detection tuning, defaults, settings
    SECURITY = 3  # security or safety relevant: privacy off, alarm or arm state, lock, protections, audio out
    LIFECYCLE = 4  # pair, unpair, factory reset, firmware install, Wi-Fi change, account actions


@dataclass(frozen=True)
class MethodRule:
    method: str  # the device method, for example "add_countdown_rule"
    tier: Tier
    kinds: frozenset[str]  # the kinds of device it may be sent to: light, plug, switch, sensor, hub, camera, doorbell, ...
    summary: str  # one plain sentence about what it does; shown to people and used in confirmations

    @property
    def needs_confirmation(self) -> bool:
        return self.tier >= Tier.SECURITY


_METHODS: dict[tuple[str, str], MethodRule] = {}


def method(name: str, tier: Tier, kinds: Iterable[str], summary: str) -> MethodRule:
    return MethodRule(name, Tier(tier), frozenset(kinds), summary)


def register_methods(rules: Iterable[MethodRule]) -> None:
    """Add declared methods to the catalog. Declaring the same method twice with another level is a bug, not a merge."""
    for rule in rules:
        for kind in rule.kinds:
            known = _METHODS.get((kind, rule.method))
            if known is not None and known.tier != rule.tier:
                raise ValueError(f"{rule.method} on {kind} is declared at level {int(known.tier)} and again at {int(rule.tier)}")
            _METHODS[(kind, rule.method)] = rule


def rule_for(kind: str, name: str) -> MethodRule | None:
    return _METHODS.get((kind, name))


def declared_methods() -> list[MethodRule]:
    return sorted(set(_METHODS.values()), key=lambda r: (r.tier, r.method))


def permitted(rule: MethodRule, tier: int, allow_methods: frozenset[str]) -> bool:
    return rule.tier <= tier or rule.method in allow_methods


def confirmation(rule: MethodRule, slug: str) -> str:
    """What must be passed as `confirm` for a level 3 or 4 method. It names the action and the device."""
    return f"{rule.method} {slug}"
