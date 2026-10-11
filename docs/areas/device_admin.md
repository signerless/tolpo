# Device administration, firmware, rooms and groups

All device traffic uses the shared guarded caller. Reads are level 0. Settings are level 2.
Changing automatic firmware updates is level 4 and needs `confirm="set_auto_update_info <device slug>"`.
When automatic updates are on, the device installs firmware and reboots by itself. When they are off,
it stops getting fixes. Both are firmware lifecycle changes.
Firmware installation is level 4 and needs `confirm="fw_download <device slug>"` plus the exact
available version. It checks that version again before starting. The device fetches the image itself.
The tool reports a start request, not a completed upgrade. Read progress separately. A device may
reboot before refresh succeeds; that is reported without retrying the write.

- `device_rename`: Rename a SMART device, camera or camera protocol hub. The current tolpo slug stays usable.
- `device_set_metadata`: Set a SMART built-in avatar identifier or location coordinates. Coordinates are not returned. UNVERIFIED write shape.
- `device_get_clock`: Read the SMART or camera protocol clock and time zone.
- `device_set_clock`: Set SMART time and an IANA time zone. Omit the timestamp to use now. Devices using the camera protocol support only the zone.
- `device_get_led`: Read SMART cached LED settings or the camera protocol LED on/off state.
- `device_set_led`: Set always, never or a SMART custom night schedule with local minutes from midnight. Level 2 on a SMART device. Level 3 on a camera or hub that speaks the camera dialect, with confirmation `setLedStatus <device id>`, because a camera with its light off no longer shows that it records.
- `firmware_get`: Read SMART versions, automatic update settings and any locally cached release notes.
- `firmware_check`: Ask a SMART device to check for an update. Read its returned version and release notes.
- `firmware_progress`: Read SMART firmware download and upgrade state without polling or retrying.
- `firmware_set_auto_update`: Set the SMART auto-update switch, start minute and random spread, after exact device confirmation. Omitted window fields stay unchanged.
- `firmware_install_update`: Start one SMART firmware install after exact device confirmation and a version check.
- `device_diagnostics`: Read versions, available signal and runtime counters and up to 100 components. Export no logs.
- `device_third_party_compatibility`: Explain the account compatibility switch and report that its value cannot be detected locally.
- `room_list`: List tolpo's local rooms and saved device slugs.
- `room_create`: Create an empty local room.
- `room_rename`: Rename a local room while keeping its members.
- `room_delete`: Delete a local room without changing devices.
- `room_assign`: Replace room members with exact device references. An empty list clears the room.
- `room_set_power`: Set power for every light, plug or switch in a local room. Report individual failures.
- `group_list`: List tolpo's local groups and saved device slugs.
- `group_create`: Create an empty local group.
- `group_rename`: Rename a local group while keeping its members.
- `group_delete`: Delete a local group without changing devices.
- `group_assign`: Replace group members with exact device references. An empty list clears the group.

SMART firmware requires python-kasa's `Firmware` module. Automatic updates require component
version 2 or later. Module checks reject unsupported devices before sending methods. Legacy IOT
request envelopes are not implemented here. Icons use the app's `avatar` field. Location uses integer
degrees multiplied by 10000. It affects sunrise and sunset behavior.
UNVERIFIED: python-kasa writes neither the icon nor the location. The audit names `avatar` as a
`set_device_info` field, and python-kasa reads the coordinates with the 10000 scale. That the device
accepts `latitude` and `longitude` in `set_device_info` is a guess until it is checked on a device.

SMART LED reads use cached module data. python-kasa rate-limits `get_led_info` because frequent
queries can crash P100 devices. Custom schedules preserve the existing flat or nested `night_mode`
layout and use `night_mode_type="custom"`. Devices without a known schedule layout refuse schedule
writes. Model-specific auto/toggle rules, LED brightness and sunrise offsets are not added here.

Rooms and groups are tolpo data, separate from the Tapo app's cloud homes, rooms and groups.
The file is `~/.local/state/tolpo/rooms.json`. `TOLPO_STATE_DIR` overrides the directory.
Local file writes are level 1. Names are flat and case-insensitive. Devices are saved as slugs.
Assignments resolve exact references before saving. A device can belong to one room and several
groups. A rename keeps the current session's device slug; a new discovery session can assign a
new slug after a device alias change. Reassign stale room members explicitly.

The version 1 JSON file has `rooms` and `groups` objects mapping names to slug lists. Writes use
an exclusive file lock and atomic replacement with mode 0600 on Unix systems. Windows has no such
lock, so there the replacement stays atomic but two tolpo processes writing at once can lose a change. Invalid or unreadable
files are refused and never silently replaced. Each collection and member list is limited to 256.
`resolve_room(name) -> list[str]` is a plain function other areas can import without shared edits.
Room power checks all members before writing and refuses cameras, doorbells, hubs and sensors.
Device failures during execution can still leave a partially changed room and are reported per device.

Diagnostics allow only version, signal, runtime and component fields. `on_time` is an on-duration
counter, not proof of uptime. Fields missing on a model are omitted. Standalone camera diagnostics
use basic
info and app components. H200 diagnostics use cached negotiation responses. Child cameras
use cached parent state, including available signal and uptime. SMART children use parent
component data. SMART component negotiation uses `component_nego`, as the real fixtures show.
Names and release notes are
flattened and bounded. Wi-Fi names, setup codes, MAC addresses, tokens, credentials and unknown
nested fields never enter these responses.

The account switch `homeAssistantCompatibility` may gate local access. The audit establishes no
local read for its account value. A successful session or rejected authentication does not identify
that value. This tool reports unknown without probing credentials or calling the cloud.

## Unsupported firmware families and other boundaries

python-kasa has no camera or doorbell firmware module. The app uses `checkFirmwareVersionByCloud`,
`startFirmwareUpgrade`, `getFirmwareUpdateStatus` and `getCloudConfig`, with `cloud_config` sections
`check_fw_version`, `fw_download`, `upgrade_info`, `upgrade_status` and `upgrade_process`.
H200 also uses the camera dialect and has no SMART firmware module.
Camera auto-update uses `auto_upgrade.common`. Their full request shapes are not established here,
so this area sends none of those firmware calls.

Locks have no python-kasa firmware module. App providers call `upgradeThingFirmware` and use
`DoorLockFirmwareDownloadState` and `AutoUpdateInfo`; exact wire methods are unconfirmed.
Robots have no dedicated firmware module. A robot can use the generic SMART firmware tools only
when python-kasa exposes an active `Firmware` module on it. This conditional path is tested with
a fake vacuum. Robot-specific transport and app provider calls remain unverified. This area
adds no robot-specific firmware methods or kind mapping. Hub-child firmware sequencing and battery
readiness must be checked on hardware. There are no batch installs or automatic retries.

Wi-Fi changes, reset, unpair, account actions, sharing, integrations, cloud catalogue access and
log export are outside this area. Cloud APIs need signing material not used by this implementation.
No shared files need changes for these tools. Other areas can opt into the room resolver by import.

## Verification

All new behavior is tested only with fakes. Complete synthetic response layouts match the
sanitized P115, C225 and H200 fixtures. Read shapes were also compared with L430C, other
cameras and D230. No real device or cloud host was contacted. Before release, verify SMART nickname,
avatar, coordinate scaling, time zone/DST behavior, flat and nested LED schedule layouts and update
windows on supported SMART lights, plugs, switches and hubs. Verify camera and H200 rename,
timezone, LED and
basic-info/component response shapes on cameras and doorbells. Check firmware installation on a
suitable SMART device, including version mismatch refusal, reboot, progress and refresh failure.
Sensor and hub-child support is conditional on the modules the library actually exposes.
The conditional vacuum firmware path also needs verification on hardware.

Safety checks were broken on purpose in scratch copies. Tests failed for all 14 mutations:
device setting levels, local write levels, firmware level preflight, confirmation, expected version,
hidden fields, text flattening and bounds, coordinate bounds, LED schedule validation, clock
validation, update window validation, local file validation, camera privacy exclusion and refresh
after writes. Each mutation was checked with diff. The original source stayed unchanged.

A review broke these rules again: the firmware install and auto-update levels, the install
confirmation and version check, the diagnostics allow-list, release note flattening, the camera
exclusion in room power, corrupt-file handling and the 0600 file mode. A test failed each time.
The whole suite passes, including the socket tests.
