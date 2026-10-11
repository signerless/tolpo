# Pairing, Wi-Fi and reset

Lifecycle operations are off under the default owner policy. Each level 4 call
needs its exact confirmation phrase, including the plan preview. A successful
plan preview never authorizes a later execution. Reboot is level 2. Wi-Fi scan
is level 0. These tools use public python-kasa operations through a small
interface and the registry's device locks. Their policy method names describe
operations, not raw device RPCs.

- `pair_hub`: Start scanning, find and add all supported children in pairing mode through `ChildSetup.pair`. Return successful additions and new registry devices. Level 4, `pair_hub <hub id>`.
- `pair_remove`: Unpair an existing child from the selected hub through `ChildSetup.unpair`. This does not remove its cloud account record. Level 4, `pair_remove <hub id>`.
- `pair_name`: Name a paired device through `set_alias`. The registry id stays stable. Names must be flat text of at most 64 UTF-8 bytes. Level 4, `pair_name <device id>`.
- `wifi_scan`: Scan on the device's own radio through `wifi_scan`. Return counts, channels and signal measurements. Never return SSIDs, BSSIDs or hardware addresses. Level 0.
- `wifi_join`: Request a Wi-Fi change through `wifi_join`. Read the Wi-Fi name, password and owner account from named environment variables. The account must match the authenticated device session. Return request and refresh status, not a claim of successful connection. Level 4, `wifi_join <device id>`.
- `reset_factory`: Request a SMART factory reset through `factory_reset`. This clears settings and the local account without downgrading firmware. A hub loses its pairings. The cloud record may remain. Level 4, `reset_factory <device id>`.
- `reset_reboot`: Reboot a SMART device through `reboot`, with a delay of 1 to 30 seconds. Report if it is offline during refresh. Level 2.
- `pair_softap_plan`: Preview the host's six provisioning steps without contacting the host or device. Level 4, `pair_softap <plan target>`.
- `pair_softap_provision`: Execute SMART SoftAP self-binding, restore the host's Wi-Fi and verify the expected device on the LAN. Level 4, `pair_softap <plan target>`.

## Hub pairing

Put only the owner's intended children in pairing mode before calling `pair_hub`.
python-kasa pairs every supported child it detects. It does not expose a public
staged scan, candidate selection or scan-stop API. The tool bundles start, list
and add, and reports successful additions. An empty scan returns a zero count.
The wait is limited to 10 seconds so the existing registry operation timeout
also has time for device work. Both SMART hubs such as H100 and camera-protocol
hubs such as H200 use `Module.ChildSetup`. Children are controlled through their
hub's existing python-kasa connection.

The report names `end_scan_child_device` and `stopScanChildDevice` but does not
give complete parameters for both. No raw stop request is guessed. It gives
`set_inherit_info {is_inherit}`, but python-kasa has no inherit-settings API.
Separate stop and inherit tools are not implemented in this public-API area.
Local removal is distinct from cloud unbinding.

After successful pairing, refresh the hub and call `registry.discover()`, the
same rescan used by the existing tool. Return new devices using registry ids
and cleaned names. The shared registry retains removed child entries. Removal
marks the selected entry offline, but shared refresh pruning is needed to stop
a later hub refresh from reviving that stale entry.

Merge also needs `package.json` to include `src/tolpo/areas/*.py` in its `files`
list. The current npm manifest ships root Python modules but omits areas. No
shared file was changed by this area.

## SoftAP plan

The owner supplies a `plan` object with these fields:

| Field | Meaning |
| --- | --- |
| `target` | Short lowercase identifier used in the confirmation phrase. |
| `device_id` | Expected device id, supplied by the owner. Execution refuses another device. |
| `ap_host` | Device's local IPv4 address on its setup network. No default gateway is assumed. |
| `interface` | Host Wi-Fi interface, such as `en0` or `wlan0`. |
| `ap_env` | Environment variable NAME holding the device's open setup Wi-Fi name. |
| `ssid_env` | Environment variable NAME holding the home Wi-Fi name. |
| `wifi_password_env` | Environment variable NAME holding the home Wi-Fi password. |
| `account_env` | Environment variable NAME holding the owner's Tapo account name. |
| `account_password_env` | Environment variable NAME holding the owner's Tapo account password. |
| `wait_seconds` | LAN verification deadline, 1 to 180 seconds; default 90. |

The host saves its current network, joins the open setup AP, authenticates once
with the owner's own account, checks the expected SMART device id, sends home
Wi-Fi settings and account, leaves the AP, restores its previous network, then
rescans until the device is reachable on the LAN. SMART `wifi_join` sends
`set_qs_info` with the account. The device then binds itself. This flow makes
no app-signed cloud request. LAN verification does not prove cloud binding;
`cloud_connected` is included only when python-kasa exposes a boolean cloud
connection feature. The registry's discovery account must match the account
in the plan for LAN verification to work.

Passwords and Wi-Fi names are read only at execution after authorization.
Do not put their values in tool arguments. Responses omit credentials, tokens,
setup codes, SSIDs and hardware addresses. Kasa and registry logs are suppressed
in the task while provisioning calls run because debug requests may contain
credentials. Library and subprocess error output is not returned.

python-kasa 0.11 can try built-in or blank credentials after rejecting the
owner's credentials. This area installs context-aware guards around those
fallback values and cached fallback credentials. Protected calls stop with an
authentication error before a fallback login. The same guards cover state
refresh and registry rescans from these tools. They use no raw device methods
and do not reproduce credential material. These guards depend on python-kasa's
transport internals. Recheck them when upgrading the dependency. A native
owner-only authentication option in python-kasa would remove this workaround.

macOS uses `/usr/sbin/networksetup`. Linux uses `nmcli`. Neither uses a shell.
macOS must already remember the previous network and its credentials. Linux
restores the saved connection by UUID. A missing restorable connection stops
the flow before switching networks. Restoration runs after failure and
cancellation, and an unsuccessful restoration is reported. Provisioning
serializes host changes through LAN verification. A host without a Wi-Fi radio
or the required system tool cannot run this flow. The MCP connection should use
local stdio or another adapter, since changing host Wi-Fi can interrupt it.

Only SMART SoftAP devices which accept the owner's credentials are supported.
Factory devices needing default or derived credentials cannot be provisioned
here. Camera `wifi_join` remains available for already authenticated LAN
devices. python-kasa scans first when needed and uses the camera's own connect
flow. Camera SoftAP authentication and account binding are outside this flow.
SMARTCAM reset and reboot are refused even on hubs because inherited SMART
methods use the wrong dialect.

## Local registration and Matter

LAN discovery registers reachable owned devices locally without an app-signed
cloud call. Hub child pairing is also a local route. The app's distinct BLE
"Local Only Device" mode can skip Wi-Fi, then enable remote control later.
That onboarding mode needs a BLE client and is not implemented here.

A standard Matter controller on the owner's own fabric is a possible route
without the Tapo cloud certificate authority. Commission the device with the
owner's controller outside these tools. python-kasa has a read-only Matter
module, not a commissioning API. This area adds no Matter commissioning code,
fabric management, setup-code display or cloud-managed Matter fabric.

Not implemented: BLE and BCCP require a radio client and handshake work; TPAP
requires a new session client; signed cloud binding, proxy binding, ownership
transfer and account unbinding require app key material; camera label QR
binding uses those cloud calls; door locks and robot vacuums use BLE-only
onboarding; camera Easy Reconnect uses derived credentials; chime binding needs
pairing keys; TSS and fast onboarding have incomplete radio details. No default,
built-in or derived account credentials are used.

## Validation

Everything in this area was tested only with fakes. The fixture directory had
no response files. Public API signatures and return shapes came from the
installed python-kasa source. Test the hub pairing and removal on H100 and
H200, naming through a child connection, device radio scan and join on SMART
and authenticated SMARTCAM devices, SMART reset and reboot, and host restoration
on macOS and Linux before relying on them. Verify device self-binding and the
LAN/cloud state after SMART SoftAP setup. No physical device was contacted.

Safety checks were deliberately broken in scratch copies and caught by failing
tests: tier checks, per-call confirmation, hidden Wi-Fi values, flattened names,
name and environment-name validation, SSID byte limits, account matching,
SMARTCAM reset exclusion, expected device identity, excluded robot onboarding,
host restoration, cancellation shielding, private logs, error redaction, and
default-credential fallback guards. Each mutation was confirmed with a diff.

The socket-free Python suite excludes `tests/test_media.py`,
`tests/test_pictures.py` and the existing loopback ffmpeg connection test
`test_real_ffmpeg_failure_names_no_password`. That test reports `unreachable`
in this sandbox instead of its expected `refused`. Node launcher tests and
release metadata checks also passed. No MCP server or network scan was run.
