# Hubs, sensors and radiator valves

These tools use the existing local session and the owner's account. All execution was tested with fakes only.
Sanitized H200 siren and LED response bodies and empty T110/T300/T310 trigger logs are covered by the fakes.
Nonempty events follow the audit and python-kasa. No valve fixtures were available. Real-device verification is still required.

The owner's H200 with T110, T300 and T310 sensors can check these tools: `hub_alarm_config`, `hub_alarm_set`,
`hub_siren`, `hub_led`, `hub_led_set`, `hub_diagnostics_read`, `hub_events`, `hub_child_rename`, `sensor_settings`,
`sensor_alarm_set` and `sensor_siren` (T300), `sensor_comfort_temperature`, `sensor_comfort_humidity`, `sensor_mode`
and `hub_child_history` (T310), and `sensor_led`. The H100 paths, `sensor_sensitivity` (T100/T150) and every
`valve_*` tool need hardware the owner does not have.

- `hub_events`: Read recent child trigger logs with UTC time, optionally filtered by child and Unix timestamp.
- `hub_alarm_config`: Read siren settings, alarm state and available tones, including H100 chime tones.
- `hub_alarm_set`: Set a tone, volume or duration at level 2 without playing it. It refuses to mute the siren.
- `hub_siren`: Sound or stop the configured hub tone at level 3 with confirmation for each call.
- `sensor_settings`: Read one sensor setting and return only its public fields.
- `sensor_sensitivity`: Set T100/T150 sensitivity and detection interval at level 2.
- `sensor_alarm_set`: Set T300 siren volume and duration at level 3 with `set_alarm_config <sensor id>` confirmation.
- `sensor_siren`: Test the T300 siren at level 3 with `play_alarm <sensor id>` confirmation.
- `sensor_comfort_temperature`: Set T310/T315 temperature comfort thresholds and an optional local warning flag at level 2.
- `sensor_comfort_humidity`: Set T310/T315 humidity comfort thresholds and an optional local warning flag at level 2.
- `sensor_mode`: Set T310/T315 high sensitivity or balanced reporting at level 2.
- `sensor_led`: Set a supported child sensor's indicator LED at level 2.
- `valve_settings`: Read one KE100 setting or calibration and descaling status.
- `valve_timed_set`: Start or cancel timed boost or hold at level 2.
- `valve_defaults_set`: Store boost duration and optional temperature, or hold duration, at level 2.
- `valve_window_set`: Set open-window behavior and link T110 sensors on the same hub at level 2.
- `valve_maintenance_start`: Start mechanical calibration or descaling at level 2.
- `valve_option_set`: Turn early start or automatic descaling on or off at level 2.
- `valve_shutdown_set`: Set seasonal shutdown and anti-blocking at level 3 with confirmation.
- `valve_work_mode_set`: Choose gentle, standard or powerful motor behavior at level 2.
- `valve_night_set`: Set night mode with optional fixed local hours at level 2.
- `valve_frost_set`: Set the frost-protection minimum, 5 to 15 C or 41 to 59 F, at level 3 with confirmation.
- `valve_schedule_set`: Enable or choose the existing local heating schedule mode at level 2.
- `valve_schedule`: Read compact local schedule blocks without changing their temperature encoding.
- `hub_child_history`: Read the history stored on a T310/T315 (last 24 hours) or a KE100 (today and yesterday), with omitted counts when capped.
- `hub_child_rename`: Rename a sensor or valve at level 2, refresh its name, and keep its tolpo id stable.
- `hub_led`: Read H100 or H200 status LED settings.
- `hub_led_set`: Set H100 or H200 status LED behavior. Level 2 on an H100. Level 3 on an H200, with confirmation `setLedStatus <hub id>`, because a hub with its light off no longer shows that it works.
- `hub_diagnostics_read`: Read hub load limits and memory counters without identifiers or support logs.

`recent_events(registry, caller, hub, device=None, limit=50, after=None)` is the plain async polling function.
It returns frozen `HubEvent` values. It reads live logs instead of python-kasa's hourly cache.
Each poll asks every sensor and switch child of that hub for 100 recent records and returns at most 100 events, newest first.
UNVERIFIED: the owner's sensors accepted `page_size` 10 on empty logs. Whether firmware honors 100 is unknown.
Children of another hub are never read through this hub.
It does not paginate older history. Overlap polls and dedupe by `(device, id, timestamp)`.
The `after` filter is exclusive. A poll fails with the child's name when that child cannot be read.
Dial events keep the numeric `rotate_deg` value. Other arbitrary event parameters are omitted.
Check `get_trigger_logs` on T110, T100, T300 and S200B/S200D children behind both H100 and H200.

H100 uses `get_alarm_configure`, `get_support_alarm_type_list`, `set_alarm_configure`, `play_alarm` and `stop_alarm`.
Its volume choices are low, normal and high. Duration is 1 to 600 seconds.
H200 uses `getSirenConfig`, `getSirenTypeList`, `getSirenStatus`, `setSirenConfig` and `setSirenStatus`.
Its volume is an integer from 1 to 10, sent as text. Duration is 1 to 6000 seconds.
`hub_alarm_set` refuses mute (H100 `mute`, H200 volume 0). Muting the siren removes a safety net, and
`setSirenConfig` is level 2 in every area, so a level 2 tool must not do it. UNVERIFIED: python-kasa allows an
H200 duration of 0, but whether 0 means silence or no time limit is unknown, so tolpo refuses it.
The H200 fixture includes ten Doorbell Ring tones. Select one with `hub_alarm_set` and play it with `hub_siren`.
Confirmation is `play_alarm <hub id>` or `stop_alarm <hub id>` on H100, and `setSirenStatus <hub id>` on H200.
Check both families' settings, tone lists, audible start and stop, and forced state refresh on hardware.
Stopping the siren stays level 3 with confirmation. It silences an alarm that the hub's own rules may have raised,
which lowers protection. On the H200 start and stop are the same method, so they cannot have different levels, and
`stop_alarm` is level 3 in the doorbell area too. The refusal names the exact phrase, so a stop costs one more call.
The siren always has a duration limit, and the hub's button and the app can stop it too.
On the H100 an owner can allow `stop_alarm` alone. On the H200 that would allow starting too.
H200 dedicated doorbell ring and custom sound upload are omitted. The audit did not establish their hub wire shapes.
Standalone chime pairing and per-doorbell settings belong to the pairing and doorbell areas.

Sensor method and field names come from the audit and the app. None was tried on a device.
UNVERIFIED: the `sensitivity` field name is inferred from an unannotated app field.
`set_led_control` sends `led_onoff` as 0 or 1. Comfort setters can carry `notification`, a local setting.
They do not change cloud push rules. `set_report_mode` takes `high_sensitivity` or `long_battery_life`.
Motion detection interval is in seconds. UNVERIFIED: the real interval limits and the T300 siren duration limits
are unknown. Tools apply broad bounds (1 to 86400 and 1 to 600 seconds); firmware can refuse narrower ones.
The T300 siren settings are level 3 because muting or shortening the siren weakens leak warnings.
UNVERIFIED: T300 tone choices are unknown. The optional sound accepts only the current `get_alarm_config` type.
Check each sensor getter and setter on its stated model, including LED firmware support and persisted settings.
Check the leak siren test audibly and confirm that it does not retry a rejected request.

KE100 tools work through the child session behind either hub family. A KE110 room thermostat is not a KE100.
Timed boost and hold durations are seconds. Boost defaults use the valve's current unit and reported target bounds.
Open-window sensor links must be T110 children of the same hub. Returned links use local names, not radio identifiers.
Calibration and descaling start a mechanical cycle. Read their status with `valve_settings` afterward.
Shutdown is level 3 because `set_shutdown_info` can leave the valve fully closed and risk frozen pipes.
Use `set_shutdown_info <valve id>` or `set_frost_protection <valve id>` as the appropriate confirmation phrase.
The frost minimum is limited to the app's range, 5 to 15 C or 41 to 59 F.
Night hours are minutes after midnight in the device's local time and can cross midnight.
Schedule modes are `everyday` and `weekday_sat_sun`.
UNVERIFIED: weekday numbering for anti-blocking and fractional schedule temperature encoding are unresolved.
Anti-blocking time writes and schedule-block editing are omitted pending those checks.
Night mode supports fixed hours. Sunrise and sunset plans were not implemented.
Check every KE100 getter and setter on real hardware: persisted settings, timed expiration, maintenance status,
window sensor links, night hours, schedule mode and defaults, plus frost and shutdown behavior.

History tools return firmware arrays and `local_time`. They do not infer sample times, order, exception meanings,
or valve temperature scaling. Sensor arrays cover the local 24-hour window. Valve arrays cover two local calendar days.
The default cap is 48 values per array, in native order. Increase `max_points` up to 2880 to get more values.
Every shortened array has an `omitted` count. Empty arrays and private fields are left out.
Check `get_temp_humidity_records` on T310/T315 and `get_temperature_records` on KE100.
Confirm native array sizes, ordering, units and status encoding before drawing charts.

Rename sends a UTF-8 base64 `nickname` through `set_device_info`. It refreshes the hub after the write.
New names resolve after the device update. The existing tolpo id remains stable.
Check child rename and alias refresh on both hub families.
LED requests follow python-kasa's shapes. H100 preserves known night-mode fields when switching the LED.
Load diagnostics use `get_device_load_info` on H100 and `getDeviceLoadInfo` with `deviceLoad` on H200.
UNVERIFIED: the H200 parameter shape `{"deviceLoad": {}}` is a guess.
Check LED persistence and load counters on both families.
The audit's `getSubDevDiagnoseMode` is a model getter, not a confirmed wire method. Debug log reads are omitted.

Pairing, unpairing, setup-code reveal, cloud protection modes, cloud push rules, long history, H110 infrared,
and sub-GHz switches and relays are outside this page's scope.

Each safety rule (setting and alarm levels, confirmations, private fields, text cleaning, input checks, model checks,
cross-hub access, rename validation) was broken on purpose, and a test failed each time.

Checked on a real H200 with T110, T300 and T310 sensors on 2026-10-12: the alarm settings and tone list, diagnostics, the
event log (empty), the hub light and the sensors' settings reads. The hub light (`hub_led_set`, level 3 with the phrase
`setLedStatus <hub id>`) was turned off and on again. The siren, sensor setting writes and the valve tools were not tried.
