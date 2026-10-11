"""Start `tolpo mcp`, do the MCP handshake and check the tools. Used to smoke-test the container image.

It runs against a made-up network and fake credentials, so it never talks to real devices:

    docker run --rm -i --entrypoint python IMAGE - < scripts/smoke_mcp.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters

EXPECTED = {"home_overview", "list_devices", "get_device", "camera_snapshot", "set_power", "set_light", "set_feature", "rescan"}

CONFIG = """\
[tapo]
email = "ci@example.com"
password = "not-a-real-password"

[discovery]
subnets = ["192.0.2.0/24"]  # TEST-NET-1: documentation addresses, nothing lives there
sweep = false
cache_file = ""
"""


async def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        config = Path(folder) / "config.toml"
        config.write_text(CONFIG)
        params = StdioServerParameters(command="tolpo", args=["mcp"], env={**os.environ, "TOLPO_CONFIG": str(config)})
        async with Client(params) as client:
            name = client.server_info.name
            tools = {tool.name for tool in (await client.list_tools()).tools}
    missing = EXPECTED - tools
    if name != "tolpo" or missing:
        sys.exit(f"unexpected server {name!r}, missing tools {sorted(missing)}")
    print(f"{name} answered the handshake with {len(tools)} tools")


asyncio.run(main())
