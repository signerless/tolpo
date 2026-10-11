"""Linux router enforcement. Own tables, native element expiry and fixed argv only."""

from dataclasses import replace
from ipaddress import collapse_addresses, ip_address, ip_network
import json
import re

from .models import NetworkError, ip, mac
from .storage import digest

LOCAL4 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "224.0.0.0/24", "239.0.0.0/8", "255.255.255.255/32")
LOCAL6 = ("fc00::/7", "fe80::/10", "ff02::/16")


def owned_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise NetworkError("Invalid firewall change identity")
    return value


def stable(value):
    """Ignore moving packet counters and expiry values when detecting rule drift."""
    if isinstance(value, list):
        return [stable(v) for v in value if not isinstance(v, dict) or "metainfo" not in v]
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in {"packets", "bytes", "expires"}}
    return value


async def snapshot(provider, target, protected):
    rules = await provider.state()
    if not isinstance(rules.get("nftables"), list):
        raise NetworkError("Unsupported nftables response shape")
    if any("flowtable" in item for item in rules["nftables"]) or '"flow"' in json.dumps(rules):
        raise NetworkError("Firewall plan refused because flow offload can bypass the rule")
    if provider.reader("/proc/sys/net/ipv4/ip_forward").strip() != "1":
        raise NetworkError("Firewall enforcement requires the local Linux router")
    interfaces = json.loads(await provider.runner(("ip", "-j", "address", "show")))
    routes = json.loads(await provider.runner(("ip", "-j", "route", "show", "table", "main")))
    routes6 = json.loads(await provider.runner(("ip", "-j", "-6", "route", "show", "table", "main")))
    protect_ip, protect_mac = set(), set()
    for interface in interfaces:
        if interface.get("address"):
            try:
                protect_mac.add(mac(interface["address"]))
            except NetworkError:
                pass  # loopback has no unicast Ethernet identity
        for address in interface.get("addr_info", []):
            protect_ip.add(ip(address["local"]))
    for route in routes + routes6:
        if route.get("gateway"):
            protect_ip.add(ip(route["gateway"]))
    for value in protected:
        try:
            protect_ip.add(ip(value))
        except NetworkError:
            protect_mac.add(mac(value))
    if not interfaces or not routes or not any(r.get("dst") == "default" for r in routes):
        raise NetworkError("Router topology is incomplete")
    clients = await provider.clients(target.addresses)
    if len(clients) != 1 or clients[0].mac != target.mac:
        raise NetworkError("Router client identity does not match the managed device")
    client = replace(clients[0], device=target.device, model=target.model, family=target.family)
    if set(client.addresses) & protect_ip or client.mac in protect_mac:
        raise NetworkError("Firewall plan targets a protected host or router")
    local = list(LOCAL4 + LOCAL6)
    for route in routes + routes6:
        dst = route.get("dst", "default")
        if (dst != "default" and not route.get("gateway") and route.get("dev") == client.network
                and route.get("type", "unicast") == "unicast"):
            if "%" in dst:
                raise NetworkError("Invalid local route")
            network = ip_network(dst, strict=True)
            if network.prefixlen == 0:
                raise NetworkError("Unsafe local route")
            local.append(str(network))
    # Protected router and infrastructure addresses are exceptions as destinations too.
    local.extend(str(ip_network(a)) for a in protect_ip)
    local = [str(n) for version in (4, 6) for n in collapse_addresses(
        ip_network(n) for n in local if ip_network(n).version == version)]
    evidence = {"rules": stable(rules), "target": client.addresses, "mac": client.mac,
                "network": client.network, "protected_ip": sorted(protect_ip),
                "protected_mac": sorted(protect_mac), "local": sorted(set(local))}
    return client, tuple(sorted(set(local))), digest(evidence)


def script(plan, local):
    plan_id = owned_id(plan.id)
    identity = mac(plan.target.mac)
    v4 = sorted(n for n in local if ip_network(n).version == 4)
    v6 = sorted(n for n in local if ip_network(n).version == 6)
    allowed4 = [ip(a) for a in plan.arguments if ip_address(a).version == 4]
    allowed6 = [ip(a) for a in plan.arguments if ip_address(a).version == 6]
    lines = [f'table inet tolpo_{plan_id} {{', f' comment "tolpo:{plan_id}"',
             " set targets { type ether_addr; flags timeout;",
             f"  elements = {{ {identity} timeout {plan.revert_seconds}s }}; }}",
             " chain forward { type filter hook forward priority -10; policy accept;"]
    for family, networks in (("ip", v4), ("ip6", v6)):
        lines.append(f"  ether saddr @targets {family} daddr {{ {', '.join(networks)} }} return")
    for family, values in (("ip", allowed4), ("ip6", allowed6)):
        if values:
            lines.append(f"  ether saddr @targets {family} daddr {{ {', '.join(values)} }} return")
    lines.extend([f'  ether saddr @targets counter drop comment "tolpo:{plan_id}"', " }", "}"])
    return "\n".join(lines) + "\n"


async def make_plan(provider, draft, protected):
    client, local, state_hash = await snapshot(provider, draft.target, protected)
    plan = replace(draft, target=client, state_hash=state_hash)
    native = script(plan, local)
    await provider.runner(("nft", "-c", "-f", "-"), native)
    return replace(plan, native=native)


async def check_plan(provider, plan, protected):
    _, local, state_hash = await snapshot(provider, plan.target, protected)
    if state_hash != plan.state_hash or script(plan, local) != plan.native:
        raise NetworkError("Firewall or network identity changed since plan")


async def apply(provider, plan):
    # The service handles idempotence. Refuse name collisions even when tagged as ours.
    if any(item.get("table", {}).get("name") == "tolpo_" + owned_id(plan.id)
           for item in (await provider.state())["nftables"]):
        raise NetworkError("Firewall table name already exists")
    await provider.runner(("nft", "-f", "-"), plan.native)
    return plan.id


async def verify(provider, plan):
    name = "tolpo_" + owned_id(plan.id)
    state = await provider.state()
    items = state["nftables"]
    table_ok = any(item.get("table", {}).get("name") == name and item["table"].get("family") == "inet"
                   and item["table"].get("comment") == "tolpo:" + plan.id for item in items)
    sets = [item["set"] for item in items if item.get("set", {}).get("table") == name and item["set"].get("name") == "targets"]
    set_ok = len(sets) == 1 and sets[0].get("type") == "ether_addr" and "timeout" in sets[0].get("flags", [])
    element_ok = set_ok and any(isinstance(e, dict) and e.get("elem", {}).get("val") == plan.target.mac
                               and 0 < e["elem"].get("expires", 0) <= plan.revert_seconds * 1000
                               for e in sets[0].get("elem", []))
    chain_ok = any(item.get("chain", {}).get("table") == name and item["chain"].get("name") == "forward"
                   and item["chain"].get("hook") == "forward" and item["chain"].get("prio") == -10 for item in items)
    match = {"match": {"op": "==", "left": {"payload": {"protocol": "ether", "field": "saddr"}}, "right": "@targets"}}
    drop_ok = any(item.get("rule", {}).get("table") == name and item["rule"].get("chain") == "forward"
                  and match in item["rule"].get("expr", []) and {"drop": None} in item["rule"].get("expr", []) for item in items)
    return bool(table_ok and element_ok and chain_ok and drop_ok)


async def rollback(provider, handle):
    name = "tolpo_" + owned_id(handle)
    state = await provider.state()
    tables = [item["table"] for item in state["nftables"] if item.get("table", {}).get("name") == name]
    if not tables:
        return  # Idempotent. Never remove a replacement object we do not own.
    if len(tables) != 1 or tables[0].get("family") != "inet" or tables[0].get("comment") != "tolpo:" + handle:
        raise NetworkError("Firewall rollback refused an object not owned by tolpo")
    await provider.runner(("nft", "delete", "table", "inet", name))
    if any(item.get("table", {}).get("name") == name for item in (await provider.state())["nftables"]):
        raise NetworkError("Firewall rollback verification failed")


async def clear_states(provider, plan):
    # Never flush the global table. Connection deletion is constrained to current client addresses.
    for address in plan.target.addresses:
        family = "ipv4" if ip_address(address).version == 4 else "ipv6"
        # conntrack -D exits 1 when no entries matched. Check before issuing the deletion.
        flows = await provider.flows(plan.target, 0, 0)
        if any(f.client == address for f in flows):
            await provider.runner(("conntrack", "-D", "-f", family, "-s", ip(address)))
    remaining = await provider.flows(plan.target, 0, 0)
    if any(f.state == "ESTABLISHED" and ip_address(f.destination_ip).is_global
           and f.destination_ip not in plan.arguments for f in remaining):
        raise NetworkError("Existing internet connections survived the firewall change")
