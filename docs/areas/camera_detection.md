# Camera detections, alarms and local events

Camera and doorbell models expose different components. Every operation reads
`getAppComponentList` first. Missing features are refused. Detection writes use
level 2. Device requests go through the shared caller and writes force a refresh.

- `camera_detect_get`: Read one detection switch, sensitivity, reported range and available arming schedule.
- `camera_detect_set`: Change one detection switch or sensitivity within a reported range.
- `camera_detect_zones_get`: Read motion rectangles, crossing lines, intrusion areas or package polygons.
- `camera_detect_zones_set`: Replace those zones, including an empty list to clear them.
- `camera_detect_schedule_set`: Update weekday arming windows for line crossing or intrusion.

Detection names are `motion`, `person`, `pet`, `vehicle`, `line_crossing`,
`intrusion`, `tamper`, `baby_cry`, `bark`, `meow`, `glass_break` and `package`.
A switch works only when the component is present. Sensitivity also needs an
existing sensitivity field and a reported `sensitivity_range`. Unknown or absent
ranges are refused. Line crossing and intrusion may store sensitivity per zone.
Other detections have no separate arming schedule in the audited app.

Zones use integer image coordinates from 0 to 10000. Motion uses `x_coor`,
`y_coor`, `width`, `height`. Lines use `pt1_x`, `pt1_y`, `pt2_x`, `pt2_y`,
`direction`, `sensitivity`. Intrusion uses four point pairs, `sensitivity`,
`threshold`, `percentage`. Package uses `point_num` and three to eight point pairs.
The tool permits up to four zones as a conservative local bound. The camera may
impose a smaller limit. Schedules map weekday names to up to six ordered
`HHMM-HHMM` windows. Use an empty list to disarm a day. Unchanged days are preserved.

Tested only against a canned wire. Sanitized C225, C465 and C610 camera fixtures
and D230 doorbell configuration fixtures are included in the area tests. Their
component, detection, alarm and last-alert shapes take precedence over the audit.
D230 has no captured component response, so its test component list is synthetic.
Request names also come from python-kasa and targeted app builder inspection.
No device, network host, media channel or cloud service was contacted by this area.

UNVERIFIED: `getPersonDetectionCapability` and `getBCDCapability` method names
are audited. Their `{module: {name: ["capability"]}}` parameters and returned
`capability` sections need camera and doorbell verification. Other sensitivity
ranges are used only when the configuration read supplies them. Sensitivity
changes are refused when no usable range is returned. Ordinary detection reads
and switch-only writes do not depend on those unverified probes. Set
`include_ranges=true` on the getter to request them explicitly. Sensitivity writes
probe them automatically. None of the captured configs include a range.

Line direction values are `both`, `AtoB` and `BtoA`.

- `camera_alarm_get`: Read sound and light alert configuration and model capabilities.
- `camera_alarm_set`: Change supported alarm actions with level 3 confirmation.
- `camera_alarm_triggers_get`: Read the device's detection choices for alarm triggers.
- `camera_alarm_triggers_set`: Change one alarm trigger with level 3 confirmation.
- `camera_alarm_status`: Read the manual alarm status and remaining seconds.
- `camera_alarm_manual`: Start or stop the manual alarm with level 3 confirmation.
- `camera_alarm_light_get`: Read spotlight or floodlight settings and limits.
- `camera_alarm_light_set`: Change supported brightness or duration at level 2.
- `camera_alarm_light_switch`: Switch a bright spotlight or floodlight now with level 3 confirmation.
- `camera_events_recent`: Poll the last alert and optional bounded SD event metadata page.
- `camera_detect_notifications_get`: Read device-side notification choices by detection event.
- `camera_detect_notifications_set`: Change one device-side notification switch or urgency at level 2.

Alarm setters accept only fields the model reports. Boolean switches are `enabled`,
`sound_alarm_enabled`, `light_alarm_enabled`, `light_rbl_enabled`. `alarm_mode` is a
list of supported `sound` and `light` actions. `alarm_type` must be in the camera's
sound choices. `alarm_duration` is seconds, with a conservative tool limit of 1 to
600 and the model's duration support flag required. `light_type` is `flicker` or
`lighting` on models whose current field uses those names. The captured cameras
return legacy `light_type="1"`; its meaning is unverified. Writes to that numeric
variant require explicit `light_type_range` choices. No numeric-to-name mapping is guessed. No custom sound upload is implemented.

UNVERIFIED: the fixtures establish the camera volume value `high`, but supply no
volume range. Other values need a reported range.
`alarm_volume` writes require an additional explicit `alarm_volume_range` in the
returned capability. Its presence and shape need hardware verification. The range
may advertise text choices or numeric bounds. Without it, volume writes are refused.

Alarm and notification event names come from the device table. Setters use the
exact identifier from the getter and preserve other choices. Notification urgency
is `none`, `general` or `critical` when the device reports it. These tools change
only local switches. Push delivery still runs through the vendor cloud.

Level 3 confirmation phrases name the device id, rather than its display name:
`setAlertConfig <id>`, `setAlertEventType <id>`, `doManualAlarm <id>`,
`setForceWhitelampState <id>` or `manualFloodlightOp <id>`. Both start and stop need
confirmation. Light settings require the reported range or duration maximum.
Turning a physical bright light on or off uses level 3 as a conservative choice.

`camera_events_recent` normally calls `getLastAlarmInfo`. It returns the timestamp,
a recognized event name and numeric code when available. Empty or invalid timestamps
are omitted. Cached python-kasa `last_alert_timestamp` and `last_alert_type` appear
separately as `cached_last`; they can lag behind the fresh result.

To include SD metadata, supply both `start_time` and `end_time` as Unix seconds.
The window must be at most one day. `limit` is 1 to 100. A response with `next_index`
has another page; pass that value as `start_index`. Each page is sorted newest first
locally. The device's page order is not yet verified, so a page is not a promise of
the newest events across the whole window. Events contain timestamps and type
codes only. No clips, file identifiers, thumbnails or media are returned.
Unknown type codes keep their number and use `unknown`. The supported mapping
covers audited codes 1 to 19, 23 to 28, 30, 31, 33 and 36.

Real-device checks still required on both camera and doorbell kinds:

- Check the two detection capability probes and their sensitivity range encoding.
  Check D230 component discovery through its hub before relying on its tools.
- Check zone replacement and table row wrappers, package polygons, and model limits.
- Check arming windows serialize as quoted HHMM-HHMM arrays inside weekday strings.
- Check partial `setAlertConfig` writes preserve unspecified settings, sound choices,
  volume capability and the 600-second local duration bound.
- Check alert and notification tables, urgency support and preserved other choices.
- Check manual alarm start, stop and returned status section without repeated calls.
- Check spotlight and floodlight ranges, duration limits and manual actions.
- Check SD `searchDetectionList` time units, channel 0, inclusive page indices,
  `to_be_continued` and ordering. Check last-alert codes versus textual types.

Skipped: multi-lens channel selection, advanced global detection zones, battery
alarm dialect, alarm schedules, separate manual-alarm settings, floodlight schedules
and trigger filters. Those need additional model-specific shapes or fixtures.
Other camera areas, custom audio and cloud delivery remain outside this area.
No HTTP routes or shared Python changes are required. Before an npm release,
the lead needs to add `src/tolpo/areas/*.py` to `package.json` files. Its current
`src/tolpo/*.py` pattern does not include area modules.

Safety mutations were run in scratch copies inside this clone. Each change was
confirmed with `diff`. The tests failed when level checks, confirmations, hidden
field filtering, event metadata projection, raw error redaction, unknown event
labels, text cleaning, integer and boolean validation, component gating, capability
flags, reported ranges, strict MCP argument types or forced refresh were weakened.
Tests also caught forced unverified probes on ordinary reads and guessed legacy
alarm light encodings.
The scratch copies were removed and the production source was unchanged.

The requested full suite had one existing sandbox failure in
`test_real_ffmpeg_failure_names_no_password`. That test attempts a localhost socket
and expects connection refusal; this sandbox reports an unreachable connection.
It was left unchanged. The final suite also excludes that socket test, besides
`tests/test_media.py` and `tests/test_pictures.py` as the build brief requires.

Final validation: 257 area tests passed. The sandbox-compatible Python suite
passed 391 tests with one additional socket test deselected. All 16 deliberate
safety mutations failed their selected tests. No production mutation was retained.
