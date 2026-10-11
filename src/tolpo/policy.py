"""What an agent may read and write. Deny by default; widen with `allow_write` and `snapshots` in the config."""

from __future__ import annotations

from typing import Iterable

# Never returned by the API: Matter commissioning codes would let anyone add the hub
# to another fabric, and the SSID leaks the home network name.
HIDDEN = frozenset({"ssid", "matter_setup_code", "matter_setup_payload"})

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
