"""Provider-neutral records. Internal identities never go straight to MCP."""

from dataclasses import dataclass, field
from typing import Any
from ipaddress import ip_address
import re

from tolpo.registry import RegistryError
from tolpo.views import clean


class NetworkError(RegistryError):
    """A safe, public network-area error."""


def ip(value: Any) -> str:
    try:
        if "%" in str(value):
            raise ValueError
        return str(ip_address(str(value)))
    except ValueError:
        raise NetworkError("Invalid network address") from None


def mac(value: str) -> str:
    value = value.lower().replace("-", ":")
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", value):
        raise NetworkError("Invalid client identity")
    if value == "00:00:00:00:00:00" or int(value[:2], 16) & 1:
        raise NetworkError("Client identity must be unicast")
    return value


def hostname(value: Any, *, wildcard: bool = False) -> str:
    """Accept DNS syntax, never instructions, URLs, credentials or shell input."""
    value = str(value).lower().rstrip(".")
    prefix = "*." if wildcard and value.startswith("*.") else ""
    rest = value[2:] if prefix else value
    if len(rest) > 253 or not rest or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in rest.split(".")
    ):
        raise NetworkError("Invalid destination name")
    return prefix + rest


def text(value: Any, limit: int = 64) -> str:
    value = str(value or "")
    value = re.sub(r"(?i)(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}", "[identity hidden]", value)
    value = re.sub(r"(?i)\b(?:password|token|secret|api[_-]?key)\s*[=:]\s*\S+", "[secret hidden]", value)
    value = re.sub(r"(?i)(?:rtsp|https?)://[^\s/@]+:[^\s/@]+@[^\s]+", "[authenticated address hidden]", value)
    return clean(value, limit)


@dataclass(frozen=True)
class Client:
    ipv4: str = ""
    ipv6: tuple[str, ...] = ()
    mac: str = ""
    hostname: str = ""
    vendor: str = ""
    network: str = ""
    connection: str = ""
    access_point: str = ""
    first_seen: float = 0
    last_seen: float = 0
    device: str = ""
    model: str = ""
    family: str = ""

    @property
    def addresses(self) -> tuple[str, ...]:
        return ((self.ipv4,) if self.ipv4 else ()) + self.ipv6


@dataclass(frozen=True)
class DnsEvent:
    time: float
    client: str
    name: str
    type: str = ""
    answers: tuple[str, ...] = ()
    action: str = "unknown"
    upstream: str = ""  # internal only: can contain authenticated resolver URLs
    source: str = ""


@dataclass(frozen=True)
class Flow:
    client: str
    destination_ip: str
    destination_port: int = 0
    protocol: str = ""
    destination_name: str = ""
    name_source: str = "none"
    start: float = 0
    last_seen: float = 0
    bytes_up: int | None = None
    bytes_down: int | None = None
    packets: int | None = None
    state: str = ""
    action: str = "unknown"
    direction: str = "outbound"
    dpi_label: str = ""
    source: str = ""


@dataclass
class DestinationSummary:
    destination: str
    first_seen: float = 0
    last_seen: float = 0
    connections: int = 0
    queries: int = 0
    bytes: int | None = None
    ports: set[int] = field(default_factory=set)
    protocols: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    addresses: set[str] = field(default_factory=set)
    purpose: str = "unknown"
    status: str = "unknown"


@dataclass(frozen=True)
class Rule:
    id: str
    owner: str = "other"
    description: str = ""
    match: dict[str, Any] = field(default_factory=dict)
    action: str = ""
    enabled: bool = True
    hits: int | None = None


@dataclass(frozen=True)
class Mapping:
    internal_ip: str
    internal_port: int
    external_port: int
    protocol: str
    description: str = ""
    lease: int | None = None
    client: str = ""


@dataclass(frozen=True)
class Plan:
    id: str
    kind: str
    target: Client
    arguments: tuple[str, ...]
    reason: str
    native: str
    expires: float
    state_hash: str
    revert_seconds: int
    provider: str
    hash: str


class Provider:
    """Adapters opt in to each operation. Unsupported means unavailable, never empty."""

    kind = "abstract"
    capabilities: frozenset[str] = frozenset()
    blind_spots: tuple[str, ...] = ("Metadata only; no payloads",)

    def __init__(self, name: str):
        self.name = name

    def describe(self) -> dict[str, Any]:
        return {"name": text(self.name), "kind": self.kind,
                "capabilities": sorted(self.capabilities), "credential": "read",
                "blind_spots": [text(s, 160) for s in self.blind_spots]}

    def require(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise NetworkError(f"{text(self.name)}: {capability} is unavailable")

    async def health(self) -> dict[str, Any]:
        raise NotImplementedError

    async def clients(self, scope: tuple[str, ...]) -> list[Client]:
        self.require("clients")
        raise NotImplementedError

    async def dns_events(self, client: Client, since: float, until: float, limit: int) -> list[DnsEvent]:
        self.require("dns_log")
        raise NotImplementedError

    async def flows(self, client: Client, since: float, until: float, live: bool = True) -> list[Flow]:
        self.require("flows_live")
        raise NotImplementedError

    async def usage(self, client: Client, window: float) -> dict[str, int]:
        self.require("bytes")
        flows = await self.flows(client, 0, window)
        if any(f.bytes_up is None or f.bytes_down is None for f in flows):
            raise NetworkError(f"{text(self.name)}: byte accounting is unavailable")
        return {"connections": len(flows), "bytes": sum((f.bytes_up or 0) + (f.bytes_down or 0) for f in flows)}

    async def rules(self, client: Client) -> list[Rule]:
        self.require("rules_read")
        raise NotImplementedError

    async def upnp_mappings(self) -> list[Mapping]:
        self.require("upnp_read")
        raise NotImplementedError

    async def plan(self, change: Plan) -> Plan:
        self.require(change.kind)
        raise NotImplementedError

    async def apply(self, plan: Plan) -> str:
        self.require(plan.kind)
        raise NotImplementedError

    async def verify(self, plan: Plan) -> bool:
        self.require(plan.kind)
        raise NotImplementedError

    async def rollback(self, handle: str) -> None:
        raise NetworkError(f"{text(self.name)}: rollback is unavailable")
