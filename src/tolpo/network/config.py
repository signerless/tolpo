"""Separate opt-in configuration. The file contains environment names, never secrets."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import tomllib
from urllib.parse import urlsplit

from .models import NetworkError, ip, mac


def private_open(path: Path, flags: int = os.O_RDONLY):
    """Check the open descriptor, including owner and symlinks, before reading."""
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        raise NetworkError("Network file cannot be opened safely") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        os.close(fd)
        raise NetworkError("Network file must be owner-only (0600)")
    return fd


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    host: str = ""
    secret_env: str = ""
    username_env: str = ""
    site: str = ""
    conntrack_file: str = "/proc/net/nf_conntrack"
    ca_file: str = ""  # Optional owner CA for a self-signed console. Verification stays on.


@dataclass(frozen=True)
class Settings:
    providers: tuple[ProviderConfig, ...] = ()
    mode: str = "observe"
    allow_write: tuple[str, ...] = ()
    history_file: str = ""
    retention_days: int = 90
    audit_file: str = ""
    protected: tuple[str, ...] = ()
    default_revert_minutes: int = 30


def load(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("TOLPO_NETWORK_CONFIG", "~/.config/tolpo/network.toml")).expanduser()
    try:
        fd = private_open(path)
    except NetworkError:
        if not path.exists() and not path.is_symlink():
            return Settings()
        raise
    try:
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        data = tomllib.loads(raw.decode())
        if set(data) - set(Settings.__dataclass_fields__):
            raise ValueError
        providers = []
        for item in data.get("providers", []):
            if set(item) - set(ProviderConfig.__dataclass_fields__):
                raise ValueError
            p = ProviderConfig(**item)
            if not all(isinstance(getattr(p, f), str) for f in ProviderConfig.__dataclass_fields__):
                raise ValueError
            if p.ca_file and (p.kind == "linux" or not Path(p.ca_file).expanduser().is_absolute()):
                raise ValueError
            if p.kind not in {"pihole", "adguard", "openwrt", "mikrotik", "opnsense", "unifi", "linux"}:
                raise ValueError
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", p.name):
                raise ValueError
            for name in (p.secret_env, p.username_env):
                if name and not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name):
                    raise ValueError
            if p.kind != "linux":
                url = urlsplit(p.host)
                if (url.scheme != "https" or not url.hostname or url.username or url.password
                        or url.query or url.fragment or url.path not in ("", "/")):
                    raise ValueError
                if not p.secret_env:
                    raise ValueError
            if p.site and not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", p.site):
                raise ValueError
            providers.append(p)
        if len(providers) > 8 or len({p.name for p in providers}) != len(providers):
            raise ValueError
        data["providers"] = tuple(providers)
        for key in ("allow_write", "protected"):
            data[key] = tuple(data.get(key, ()))
            if not all(isinstance(v, str) for v in data[key]):
                raise ValueError
        for value in data["protected"]:
            # One literal IP or MAC per entry. A range here would be a surprise exception.
            try:
                ip(value)
            except NetworkError:
                mac(value)
        cfg = Settings(**data)
        if (cfg.mode not in {"observe", "enforce"} or set(cfg.allow_write) - {"block_internet", "allow_only"}
                or type(cfg.retention_days) is not int or type(cfg.default_revert_minutes) is not int
                or not 1 <= cfg.retention_days <= 365 or not 1 <= cfg.default_revert_minutes <= 60):
            raise ValueError
        for value in (cfg.history_file, cfg.audit_file):
            if not isinstance(value, str) or (value and not Path(value).expanduser().is_absolute()):
                raise ValueError
        return cfg
    except (ValueError, TypeError, KeyError, UnicodeError, NetworkError):
        raise NetworkError("Invalid network configuration; inline credentials are forbidden") from None


def credential(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise NetworkError("Provider credential is unavailable")
    return value
