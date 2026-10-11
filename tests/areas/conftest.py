import pytest
from mcp import Client

from tolpo import policy
from tolpo.config import Config
from tolpo.mcp_server import build_server
from tolpo.registry import Registry

from .. import fakes
from .fakes import FakeCaller


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def make_house():
    """house(tier=1, allow_methods=(), responses=None, extra=(), **config) -> (registry, caller, devices)

    The fake house of tests/fakes.py with a FakeCaller. Areas that declare methods are loaded by the caller itself."""

    def make(*, tier=1, allow_methods=(), responses=None, extra=(), **config):
        reg = Registry(
            Config(email="a", password="b", cache_ttl=0.0, cache_file="", tier=tier, allow_methods=frozenset(allow_methods), **config)
        )
        devices = fakes.populate(reg, extra)
        return reg, FakeCaller(reg, responses), devices

    return make


@pytest.fixture
def mcp():
    """mcp(registry, caller) -> an MCP client for a server that has every area loaded. Use it with `async with`."""

    def client(reg, caller):
        return Client(build_server(reg, discover=False, caller=caller))

    return client


@pytest.fixture
def clean_catalog():
    """Tests that declare their own methods get a catalog that is put back afterwards."""
    saved = dict(policy._METHODS)
    yield
    policy._METHODS.clear()
    policy._METHODS.update(saved)
