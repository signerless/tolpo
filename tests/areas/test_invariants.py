"""Rules that must hold for every tool of every area, now and for areas added later."""

import itertools

import pytest

from tolpo import policy
from tolpo.policy import Tier

pytestmark = pytest.mark.anyio

# Reading these starts a video session or writes a file, which the fake house cannot answer; they have their own tests.
HEAVY = ("snapshot", "clip", "thumbnail", "frames", "live", "watch")
DEVICE_PARAMS = {"device", "camera", "hub", "ref", "target", "lock", "vacuum", "sensor", "child"}
MAX_CALLS_PER_TOOL = 12


@pytest.fixture(autouse=True)
def private_folders(tmp_path, monkeypatch):
    """Nothing here may read or write the real home of whoever runs the tests."""
    monkeypatch.setenv("TOLPO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TOLPO_NETWORK_CONFIG", str(tmp_path / "network.toml"))
    monkeypatch.setenv("TOLPO_CLIP_DIR", str(tmp_path / "clips"))


def hint(tool, name):
    return getattr(tool.annotations, name, None) if tool.annotations else None


def argument_sets(tool, slugs):
    """Every device for a tool that takes one, and every value of an enumeration it requires. Nothing else is guessed."""
    props = (tool.input_schema or {}).get("properties", {})
    device_param = next((p for p in props if p in DEVICE_PARAMS), None)
    fixed, choices = {}, {}
    for name in (tool.input_schema or {}).get("required", []):
        if name == device_param:
            continue
        values = props.get(name, {}).get("enum")
        if values is None:
            for branch in props.get(name, {}).get("anyOf", []):
                values = values or branch.get("enum")
        schema = props.get(name, {})
        kind = schema.get("type") or next((b.get("type") for b in schema.get("anyOf", []) if b.get("type") != "null"), None)
        if values:
            choices[name] = list(values)
        elif "default" in schema:
            fixed[name] = schema["default"]
        elif kind == "boolean":
            fixed[name] = True
        elif kind in ("integer", "number"):
            fixed[name] = schema.get("minimum", 1)
        elif kind == "string":
            fixed[name] = "x"
        else:
            return []  # a list or an object is not guessed here
    combos = [dict(zip(choices, combo)) for combo in itertools.product(*choices.values())] if choices else [{}]
    devices = [{device_param: slug} for slug in slugs] if device_param else [{}]
    return [{**fixed, **c, **d} for d in devices for c in combos][:MAX_CALLS_PER_TOOL]


async def test_a_tool_marked_read_only_sends_only_read_methods(make_house, mcp):
    """Calls every read-only tool with whatever arguments can be guessed and checks the level of each method it sent.
    A tool that stops on an unexpected answer before it reaches a write is not caught here: the area tests cover that."""
    reg, caller, _ = make_house(tier=4)  # the highest level, so that only the tool itself decides what it sends
    slugs = [e.slug for e in reg.entries()]
    checked = 0
    async with mcp(reg, caller) as client:
        for tool in (await client.list_tools()).tools:
            if not hint(tool, "read_only_hint") or any(word in tool.name for word in HEAVY):
                continue
            for args in argument_sets(tool, slugs):
                before = len(caller.sent)
                await client.call_tool(tool.name, args)  # an error is fine, the question is only what was sent
                for slug, method, _ in caller.sent[before:]:
                    rule = policy.rule_for(reg.resolve_one(slug).kind, method)
                    assert rule is not None and rule.tier == Tier.READ, f"{tool.name} is read-only but sent {method} (level {rule and int(rule.tier)})"
                    checked += 1
    assert checked > 50  # the test must really have exercised the areas


async def test_every_tool_marked_destructive_asks_for_the_confirmation_phrase(mcp, make_house):
    reg, caller, _ = make_house(tier=4)
    async with mcp(reg, caller) as client:
        tools = (await client.list_tools()).tools
    risky = [t for t in tools if hint(t, "destructive_hint")]
    assert len(risky) > 20
    # set_feature is the original generic setter. It is governed by `allow_write`, not by method levels.
    missing = [t.name for t in risky if t.name != "set_feature" and "confirm" not in (t.input_schema or {}).get("properties", {})]
    assert not missing, f"level 3 and 4 tools without a confirm argument: {missing}"


async def test_no_tool_is_both_read_only_and_destructive(mcp, make_house):
    reg, caller, _ = make_house(tier=4)
    async with mcp(reg, caller) as client:
        tools = (await client.list_tools()).tools
    both = [t.name for t in tools if hint(t, "read_only_hint") and hint(t, "destructive_hint")]
    assert not both
