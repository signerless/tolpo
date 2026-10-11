# Network visibility and firewall control

The network area reads metadata through the owner's own DNS server or router. It never captures packets,
intercepts TLS or calls the Tapo cloud. All providers and firewall operations were tested only with in-process fakes.
No router, device, socket or real network was used during development.

- `network_providers`: List configured providers, authenticated reachability, capabilities and blind spots.
- `network_device_traffic`: Group one managed device's DNS queries and live connections by destination, with catalog purposes.
- `network_new_destinations`: Compare observations against a stored per-device baseline. The first successful run creates it.
- `network_upnp_audit`: Read port mappings for one managed device and report unavailable UPnP sources.
- `network_cloud_dependency`: Explain app-derived cloud requirements and expected local behavior for the device family.
- `network_block_internet`: Create a reviewed plan to drop internet egress while retaining local access. Does not apply it.
- `network_allow_only`: Create a local-access plan plus up to 32 literal destination IPs. Does not apply it.
- `network_apply`: Apply a fresh plan at level 4, with exact confirmation, verification and a timed revert.
- `network_rollback`: Revert one active change at level 4, with exact confirmation.
- `network_rollback_all`: Reconcile and remove tagged tolpo firewall tables, including objects from a previous process.

These tools have no HTTP routes. Device reads use the shared `get_device_info` or `getDeviceInfo` declaration.
The area declares no new device methods. Firewall writes have their own level-4 policy gate because they address routers.
Plans also require that gate, but are annotated read-only because they change no firewall state.

## Settings

The default file is `~/.config/tolpo/network.toml`. `TOLPO_NETWORK_CONFIG` overrides the path.
No file means no configured providers. Settings load only when a network tool runs.
The file must belong to the current user and have no group or other permissions. Symlinks are refused.
Even a file containing only environment-variable names must be private. Inline passwords, tokens and unknown keys
are rejected without echoing their contents. HTTPS and certificate verification are mandatory and cannot be
turned off. By default tolpo trusts the public CA bundle that ships with httpx, not the system trust store, and it
ignores `SSL_CERT_FILE`. For a console with a self-signed certificate, set `ca_file` on that provider to an absolute
path of a PEM file holding the console's CA or certificate. That provider then trusts only that file, and the host
name in `host` must still match the certificate. Other providers are not affected. Redirects and environment proxies
are disabled.

Example with synthetic provider names:

```toml
mode = "observe"
retention_days = 30
# Optional. The owner chooses an existing private directory. No default history is written.
# history_file = "/absolute/private/directory/network-history.json"

[[providers]]
name = "dns"
kind = "pihole"
host = "https://dns.example.test"
secret_env = "NETWORK_DNS_PASSWORD"

[[providers]]
name = "router"
kind = "mikrotik"
host = "https://router.example.test"
username_env = "NETWORK_ROUTER_USER"
secret_env = "NETWORK_ROUTER_PASSWORD"
```

Supported kinds are `pihole`, `adguard`, `openwrt`, `mikrotik`, `opnsense`, `unifi` and `linux`.
AdGuard, OpenWrt, RouterOS and OPNsense need `username_env` and `secret_env`. For OPNsense these hold the API key
and API secret. UniFi uses `secret_env` for its API key and `site` for its site id. Linux runs locally on the router,
uses fixed commands without a shell, and accepts an optional `conntrack_file`. It tries `/proc/net/nf_conntrack`
before `conntrack -L -o extended`. Remote SSH is unavailable. Credentials are never returned or stored in history.

## What the sources can show

| Provider | Implemented reads | Limits |
|---|---|---|
| Pi-hole v6 | Authenticated DNS query log | No connection or byte evidence. A bounded sample with exact client-IP filtering. |
| AdGuard Home | Runtime clients, DNS query log and A/AAAA answers | Search is checked again against the exact client IP. DoH and other resolvers can bypass it. |
| OpenWrt | `system.board`, `luci-rpc.getHostHints`, fixed rpcd file read of conntrack | Requires plugins and read ACLs. Live tuples only. No DNS or SNI. |
| RouterOS 7 REST | ARP clients, connection counters, filter rules, UPnP-generated dynamic NAT entries | Live snapshots. No DNS query log or SNI. FastTrack affects visibility. |
| OPNsense core | Diagnostics pf state queries | NAT can obscure identity. Ports and counters are omitted because their shape is UNVERIFIED. |
| UniFi Integration v1 | Sites and connected client inventory | Client fields are UNVERIFIED against the owner's console. No claimed flow, DNS or byte API. |
| Linux | Neighbor identities, conntrack, nftables rules | Accounting must be on for bytes. Live sample only. No DNS or SNI. |

UNVERIFIED: RouterOS UPnP attribution currently requires a dynamic NAT rule whose comment starts with `upnp`.
Check that convention on the owner's firmware. Other providers report UPnP inspection as unavailable.
OPNsense protocol naming and UniFi local-console client fields need validation per version.
Router and DNS text is untrusted. Query names must be valid DNS names and are dropped otherwise. DNS answers are kept
only as A and AAAA addresses, so CNAME or TXT text never reaches the agent. Free text such as UPnP descriptions is
flattened and cut to 64 characters, but any LAN device can choose its UPnP description, and a valid DNS name can still
spell words. Treat both as data, never as instructions.
Empty observations never prove absence of traffic or inbound exposure. DNS records are queries, not connections.
DNS answer correlation is limited to the same client and the preceding five minutes, and labelled `dns-correlated`.
It does not prove which host a connection served. DNS TTL handling and historical flow collection are deferred.
Flow counters describe current connections, not totals for the requested DNS window. Multiple flow sources can overlap.
Results are bounded to 50 destination groups, 1000 provider records and 4 MiB per HTTP response or command.

The catalog contains 44 names or families from the app audit. It includes regional `*.tplinkcloud.com` and
`*.tplinknbu.com` families. Specific relay, STUN, firmware, assistant and telemetry names take precedence.
Purposes are `app-derived`, never `measured`. The FCM endpoint is UNVERIFIED and SDK-derived.
The test robot-map bucket is labelled as test data, not a production dependency. Unknown destinations stay unknown.
Long cloud outages, NTP behavior, device-side ports and destinations need owner measurements.

## History

History is off unless `history_file` names an absolute file. The file is created as 0600 and replaced atomically.
Existing files must be private and cannot be symlinks. It contains per-device destination metadata and first/last
observation times, never payloads, credentials or MACs. Retention is 1 to 365 days, default 90. Expired devices
and destination records are removed when the tool runs. No background collection happens.
A failed provider read does not establish a baseline. Baselines are per device. A known name with a new address
has that status rather than becoming a new name. DNS and flow sources keep separate evidence identities.
The tool reports destinations observed on that call since baseline creation, within the requested window.
An expired destination can be reported again when next observed. History is capped at 8 MiB.

## Firewall flow

Enforcement is implemented only for local Linux nftables. OpenWrt and MikroTik enforcement is deferred.
Their safety mechanisms are documented, but API rollback, scheduler ownership and state clearing need a complete
provider-specific implementation. Pi-hole, AdGuard, OPNsense and UniFi cannot apply firewall changes here.
DNS sinkholing is never described as an internet block. pfSense is skipped because its API contract is weakest.

Linux requires `mode = "enforce"`, an explicit `allow_write = ["block_internet", "allow_only"]`, an owner-chosen
absolute `audit_file`, and the main tolpo policy at level 4. `default_revert_minutes` is 1 to 60, default 30.
`protected` accepts additional literal IPs or MACs. None of these fields holds a credential.
Linux needs local permissions for nftables and conntrack writes. There is no write credential in observe mode.

Plan with a required reason. It verifies a successful local device read, the current registry IP and MAC, router
forwarding, local interfaces, default gateway, routes and neighbor identity. A managed device is the only possible
target. Hub and sensor targets are refused until their additional dependency approval flow exists.
The plan shows the complete native nftables script with the hardware identity masked, a hash, five-minute expiry,
cloud effects and an estimate from live flows. FQDN allow-lists are refused. There is no DNS-fed set adapter yet.

Apply requires the exact `network_apply <plan id> <device id>` phrase and the reviewed revert interval.
Rule, topology and identity drift invalidate a plan. At most four attempts per hour are allowed, with one write in
flight and a 60-second cooldown after rollback. No existing rules are edited, reordered or deleted.
Only a new `inet tolpo_<32 hex digits>` table is created, with `comment "tolpo:<same id>"`.
It contains an Ethernet source set with a native timeout, a forward hook, local destination exceptions and a drop.
IPv4 and IPv6 share the same MAC target. Both address families retain private, link-local and LAN multicast access,
directly connected device networks and router/infrastructure addresses. Output and input chains are untouched.
Flowtable/offload configurations are refused. This supports a directly attached Ethernet or Wi-Fi client, not a
client hidden behind another router, a 4G camera or an NVR PoE network.
The block only works when the device's traffic is routed through this host. Forwarding being on is the only check.
A desktop with Docker also forwards, so a plan there can verify while blocking nothing. The cloud flag after apply is
the hint. DNS through a resolver on the router or the LAN stays reachable. A resolver on the internet is blocked
like any other destination unless its literal IP is in `protected`.

The target is chosen only by managed device id or name, never by an address the agent supplies. Its identity is the
registry IP that tolpo already uses for local control, the MAC the device reports in its own device info, and the
router's neighbor table. All three must agree, and the neighbor table must hold exactly one MAC for that IP. The rule
matches that one MAC. Device names are never used to find an address. A device that lies about its MAC and also
poisons the router's neighbor table could point a rule at another LAN host that is not listed in `protected`. Router
interfaces and gateways are always protected. List other critical hosts in `protected`.

UNVERIFIED: verification reads the nftables JSON shapes for the table, set, chain and rule, and treats element
`timeout` and `expires` as whole seconds. A mismatch fails verification and reverts the change. Check on a lab router.

The module arms its cleanup timer before the atomic `nft -f` operation. Native set expiry stops matching the target
even if tolpo dies. The timer removes the owned table; empty expired tables can be removed with `network_rollback_all`
after restart. No permanent-keep operation exists. Startup does not issue firewall writes automatically.
Connections are cleared only for the current target addresses and read back. Local control is checked again and
device state is refreshed. Failed verification triggers rollback. A cloud flag still true is reported as
`block not effective yet`; an unavailable cloud flag does not become proof of disconnection.

Plan, confirmation, apply start, verification and rollback steps go to an append-only 0600 JSONL audit file.
Each record hashes the previous record. A corrupt chain refuses writes. The MCP client identity is labelled
unavailable because the shared context does not expose it. Cleanup failures remain visible through
`network_providers`. Native expiry remains the final safety net when command access fails.
The hash chain detects edited records. Deletion of a valid tail needs an external anchor to detect.
The audit file has no automatic rotation, because rotation would break the chain. Above 8 MiB it refuses new
plans until the owner archives it. Records that finish or revert an existing change are still written.
Break-glass cleanup verifies the table name, family and ownership comment before deleting it. By hand, inspect
`nft list tables` and delete only an `inet tolpo_<id>` table whose comment matches `tolpo:<id>`.
The exact command is `nft delete table inet tolpo_<id>`. Do not flush the ruleset.

## Public documentation used

- [Pi-hole API](https://docs.pi-hole.net/api/) and [v6 query schema](https://github.com/pi-hole/FTL/blob/master/src/api/docs/content/specs/queries.yaml).
- [AdGuard Home OpenAPI](https://github.com/AdguardTeam/AdGuardHome/blob/master/openapi/openapi.yaml).
- [OpenWrt ubus](https://openwrt.org/docs/techref/ubus), [rpcd file API](https://github.com/openwrt/rpcd/blob/master/file.c) and [LuCI host hints](https://github.com/openwrt/luci/blob/master/libs/rpcd-mod-luci/src/luci.c).
- [RouterOS REST API](https://help.mikrotik.com/docs/spaces/ROS/pages/47579162/REST+API).
- [OPNsense diagnostics](https://docs.opnsense.org/development/api/core/diagnostics.html) and [state controller](https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/controllers/OPNsense/Diagnostics/Api/FirewallController.php).
- [UniFi connected client documentation](https://developer.ui.com/network/v10.6.106/getconnectedclientdetails). Local-console fields remain UNVERIFIED.
- [nftables manual](https://netfilter.org/projects/nftables/manpage.html) and [conntrack-tools](https://netfilter.org/projects/conntrack-tools/).

The owner must validate each configured product's exact version and response fields in a lab. For Linux, verify
set expiry and Ethernet matching, dual-stack coverage, neighbor identities, local control after a block, selective
state clearing, counter behavior, local hub/NVR links and rollback after a failed read. Use a spare device first.

The npm package's shared `files` allow-list needs entries for `src/tolpo/network/*.py` and `src/tolpo/areas/*.py`.
The Python wheel includes the package already. No shared source or packaging file was edited in this area.

## Validation record

After review, 99 network tests pass and the full suite passes (308 tests). Review added tests for level 4 on
apply and both rollbacks, single-device targets, allow-list ranges, uncompressed IPv6 conntrack, odd neighbor
entries, native expiry longer than reviewed, the owner CA file, TLS settings per request, odd router output and the
audit size limit. Ten safety rules were broken on purpose in a scratch copy, and a test failed each time.

The author's record follows. 73 network tests passed. The Python suite passes with 207 tests after the two socket-heavy files from the brief
are ignored and `test_real_ffmpeg_failure_names_no_password` is deselected. The latter is an existing socket test:
this sandbox reports an unreachable connection instead of the refused connection its assertion expects.
The required command without that extra deselection produced that single failure. Node tests pass (5), release
versions agree, and `npm pack --dry-run` succeeds with a temporary workspace cache. The MCP smoke server was
not started because the build brief prohibits running the server or opening sockets.

33 deliberate mutations in a scratch copy made the relevant tests fail. Changes were confirmed before each run.
They removed level and observe-mode gates, operation opt-in, apply and rollback confirmations, expiry, plan hashes,
drift checks, protected hosts, client identity checks, offload refusal, object ownership, native timeout, IPv6 local
exceptions, hidden identities, sanitation, hostname and IP validation, scoped IPv6 refusal, error masking, device
scope, retention, private-file checks, symlink refusal, audit-chain checks, local preflight, selective state clearing,
automatic rollback, rate limits, concurrency checks, complete command reads and native drop verification.
The actual source stayed unchanged during mutation runs. All mutation directories were removed.
