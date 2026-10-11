# Robot vacuums

All vacuum tools are tested with fakes only. The owner has no robot vacuum.
No vacuum response fixtures were available. Requests follow python-kasa 0.11
and the app protocol audit. Model and firmware support still need real-device checks.
Every request uses the declared-method caller on the owner's existing local session.
No discovery, onboarding or transport is added here. There are no HTTP routes.

Start, pause, return, stop, targeted cleaning and task group starts are level 1. Cleaning
preferences, volume, dust collection, schedules and quiet hours are level 2.
Locate and child lock are level 3. Resetting a wear counter is level 4 because it
erases history. Level 3 and 4 calls require the exact device method and robot id
in `confirm`. For example, `setChildLockInfo floor-robot`.
A write reports the requested change after the device accepts it, then refreshes
state. A failed refresh is reported as stale state. It does not undo the write.

- `vacuum_status`: Read battery, cleaning progress, mop state, status and every reported error and prompt. Level 0.
- `vacuum_settings`: Read global cleaning settings and available dust collection, volume and child lock settings. Level 0.
- `vacuum_start`: Start a new standard whole-home job. Level 1. Use pause with `paused=false` to resume.
- `vacuum_pause`: Pause or resume the current job with `paused`. Level 1.
- `vacuum_return_home`: Return to the charging dock or cancel return with `returning`. Level 1.
- `vacuum_preferences`: Change any of the global cleaning preferences in one call. Level 2. Suction: 0 off, 1 quiet, 2 standard, 3 turbo, 4 max, 5 ultra. Water level: 0 off, 1 low, 2 medium, 3 high. Passes: 1 to 3. Cleaning type: 0 vacuum and mop, 1 mop, 2 vacuum, 3 vacuum then mop, 4 AI. Route density: 1 daily, 2 fast, 3 meticulous. Carpet boost: on or off. Only the given ones change. Each is sent as its own request, the shape python-kasa uses. If one fails, the error names the ones already changed.
- `vacuum_empty_bin`: Run dust collection at a compatible dock. Level 2.
- `vacuum_auto_empty`: Enable or disable automatic emptying and choose 0 smart, 1 light, 2 balanced or 3 max. Preserve known frequency. Level 2.
- `vacuum_volume`: Set speaker volume from 0 to 100. Level 2.
- `vacuum_locate`: Play the seek-me sound. Level 3. Confirm with `playSelectAudio <robot id>`.
- `vacuum_child_lock`: Lock or unlock physical buttons. Level 3. Confirm with `setChildLockInfo <robot id>`.
- `vacuum_consumables`: Read wear in minutes and estimated remaining time. Level 0. Lifetimes are python-kasa assumptions, not device guarantees.
- `vacuum_reset_consumable`: Reset one replaced or cleaned part's wear counter. Level 4. Confirm with `resetConsumablesTime <robot id>`.
- `vacuum_records`: Read totals, last clean and up to 32 history records. Level 0. Times can reveal presence patterns.
- `vacuum_stop`: End the current job with `setSwitchClean` and `clean_on=false`. Level 1. UNVERIFIED on a device. The decompiled app's stop sends the same request.
- `vacuum_spot`: Start mode 2 around the robot's current position. Level 1.
- `vacuum_clean_rooms`: Start mode 3 in selected rooms on an explicit map. Level 1. Uses global preferences.
- `vacuum_zones`: Start mode 4 in up to 16 rectangles with per-zone preferences. Level 1. Requires known map coordinates and units.
- `vacuum_preset`: Start mode 5 with an existing custom rule id and its map id. Level 1.
- `vacuum_task_group`: Start an existing saved task group with `startCleanTaskGroup`. Level 1. This corresponds to mode 7 without guessing a mode-7 `setSwitchClean` payload.
- `vacuum_maps`: Read saved map metadata and the current map id. Level 0. Decode and flatten map names.
- `vacuum_map`: Read one map's metadata and its room ids, names and preferences. Omit the map id for the current map. Level 0. The bitmap is not decoded or returned.
- `vacuum_schedules`: Read one schedule page from a start index, capped at 32 rules. Level 0. Weekdays stay as raw protocol values.
- `vacuum_set_schedule`: UNVERIFIED: add or edit a repeating room schedule with a raw weekday value and local start minute. Level 2. Uses vacuum-only defaults.
- `vacuum_remove_schedule`: Remove one existing schedule id. Level 2. Never removes all schedules.
- `vacuum_do_not_disturb`: Read quiet hours and the behavior bit field. Level 0.
- `vacuum_set_do_not_disturb`: Set quiet hours, including overnight windows. Level 2. Give a behavior bit sum, or omit it to keep the current one.

Suction and water values are protocol values. App labels can be offset by one.
Ultra suction resets after a job. Zone corners use the protocol key `vertexs`.
They must be four distinct integer coordinate pairs in perimeter order, forming
an axis-aligned rectangle. Defaults are standard suction, no water, one pass,
vacuum only, daily route and vacuum-only carpet cleaning. Coordinates are not
converted or inferred. The map reader cannot supply a decoded floor plan.

Consumable estimates use 400 hours for the main brush, 200 for the side brush and
filter, and 30 for sensors and charging contacts. Only these five counters can be
reset. Brush cover, mop cloth and tray counters are returned when present, without
inventing lifetimes or reset identifiers.

Schedules can start unattended motion. `week_day` is an integer protocol value,
not a named weekday. Its bit mapping is UNVERIFIED. The interpretation of `s_min`
as a device-local start minute is also UNVERIFIED. Verify both independently on
the model before creating or enabling a schedule. Add and edit return any reported
`conflict_id`; the tool never resolves a conflict by deleting another schedule.
The schedule wrapper sets standard suction, no water, one pass, vacuum only and
daily route. It does not implement one-off schedules or cloud copies.

Quiet hours suppress scheduled starts. Behavior bits are 1 no automatic resume,
2 voice prompts off, 4 dim LEDs, 8 no automatic emptying and 16 no automatic drying.
Add the bits to combine them. Start and end use minutes from 0 to 1439 in the
robot's local day. Equal times are rejected because full-day semantics are unclear.
The bit field travels under the key `display`. The decompiled app shows that key
through an unrelated library constant, so it is UNVERIFIED on a device. Tools show
it as `behavior`.

Map bitmap encoding is not sufficiently described by the audit. The text payload
and LZ4 length fields do not define its pixel layout or framing. The reader returns
metadata and room preferences only, states `bitmap_decoded=false`, and omits raw
rasters, positions, paths, overlays and arbitrary fields. Map names and cleaning
history are private household data that will be visible to the calling agent.
Lists and names are bounded. No passwords, tokens, setup codes, Wi-Fi names or
MAC addresses are returned.

The status table covers app codes 0 to 19 and python-kasa's error status 100.
The error table covers 1 to 45. The prompt table covers all 26 app codes, including
30, 31 and 33. Unknown codes retain their number and say `unknown`. Errors are not
reduced to the first entry. Code 8 means sleep, error 4 means wheel lifted and
error 15 means tilting, matching the app's names rather than older library labels.

Newer models start and stop through `runCleanTask` instead of `setSwitchClean`.
The decompiled app picks it by model. tolpo does not send `runCleanTask`, so start,
stop and targeted cleaning may not work on those models. The app's task mode 6 was
not found in a request, so there is no task start.

Not done: map edits, room split or merge, map deletion, no-go zone and virtual
wall writes, dock wash, dry or hair-cut, voice packs, map backup, manual drive,
TPAP, BLE onboarding, cloud passthrough, firmware and factory reset. Manual drive
has no proven bounded duration or safe stop behavior. Signed download features
need cloud URLs. No app keys, credentials or code are reproduced.

Real-device verification must check the camelCase methods on the specific model,
start and pause versus resume behavior, the stop command, zone coordinate
units, preset and task group starts, module-dependent dock and mop reads, schedule day
and time encoding and conflict behavior, the quiet window key `display`, quiet-hour
suppression, and whether preset names in records (`rule_name`) are base64 like map names. Test all
writes with the owner's own robot and account before relying on them.
