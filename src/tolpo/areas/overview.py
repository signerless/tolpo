"""Which areas tolpo has, and which of their tools are on."""

from typing import Any

from ..policy import Tier


def tools(ctx):
    @ctx.tool(title="Areas")
    async def tolpo_areas() -> dict[str, Any]:
        """List the parts of tolpo (areas such as lights, plugs, cameras), whether each is on, and the tools each one offers.
        Use it to find out what you can do before guessing a tool name. An area that is off was not enabled by the owner."""
        return {
            "areas": [
                {"area": name, "on": info["enabled"], "about": info["summary"], "tools": info["tools"]}
                for name, info in sorted(ctx.catalog.items())
                if name != "overview"
            ],
            "level": ctx.cfg.tier,
        }
