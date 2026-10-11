"""What an agent may read and write. Deny by default; widen with `allow_write` in the config."""

from __future__ import annotations

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
