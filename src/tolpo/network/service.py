"""Scope, compact views and safety gates shared by the MCP tools."""

import asyncio
from dataclasses import asdict, replace
from ipaddress import ip_address
import time
from typing import Any
import uuid

from tolpo.calls import NeedsConfirmation
from tolpo.registry import Forbidden

from .catalog import dependency, explain
from .config import Settings, load
from .models import Client, DestinationSummary, NetworkError, Plan, hostname, ip, mac, text
from .providers import create
from .storage import Audit, History, digest


def safe_error(provider: str, exc: Exception) -> dict:
    # Only our exceptions have messages designed to be public. Third-party errors can contain secrets.
    return {"provider": text(provider), "error": text(str(exc), 160) if isinstance(exc, NetworkError) else "Provider read failed"}


def window(value: str) -> int:
    import re
    match = re.fullmatch(r"([1-9][0-9]{0,2})([mhd])", value)
    if not match:
        raise NetworkError("Window must be minutes, hours or days, up to 90 days")
    seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    if seconds > 90 * 86400:
        raise NetworkError("Window exceeds 90 days")
    return seconds


class Service:
    def __init__(self, registry, caller, settings: Settings, providers=None, *, clock=time.time):
        self.registry, self.caller, self.settings, self.clock = registry, caller, settings, clock
        self.providers = providers if providers is not None else [create(p) for p in settings.providers]
        self.history = History(settings.history_file, settings.retention_days)
        self.audit = Audit(settings.audit_file)
        self.plans: dict[str, Plan] = {}
        self.active: dict[str, Plan] = {}
        self.timers: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        self.history_lock = asyncio.Lock()
        self.writes: list[float] = []
        self.cooldown_until = 0.0

    def target(self, device: str):
        entry = self.registry.resolve_one(device)
        if entry.root:
            raise NetworkError("This device has no independent network address; inspect its hub")
        address = ip(entry.device.host)
        if ip_address(address).is_loopback or ip_address(address).is_multicast or ip_address(address).is_unspecified:
            raise NetworkError("Device network identity is unavailable")
        return entry, Client(ipv4=address if ip_address(address).version == 4 else "",
                             ipv6=(address,) if ip_address(address).version == 6 else (),
                             device=entry.slug, family=entry.kind, model=text(entry.device.model))

    async def status(self):
        result = []
        for p in self.providers:
            item = p.describe()
            try:
                item.update(await p.health())
            except Exception as exc:
                item.update(reachable=False, **safe_error(p.name, exc))
            result.append(item)
        out = {"mode": self.settings.mode, "providers": result, "history": bool(self.settings.history_file)}
        if getattr(self, "rollback_failure", ""):
            out["rollback_error"] = {"change_id": self.rollback_failure, "error": "Timed cleanup failed; native element expiry remains armed"}
        if self.active:
            out["active_changes"] = list(self.active)
        return out

    async def observations(self, device: str, span: str):
        entry, target = self.target(device)
        until = self.clock()
        since = until - window(span)
        flows, dns, errors, limits = [], [], [], []
        for p in self.providers:
            limits.extend(f"{text(p.name)}: {text(spot, 160)}" for spot in p.blind_spots)
            try:
                client = target
                if "clients" in p.capabilities:
                    matches = await p.clients(target.addresses)
                    if matches:
                        addresses = {a for c in matches for a in c.addresses}
                        # Do not join other clients just because a provider returns them in a search.
                        if all(set(c.addresses) & set(target.addresses) for c in matches):
                            client = replace(target, ipv4=target.ipv4, ipv6=tuple(a for a in addresses if ip_address(ip(a)).version == 6))
                if "dns_log" in p.capabilities:
                    events = await p.dns_events(client, since, until, 1000)
                    dns.extend(e for e in events if e.client in client.addresses and since <= e.time <= until)
                    if len(events) >= 1000:
                        limits.append(f"{text(p.name)}: query sample reached limit")
                if "flows_live" in p.capabilities:
                    entries = await p.flows(client, since, until)
                    flows.extend(f for f in entries if f.client in client.addresses)
                    if len(entries) >= 1000:
                        limits.append(f"{text(p.name)}: connection sample reached limit")
            except Exception as exc:
                errors.append(safe_error(p.name, exc))
        return entry, flows, dns, errors, limits

    async def traffic(self, device: str, span: str = "24h"):
        entry, flows, dns, errors, limits = await self.observations(device, span)
        grouped: dict[str, DestinationSummary] = {}
        observations = []
        for event in dns:
            try:
                name = hostname(event.name)
            except NetworkError:
                limits.append("Invalid DNS name omitted")
                continue
            group = grouped.setdefault(name, DestinationSummary(name))
            group.queries += 1
            group.sources.add("dns")
            group.first_seen = min(group.first_seen or event.time, event.time)
            group.last_seen = max(group.last_seen, event.time)
            observations.append({"destination": name, "source": text(event.source), "evidence": "dns"})
        for flow in flows:
            addr = ip(flow.destination_ip)
            name = ""
            if flow.destination_name:
                try:
                    name = hostname(flow.destination_name)
                except NetworkError:
                    limits.append("Invalid connection name omitted")
            # DNS answer correlation is evidence, not proof. Never resolve PTR over the network.
            if not name:
                names = {hostname(e.name) for e in dns if e.client == flow.client and addr in e.answers
                         and e.time <= (flow.last_seen or self.clock()) and self.clock() - e.time <= 300
                         and _valid_name(e.name)}
                if len(names) == 1:
                    name = names.pop()
            key = name or addr
            group = grouped.setdefault(key, DestinationSummary(key))
            group.connections += 1
            if flow.bytes_up is not None or flow.bytes_down is not None:
                group.bytes = (group.bytes or 0) + (flow.bytes_up or 0) + (flow.bytes_down or 0)
            if flow.destination_port:
                group.ports.add(flow.destination_port)
            if flow.protocol:
                group.protocols.add(text(flow.protocol))
            group.addresses.add(addr)
            group.sources.add(text(flow.name_source) if flow.destination_name else "dns-correlated" if name else "IP only")
            seen = flow.last_seen or self.clock()
            group.first_seen = min(group.first_seen or flow.start or seen, flow.start or seen)
            group.last_seen = max(group.last_seen, seen)
            observations.append({"destination": key, "address": addr, "port": flow.destination_port,
                                 "protocol": text(flow.protocol), "source": text(flow.source), "evidence": "connection"})
        destinations = []
        for g in sorted(grouped.values(), key=lambda x: (-(x.bytes or 0), -x.connections, -x.queries, x.destination)):
            info = explain(g.destination)
            item: dict[str, Any] = {"destination": g.destination, "purpose": info["purpose"], "sources": sorted(g.sources)}
            for key, value in (("connections", g.connections), ("queries", g.queries), ("ports", sorted(g.ports)),
                               ("protocols", sorted(g.protocols)), ("first_seen", g.first_seen), ("last_seen", g.last_seen)):
                if value:
                    item[key] = value
            if g.bytes is not None:
                item["bytes"] = g.bytes
            if info.get("family"):
                item["catalog_source"] = "app-derived"
            destinations.append(item)
        result = {"device": text(entry.device.alias), "destinations": destinations[:50],
                  "strength": "none", "limits": sorted(set(limits)) + ["Metadata only. DNS queries do not prove connections. Live flow counters are not window totals. Missing records do not prove no traffic."]}
        if len(destinations) > 50:
            result["omitted"] = len(destinations) - 50
        if errors:
            result["errors"] = errors
        if not self.providers:
            result["limits"].append("No network providers configured")
        return result, observations

    async def new_destinations(self, device: str, span: str):
        # Validate the window before contacting a provider or touching history.
        seconds = window(span)
        if not self.settings.history_file:
            raise NetworkError("Destination history is not enabled")
        async with self.history_lock:
            entry, _ = self.target(device)
            traffic, observed = await self.traffic(device, span)
            if traffic.get("errors") or not self.providers:
                return {"device": traffic["device"], "baseline_created": False,
                        "errors": traffic.get("errors", [{"error": "No network providers configured"}])}
            now = self.clock()
            result = self.history.compare(entry.slug, observed, now, now - seconds)
            return {"device": traffic["device"], **result}

    async def upnp(self, device: str):
        entry, target = self.target(device)
        mappings, errors, unavailable = [], [], []
        for p in self.providers:
            if "upnp_read" not in p.capabilities:
                unavailable.append(text(p.name))
                continue
            try:
                for m in await p.upnp_mappings():
                    if ip(m.internal_ip) in target.addresses:
                        item = {"provider": text(p.name), "internal_port": m.internal_port,
                                "external_port": m.external_port, "protocol": text(m.protocol)}
                        if m.description:
                            item["description"] = text(m.description)
                        if m.lease is not None:
                            item["lease_seconds"] = m.lease
                        mappings.append(item)
            except Exception as exc:
                errors.append(safe_error(p.name, exc))
        result = {"device": text(entry.device.alias), "mappings": mappings[:50],
                  "limits": "Only mappings visible through configured APIs. Empty results do not prove no exposure."}
        if unavailable:
            result["unavailable"] = unavailable
        if errors:
            result["errors"] = errors
        return result

    def write_gate(self, action: str):
        if self.registry.cfg.tier < 4 or self.settings.mode != "enforce" or action not in self.settings.allow_write:
            raise Forbidden(f"{action} is a level 4 firewall action and the owner's policy blocks it")

    def enforcement_provider(self, name: str = ""):
        candidates = [p for p in self.providers if p.kind == "linux" and (not name or p.name == name)]
        if len(candidates) != 1:
            raise NetworkError("Firewall action requires one supported provider with server-side expiry")
        return candidates[0]

    async def local_check(self, entry):
        method = "getDeviceInfo" if entry.kind in {"camera", "doorbell"} else "get_device_info"
        await self.caller.call(entry, method)

    def log(self, event: str, plan: Plan, **extra):
        self.audit.append({"time": self.clock(), "event": event, "plan": plan.id,
                           "device": text(plan.target.device), "provider": text(plan.provider),
                           "reason": plan.reason, "client": "MCP (identity unavailable)", **extra})

    async def plan(self, kind: str, device: str, hosts: list[str], reason: str):
        self.write_gate(kind)
        if not text(reason) or len(reason) > 256:
            raise NetworkError("Firewall plan requires a reason of 1 to 256 characters")
        if kind not in {"block_internet", "allow_only"}:
            raise NetworkError("Unsupported firewall action")
        if (kind == "block_internet" and hosts) or (kind == "allow_only" and not hosts) or len(hosts) > 32:
            raise NetworkError("Invalid destination allow-list")
        # Literal addresses only. FQDN DNS-fed sets need a separate, verified resolver integration.
        addresses = tuple(sorted({ip(h) for h in hosts}))
        entry, target = self.target(device)
        if entry.kind in {"hub", "sensor"}:
            raise NetworkError("Hub dependencies require an additional approval flow; plan unavailable")
        provider = self.enforcement_provider()
        expected_mac = mac(str(getattr(entry.device, "mac", "")))
        target = replace(target, mac=expected_mac)
        if any(p.target.mac == target.mac for p in self.active.values()):
            raise NetworkError("This device already has an active firewall change")
        await self.local_check(entry)
        now = self.clock()
        plan_id = uuid.uuid4().hex
        draft = Plan(plan_id, kind, target, addresses, text(reason, 256), "", now + 300, "",
                     self.settings.default_revert_minutes * 60, provider.name, "")
        plan = await provider.plan(draft, protected=self.settings.protected)
        plan = replace(plan, hash=digest(asdict(plan)))
        self.plans = {k: v for k, v in self.plans.items() if v.expires > now}
        if len(self.plans) >= 32:
            raise NetworkError("Firewall plan limit reached")
        # Show and log the complete native script with only the hardware identity masked.
        preview = plan.native.replace(plan.target.mac, "<device identity>")
        flows = await provider.flows(plan.target, now - 3600, now)
        affected = [f for f in flows if ip_address(f.destination_ip).is_global and f.destination_ip not in addresses]
        self.log("plan", plan, native=preview, plan_hash=plan.hash)
        self.plans[plan.id] = plan
        return {"plan_id": plan.id, "kind": kind, "device": text(entry.device.alias), "level": 4,
                "confirm": f"network_apply {plan.id} {entry.slug}", "native": preview,
                "revert_after_minutes": plan.revert_seconds // 60, "expires": plan.expires, "hash": plan.hash,
                "strength": "firewall", "fidelity": "exact IPs" if addresses else "MAC egress filter",
                "effects": dependency(entry.kind),
                "simulation": {"live_connections_affected": len(affected), "destinations": sorted({ip(f.destination_ip) for f in affected})[:20]},
                "limits": "Local networks and router services stay reachable. Clocks may drift without local NTP. Provider and device verification follow apply."}

    async def apply(self, plan_id: str, confirm: str, minutes: int):
        if self.lock.locked():
            raise NetworkError("A firewall write is already in flight")
        async with self.lock:
            plan = self.plans.get(plan_id)
            if not plan:
                raise NetworkError("Firewall plan is unknown")
            self.write_gate(plan.kind)
            if digest(asdict(replace(plan, hash=""))) != plan.hash:
                raise NetworkError("Firewall plan integrity check failed")
            phrase = f"network_apply {plan.id} {plan.target.device}"
            if confirm != phrase:
                raise NeedsConfirmation(f"Firewall apply requires confirm={phrase!r}")
            if not 1 <= minutes <= 60 or minutes * 60 != plan.revert_seconds:
                raise NetworkError("Revert interval must match the reviewed plan")
            if plan.id in self.active:
                return {"change_id": plan.id, "status": "already applied"}
            now = self.clock()
            if plan.expires <= now:
                raise NetworkError("Firewall plan has expired")
            if now < self.cooldown_until or len([t for t in self.writes if t > now - 3600]) >= 4:
                raise NetworkError("Firewall change rate limit or rollback cooldown is active")
            entry, target = self.target(plan.target.device)
            if target.addresses != ((plan.target.ipv4,) if plan.target.ipv4 else plan.target.ipv6[:1]) or mac(str(getattr(entry.device, "mac", ""))) != plan.target.mac:
                raise NetworkError("Device identity changed since plan")
            provider = self.enforcement_provider(plan.provider)
            await self.local_check(entry)
            await provider.check_plan(plan, protected=self.settings.protected)
            self.log("confirm", plan, confirm=phrase)
            self.log("apply_start", plan)
            # Arm the process timer before apply; the native set timeout also survives process death.
            self.active[plan.id] = plan
            self.timers[plan.id] = asyncio.create_task(self.expire(plan))
            self.writes = [t for t in self.writes if t > now - 3600] + [now]
            try:
                await provider.apply(plan)
                await provider.clear_states(plan)
                if not await provider.verify(plan):
                    raise NetworkError("Provider verification failed")
                await self.local_check(entry)
                await self.registry.refresh(entry, force=True)
                cloud = entry.device.features.get("cloud_connection")
                result = {"change_id": plan.id, "revert_at": now + plan.revert_seconds,
                          "local_control": True, "provider_verified": True,
                          "status": "block not effective yet" if cloud and cloud.value else "provider rule verified; cloud state unavailable" if cloud is None else "cloud disconnected"}
                self.log("verify", plan, result=result)
                return result
            except BaseException:
                try:
                    await asyncio.shield(self._rollback(plan))
                except Exception:
                    # Leave active state and timer intact so a failed rollback remains retryable.
                    if plan.id not in self.active:
                        raise NetworkError("Firewall change reverted; audit logging failed") from None
                    raise NetworkError("Firewall apply failed and rollback failed; server-side expiry remains armed") from None
                raise NetworkError("Firewall apply or verification failed; change reverted") from None

    async def expire(self, plan: Plan):
        await asyncio.sleep(plan.revert_seconds)
        async with self.lock:
            if plan.id in self.active:
                try:
                    await self._rollback(plan)
                except Exception:
                    # A failure must be visible on the next status call, not an unobserved task error.
                    self.rollback_failure = plan.id

    async def _rollback(self, plan: Plan):
        log_failure = None
        try:
            self.log("rollback_start", plan)
        except Exception as exc:
            log_failure = exc
        await self.enforcement_provider(plan.provider).rollback(plan.id)
        self.active.pop(plan.id, None)
        timer = self.timers.pop(plan.id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        self.cooldown_until = self.clock() + 60
        if getattr(self, "rollback_failure", "") == plan.id:
            self.rollback_failure = ""
        self.log("rollback", plan)
        if log_failure:
            raise log_failure

    async def rollback(self, change_id: str, confirm: str):
        async with self.lock:
            if self.registry.cfg.tier < 4:
                raise Forbidden("Firewall rollback is a level 4 action and the owner's policy blocks it")
            if confirm != f"network_rollback {change_id}":
                raise NeedsConfirmation(f"Firewall rollback requires confirm='network_rollback {change_id}'")
            plan = self.active.get(change_id)
            if not plan:
                raise NetworkError("Firewall change is unknown; use owned-object reconciliation")
            await self._rollback(plan)
            return {"change_id": change_id, "status": "reverted"}

    async def rollback_all(self, confirm: str):
        if self.registry.cfg.tier < 4:
            raise Forbidden("Firewall rollback is a level 4 action and the owner's policy blocks it")
        if confirm != "network_rollback_all":
            raise NeedsConfirmation("Firewall rollback requires confirm='network_rollback_all'")
        async with self.lock:
            provider = self.enforcement_provider()
            state = await provider.state()
            import re
            handles = []
            for item in state.get("nftables", []):
                table = item.get("table", {})
                match = re.fullmatch(r"tolpo_([0-9a-f]{32})", table.get("name", ""))
                if match and table.get("family") == "inet" and table.get("comment") == "tolpo:" + match[1]:
                    handles.append(match[1])
            errors = []
            for handle in handles:
                try:
                    if handle in self.active:
                        await self._rollback(self.active[handle])
                    else:
                        self.audit.append({"event": "reconcile_rollback_start", "time": self.clock(), "plan": handle})
                        await provider.rollback(handle)
                        self.audit.append({"event": "reconcile_rollback", "time": self.clock(), "plan": handle})
                except Exception as exc:
                    errors.append(safe_error(provider.name, exc))
            result = {"status": "cleanup incomplete" if errors else "reverted", "objects": len(handles) - len(errors)}
            if errors:
                result["errors"] = errors
            return result


def _valid_name(value):
    try:
        hostname(value)
        return True
    except NetworkError:
        return False


def service_for(registry, caller) -> Service:
    service = getattr(registry, "_network_service", None)
    if service is None:
        service = Service(registry, caller, load())
        registry._network_service = service
    return service
