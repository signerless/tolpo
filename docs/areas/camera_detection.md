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

Zones use integer image coordinates from 0 to 10000. The D230 motion rectangle
confirms that scale for motion only. Motion uses `x_coor`,
`y_coor`, `width`, `height`. Lines use `pt1_x`, `pt1_y`, `pt2_x`, `pt2_y`,
`direction`, `sensitivity`. Intrusion uses four point pairs, `sensitivity`,
`threshold`, `percentage`. Package uses `point_num` and three to eight point pairs.
The tool permits up to four zones as a conservative local bound. The camera may
impose a smaller limit.

UNVERIFIED: the zone read parameter, whether a zone write replaces or appends, what
an empty list means to the camera, the point scale for lines, areas and packages,
the 0 to 100 bounds of zone `sensitivity`, `threshold` and `percentage`, and the
three to eight package points. UNVERIFIED: each weekday of an arming schedule is
sent as a JSON list of `HHMM-HHMM` strings. Schedules map weekday names to up to six ordered
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
- `camera_detect_notifications_set`: Change one device-side notification switch or urgency with level 3 confirmation.

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

UNVERIFIED: `camera_alarm_get` asks for `capability` in the same `getAlertConfig`
read, as the audit describes. The owner's cameras were read without it. If a camera
does not return it, every write that needs a capability flag is refused.
UNVERIFIED: the 1 to 600 second duration bound. The owner's cameras report `"0"`.

Alarm and notification event names come from the device table. Setters use the
exact identifier from the getter and preserve other choices. Notification urgency
is `none`, `general` or `critical` when the device reports it. These tools change
only local switches. Push delivery still runs through the vendor cloud.

Level 3 confirmation phrases name the device id, rather than its display name:
`setAlertConfig <id>`, `setAlertEventType <id>`, `setMsgPushEventList <id>`,
`doManualAlarm <id>`, `setForceWhitelampState <id>` or `manualFloodlightOp <id>`.
Notification choices are level 3 because they decide whether the owner is warned.
Both start and stop need confirmation. None of the owner's cameras report the
`manualAlarm` component, so the manual alarm is refused on them.
UNVERIFIED: the manual alarm status parameter and body, the light read parameters,
and the `force_wtl_state` and floodlight `action` values. Light settings require the reported range or duration maximum.
Turning a physical bright light on or off uses level 3 as a conservative choice.

`camera_events_recent` normally calls `getLastAlarmInfo`. It returns the timestamp,
a recognized event name and numeric code when available. Empty or invalid timestamps
are omitted. The owner's cameras report the last alert type as text, for example
`motion`. Only names from the known list are returned; anything else is `unknown`. Cached python-kasa `last_alert_timestamp` and `last_alert_type` appear
separately as `cached_last`; they can lag behind the fresh result.

To include SD metadata, supply both `start_time` and `end_time` as Unix seconds.
The window must be at most one day. `limit` is 1 to 100. A response with `next_index`
has another page; pass that value as `start_index`. Each page is sorted newest first
locally. The device's page order is not yet verified, so a page is not a promise of
the newest events across the whole window. Events contain timestamps and type
codes only. No clips, file identifiers, thumbnails or media are returned.
UNVERIFIED: the SD page request (channel 0, inclusive indices, seconds). The reply
fields match the app.
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
No HTTP routes.

## What was checked on real devices

On 2026-10-12, reads on a C225, a C465, a C610 and a D230: every detection type the camera reports, the zones, the alarm
settings and the last alert. The motion detection switch of the C225 was turned off and on again with `camera_detect_set`.
A change of the motion sensitivity was refused because the camera does not report a usable range, which is the intended
behavior. The D230 (behind the hub) reports its components through the hub; most detection and alarm methods are not
available on that model. No alarm, light or notification setting was written.
