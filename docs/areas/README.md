# Area modules

An area is one file in `src/tolpo/areas/` that adds a part of what the Tapo app can do: lights, plugs, cameras,
pairing and so on. Adding an area means adding files. Nothing shared is edited, so many people can work at once.

For each area you add:

- `src/tolpo/areas/<area>.py`, the code.
- `tests/areas/test_<area>.py`, the tests.
- `docs/areas/<area>.md`, what it does for a person, with one line per tool in the form ``- `tool_name`: what it does``.
  A test checks that every tool is documented there or in `llms.txt`.

## What a module may define

```python
# src/tolpo/areas/example.py
# Do NOT add `from __future__ import annotations` here: the MCP SDK reads the tool signatures at runtime.
from typing import Annotated, Any

from pydantic import Field

from tolpo import policy
from tolpo.policy import Tier

METHODS = [  # every device method this area sends, with how risky it is
    policy.method("get_example", Tier.READ, ["plug", "light"], "Read the example setting."),
    policy.method("set_example", Tier.SETTING, ["plug", "light"], "Change the example setting."),
]


def tools(ctx):  # MCP tools; the docstring is what the agent reads
    @ctx.tool(title="Example setting", tier=Tier.READ)
    async def example_setting(device: Annotated[str, Field(description="Device id or name")]) -> dict[str, Any]:
        """Read the example setting of one device."""
        await ctx.ready_for([device])
        with ctx.translated():
            entry = ctx.registry.resolve_one(device)
            return await ctx.caller.call(entry, "get_example")


def routes(ctx):  # HTTP routes, behind the same token as the rest
    @ctx.route("/devices/{ref}/example")
    async def example(ref: str):
        return await ctx.caller.call(ctx.registry.resolve_one(ref), "get_example")
```

Share the logic between the tool and the route by putting it in plain functions that take the registry and the caller.

## Levels

Every device method has a level. The owner picks the highest allowed level (`policy.tier`, 1 by default) and can allow
single methods above it (`policy.allow_methods`).

| Level | Name | Examples |
|---|---|---|
| 0 | READ | look at anything that is not secret |
| 1 | CONTROL | reversible control: power, brightness, color, modes |
| 2 | SETTING | changes behavior: schedules, detection tuning, defaults |
| 3 | SECURITY | security or safety: privacy off, alarm or arm state, lock, protections, audio out |
| 4 | LIFECYCLE | pair, unpair, factory reset, firmware install, Wi-Fi change, account actions |

Levels 3 and 4 need a confirmation phrase on every call: `confirm="<method> <device id>"` (a few name a change or a plan
instead). The caller refuses without it and says which phrase is needed, so the phrase is a pause for the agent to ask
the person, not proof that the person agreed. A tool at level 3 or 4 takes a `confirm` argument and passes it on.

## Rules

- Send device methods only through `ctx.caller`. It refuses what is not declared, above the owner's level, or
  unconfirmed. Never call `device.protocol` yourself.
- A refusal says what the action is, never how to widen the permission.
- Never return Wi-Fi names, setup codes, passwords or tokens. Keep answers compact: leave out what is empty.
- Names and readings come from other people or from hardware. Flatten and shorten them with `views.clean` before an
  agent sees them.
- Tests use `tests/areas/fakes.py` (`FakeCaller`, which runs the real policy) and the `make_house` and `mcp` fixtures from
  `tests/areas/conftest.py`. They never touch a real device or the network.
- For every safety rule add a test, then break the rule on purpose and check that the test fails.
- Do not edit `README.md`, `llms.txt`, `AGENTS.md`, `policy.py`, `registry.py`, `mcp_server.py` or `api.py` from an area.
  If you need a change there, say so in your final report.
