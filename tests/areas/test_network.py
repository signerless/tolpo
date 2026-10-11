"""Router tests use MockTransport, JSON and command fakes. No sockets or real files."""

import asyncio
from contextlib import nullcontext
from dataclasses import replace
import json
from pathlib import Path
import re
import socket
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tolpo.areas import network as area
from tolpo.calls import NeedsConfirmation
from tolpo.network.catalog import CATALOG, explain
from tolpo.network.config import ProviderConfig, Settings, load
from tolpo.network.models import Client, DnsEvent, Flow, Mapping, NetworkError, Provider, hostname, ip, mac, text
from tolpo.network.providers import AdGuard, Linux, MikroTik, OPNsense, OpenWrt, PiHole, UniFi, conntrack, run
from tolpo.network.service import Service, window
from tolpo.network.storage import Audit, History
from tolpo.registry import Forbidden

ADDR = "192.0.2.10"
ADDR6 = "2001:db8::10"
IDENTITY = "02:00:00:00:00:10"
CLIENT = Client(ipv4=ADDR, ipv6=(ADDR6,), mac=IDENTITY)
NOW = 1700000000.0


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    monkeypatch.setenv("TOLPO_NETWORK_CONFIG", str(tmp_path / "absent.toml"))
    for key, value in (("NETWORK_TEST_SECRET", "synthetic-secret"), ("NETWORK_TEST_USER", "synthetic-user")):
        monkeypatch.setenv(key, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("A test tried a real network or subprocess")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)


def cfg(kind, **kwargs):
    return ProviderConfig("test", kind, "https://router.example.test", "NETWORK_TEST_SECRET", "NETWORK_TEST_USER", **kwargs)


def config_file(tmp_path, contents, mode=0o600):
    path = tmp_path / "network.toml"
    path.write_text(contents)
    path.chmod(mode)
    return path


def test_config_missing_override_and_env_names(tmp_path, monkeypatch):
    assert load().providers == ()
    path = config_file(tmp_path, '[[providers]]\nname="dns"\nkind="pihole"\nhost="https://dns.example.test"\nsecret_env="NETWORK_TEST_SECRET"\n')
    monkeypatch.setenv("TOLPO_NETWORK_CONFIG", str(path))
    settings = load()
    assert settings.mode == "observe" and settings.allow_write == ()
    assert settings.providers[0].secret_env == "NETWORK_TEST_SECRET"
    assert "synthetic-secret" not in str(settings)


@pytest.mark.parametrize("contents", [
    'password="hidden-value"', 'token="hidden-value"', 'retention_days=0', 'retention_days=366',
    'retention_days=true', 'default_revert_minutes=true',
    'default_revert_minutes=0', 'mode="bad"', 'history_file="relative"', 'allow_write=["isolate"]',
    '[[providers]]\nname="x"\nkind="pihole"\nhost="http://dns.example.test"\nsecret_env="NETWORK_TEST_SECRET"',
    '[[providers]]\nname="x"\nkind="pihole"\nhost="https://u:hidden-value@dns.example.test"\nsecret_env="NETWORK_TEST_SECRET"',
    '[[providers]]\nname="x"\nkind="pihole"\nhost="https://dns.example.test?token=hidden-value"\nsecret_env="NETWORK_TEST_SECRET"',
    '[[providers]]\nname="x"\nkind="pihole"\nhost="https://dns.example.test"\nsecret_env="bad-name"',
    '[[providers]]\nname="x"\nkind="pfsense"', '[[providers]]\nname="x"\nkind="linux"\npassword="hidden-value"',
    '[[providers]]\nname="x"\nkind="pihole"\nhost=5\nsecret_env="NETWORK_TEST_SECRET"',
    '[[providers]]\nname="x"\nkind="pihole"\nhost="https://dns.example.test"\nsecret_env="NETWORK_TEST_SECRET"\nca_file="relative.pem"',
    '[[providers]]\nname="x"\nkind="linux"\nca_file="/absolute/ca.pem"',
])
def test_config_refuses_secrets_insecure_transport_and_invalid_inputs(tmp_path, contents):
    with pytest.raises(NetworkError) as error:
        load(config_file(tmp_path, contents))
    assert "hidden-value" not in str(error.value)


def test_config_owner_only_and_symlink(tmp_path):
    path = config_file(tmp_path, "", 0o644)
    with pytest.raises(NetworkError, match="owner-only"):
        load(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(NetworkError):
        load(link)


def test_owner_ca_file_is_scoped_and_keeps_verification(tmp_path):
    import certifi
    import ssl
    path = config_file(tmp_path, f'[[providers]]\nname="dns"\nkind="pihole"\nhost="https://dns.example.test"\nsecret_env="NETWORK_TEST_SECRET"\nca_file="{certifi.where()}"\n')
    owned = PiHole(load(path).providers[0])
    context = owned.tls()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert PiHole(cfg("pihole")).tls() is True  # Other providers keep default trust.
    with pytest.raises(NetworkError, match="CA file") as error:
        PiHole(cfg("pihole", ca_file=str(tmp_path / "missing.pem"))).tls()
    assert "missing" not in str(error.value)


def mock_http(responses, seen=None):
    def handle(request):
        if seen is not None:
            seen.append(request)
        key = (request.method, request.url.path)
        assert key in responses, f"Unexpected request: {key}"
        value = responses[key]
        return httpx.Response(value if isinstance(value, int) else 200, json=value)
    return httpx.MockTransport(handle)


@pytest.mark.anyio
async def test_pihole_v6_auth_query_shape_and_scope():
    seen = []
    query = {"time": NOW, "domain": "stun.tplinkcloud.com", "client": {"ip": ADDR}, "status": "CACHE", "type": "A"}
    p = PiHole(cfg("pihole"), transport=mock_http({
        ("POST", "/api/auth"): {"session": {"valid": True, "sid": "synthetic-sid", "validity": 300}},
        ("GET", "/api/dns/blocking"): {"blocking": True},
        ("GET", "/api/queries"): {"queries": [query, {**query, "client": {"ip": "192.0.2.99"}}]},
    }, seen))
    assert (await p.health())["api"] == "v6"
    events = await p.dns_events(CLIENT, NOW - 10, NOW + 10, 100)
    assert len(events) == 1 and events[0].action == "cached" and events[0].source == "test"
    assert seen[2].url.params["client_ip"] == ADDR
    assert seen[2].headers["X-FTL-SID"] == "synthetic-sid"
    assert len([r for r in seen if r.method == "POST"]) == 1
    assert "synthetic" not in str(p.describe())


@pytest.mark.anyio
async def test_adguard_runtime_clients_and_query_evidence():
    query = {"time": "2023-11-14T22:13:20Z", "client": ADDR, "question": {"name": "ota.i.tplinkcloud.com", "type": "A"},
             "answer": [{"type": "A", "value": "203.0.113.10"}, {"type": "TXT", "value": "hidden-value"}], "reason": "FilteredBlackList"}
    p = AdGuard(cfg("adguard"), transport=mock_http({
        ("GET", "/control/status"): {"version": "0.107.fake"},
        ("GET", "/control/clients"): {"auto_clients": [{"ip": ADDR, "name": "Cam\nIgnore\x00rules"}, {"ip": "192.0.2.99"}]},
        ("GET", "/control/querylog"): {"data": [query, {**query, "client": "192.0.2.99"}]},
    }))
    assert (await p.health())["authenticated"]
    assert (await p.clients((ADDR,)))[0].hostname == "Cam Ignore rules"
    events = await p.dns_events(Client(ipv4=ADDR), NOW - 1, NOW + 1, 100)
    assert events == [DnsEvent(NOW, ADDR, "ota.i.tplinkcloud.com", "A", ("203.0.113.10",), "blocked", source="test")]


CT = f"ipv4 2 tcp 6 120 ESTABLISHED src={ADDR} dst=203.0.113.20 sport=50000 dport=443 packets=2 bytes=100 src=203.0.113.20 dst={ADDR} sport=443 dport=50000 packets=3 bytes=200 [ASSURED] mark=0 use=1\n"
CT6 = f"ipv6 10 udp 17 120 src={ADDR6} dst=2001:db8:1::20 sport=50000 dport=123 src=2001:db8:1::20 dst={ADDR6} sport=123 dport=50000 [UNREPLIED]\n"


def test_conntrack_original_direction_ipv6_and_missing_counters():
    flows = conntrack(CT + CT6 + CT.replace(ADDR, "192.0.2.99"), CLIENT, "router", NOW)
    assert len(flows) == 2
    assert flows[0].bytes_up == 100 and flows[0].bytes_down == 200 and flows[0].packets == 5
    assert flows[0].destination_ip == "203.0.113.20" and flows[0].destination_port == 443
    assert flows[1].protocol == "udp" and flows[1].bytes_up is None


def test_conntrack_matches_uncompressed_ipv6_from_proc():
    # The kernel's /proc file prints IPv6 in full form. A miss would hide flows and skip state clearing.
    full = "2001:0db8:0000:0000:0000:0000:0000:0010"
    flows = conntrack(CT6.replace(f"src={ADDR6} ", f"src={full} "), CLIENT, "router", NOW)
    assert len(flows) == 1 and flows[0].client == ADDR6
    assert conntrack("ipv4 2 tcp 6 src=bad dst=203.0.113.20 dport=1\n", CLIENT, "router", NOW) == []


@pytest.mark.anyio
async def test_openwrt_ubus_login_hints_and_fixed_file_read():
    seen = []
    def handle(request):
        body = json.loads(request.content)
        seen.append(body["params"])
        obj, method = body["params"][1:3]
        value = {("session", "login"): {"ubus_rpc_session": "synthetic-sid"},
                 ("system", "board"): {"release": {"version": "24.10.fake"}},
                 ("luci-rpc", "getHostHints"): {IDENTITY: {"ipaddrs": [ADDR], "ip6addrs": [ADDR6], "name": "Cam\nname"}},
                 ("file", "read"): {"data": CT + CT6}}[(obj, method)]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": [0, value]})
    p = OpenWrt(cfg("openwrt"), transport=httpx.MockTransport(handle))
    assert (await p.health())["version"] == "24.10.fake"
    assert (await p.clients((ADDR,)))[0].addresses == CLIENT.addresses
    assert len(await p.flows(CLIENT, NOW - 1, NOW)) == 2
    assert seen[-1][3] == {"path": "/proc/net/nf_conntrack"}
    assert len([s for s in seen if s[1] == "session"]) == 1


@pytest.mark.anyio
async def test_mikrotik_rest_strings_flows_rules_and_upnp():
    responses = {
        ("GET", "/rest/system/resource"): [{"version": "7.20.fake"}],
        ("GET", "/rest/ip/arp"): [{"address": ADDR, "mac-address": IDENTITY, "interface": "iot\nlan"}],
        ("GET", "/rest/ip/firewall/connection"): [
            {"src-address": ADDR + ":50000", "dst-address": "203.0.113.20:443", "protocol": "tcp", "orig-bytes": "100", "repl-bytes": "200"},
            {"src-address": "[" + ADDR6 + "]:50000", "dst-address": "[2001:db8:1::20]:443", "protocol": "tcp"},
            {"src-address": "192.0.2.99:50000", "dst-address": "203.0.113.20:443"}],
        ("GET", "/rest/ip/firewall/filter"): [{".id": "*1", "comment": "owner\nrule", "action": "drop", "disabled": "false"}],
        ("GET", "/rest/ip/firewall/nat"): [
            {"dynamic": "true", "comment": "upnp camera\ntext", "to-addresses": ADDR, "to-ports": "554", "dst-port": "12345", "protocol": "tcp"},
            {"dynamic": "true", "comment": "other dynamic rule"}],
    }
    p = MikroTik(cfg("mikrotik"), transport=mock_http(responses))
    assert (await p.health())["version"] == "7.20.fake"
    assert (await p.clients((ADDR,)))[0].network == "iot lan"
    flows = await p.flows(CLIENT, NOW - 1, NOW)
    assert len(flows) == 2 and flows[0].bytes_down == 200 and flows[1].destination_port == 443
    assert (await p.rules(CLIENT))[0].owner == "other"
    assert (await p.upnp_mappings())[0].external_port == 12345
    with pytest.raises(NetworkError, match="unavailable"):
        await p.apply(SimpleNamespace(kind="block_internet"))


@pytest.mark.anyio
async def test_opnsense_documented_state_endpoint_omits_unknown_counters():
    seen = []
    p = OPNsense(cfg("opnsense"), transport=mock_http({
        ("GET", "/api/diagnostics/firewall/pf_states"): {"current": "2"},
        ("POST", "/api/diagnostics/firewall/query_states"): {"rows": [
            {"src_addr": ADDR, "dst_addr": "203.0.113.20", "proto": "tcp", "bytes": "hidden-value"},
            {"src_addr": "192.0.2.99", "dst_addr": "203.0.113.20"}]},
    }, seen))
    assert (await p.health())["authenticated"]
    flows = await p.flows(Client(ipv4=ADDR), NOW - 1, NOW)
    assert len(flows) == 1 and flows[0].bytes_up is None and flows[0].destination_port == 0
    assert json.loads(seen[-1].content)["searchPhrase"] == ADDR


@pytest.mark.anyio
async def test_unifi_official_inventory_does_not_claim_flows():
    p = UniFi(cfg("unifi", site="fake-site"), transport=mock_http({
        ("GET", "/proxy/network/integration/v1/sites"): {"data": [{"id": "fake-site"}]},
        ("GET", "/proxy/network/integration/v1/sites/fake-site/clients"): {"data": [
            {"ipAddress": ADDR, "macAddress": IDENTITY, "name": "Cam\nname", "type": "WIRELESS"},
            {"ipAddress": "192.0.2.99", "macAddress": IDENTITY}]},
    }))
    assert (await p.health())["authenticated"]
    assert len(await p.clients((ADDR,))) == 1
    assert "flows_live" not in p.capabilities
    with pytest.raises(NetworkError, match="unavailable"):
        await p.flows(CLIENT, NOW - 1, NOW)


@pytest.mark.anyio
@pytest.mark.parametrize("cls,path", [(PiHole, "/api/auth"), (AdGuard, "/control/status"), (OpenWrt, "/ubus"), (MikroTik, "/rest/system/resource"), (OPNsense, "/api/diagnostics/firewall/pf_states"), (UniFi, "/proxy/network/integration/v1/sites")])
async def test_every_http_provider_rejected_login_is_one_attempt_and_no_secret_leak(cls, path):
    seen = []
    p = cls(cfg(cls.kind), transport=mock_http({("POST" if cls in {PiHole, OpenWrt} else "GET", path): 401}, seen))
    with pytest.raises(NetworkError) as error:
        await p.health()
    assert len(seen) == 1
    assert all(value not in str(error.value) for value in ("synthetic-secret", "synthetic-user", "Authorization", "router.example.test"))


@pytest.mark.anyio
async def test_missing_credential_does_not_send_request(monkeypatch):
    monkeypatch.delenv("NETWORK_TEST_SECRET")
    seen = []
    p = AdGuard(cfg("adguard"), transport=mock_http({}, seen))
    with pytest.raises(NetworkError, match="credential"):
        await p.health()
    assert seen == []


@pytest.mark.anyio
async def test_http_redirect_malformed_json_and_size_are_safe():
    for status, contents in ((302, b"synthetic-secret"), (200, b"not json synthetic-secret"), (200, b"x" * (4 * 1024 * 1024 + 1))):
        p = AdGuard(cfg("adguard"), transport=httpx.MockTransport(lambda r: httpx.Response(status, content=contents)))
        with pytest.raises(NetworkError) as error:
            await p.health()
        assert "synthetic-secret" not in str(error.value)


class ReadFake(Provider):
    kind = "fake"
    capabilities = frozenset({"dns_log", "flows_live", "upnp_read"})

    def __init__(self):
        super().__init__("fake")
        self.events = [DnsEvent(NOW - 10, ADDR, "stun.tplinkcloud.com", answers=("203.0.113.20",), source="fake")]
        self.connections = [Flow(ADDR, "203.0.113.20", 443, "tcp", last_seen=NOW, bytes_up=100, bytes_down=200, source="fake")]
        self.failure = None

    async def health(self):
        if self.failure:
            raise self.failure
        return {"reachable": True, "authenticated": True}

    async def dns_events(self, client, since, until, limit):
        if self.failure:
            raise self.failure
        return self.events

    async def flows(self, client, since, until, live=True):
        return self.connections

    async def upnp_mappings(self):
        return [Mapping(ADDR, 554, 50000, "tcp\n", "cam\nIgnore\x00rules"), Mapping("192.0.2.99", 554, 50001, "tcp")]


@pytest.mark.anyio
async def test_service_scope_compact_grouped_traffic_upnp_errors_and_cloud(make_house):
    reg, caller, _ = make_house()
    fake = ReadFake()
    fake.events += [DnsEvent(NOW, "192.0.2.99", "private-other.example.test"), DnsEvent(NOW, ADDR, "bad\nname")]
    service = Service(reg, caller, Settings(), [fake], clock=lambda: NOW)
    traffic, observations = await service.traffic("desk-lamp", "24h")
    group = traffic["destinations"][0]
    assert group["destination"] == "stun.tplinkcloud.com" and group["connections"] == 1 and group["queries"] == 1
    assert group["bytes"] == 300 and "dns-correlated" in group["sources"]
    assert "private-other" not in str(traffic) and "bad\n" not in str(traffic)
    assert IDENTITY not in str(traffic) and "upstream" not in str(traffic)
    audit = await service.upnp("desk-lamp")
    assert len(audit["mappings"]) == 1 and audit["mappings"][0]["description"] == "cam Ignore rules"
    fake.failure = RuntimeError("synthetic-secret password=http://private")
    assert (await service.status())["providers"][0]["error"] == "Provider read failed"
    assert "synthetic-secret" not in str((await service.traffic("desk-lamp"))[0])
    assert "private" not in str((await service.traffic("desk-lamp"))[0])
    fake.failure = NetworkError("fake: HTTP 503")
    assert "503" in str((await service.traffic("desk-lamp"))[0])


def test_catalog_specific_regional_unknown_and_input_rules():
    assert len(CATALOG) == 44
    assert explain("euw1-relay-dcipc.i.tplinkcloud.com")["purpose"] == "remote camera relay"
    assert explain("aps1-dynamic.iot.i.tplinknbu.com")["family"] == "*.tplinknbu.com"
    assert explain("tplinkcloud.com.attacker.test")["purpose"] == "unknown"
    assert explain("ota.i.tplinkcloud.com.")["source"] == "app-derived"
    for bad in ("https://u:secret@host", "*.bad.test", "bad\nname", "a;rm", "x" * 254):
        with pytest.raises(NetworkError):
            hostname(bad)
    assert hostname("*.tplinkcloud.com", wildcard=True) == "*.tplinkcloud.com"
    for bad in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "01:00:00:00:00:00", "bad"):
        with pytest.raises(NetworkError):
            mac(bad)
    for bad in ("0h", "1w", "91d", "12 hours", "9999d", "-1h"):
        with pytest.raises(NetworkError):
            window(bad)


def test_router_text_flatten_shortening_and_hidden_identities():
    assert text("cam\n\x00 name") == "cam name"
    assert len(text("x" * 100)) == 64
    assert IDENTITY not in text("cam " + IDENTITY)
    for value in ("password=synthetic-secret", "token=synthetic-secret", "rtsp://user:synthetic-secret@camera.test/stream1"):
        assert "synthetic-secret" not in text(value)


def test_ip_arguments_are_single_literal_addresses():
    for bad in ("any", "192.0.2.0/24", "*.tplinkcloud.com", "192.0.2.10;rm", "8.8.8.8\nflush ruleset",
                "fe80::1%eth0", "fe80::1%eth0;\nflush ruleset"):
        with pytest.raises(NetworkError):
            ip(bad)


@pytest.mark.anyio
async def test_empty_providers_missing_device_child_scope_and_compact_limit(make_house):
    reg, caller, _ = make_house()
    service = Service(reg, caller, Settings(), [], clock=lambda: NOW)
    assert (await service.status())["providers"] == []
    assert "No network providers configured" in (await service.traffic("desk-lamp"))[0]["limits"]
    with pytest.raises(Exception):
        await service.traffic("not-a-device")
    child = next(e for e in reg._entries.values() if e.root)
    with pytest.raises(NetworkError, match="hub"):
        service.target(child.slug)
    fake = ReadFake()
    fake.connections = []
    fake.events = [DnsEvent(NOW, ADDR, f"d{n}.example.test", source="fake") for n in range(55)]
    service.providers = [fake]
    result, _ = await service.traffic("desk-lamp")
    assert len(result["destinations"]) == 50 and result["omitted"] == 5


@pytest.mark.anyio
async def test_upnp_provider_error_and_unavailability_are_explicit(make_house):
    reg, caller, _ = make_house()
    fake = ReadFake()
    async def failed():
        raise RuntimeError("synthetic-secret")
    fake.upnp_mappings = failed
    service = Service(reg, caller, Settings(), [fake], clock=lambda: NOW)
    result = await service.upnp("desk-lamp")
    assert result["errors"] == [{"provider": "fake", "error": "Provider read failed"}]
    assert "synthetic-secret" not in str(result)


@pytest.mark.anyio
async def test_history_opt_in_first_baseline_new_name_new_ip_retention_and_no_duplicate(make_house, tmp_path):
    reg, caller, _ = make_house()
    fake = ReadFake()
    now = [NOW]
    service = Service(reg, caller, Settings(history_file=str(tmp_path / "history"), retention_days=1), [fake], clock=lambda: now[0])
    first = await service.new_destinations("desk-lamp", "24h")
    assert first["baseline_created"] and first["new"] == []
    assert (tmp_path / "history").stat().st_mode & 0o777 == 0o600
    now[0] += 30
    fake.connections = [replace(fake.connections[0], destination_ip="203.0.113.21", destination_name="stun.tplinkcloud.com", name_source="sni")]
    second = await service.new_destinations("desk-lamp", "24h")
    assert len(second["new"]) == 1 and second["new"][0]["status"] == "known name, new address"
    fake.events = [DnsEvent(now[0], ADDR, "new.example.test", source="fake")] * 2
    fake.connections = []
    now[0] += 30
    assert len((await service.new_destinations("desk-lamp", "24h"))["new"]) == 1
    now[0] += 2 * 86400
    fake.events = [DnsEvent(now[0], ADDR, "new.example.test", source="fake")]
    assert (await service.new_destinations("desk-lamp", "24h"))["baseline_created"]
    history = (tmp_path / "history").read_text()
    assert "stun" not in history and IDENTITY not in history
    with pytest.raises(NetworkError, match="not enabled"):
        await Service(reg, caller, Settings(), [fake]).new_destinations("desk-lamp", "7d")


@pytest.mark.anyio
async def test_history_failure_does_not_establish_baseline(make_house, tmp_path):
    reg, caller, _ = make_house()
    fake = ReadFake()
    fake.failure = NetworkError("fake: HTTP 503")
    service = Service(reg, caller, Settings(history_file=str(tmp_path / "history")), [fake], clock=lambda: NOW)
    result = await service.new_destinations("desk-lamp", "7d")
    assert not result["baseline_created"] and not (tmp_path / "history").exists()


def test_history_and_audit_owner_only_symlink_chain_tampering(tmp_path):
    history = History(str(tmp_path / "history"), 30)
    history.compare("fake", [], NOW, NOW - 100)
    history.path.chmod(0o644)
    with pytest.raises(NetworkError, match="owner-only"):
        history.compare("fake", [], NOW, NOW - 100)
    audit = Audit(str(tmp_path / "audit"))
    audit.append({"event": "plan", "time": NOW})
    audit.append({"event": "verify", "time": NOW + 1})
    records = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert records[1]["previous"] == records[0]["hash"]
    audit.path.write_text(audit.path.read_text().replace('"plan"', '"tampered"'))
    with pytest.raises(NetworkError, match="chain"):
        audit.append({"event": "rollback"})


def test_corrupt_history_refuses_baseline_replacement(tmp_path):
    path = config_file(tmp_path, '{"device":{"baseline":0,"updated":1700000000,"seen":[]}}')
    with pytest.raises(NetworkError, match="invalid"):
        History(str(path), 30).compare("device", [], NOW, NOW - 100)


@pytest.mark.anyio
async def test_fixed_command_runner_collects_chunks_and_masks_failed_output(monkeypatch):
    calls = []
    class Output:
        def __init__(self):
            self.parts = [b'{"a":', b'1}', b'']
        async def read(self, limit):
            return self.parts.pop(0)
    class Input:
        def write(self, data):
            calls.append(data)
        async def drain(self):
            pass
        def close(self):
            pass
    process = SimpleNamespace(stdout=Output(), stdin=Input(), returncode=None)
    async def wait():
        process.returncode = 0
    process.wait = wait
    process.kill = lambda: None
    async def fake_exec(*argv, **kwargs):
        calls.append(argv)
        assert "shell" not in kwargs and kwargs["stderr"] == asyncio.subprocess.DEVNULL
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await run(("nft", "-c", "-f", "-"), "synthetic input") == '{"a":1}'
    assert calls == [("nft", "-c", "-f", "-"), b"synthetic input"]
    process.stdout.parts = [b"synthetic-secret", b""]
    async def fail_wait():
        process.returncode = 1
    process.wait = fail_wait
    with pytest.raises(NetworkError) as error:
        await run(("nft", "--version"))
    assert "synthetic-secret" not in str(error.value)


class FakeLinux:
    """Fake kernel, nft and ip command output. The commands never execute."""
    def __init__(self):
        self.state = {"nftables": []}
        self.calls = []
        self.neighbors = [{"dst": ADDR, "lladdr": IDENTITY, "dev": "iot"}, {"dst": ADDR6, "lladdr": IDENTITY, "dev": "iot"}]
        self.interfaces = [{"address": "02:00:00:00:00:01", "addr_info": [{"local": "192.0.2.1"}, {"local": "2001:db8::1"}]}]
        self.routes = [{"dst": "default", "gateway": "192.0.2.254", "dev": "wan"}, {"dst": "192.0.2.0/24", "dev": "iot"}]
        self.routes6 = [{"dst": "default", "gateway": "2001:db8:ffff::1", "dev": "wan"}, {"dst": "2001:db8::/64", "dev": "iot"}]
        self.ct = CT + CT6
        self.forward = "1"
        self.fail_apply = False
        self.fail_rollback = False
        self.missing_drop = False
        self.native_seconds = None  # Simulate a kernel that kept a different element timeout.

    def reader(self, path):
        return self.forward if path == "/proc/sys/net/ipv4/ip_forward" else self.ct

    async def runner(self, argv, stdin=None):
        self.calls.append((argv, stdin))
        if argv == ("nft", "--version"):
            return "nftables v1.1.fake\n"
        if argv == ("nft", "-j", "list", "ruleset"):
            return json.dumps(self.state)
        if argv in (("ip", "-j", "-4", "neigh", "show"), ("ip", "-j", "-6", "neigh", "show")):
            return json.dumps([r for r in self.neighbors if (":" in r["dst"]) == (argv[2] == "-6")])
        if argv == ("ip", "-j", "address", "show"):
            return json.dumps(self.interfaces)
        if argv == ("ip", "-j", "route", "show", "table", "main"):
            return json.dumps(self.routes)
        if argv == ("ip", "-j", "-6", "route", "show", "table", "main"):
            return json.dumps(self.routes6)
        if argv == ("nft", "-c", "-f", "-"):
            assert "flags timeout" in stdin and "ip6 daddr" in stdin
            return ""
        if argv == ("nft", "-f", "-"):
            if self.fail_apply:
                raise NetworkError("Local network command failed")
            plan_id = re.search(r"table inet tolpo_([0-9a-f]{32})", stdin)[1]
            name = "tolpo_" + plan_id
            seconds = self.native_seconds or int(re.search(r"timeout ([0-9]+)s", stdin)[1])
            items = [{"table": {"family": "inet", "name": name, "comment": "tolpo:" + plan_id}},
                     {"set": {"family": "inet", "table": name, "name": "targets", "type": "ether_addr", "flags": ["timeout"], "elem": [{"elem": {"val": IDENTITY, "timeout": seconds, "expires": seconds - 1}}]}},
                     {"chain": {"table": name, "name": "forward", "hook": "forward", "prio": -10}},
                     {"rule": {"table": name, "chain": "forward", "expr": [{"match": {"op": "==", "left": {"payload": {"protocol": "ether", "field": "saddr"}}, "right": "@targets"}}, {"drop": None}]}}]
            if self.missing_drop:
                items.pop()
            self.state["nftables"] += items
            return ""
        if argv[:3] == ("nft", "delete", "table"):
            if self.fail_rollback:
                raise NetworkError("Local network command failed")
            name = argv[-1]
            self.state["nftables"] = [item for item in self.state["nftables"] if next(iter(item.values())).get("name") != name and next(iter(item.values())).get("table") != name]
            return ""
        if argv[:2] == ("conntrack", "-D"):
            assert argv[-1] in CLIENT.addresses and "-F" not in argv
            self.ct = "\n".join(line for line in self.ct.splitlines() if f"src={argv[-1]} " not in line)
            return ""
        if argv == ("conntrack", "-L", "-o", "extended"):
            return self.ct
        raise AssertionError(f"Unexpected command {argv}")


def writable(make_house, tmp_path, *, tier=4, mode="enforce"):
    reg, caller, devices = make_house(tier=tier)
    entry = reg.resolve_one("desk-lamp")
    entry.device.mac = IDENTITY
    kernel = FakeLinux()
    provider = Linux(cfg("linux"), runner=kernel.runner, reader=kernel.reader)
    settings = Settings(mode=mode, allow_write=("block_internet", "allow_only"), audit_file=str(tmp_path / "audit"))
    now = [NOW]
    service = Service(reg, caller, settings, [provider], clock=lambda: now[0])
    return service, kernel, entry, now


async def apply_plan(service, plan):
    return await service.apply(plan["plan_id"], plan["confirm"], plan["revert_after_minutes"])


@pytest.mark.anyio
async def test_linux_read_contract_file_fallback_and_unsupported_upnp():
    kernel = FakeLinux()
    provider = Linux(cfg("linux"), runner=kernel.runner, reader=kernel.reader)
    assert (await provider.health())["reachable"]
    assert (await provider.clients((ADDR,)))[0].addresses == CLIENT.addresses
    assert len(await provider.flows(CLIENT, NOW - 100, NOW)) == 2
    assert await provider.rules(CLIENT) == []
    def unavailable(path):
        raise NetworkError("unavailable")
    provider.reader = unavailable
    assert len(await provider.flows(CLIENT, NOW - 100, NOW)) == 2
    assert ("conntrack", "-L", "-o", "extended") in [argv for argv, _ in kernel.calls]
    with pytest.raises(NetworkError, match="unavailable"):
        await provider.upnp_mappings()


@pytest.mark.anyio
async def test_linux_neighbors_skip_incomplete_and_odd_link_addresses():
    kernel = FakeLinux()
    kernel.neighbors = [{"dst": ADDR, "dev": "iot", "state": ["FAILED"]},
                        {"dst": "192.0.2.77", "lladdr": "00:00:00:00:00:00", "dev": "iot"},
                        {"dst": "192.0.2.78", "lladdr": "01:00:5e:00:00:01", "dev": "iot"}] + kernel.neighbors
    provider = Linux(cfg("linux"), runner=kernel.runner, reader=kernel.reader)
    found = await provider.clients((ADDR,))
    assert len(found) == 1 and found[0].mac == IDENTITY and found[0].addresses == CLIENT.addresses
    assert found[0].network == "iot"


@pytest.mark.anyio
async def test_linux_plan_apply_verify_dual_stack_identity_mask_and_owned_rollback(make_house, tmp_path):
    service, kernel, entry, now = writable(make_house, tmp_path)
    kernel.state["nftables"] = [{"table": {"family": "inet", "name": "owner", "comment": "owner"}}]
    plan = await service.plan("block_internet", "desk-lamp", [], "Test cloud independence")
    assert plan["simulation"]["live_connections_affected"] == 0  # Documentation ranges are not global.
    assert IDENTITY not in str(plan) and "ip6 daddr" in plan["native"]
    assert plan["level"] == 4 and "timeout 1800s" in plan["native"]
    assert not any(argv == ("nft", "-f", "-") for argv, _ in kernel.calls)
    result = await apply_plan(service, plan)
    assert result["provider_verified"] and result["local_control"]
    assert service.caller.methods_sent() == ["get_device_info"] * 3
    assert kernel.ct == "" and entry.last_update > 0
    assert (await apply_plan(service, plan))["status"] == "already applied"
    await service.rollback(plan["plan_id"], "network_rollback " + plan["plan_id"])
    assert kernel.state == {"nftables": [{"table": {"family": "inet", "name": "owner", "comment": "owner"}}]}
    assert service.active == {}
    events = [json.loads(line)["event"] for line in (tmp_path / "audit").read_text().splitlines()]
    assert events == ["plan", "confirm", "apply_start", "verify", "rollback_start", "rollback"]
    assert IDENTITY not in (tmp_path / "audit").read_text()


@pytest.mark.anyio
@pytest.mark.parametrize("tier,mode", [(1, "enforce"), (3, "enforce"), (4, "observe")])
async def test_level4_and_enforce_mode_gate_before_commands(make_house, tmp_path, tier, mode):
    service, kernel, _, _ = writable(make_house, tmp_path, tier=tier, mode=mode)
    with pytest.raises(Forbidden, match="level 4"):
        await service.plan("block_internet", "desk-lamp", [], "test")
    assert kernel.calls == [] and service.caller.sent == []


@pytest.mark.anyio
async def test_enforcement_input_and_provider_refusals(make_house, tmp_path):
    service, kernel, _, _ = writable(make_house, tmp_path)
    for kind, hosts, reason in (("block_internet", [], ""), ("allow_only", [], "test"), ("allow_only", ["*.tplinkcloud.com"], "test"),
                                ("allow_only", ["8.8.8.8; rm"], "test"), ("block_internet", ["8.8.8.8"], "test")):
        with pytest.raises(NetworkError):
            await service.plan(kind, "desk-lamp", hosts, reason)
    assert kernel.calls == []
    service.settings = replace(service.settings, allow_write=())
    with pytest.raises(Forbidden):
        await service.plan("block_internet", "desk-lamp", [], "test")
    service.settings = replace(service.settings, allow_write=("block_internet", "allow_only"))
    service.providers = [ReadFake()]
    with pytest.raises(NetworkError, match="supported provider"):
        await service.plan("block_internet", "desk-lamp", [], "test")


@pytest.mark.anyio
@pytest.mark.parametrize("danger", ["host", "gateway", "protected_ip", "protected_mac", "wrong_mac", "flowtable", "not_router", "topology"])
async def test_protected_hosts_identity_and_offload_refusals(make_house, tmp_path, danger):
    service, kernel, _, _ = writable(make_house, tmp_path)
    if danger == "host":
        kernel.interfaces[0]["addr_info"].append({"local": ADDR})
    elif danger == "gateway":
        kernel.routes[0]["gateway"] = ADDR
    elif danger.startswith("protected"):
        service.settings = replace(service.settings, protected=(ADDR if danger.endswith("ip") else IDENTITY,))
    elif danger == "wrong_mac":
        kernel.neighbors[0]["lladdr"] = "02:00:00:00:00:99"
    elif danger == "flowtable":
        kernel.state["nftables"] = [{"flowtable": {"name": "offload"}}]
    elif danger == "not_router":
        kernel.forward = "0"
    elif danger == "topology":
        kernel.routes = []
    with pytest.raises(NetworkError):
        await service.plan("block_internet", "desk-lamp", [], "test")
    assert not any(argv == ("nft", "-f", "-") for argv, _ in kernel.calls)


@pytest.mark.anyio
async def test_exact_confirmation_unknown_expired_drift_and_identity_changed(make_house, tmp_path):
    service, kernel, entry, now = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    for phrase in ("", "yes", plan["confirm"] + " other"):
        with pytest.raises(NeedsConfirmation):
            await service.apply(plan["plan_id"], phrase, 30)
    with pytest.raises(NetworkError, match="unknown"):
        await service.apply("not-a-plan", "yes", 30)
    with pytest.raises(NetworkError, match="interval"):
        await service.apply(plan["plan_id"], plan["confirm"], 0)
    now[0] += 301
    with pytest.raises(NetworkError, match="expired"):
        await apply_plan(service, plan)
    now[0] = NOW
    kernel.state["nftables"].append({"table": {"family": "inet", "name": "drift"}})
    with pytest.raises(NetworkError, match="changed"):
        await apply_plan(service, plan)
    kernel.state["nftables"] = []
    entry.device.mac = "02:00:00:00:00:99"
    with pytest.raises(NetworkError, match="identity changed"):
        await apply_plan(service, plan)
    assert not any(argv == ("nft", "-f", "-") for argv, _ in kernel.calls)


@pytest.mark.anyio
async def test_apply_rechecks_policy_plan_integrity_and_input_interval(make_house, tmp_path):
    service, kernel, entry, now = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    service.settings = replace(service.settings, mode="observe")
    with pytest.raises(Forbidden):
        await apply_plan(service, plan)
    service.settings = replace(service.settings, mode="enforce")
    original = service.plans[plan["plan_id"]]
    service.plans[plan["plan_id"]] = replace(original, native=original.native + "flush ruleset\n")
    with pytest.raises(NetworkError, match="integrity"):
        await apply_plan(service, plan)
    service.plans[plan["plan_id"]] = original
    with pytest.raises(NetworkError, match="interval"):
        await service.apply(plan["plan_id"], plan["confirm"], 31)
    entry.device.host = "192.0.2.99"
    with pytest.raises(NetworkError, match="identity changed"):
        await apply_plan(service, plan)
    assert not any(argv == ("nft", "-f", "-") for argv, _ in kernel.calls)


@pytest.mark.anyio
async def test_one_write_in_flight_rate_limits_and_rollback_cooldown(make_house, tmp_path):
    service, kernel, entry, now = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    async with service.lock:
        with pytest.raises(NetworkError, match="in flight"):
            await asyncio.wait_for(apply_plan(service, plan), timeout=0.2)
    service.writes = [NOW] * 4
    with pytest.raises(NetworkError, match="rate limit"):
        await apply_plan(service, plan)
    service.writes = []
    await apply_plan(service, plan)
    with pytest.raises(NetworkError, match="already has"):
        await service.plan("block_internet", "desk-lamp", [], "test")
    await service.rollback(plan["plan_id"], "network_rollback " + plan["plan_id"])
    next_plan = await service.plan("block_internet", "desk-lamp", [], "test")
    with pytest.raises(NetworkError, match="cooldown"):
        await apply_plan(service, next_plan)


@pytest.mark.anyio
async def test_failed_preflight_sends_no_write_and_both_families_clear(make_house, tmp_path):
    service, kernel, entry, now = writable(make_house, tmp_path)
    service.caller.respond("get_device_info", NetworkError("Device read failed"))
    with pytest.raises(NetworkError, match="read failed"):
        await service.plan("block_internet", "desk-lamp", [], "test")
    assert kernel.calls == []
    service.caller.respond("get_device_info", {})
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    service.caller.respond("get_device_info", NetworkError("Device read failed"))
    with pytest.raises(NetworkError, match="read failed"):
        await apply_plan(service, plan)
    assert not any(argv == ("nft", "-f", "-") for argv, _ in kernel.calls)
    service.caller.respond("get_device_info", {})
    await apply_plan(service, plan)
    deletes = [argv for argv, _ in kernel.calls if argv[:2] == ("conntrack", "-D")]
    assert {argv[-1] for argv in deletes} == set(CLIENT.addresses)
    await service.rollback_all("network_rollback_all")


@pytest.mark.anyio
async def test_server_side_expiry_verification_and_audit_failure_still_reverts(make_house, tmp_path):
    service, kernel, entry, now = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    original = service.providers[0].verify
    async def verify_and_break_audit(plan):
        valid = await original(plan)
        (tmp_path / "audit").chmod(0o644)
        return valid
    service.providers[0].verify = verify_and_break_audit
    with pytest.raises(NetworkError, match="reverted; audit logging failed"):
        await apply_plan(service, plan)
    assert not service.active and kernel.state["nftables"] == []


@pytest.mark.anyio
async def test_timer_cleanup_failure_is_visible_and_recoverable(make_house, tmp_path, monkeypatch):
    service, kernel, entry, now = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    await apply_plan(service, plan)
    service.timers[plan["plan_id"]].cancel()
    async def immediate(seconds):
        return None
    monkeypatch.setattr(asyncio, "sleep", immediate)
    kernel.fail_rollback = True
    await service.expire(service.active[plan["plan_id"]])
    assert (await service.status())["rollback_error"]["change_id"] == plan["plan_id"]
    kernel.fail_rollback = False
    await service.rollback(plan["plan_id"], "network_rollback " + plan["plan_id"])
    assert "rollback_error" not in await service.status()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["apply", "verify", "long_expiry", "local_after", "rollback"])
async def test_apply_failures_revert_and_failed_revert_stays_visible(make_house, tmp_path, failure):
    service, kernel, entry, now = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    if failure == "apply":
        kernel.fail_apply = True
    elif failure == "long_expiry":
        kernel.native_seconds = 3600
    elif failure == "verify":
        kernel.missing_drop = True
    elif failure in {"local_after", "rollback"}:
        calls = [0]
        original = service.local_check
        async def check(entry):
            calls[0] += 1
            if calls[0] == 2:
                raise NetworkError("Local device unavailable")
            await original(entry)
        service.local_check = check
        kernel.fail_rollback = failure == "rollback"
    with pytest.raises(NetworkError, match="rollback failed" if failure == "rollback" else "reverted"):
        await apply_plan(service, plan)
    if failure == "rollback":
        assert plan["plan_id"] in service.active
        assert plan["plan_id"] in service.timers
        kernel.fail_rollback = False
        await service.rollback(plan["plan_id"], "network_rollback " + plan["plan_id"])
    else:
        assert service.active == {} and kernel.state["nftables"] == []


@pytest.mark.anyio
async def test_allow_only_literal_both_families_and_cloud_flag_is_not_false_success(make_house, tmp_path):
    service, kernel, entry, now = writable(make_house, tmp_path)
    entry.device.features["cloud_connection"] = SimpleNamespace(value=True)
    plan = await service.plan("allow_only", "desk-lamp", ["203.0.113.50", "2001:db8:1::50"], "allow measured IPs")
    assert "203.0.113.50" in plan["native"] and "2001:db8:1::50" in plan["native"]
    assert (await apply_plan(service, plan))["status"] == "block not effective yet"
    with pytest.raises(NeedsConfirmation):
        await service.rollback(plan["plan_id"], "yes")
    await service.rollback_all("network_rollback_all")


@pytest.mark.anyio
async def test_timer_and_restart_owned_cleanup_preserve_other_objects(make_house, tmp_path, monkeypatch):
    service, kernel, entry, now = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    await apply_plan(service, plan)
    timer = service.timers[plan["plan_id"]]
    timer.cancel()
    async def immediate(seconds):
        assert seconds == 1800
    monkeypatch.setattr(asyncio, "sleep", immediate)
    await service.expire(service.active[plan["plan_id"]])
    assert not service.active and kernel.state["nftables"] == []
    now[0] += 61
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    await apply_plan(service, plan)
    service.timers[plan["plan_id"]].cancel()
    service.active.clear()  # A restarted service has no in-memory plan.
    service.timers.clear()
    kernel.state["nftables"].append({"table": {"family": "inet", "name": "tolpo_" + "f" * 32, "comment": "owner"}})
    with pytest.raises(NeedsConfirmation):
        await service.rollback_all("yes")
    assert (await service.rollback_all("network_rollback_all"))["objects"] == 1
    assert kernel.state["nftables"] == [{"table": {"family": "inet", "name": "tolpo_" + "f" * 32, "comment": "owner"}}]
    with pytest.raises(NetworkError, match="not owned"):
        await service.providers[0].rollback("f" * 32)


@pytest.mark.anyio
async def test_audit_failure_blocks_apply_before_mutation(make_house, tmp_path):
    service, kernel, _, _ = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    (tmp_path / "audit").chmod(0o644)
    with pytest.raises(NetworkError, match="owner-only"):
        await apply_plan(service, plan)
    assert not any(argv == ("nft", "-f", "-") for argv, _ in kernel.calls)


def captured_tools(service):
    result = {}
    def tool(**kwargs):
        def decorate(fn):
            result[fn.__name__] = fn
            return fn
        return decorate
    async def ready(devices):
        return None
    service.registry._network_service = service
    area.tools(SimpleNamespace(registry=service.registry, caller=service.caller, tool=tool, ready_for=ready, translated=nullcontext))
    return result


@pytest.mark.anyio
async def test_every_tool_thin_wrapper_and_doc_contract(make_house, tmp_path):
    service, kernel, entry, now = writable(make_house, tmp_path)
    tools = captured_tools(service)
    assert len(tools) == 10
    assert (await tools["network_providers"]())["providers"][0]["kind"] == "linux"
    assert (await tools["network_device_traffic"]("desk-lamp"))["destinations"]
    with pytest.raises(NetworkError, match="not enabled"):
        await tools["network_new_destinations"]("desk-lamp")
    assert (await tools["network_upnp_audit"]("desk-lamp"))["unavailable"] == ["test"]
    assert "remote control" in (await tools["network_cloud_dependency"]("desk-lamp"))["cloud_required"]
    plan = await tools["network_allow_only"]("desk-lamp", ["203.0.113.50"], "test")
    await tools["network_apply"](plan["plan_id"], plan["confirm"], 30)
    await tools["network_rollback"](plan["plan_id"], "network_rollback " + plan["plan_id"])
    now[0] += 61
    plan = await tools["network_block_internet"]("desk-lamp", "test")
    await tools["network_apply"](plan["plan_id"], plan["confirm"], 30)
    await tools["network_rollback_all"]("network_rollback_all")
    doc = Path("docs/areas/network.md").read_text()
    assert all(f"- `{name}`:" in doc for name in tools)
    assert all(fn.__doc__ and fn.__annotations__["return"] == dict[str, Any] for fn in tools.values())


@pytest.mark.anyio
async def test_mcp_registration_policy_hints_and_safe_errors(make_house, mcp):
    reg, caller, _ = make_house()
    reg._network_service = Service(reg, caller, Settings(), [ReadFake()], clock=lambda: NOW)
    async with mcp(reg, caller) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["network_device_traffic"].annotations.read_only_hint
        assert tools["network_apply"].annotations.destructive_hint
        result = await client.call_tool("network_device_traffic", {"device": "desk-lamp"})
        assert not result.is_error and result.structured_content["destinations"]
        blocked = await client.call_tool("network_block_internet", {"device": "desk-lamp", "reason": "test"})
        assert blocked.is_error and "level 4" in blocked.content[0].text
        assert "allow_write" not in blocked.content[0].text


@pytest.mark.anyio
async def test_every_firewall_write_refuses_below_level4_before_commands(make_house, tmp_path):
    service, kernel, _, _ = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    await apply_plan(service, plan)
    service.registry.cfg = replace(service.registry.cfg, tier=3)
    kernel.calls.clear()
    with pytest.raises(Forbidden, match="level 4"):
        await service.rollback(plan["plan_id"], "network_rollback " + plan["plan_id"])
    with pytest.raises(Forbidden, match="level 4"):
        await service.rollback_all("network_rollback_all")
    assert kernel.calls == [] and plan["plan_id"] in service.active
    service.registry.cfg = replace(service.registry.cfg, tier=4)
    await service.rollback(plan["plan_id"], "network_rollback " + plan["plan_id"])
    service.cooldown_until = 0
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    service.registry.cfg = replace(service.registry.cfg, tier=3)
    kernel.calls.clear()
    with pytest.raises(Forbidden, match="level 4"):
        await apply_plan(service, plan)
    assert kernel.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("ref", ["192.0.2.50", "192.0.2.0/24", "*", "0.0.0.0", "router.example.test"])
async def test_firewall_target_is_only_one_managed_device(make_house, tmp_path, ref):
    service, kernel, _, _ = writable(make_house, tmp_path)
    with pytest.raises(Exception) as error:
        await service.plan("block_internet", ref, [], "test")
    assert not isinstance(error.value, AssertionError)
    assert not any(argv[0] == "nft" for argv, _ in kernel.calls)


@pytest.mark.anyio
@pytest.mark.parametrize("hosts", [["0.0.0.0/0"], ["::/0"], ["*"], ["203.0.113.0/24"], ["dns.google"], ["203.0.113.1-203.0.113.9"]])
async def test_allow_list_refuses_ranges_wildcards_and_names(make_house, tmp_path, hosts):
    service, kernel, _, _ = writable(make_house, tmp_path)
    with pytest.raises(NetworkError):
        await service.plan("allow_only", "desk-lamp", hosts, "test")
    assert kernel.calls == []


@pytest.mark.anyio
async def test_full_audit_log_refuses_new_plans_but_still_records_rollback(make_house, tmp_path, monkeypatch):
    from tolpo.network import storage
    service, kernel, _, _ = writable(make_house, tmp_path)
    plan = await service.plan("block_internet", "desk-lamp", [], "test")
    monkeypatch.setattr(storage, "MAX_AUDIT_BYTES", 10)
    with pytest.raises(NetworkError, match="audit log is full"):
        await service.plan("allow_only", "desk-lamp", ["203.0.113.50"], "test")
    await apply_plan(service, plan)
    await service.rollback(plan["plan_id"], "network_rollback " + plan["plan_id"])
    events = [json.loads(line)["event"] for line in (tmp_path / "audit").read_text().splitlines()]
    assert events[-1] == "rollback" and kernel.state["nftables"] == []
