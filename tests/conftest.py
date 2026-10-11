import pytest
from fastapi.testclient import TestClient

from tolpo.api import create_app
from tolpo.config import Config
from tolpo.registry import Registry

from . import fakes


@pytest.fixture
def build():
    """Return a factory so a test can choose the policy and token it runs with."""
    clients = []

    def make(*, allow_write=frozenset(), token="", extra=(), **settings):
        cfg = Config(
            email="a@example.com", password="pw", token=token, allow_write=frozenset(allow_write),
            cache_ttl=0.0, cache_file="", **settings,
        )
        reg = Registry(cfg)
        devices = fakes.populate(reg, extra)
        client = TestClient(create_app(cfg, reg, discover=False, allowed_hosts=["testserver"]))
        client.__enter__()
        clients.append(client)
        return client, devices

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(build):
    return build()[0]


@pytest.fixture
def ffmpeg(tmp_path, monkeypatch):
    """A stand-in for ffmpeg: no camera, no video library. See fakes.FakeFfmpeg."""
    return fakes.FakeFfmpeg(tmp_path, monkeypatch)
