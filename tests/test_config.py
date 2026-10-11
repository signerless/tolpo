import json

import pytest

from tolpo.cli import NO_CREDENTIALS, check
from tolpo.config import Config, is_loopback, load_config
from tolpo.registry import jsonable
import datetime
import enum


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("TAPO_EMAIL", "TAPO_PASSWORD", "TOLPO_TOKEN", "TOLPO_HOST", "TOLPO_PORT",
                 "TOLPO_CONFIG", "TOLPO_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)


def test_secrets_are_not_in_repr():
    cfg = Config(email="me@example.com", password="hunter2", token="tok3n")
    text = repr(cfg)
    assert "hunter2" not in text and "tok3n" not in text and "me@example.com" not in text


def test_toml_then_env_override(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[tapo]\nemail="a@x.com"\npassword="p"\n[server]\nport=9000\n[policy]\nallow_write=["pan_left"]\n'
                    '[discovery]\nsubnets=["10.0.0.0/24"]\n')
    path.chmod(0o600)
    cfg = load_config(str(path))
    assert (cfg.email, cfg.port, cfg.subnets, cfg.allow_write) == ("a@x.com", 9000, ("10.0.0.0/24",), frozenset({"pan_left"}))
    monkeypatch.setenv("TAPO_EMAIL", "b@x.com")
    monkeypatch.setenv("TOLPO_PORT", "9100")
    cfg = load_config(str(path))
    assert (cfg.email, cfg.password, cfg.port) == ("b@x.com", "p", 9100)


def test_credentials_file_is_read_in_place(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"email": "c@x.com", "password": "secret", "devices": {}}))
    creds.chmod(0o600)
    monkeypatch.setenv("TOLPO_CREDENTIALS_FILE", str(creds))
    cfg = load_config(str(tmp_path / "missing.toml"))
    assert (cfg.email, cfg.password) == ("c@x.com", "secret")


def test_server_refuses_to_start_unsafely():
    ok = Config(email="a", password="b")
    assert check(ok) is None
    assert check(Config()) == NO_CREDENTIALS
    assert "token" in check(Config(email="a", password="b", host="0.0.0.0"))
    assert check(Config(email="a", password="b", host="0.0.0.0", token="t")) is None
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and not is_loopback("192.168.1.5")


def test_jsonable():
    class Color(enum.Enum):
        RED = 1

    assert jsonable(datetime.datetime(2026, 1, 2, 3, 4)) == "2026-01-02T03:04:00"
    assert jsonable((1, 2)) == [1, 2] and jsonable(Color.RED) == "RED" and jsonable(None) is None
