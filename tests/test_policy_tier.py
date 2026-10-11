"""The level (`tier`) also governs what the original tools may change: power, brightness and color are level 1."""

import pytest
from mcp import Client

from tolpo import policy
from tolpo.config import Config
from tolpo.mcp_server import build_server
from tolpo.registry import Forbidden, Registry

from . import fakes

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def house(**config):
    reg = Registry(Config(email="a", password="b", cache_ttl=0.0, cache_file="", **config))
    return reg, fakes.populate(reg)


def test_the_features_writable_out_of_the_box_are_level_1():
    for kind, feature in (("plug", "state"), ("light", "brightness"), ("light", "hsv"), ("light", "color_temperature")):
        assert policy.can_write(kind, feature, tier=1) and policy.can_write(kind, feature)  # level 1 is the default
        assert not policy.can_write(kind, feature, tier=0)
    assert policy.can_write("plug", "state", tier=4)


def test_a_feature_the_owner_names_is_allowed_at_any_level_and_a_hidden_one_never():
    assert policy.can_write("camera", "state", frozenset({"camera.state"}), tier=0)
    assert policy.can_write("camera", "pan_left", frozenset({"pan_left"}), tier=0)
    assert not policy.can_write("hub", "pair", frozenset({"pair"}), tier=4)
    assert not policy.can_write("hub", "ssid", frozenset({"ssid"}), tier=4)
    assert not policy.can_write("camera", "state", tier=4)  # a camera's switch is privacy, not power


async def test_at_level_0_power_and_light_changes_are_refused_and_not_offered():
    reg, devices = house(tier=0)
    lamp, bulb = reg.resolve_one("desk-lamp"), reg.resolve_one("office-1")
    for call in (reg.set_power(lamp, True), reg.toggle(lamp), reg.set_light(bulb, brightness=30), reg.set_feature(lamp, "state", True)):
        with pytest.raises(Forbidden):
            await call
    assert devices["lamp"].is_on is False
    assert "power" not in reg.capabilities(lamp) and "brightness" not in reg.capabilities(bulb)


async def test_at_level_1_they_work_as_before():
    reg, devices = house(tier=1)
    await reg.set_power(reg.resolve_one("desk-lamp"), True)
    assert devices["lamp"].is_on is True
    assert "power" in reg.capabilities(reg.resolve_one("desk-lamp"))


async def test_the_mcp_tools_follow_the_level():
    reg, devices = house(tier=0)
    async with Client(build_server(reg, discover=False)) as client:
        refused = await client.call_tool("set_power", {"targets": "desk-lamp", "on": True})
        listed = await client.call_tool("get_device", {"device": "desk-lamp"})
    assert refused.is_error and devices["lamp"].is_on is False
    assert "tier" not in refused.content[0].text and "allow_write" not in refused.content[0].text
    assert "power" not in str(listed.structured_content.get("can"))
