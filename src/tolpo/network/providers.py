"""Read-only adapters for documented HTTP APIs and fixed local commands.

References and version limits are in docs/areas/network.md. No adapter retries login.
"""

import asyncio
from datetime import datetime
from ipaddress import ip_address
import json
from pathlib import Path
import re
import time
from typing import Any

import httpx

from .config import ProviderConfig, credential
from .models import Client, DnsEvent, Flow, Mapping, NetworkError, Provider, Rule, ip, mac, text

MAX_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 1000


def rows(data: Any, key: str | None = None) -> list[dict]:
    data = data[key] if key else data
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise NetworkError("Unsupported provider response shape")
    return data[:MAX_RECORDS]


def timestamp(value: Any) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def count(value: Any) -> int | None:
    if value is None or value == "":
        return None
    n = int(value)
    if n < 0:
        raise NetworkError("Invalid provider counter")
    return n


def port(value: Any) -> int:
    n = int(value or 0)
    if not 0 <= n <= 65535:
        raise NetworkError("Invalid provider port")
    return n


def client_from(addresses, **kwargs) -> Client:
    addresses = tuple(ip(a) for a in addresses if a)
    return Client(ipv4=next((a for a in addresses if ip_address(a).version == 4), ""),
                  ipv6=tuple(a for a in addresses if ip_address(a).version == 6), **kwargs)


class HTTPProvider(Provider):
    def __init__(self, cfg: ProviderConfig, *, transport=None):
        super().__init__(cfg.name)
        self.cfg = cfg
        self.transport = transport
        self.sid = ""
        self.sid_until = 0.0

    async def request(self, method: str, path: str, *, params=None, body=None, authenticate=True):
        headers = {}
        auth = None
        if authenticate:
            if self.kind == "unifi":
                headers["X-API-KEY"] = credential(self.cfg.secret_env)
            elif self.kind in {"adguard", "mikrotik", "opnsense"}:
                auth = httpx.BasicAuth(credential(self.cfg.username_env), credential(self.cfg.secret_env))
            elif self.kind == "pihole":
                if not self.sid or time.monotonic() >= self.sid_until:
                    login = await self.request("POST", "/api/auth", body={"password": credential(self.cfg.secret_env)}, authenticate=False)
                    session = login.get("session", {})
                    if not session.get("valid") or not isinstance(session.get("sid"), str):
                        raise NetworkError(f"{text(self.name)}: authentication rejected")
                    self.sid = session["sid"]
                    self.sid_until = time.monotonic() + min(float(session.get("validity", 300)), 1800)
                headers["X-FTL-SID"] = self.sid
        try:
            async with httpx.AsyncClient(base_url=self.cfg.host, transport=self.transport,
                                        timeout=10, verify=True, follow_redirects=False, trust_env=False) as client:
                async with client.stream(method, path, params=params, json=body, headers=headers, auth=auth) as response:
                    if response.status_code in {401, 403}:
                        raise NetworkError(f"{text(self.name)}: authentication or permission rejected")
                    if not 200 <= response.status_code < 300:
                        raise NetworkError(f"{text(self.name)}: HTTP {response.status_code}")
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_BYTES:
                            raise NetworkError(f"{text(self.name)}: response limit exceeded")
                    return json.loads(buf)
        except (httpx.HTTPError, ValueError, TypeError):
            # Never forward request URLs, authorization headers, bodies or vendor errors.
            raise NetworkError(f"{text(self.name)}: request failed or response is invalid") from None


class PiHole(HTTPProvider):
    kind = "pihole"
    capabilities = frozenset({"dns_log"})
    blind_spots = ("DNS queries only; not connections or bytes", "DoH, cached answers and other resolvers are invisible", "Bounded sample, up to 1000 queries per device")

    async def health(self):
        await self.request("GET", "/api/dns/blocking")
        return {"reachable": True, "authenticated": True, "api": "v6"}

    async def dns_events(self, client, since, until, limit):
        result = []
        for addr in client.addresses:
            data = await self.request("GET", "/api/queries", params={"client_ip": addr, "from": since, "until": until, "length": min(limit, MAX_RECORDS)})
            for r in rows(data, "queries"):
                at = timestamp(r["time"])
                if r["client"]["ip"] == addr and since <= at <= until:
                    status = str(r.get("status") or "")
                    action = "blocked" if status.startswith(("GRAVITY", "REGEX", "DENY", "EXTERNAL_BLOCKED")) else "cached" if status.startswith("CACHE") else "unknown"
                    result.append(DnsEvent(at, addr, str(r["domain"]), str(r.get("type", "")), action=action, source=self.name))
        return result[:limit]


class AdGuard(HTTPProvider):
    kind = "adguard"
    capabilities = frozenset({"dns_log", "clients"})
    blind_spots = PiHole.blind_spots

    async def health(self):
        data = await self.request("GET", "/control/status")
        return {"reachable": True, "authenticated": True, "version": text(data.get("version", "unknown"))}

    async def clients(self, scope):
        data = await self.request("GET", "/control/clients")
        # Persistent ids can be CIDRs, MACs or DoH ids. Only runtime IP identities are used.
        return [client_from([r["ip"]], hostname=text(r.get("name")))
                for r in rows(data, "auto_clients") if r.get("ip") in scope]

    async def dns_events(self, client, since, until, limit):
        result = []
        for addr in client.addresses:
            data = await self.request("GET", "/control/querylog", params={"search": addr, "limit": min(limit, MAX_RECORDS)})
            for r in rows(data, "data"):
                at = timestamp(r["time"])
                if r["client"] != addr or not since <= at <= until:
                    continue
                answers = tuple(ip(a["value"]) for a in r.get("answer", []) if a.get("type") in {"A", "AAAA"})
                action = "blocked" if str(r.get("reason", "")).startswith("Filtered") else "cached" if r.get("cached") else "unknown"
                result.append(DnsEvent(at, addr, str(r["question"]["name"]), str(r["question"].get("type", "")), answers, action, source=self.name))
        return result[:limit]


def conntrack(text_output: str, client: Client, source: str, now: float) -> list[Flow]:
    """Both /proc and conntrack -L have original and reply tuples. Keep original only."""
    result = []
    for line in text_output.splitlines():
        tokens = line.split()
        protocol = next((x for x in tokens[:5] if x in {"tcp", "udp", "icmp", "icmpv6"}), "")
        values: dict[str, list[str]] = {}
        for token in tokens:
            if "=" in token:
                key, value = token.split("=", 1)
                values.setdefault(key, []).append(value)
        if not values.get("src") or values["src"][0] not in client.addresses:
            continue
        if not values.get("dst") or not protocol:
            raise NetworkError("Unsupported conntrack record")
        size = values.get("bytes", [])
        packets = values.get("packets", [])
        result.append(Flow(values["src"][0], ip(values["dst"][0]), port(values.get("dport", [0])[0]), protocol,
                           last_seen=now, bytes_up=count(size[0]) if size else None,
                           bytes_down=count(size[1]) if len(size) > 1 else None,
                           packets=sum(int(p) for p in packets) if packets else None,
                           state=next((s for s in tokens if s in {"ESTABLISHED", "SYN_SENT", "TIME_WAIT", "UNREPLIED", "[UNREPLIED]"}), ""), source=source))
        if len(result) >= MAX_RECORDS:
            break
    return result


class OpenWrt(HTTPProvider):
    kind = "openwrt"
    capabilities = frozenset({"clients", "flows_live"})
    blind_spots = ("Requires rpcd file read ACL and luci-rpc plugin", "Live IP tuples only; no DNS or SNI", "Accounting may be disabled; offloaded flows may be missing", "rpcd may truncate proc-file output; sample is incomplete")

    async def rpc(self, obj, method, args):
        if not self.sid or time.monotonic() >= self.sid_until:
            login = await self.request("POST", "/ubus", authenticate=False, body={"jsonrpc": "2.0", "id": 1, "method": "call", "params": ["0" * 32, "session", "login", {"username": credential(self.cfg.username_env), "password": credential(self.cfg.secret_env)}]})
            if login.get("error") or login.get("result", [1])[0] != 0:
                raise NetworkError(f"{text(self.name)}: authentication rejected")
            self.sid = login["result"][1]["ubus_rpc_session"]
            self.sid_until = time.monotonic() + 240
        data = await self.request("POST", "/ubus", authenticate=False, body={"jsonrpc": "2.0", "id": 2, "method": "call", "params": [self.sid, obj, method, args]})
        if data.get("error") or data.get("result", [1])[0] != 0:
            raise NetworkError(f"{text(self.name)}: ubus read rejected")
        return data["result"][1]

    async def health(self):
        data = await self.rpc("system", "board", {})
        return {"reachable": True, "authenticated": True, "version": text(data.get("release", {}).get("version", "unknown"))}

    async def clients(self, scope):
        data = await self.rpc("luci-rpc", "getHostHints", {})
        result = []
        for identity, r in data.items():
            addresses = r.get("ipaddrs", []) + r.get("ip6addrs", [])
            if set(addresses) & set(scope):
                result.append(client_from(addresses, mac=mac(identity), hostname=text(r.get("name"))))
        return result

    async def flows(self, client, since, until, live=True):
        data = await self.rpc("file", "read", {"path": "/proc/net/nf_conntrack"})
        return conntrack(data["data"], client, self.name, until)


def endpoint(value: str) -> tuple[str, int]:
    try:
        return ip(value), 0
    except NetworkError:
        host, sep, value_port = value.rpartition(":")
        if not sep:
            raise NetworkError("Invalid connection endpoint")
        return ip(host.strip("[]")), port(value_port)


class MikroTik(HTTPProvider):
    kind = "mikrotik"
    capabilities = frozenset({"clients", "flows_live", "bytes", "rules_read", "upnp_read"})
    blind_spots = ("RouterOS 7 REST, live connections only", "No DNS query log or SNI", "FastTrack and offload affect visibility", "UPnP detection uses dynamic NAT comments (UNVERIFIED per firmware)")

    async def health(self):
        data = rows(await self.request("GET", "/rest/system/resource"))
        version = str(data[0]["version"])
        if not version.startswith("7."):
            raise NetworkError(f"{text(self.name)}: unsupported RouterOS version")
        return {"reachable": True, "authenticated": True, "version": text(version)}

    async def clients(self, scope):
        data = rows(await self.request("GET", "/rest/ip/arp"))
        return [client_from([r["address"]], mac=mac(r["mac-address"]), network=text(r.get("interface")))
                for r in data if r.get("address") in scope and r.get("mac-address")]

    async def flows(self, client, since, until, live=True):
        result = []
        for r in rows(await self.request("GET", "/rest/ip/firewall/connection")):
            src, _ = endpoint(r["src-address"])
            if src not in client.addresses:
                continue
            dst, p = endpoint(r["dst-address"])
            result.append(Flow(src, dst, p, text(r.get("protocol")), last_seen=until,
                               bytes_up=count(r.get("orig-bytes")), bytes_down=count(r.get("repl-bytes")),
                               state=text(r.get("tcp-state")), source=self.name))
        return result

    async def rules(self, client):
        result = []
        for r in rows(await self.request("GET", "/rest/ip/firewall/filter")):
            result.append(Rule(text(r[".id"]), "tolpo" if str(r.get("comment", "")).startswith("tolpo:") else "other", text(r.get("comment")), action=text(r.get("action")), enabled=r.get("disabled") != "true"))
        return result

    async def upnp_mappings(self):
        result = []
        # UNVERIFIED: firmware may format the UPnP-generated comment differently.
        for r in rows(await self.request("GET", "/rest/ip/firewall/nat", params={"dynamic": "true"})):
            if r.get("dynamic") != "true" or not str(r.get("comment", "")).lower().startswith("upnp"):
                continue
            result.append(Mapping(ip(r["to-addresses"]), port(r["to-ports"]), port(r["dst-port"]), text(r["protocol"]), text(r.get("comment"))))
        return result


class OPNsense(HTTPProvider):
    kind = "opnsense"
    capabilities = frozenset({"flows_live"})
    blind_spots = ("Core pf live states only; no DNS or SNI", "State counter and port fields are UNVERIFIED; omitted", "NAT may obscure original client identity")

    async def health(self):
        await self.request("GET", "/api/diagnostics/firewall/pf_states")
        return {"reachable": True, "authenticated": True, "api": "core diagnostics"}

    async def flows(self, client, since, until, live=True):
        result = []
        for addr in client.addresses:
            data = await self.request("POST", "/api/diagnostics/firewall/query_states", body={"searchPhrase": addr, "rowCount": MAX_RECORDS, "current": 1})
            for r in rows(data, "rows"):
                if r.get("src_addr") == addr:
                    # UNVERIFIED: protocol naming differs by version. Do not invent bytes or ports.
                    result.append(Flow(addr, ip(r["dst_addr"]), protocol=text(r.get("proto", "")), last_seen=until, source=self.name))
        return result


class UniFi(HTTPProvider):
    kind = "unifi"
    capabilities = frozenset({"clients"})
    blind_spots = ("Official Integration API inventory only; no documented flows, bytes or DNS", "UNVERIFIED against owner console version")

    async def health(self):
        await self.request("GET", "/proxy/network/integration/v1/sites", params={"limit": 1})
        return {"reachable": True, "authenticated": True, "api": "integration v1"}

    async def clients(self, scope):
        if not self.cfg.site:
            raise NetworkError(f"{text(self.name)}: site is unavailable")
        # UNVERIFIED: local-console fields need validation on the owner's Network version.
        data = await self.request("GET", f"/proxy/network/integration/v1/sites/{self.cfg.site}/clients", params={"limit": MAX_RECORDS})
        return [client_from([r["ipAddress"]], mac=mac(r["macAddress"]), hostname=text(r.get("name")), connection=text(r.get("type")))
                for r in rows(data, "data") if r.get("ipAddress") in scope]


async def run(argv: tuple[str, ...], stdin: str | None = None) -> str:
    """Fixed argv only. Bound output and time, suppress all raw command errors."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        if stdin is not None:
            process.stdin.write(stdin.encode())
            await process.stdin.drain()
        process.stdin.close()
        async def collect():
            data = bytearray()
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise NetworkError("Local command response limit exceeded")
            await process.wait()
            if process.returncode:
                raise NetworkError("Local network command failed")
            return data.decode()
        return await asyncio.wait_for(collect(), timeout=10)
    except (OSError, asyncio.TimeoutError, UnicodeError):
        raise NetworkError("Local network command unavailable or failed") from None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


class Linux(Provider):
    kind = "linux"
    capabilities = frozenset({"clients", "flows_live", "rules_read", "block_internet", "allow_only", "atomic_apply", "server_side_revert"})
    blind_spots = ("Runs on the Linux router itself; no SSH", "Conntrack live snapshot only; counters require accounting", "No DNS or SNI; flowtables must be absent for enforcement", "Allow-only supports literal IPs, not FQDNs")

    def __init__(self, cfg: ProviderConfig, *, runner=run, reader=None):
        super().__init__(cfg.name)
        self.cfg, self.runner = cfg, runner
        self.reader = reader or self.read_file

    @staticmethod
    def read_file(path: str) -> str:
        try:
            with Path(path).open() as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise NetworkError("Local network file limit exceeded")
            return data
        except OSError:
            raise NetworkError("Local conntrack file unavailable") from None

    async def health(self):
        version = await self.runner(("nft", "--version"))
        return {"reachable": True, "authenticated": True, "version": text(version)}

    async def clients(self, scope):
        data = []
        for family in ("-4", "-6"):
            data.extend(rows(json.loads(await self.runner(("ip", "-j", family, "neigh", "show")))))
        identities = {mac(r["lladdr"]) for r in data if r.get("dst") in scope and r.get("lladdr")}
        return [client_from([r["dst"] for r in data if r.get("lladdr") and mac(r["lladdr"]) == identity],
                            mac=identity, network=text(next(r.get("dev", "") for r in data if r.get("dst") in scope and mac(r.get("lladdr", "")) == identity)))
                for identity in identities]

    async def flows(self, client, since, until, live=True):
        try:
            data = self.reader(self.cfg.conntrack_file)
        except NetworkError:
            data = await self.runner(("conntrack", "-L", "-o", "extended"))
        return conntrack(data, client, self.name, until)

    async def state(self):
        return json.loads(await self.runner(("nft", "-j", "list", "ruleset")))

    async def rules(self, client):
        result = []
        for item in (await self.state()).get("nftables", []):
            if "rule" in item:
                r = item["rule"]
                result.append(Rule(str(r.get("handle", "")), "tolpo" if str(r.get("comment", "")).startswith("tolpo:") else "other", text(r.get("comment"))))
        return result

    async def plan(self, change, *, protected=()):
        from .enforcement import make_plan
        return await make_plan(self, change, protected)

    async def check_plan(self, plan, *, protected=()):
        from .enforcement import check_plan
        return await check_plan(self, plan, protected)

    async def apply(self, plan):
        from .enforcement import apply
        return await apply(self, plan)

    async def verify(self, plan):
        from .enforcement import verify
        return await verify(self, plan)

    async def rollback(self, handle):
        from .enforcement import rollback
        await rollback(self, handle)

    async def clear_states(self, plan):
        from .enforcement import clear_states
        await clear_states(self, plan)


def create(cfg: ProviderConfig, **kwargs) -> Provider:
    cls = {"pihole": PiHole, "adguard": AdGuard, "openwrt": OpenWrt, "mikrotik": MikroTik,
           "opnsense": OPNsense, "unifi": UniFi, "linux": Linux}[cfg.kind]
    return cls(cfg, **kwargs)
