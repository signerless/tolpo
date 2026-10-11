"""Opt-in router visibility and reviewed firewall changes. No startup network I/O."""

from typing import Annotated, Any

from pydantic import Field

from tolpo.network.catalog import dependency
from tolpo.network.models import text
from tolpo.network.service import service_for
from tolpo.policy import Tier

METHODS = []  # Device reads are declared by areas.common. Firewall writes have their own level-4 gate.


def tools(ctx):
    def service():
        return service_for(ctx.registry, ctx.caller)

    async def ready(device):
        await ctx.ready_for([device])

    @ctx.tool(title="Network providers")
    async def network_providers() -> dict[str, Any]:
        """List configured providers, authenticated reachability, capabilities and blind spots."""
        with ctx.translated():
            return await service().status()

    @ctx.tool(title="Device traffic")
    async def network_device_traffic(
        device: Annotated[str, Field(description="Managed device id or name")],
        window: Annotated[str, Field(description="DNS sample window, such as 24h or 7d. Flows are live.")] = "24h",
    ) -> dict[str, Any]:
        """Group observed DNS queries and live connections by destination. Metadata only."""
        await ready(device)
        with ctx.translated():
            result, _ = await service().traffic(device, window)
            return result

    @ctx.tool(title="New destinations")
    async def network_new_destinations(
        device: Annotated[str, Field(description="Managed device id or name")],
        window: Annotated[str, Field(description="Look back from now, such as 7d")] = "7d",
    ) -> dict[str, Any]:
        """Compare observations to an owner-chosen private history file. First run creates a baseline."""
        await ready(device)
        with ctx.translated():
            return await service().new_destinations(device, window)

    @ctx.tool(title="UPnP audit")
    async def network_upnp_audit(
        device: Annotated[str, Field(description="Managed device id or name")],
    ) -> dict[str, Any]:
        """Read router port mappings for one managed device. State which providers cannot inspect UPnP."""
        await ready(device)
        with ctx.translated():
            return await service().upnp(device)

    @ctx.tool(title="Cloud dependency")
    async def network_cloud_dependency(
        device: Annotated[str, Field(description="Managed device id or name")],
    ) -> dict[str, Any]:
        """Explain app-derived cloud dependencies and expected local behavior. No cloud request."""
        await ready(device)
        with ctx.translated():
            entry = ctx.registry.resolve_one(device)
            return {"device": text(entry.device.alias), **dependency(entry.kind)}

    @ctx.tool(title="Plan internet block")
    async def network_block_internet(
        device: Annotated[str, Field(description="Managed device id or name")],
        reason: Annotated[str, Field(description="Reason for the firewall change, required")],
    ) -> dict[str, Any]:
        """Create a level-4 internet-block plan. No firewall change. Local networks remain reachable."""
        await ready(device)
        with ctx.translated():
            return await service().plan("block_internet", device, [], reason)

    @ctx.tool(title="Plan destination allow-list")
    async def network_allow_only(
        device: Annotated[str, Field(description="Managed device id or name")],
        hosts: Annotated[list[str], Field(description="Up to 32 literal IPv4 or IPv6 destinations. FQDNs are unavailable.")],
        reason: Annotated[str, Field(description="Reason for the firewall change, required")],
    ) -> dict[str, Any]:
        """Plan local access plus these literal destination IPs. Does not apply the change."""
        await ready(device)
        with ctx.translated():
            return await service().plan("allow_only", device, hosts, reason)

    @ctx.tool(title="Apply firewall plan", tier=Tier.LIFECYCLE)
    async def network_apply(
        plan_id: Annotated[str, Field(description="Id returned by a fresh firewall plan")],
        confirm: Annotated[str, Field(description="Exact confirmation phrase from the reviewed plan")],
        revert_after_minutes: Annotated[int, Field(description="Must match the reviewed plan, 1 to 60 minutes")] = 30,
    ) -> dict[str, Any]:
        """Apply a level-4 plan with native expiry, local checks and a module-owned revert timer."""
        with ctx.translated():
            return await service().apply(plan_id, confirm, revert_after_minutes)

    @ctx.tool(title="Revert firewall change", tier=Tier.LIFECYCLE)
    async def network_rollback(
        change_id: Annotated[str, Field(description="Id returned by firewall apply")],
        confirm: Annotated[str, Field(description="network_rollback followed by the change id")],
    ) -> dict[str, Any]:
        """Revert one active change. Level 4 and exact confirmation are required."""
        with ctx.translated():
            return await service().rollback(change_id, confirm)

    @ctx.tool(title="Reconcile and revert owned firewall objects", tier=Tier.LIFECYCLE)
    async def network_rollback_all(
        confirm: Annotated[str, Field(description="Exact phrase: network_rollback_all")],
    ) -> dict[str, Any]:
        """Break-glass cleanup of tagged tolpo tables, including changes from a previous process."""
        with ctx.translated():
            return await service().rollback_all(confirm)
