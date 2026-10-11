"""Pairing and provisioning through python-kasa's own lifecycle support."""

from typing import Annotated, Any

from pydantic import Field

from .. import policy, provision
from ..policy import Tier

WIFI_KINDS = ["plug", "light", "switch", "hub", "camera", "doorbell"]
SMART_KINDS = ["plug", "light", "switch", "hub"]
NAME_KINDS = [*WIFI_KINDS, "sensor", "thermostat", "valve"]

# These names authorize public python-kasa operations. They are not raw RPCs.
# The shared set_device_info declaration remains level 2. This area's naming
# step belongs to onboarding and deliberately requires level 4 confirmation.
METHODS = [
    policy.method("wifi_scan", Tier.READ, WIFI_KINDS, "Read radio measurements without Wi-Fi names."),
    policy.method("reset_reboot", Tier.SETTING, SMART_KINDS, "Reboot a SMART device."),
    policy.method("pair_hub", Tier.LIFECYCLE, ["hub"], "Scan and pair all supported hub children in pairing mode."),
    policy.method("pair_remove", Tier.LIFECYCLE, ["hub"], "Remove a child from its hub locally."),
    policy.method("pair_name", Tier.LIFECYCLE, NAME_KINDS, "Name a device during onboarding."),
    policy.method("wifi_join", Tier.LIFECYCLE, WIFI_KINDS, "Send home Wi-Fi settings to a device."),
    policy.method("reset_factory", Tier.LIFECYCLE, SMART_KINDS, "Erase a SMART device's settings and account."),
    policy.method("pair_softap", Tier.LIFECYCLE, SMART_KINDS, "Move host Wi-Fi and provision an expected SMART device with the owner's account."),
]


def tools(ctx):
    adapter = provision.KasaLifecycle()

    async def entry_for(device):
        await ctx.ready_for([device])
        return ctx.registry.resolve_one(device)

    @ctx.tool(title="Pair hub children", tier=Tier.LIFECYCLE)
    async def pair_hub(
        device: Annotated[str, Field(description="Hub id or name")],
        timeout: Annotated[int, Field(ge=1, le=10, description="Seconds to wait for children in pairing mode")] = 10,
        confirm: Annotated[str | None, Field(description="Exact phrase: pair_hub <hub id>")] = None,
    ) -> dict[str, Any]:
        """Start a hub scan, detect and add all supported children in pairing mode. Returns successful additions. Press pairing buttons first. This pairs all found devices."""
        with ctx.translated():
            return await provision.pair_hub(ctx.registry, adapter, await entry_for(device), timeout, confirm)

    @ctx.tool(title="Remove hub child", tier=Tier.LIFECYCLE)
    async def pair_remove(
        device: Annotated[str, Field(description="Hub id or name")],
        child: Annotated[str, Field(description="Existing child id or name on this hub")],
        confirm: Annotated[str | None, Field(description="Exact phrase: pair_remove <hub id>")] = None,
    ) -> dict[str, Any]:
        """Unpair a child from this hub locally. Account-side cloud removal is a separate action in the owner's app."""
        with ctx.translated():
            return await provision.pair_remove(ctx.registry, adapter, await entry_for(device), child, confirm)

    @ctx.tool(title="Name paired device", tier=Tier.LIFECYCLE)
    async def pair_name(
        device: Annotated[str, Field(description="Paired device id or name")],
        name: Annotated[str, Field(min_length=1, max_length=64, description="New flat display name of at most 64 UTF-8 bytes")],
        confirm: Annotated[str | None, Field(description="Exact phrase: pair_name <device id>")] = None,
    ) -> dict[str, Any]:
        """Set the device name as an onboarding step. The registry id stays stable."""
        with ctx.translated():
            return await provision.pair_name(ctx.registry, adapter, await entry_for(device), name, confirm)

    @ctx.tool(title="Scan device Wi-Fi radio", tier=Tier.READ)
    async def wifi_scan(device: Annotated[str, Field(description="Device id or name with its own Wi-Fi radio")]) -> dict[str, Any]:
        """Scan on the device's own radio. Returns counts and signal measurements. Wi-Fi names and hardware addresses stay hidden."""
        with ctx.translated():
            return await provision.wifi_scan(ctx.registry, adapter, await entry_for(device))

    @ctx.tool(title="Join home Wi-Fi", tier=Tier.LIFECYCLE)
    async def wifi_join(
        device: Annotated[str, Field(description="Device id or name with its own Wi-Fi radio")],
        ssid_env: Annotated[str, Field(description="Owner-configured environment variable NAME holding the home Wi-Fi name")],
        password_env: Annotated[str, Field(description="Owner-configured environment variable NAME holding the Wi-Fi password")],
        account_env: Annotated[str, Field(description="Owner-configured environment variable NAME holding the Tapo account name; must match the device session")],
        account_password_env: Annotated[str, Field(description="Owner-configured environment variable NAME holding the Tapo account password; must match the device session")],
        keytype: Annotated[str, Field(description="Wi-Fi security: wpa2_psk, wpa_psk or none")] = "wpa2_psk",
        confirm: Annotated[str | None, Field(description="Exact phrase: wifi_join <device id>")] = None,
    ) -> dict[str, Any]:
        """Request a Wi-Fi change using environment values. SMART devices also send their authenticated owner's account. A request is not proof that the device joined. Cameras use python-kasa's scan and connect flow."""
        with ctx.translated():
            return await provision.wifi_join(ctx.registry, adapter, await entry_for(device), ssid_env, password_env, account_env, account_password_env, keytype, confirm)

    @ctx.tool(title="Factory reset SMART device", tier=Tier.LIFECYCLE)
    async def reset_factory(
        device: Annotated[str, Field(description="SMART device id or name; camera-protocol hubs are unsupported")],
        confirm: Annotated[str | None, Field(description="Exact phrase: reset_factory <device id>")] = None,
    ) -> dict[str, Any]:
        """Erase device settings and its local account. Hub reset drops pairings. Cloud records may remain. Firmware is not downgraded."""
        with ctx.translated():
            return await provision.reset(ctx.registry, adapter, await entry_for(device), factory=True, confirm=confirm)

    @ctx.tool(title="Reboot SMART device", tier=Tier.SETTING)
    async def reset_reboot(
        device: Annotated[str, Field(description="SMART device id or name; camera-protocol hubs are unsupported")],
        delay: Annotated[int, Field(ge=1, le=30, description="Seconds before reboot")] = 1,
    ) -> dict[str, Any]:
        """Reboot a SMART device. It may be offline during the state refresh."""
        with ctx.translated():
            return await provision.reset(ctx.registry, adapter, await entry_for(device), delay=delay)

    @ctx.tool(title="Plan SoftAP provisioning", tier=Tier.LIFECYCLE)
    async def pair_softap_plan(
        plan: Annotated[provision.SoftAPPlan, Field(description="Expected device id, local AP address, Wi-Fi interface and environment variable names. No password values.")],
        confirm: Annotated[str | None, Field(description="Exact phrase: pair_softap <plan target>")] = None,
    ) -> dict[str, Any]:
        """Show the authorized host steps for SMART SoftAP self-binding. No host or device is contacted. Execution needs a new confirmed call."""
        with ctx.translated():
            return provision.softap_plan(ctx.registry, plan, confirm)

    @ctx.tool(title="Provision SMART device on SoftAP", tier=Tier.LIFECYCLE)
    async def pair_softap_provision(
        plan: Annotated[provision.SoftAPPlan, Field(description="Expected device id, local AP address, Wi-Fi interface and environment variable names. No password values.")],
        confirm: Annotated[str | None, Field(description="Exact phrase: pair_softap <plan target>")] = None,
    ) -> dict[str, Any]:
        """Join an open device AP, authenticate with the owner's environment-supplied account, send Wi-Fi and account, restore host Wi-Fi and verify on the LAN. The OS must remember the previous network. Camera default logins and cloud QR binding are unsupported."""
        with ctx.translated():
            # Check before constructing any host adapter or reading env values.
            provision.softap_plan(ctx.registry, plan, confirm)
            return await provision.softap_provision(ctx.registry, adapter, plan, provision.host_wifi(plan.interface), confirm=confirm)
