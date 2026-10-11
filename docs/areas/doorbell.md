# Doorbells and chimes

These tools use the owner's authenticated local connection. Writes stay deny by default.
Level 2 changes settings. Level 3 controls audible output, an electrical chime or the anti-theft alarm and requires the exact method and device
id confirmation phrase on every call. Every successful write refreshes device state.

- `doorbell_events_recent`: Poll last alarm, call state and child-scoped hub trigger logs as compact observations.
- `chime_settings_get`: Read a paired doorbell's chime tone, volume, duration, enable state and available tones.
- `chime_settings_set`: Set those per-doorbell chime settings at level 2.
- `chime_sound`: Play or stop a standalone chime or H100 sound at level 3 with confirmation.
- `doorbell_settings_get`: Read ring settings, hub/chime plans, in-wall chime, stored voice replies, auto response, anti-theft, wake-up, battery mode or button color.
- `doorbell_ring_sound_set`: Set the doorbell or connected hub ring tone and volume at level 2.
- `doorbell_ring_enabled_set`: Enable or disable ringing at the doorbell or its connected hub at level 2.
- `doorbell_ring_preview`: Play or stop a ring preview at the doorbell or its connected hub at level 3.
- `doorbell_ring_plan_set`: Set one hub or paired-chime ring window at level 2.
- `doorbell_existing_chime_set`: Set the type and ring length of an in-wall chime at level 3.
- `doorbell_quick_reply`: Play or stop a visitor voice reply at level 3.
- `doorbell_auto_response_set`: Switch automatic voice replies on or off, or choose the reply and its delay, at level 2. One change per call.
- `doorbell_anti_theft_set`: Arm or disarm the anti-theft alarm at level 3 with confirmation.
- `doorbell_wake_up_set`: Select detection or button press as the wake-up trigger at level 2.
- `doorbell_pre_wake`: Request pre-wake for a doorbell through its parent hub at level 2.
- `doorbell_battery_mode_set`: Select performance, battery life or maximum power saving at level 2.
- `doorbell_button_color_set`: Change the button LED color name at level 2.

All execution tests use fakes. No device or network connection was made during this build.
Sanitized fixtures arrived during the build. The H200 `getSirenTypeList` response shape matches them. Read fixture data is distinct from testing a write. Every write remains UNVERIFIED on hardware.
The D230 fixture contains generic camera settings, but no ring, reply, wake-up or anti-theft response.
There is no standalone chime fixture. All doorbell-specific response shapes remain UNVERIFIED.

`doorbell_events_recent` always returns `unverified: true`. Neither an alarm type nor a call state is known to prove a
physical button press. It does not receive cloud push or answer calls. Failures include the device, method and reason
beside any partial results. Policy refusals remain errors.

Each poll reads the latest alarm and one bounded trigger page. Use `cursor` as the next `since` Unix timestamp.
Call state is a snapshot with no event timestamp. Equal-second events, older pages and alarms replaced between polls
may be missed. There is no persistent queue. An agent chooses its polling interval and retains the observations.
The parent hub is selected automatically. An explicit hub must be that doorbell's parent.
H200 rejects direct `get_trigger_logs`. Sensor fixtures confirm child-scoped `get_trigger_logs` with `start_id` and
`page_size`, so the doorbell child path is used instead. The D230 fixture lists no `trigger_log` component, so this
read may fail on a D230. A failure shows as "device does not support this method" under `errors`.
`getLastAlarmInfo` uses the request and reply shape of the C225 and C610 fixtures. The D230 has no fixture for it.
UNVERIFIED: the `getCallStateInfo` shape, and which observations change when a D230 button is pressed. Check all three sources before and after a real D230 press behind H200.

Per-doorbell chime settings use the doorbell's cached address internally. No addresses, keys, passwords, setup codes or
raw device errors are returned. Device text is shortened and flattened. Unknown response fields are discarded.
A SMART chime/H100 uses the snake-case chime methods. H200 uses a camera `chime` wrapper and gets tones from
`getSirenTypeList`. These chime request wrappers match static protocol models, but remain UNVERIFIED on hardware.
Check `get_chime_alarm_configure` and `set_chime_alarm_configure` on each model and confirm only the selected doorbell's
configuration changes. UNVERIFIED: cached address separators, optional `func_type` behavior and model duration limits.

Chime volumes are `mute`, `low`, `normal` and `high`. Tones must be advertised. The 1 to 600 second chime bound comes
from python-kasa's SMART hub alarm module. Confirm sounds with `play_alarm DEVICE_ID` or `stop_alarm DEVICE_ID`.
A preview passes its tone, volume and duration with `play_alarm` and does not change the saved settings.
The H200 siren is the hub's own alarm sound. Read it with `hub_alarm_config`, change it with `hub_alarm_set` and sound
it with `hub_siren` (hub area). This area only reads the H200 tone list.

Reads ask for a section as a list, `{"<module>": {"name": ["<section>"]}}`, as the camera fixtures do.
UNVERIFIED: Doorbell ring and hub ring wrappers vary with component versions. The default doorbell wrappers follow
ring version 3. `legacy=true` selects older read, enable and preview wrappers without retrying failed writes.
Ring volume is an integer gear sent as a string. The API accepts 0 through 100, but that is not a claim that every model
supports that range. Older models use 0/1/2; choose a gear supported by the device. Ring type names must come from the
read tool. Check all `getRing*`/`setRing*` and `getHubRing*`/`setHubRing*` methods on the target model, including H200
forwarding to its D230 child. Confirm previews with `setRingPlay DEVICE_ID` or `setHubRingPlay DEVICE_ID`.

Ring plans accept one `HHMM-HHMM,WEEKDAY_MASK` window with valid clock values and a mask from 1 to 127.
The app's default is `0000-0000,127`. UNVERIFIED: weekday bit order, overnight/equal-time interpretation and whether
one window replaces other windows. Preserve a plan read from the device until verified. Check `getHubRingPlan`,
`setHubRingPlan`, `getChimeRingPlan` and `setChimeRingPlan` on hardware. Multiple-window editing is deferred.

In-wall chime settings are electrical. Wrong values can strain a coil. Only `mechanical` and `digital` are offered.
The API bounds ring length to 0 through 10. UNVERIFIED: length units, valid per-model range and whether zero means
use a default. The app uses zero during installation. Verify `getExistingChimeCfg` and `setExistingChimeCfg` on a wired
model using its installed chime specifications. Confirm with `setExistingChimeCfg DEVICE_ID`. Bypass setup is deferred.

`doorbell_settings_get` with `quick_replies` lists stored voice reply ids, names, durations and read-only flags.
Voice play and automatic replies require an id listed by `getQuickRespList`. Playback does not force replacement of
another reply. Confirm with `playQuickResp DEVICE_ID` or `stopQuickResp DEVICE_ID`. Check table shape, audible reply and
stop behavior on a real doorbell. Auto response configuration and enable are separate device settings. The tool takes
`enabled` alone or `reply_id` with `delay`, so each call is one write.
UNVERIFIED: `play_interval` units and valid range. The API bounds it to 0 through 600. Check `getSmartRespStatus`,
`getSmartRespConfig`, `setSmartRespStatus` and `setSmartRespConfig` on hardware.

Anti-theft is level 3. Arming it blocks a factory reset by button. Disarming it turns off an alarm that protects the
doorbell. Confirm with `setAntiTheftEnable DEVICE_ID`. Check `getAntiTheftEnable`, `getAntiTheftStatus` and `setAntiTheftEnable`
on a real doorbell. This area does not reset a device or control its anti-theft alarm directly.
UNVERIFIED: `getWakeUpConfig`/`setWakeUpConfig`, hub `setPreWakeup`, `getBatteryOperatingMode`/`setBatteryOperatingMode`
and `getLedColor`/`setLedColor` support and behavior on D230/H200. Wake-up trades battery life for latency.
Battery modes are `monitor_performance_first`, `battery_life_first` and `super_power_saving`; per-model support and
`follow_config` meaning need checking. LED color names are model-specific and remain UNVERIFIED.

Pairing is deferred. `getChimeCtrlKey`, `add_ring_key` and `addChimeCtrlDevice` are not declared or exposed.
No pairing key, bind code or registration-order fixture is available. The audit identifies both key exchange and BLE
onboarding, but does not establish a complete safe exchange for standalone chimes or H200. No derived or app key
material was read. No pairing secret is accepted by a tool or returned to an agent.
Calls, talk, cloud push, call screens, Do Not Disturb, sharing, locks, firmware and reset stay outside this area.

Twenty deliberate mutations were checked in scratch source copies. Each change was verified with a diff, and each
caused its targeted tests to fail. These covered owner levels, exact confirmations, method tiers, kind checks, hidden
fields, raw error secrecy, text flattening, integer and boolean checks, plain text, choice and schedule validation,
advertised tones, listed reply ids, strict MCP types, compound-action preflight, cached addresses, parent selection,
write refresh and the UNVERIFIED event label. The original files were unchanged by the mutation runs.
