# Lights and light strips

These tools address one light. Use the child id for the downlight or ring of a ceiling light.
Raw settings use the shared caller and the owner's method level. Settings require level 2.

- `light_get_defaults`: Read the power-on default and power-outage behavior, including supported outage choices.
- `light_set_defaults`: Change the power-on default or outage behavior while preserving the other setting.
- `light_list_rules`: Read schedules, countdown timers, or Away Mode rules. Schedules support a page start index.
- `light_add_rule`: Add a validated rule to the light or one light child.
- `light_edit_rule`: Replace a complete rule, including its existing id.
- `light_delete_rule`: Delete one rule by id.
- `light_enable_rule`: Enable or disable an existing rule, preserving its full state and timing.
- `light_next_event`: Read the next schedule, timer, or Away Mode event for one light or child.
- `light_features`: List built-in effect choices, fade settings, strip length, and LED when supported.
- `light_set_effect`: Apply a built-in strip effect or bulb Party or Relax through python-kasa.
- `light_set_transition`: Set on/off fade seconds, smoothing on change, or the older all-fades toggle.
- `light_set_length`: Set a strip's lit length in 10 cm segments within its reported feature range.
- `light_set_led`: Set the indicator LED on a light that exposes it.
- `light_custom_effect`: Apply a validated classic strip effect or static color painting object.
- `light_get_auto_white`: Read the Auto-White enable flag and mode.
- `light_set_auto_white`: Enable or disable circadian, light compensation, or light tracking Auto-White.
- `light_get_presets`: Read saved brightness or color states, including per-child presets.
- `light_edit_preset`: Edit one stored preset by zero-based index and return its previous value.
- `light_get_segment_effect`: Read decoded Lighting Effect V2 metadata without its undecoded states or display colors.

Pass a `settings` object with `type` (`custom`, `last_states`, `always_on`, `smart_status`, or
`toggle_states`), `state`, `on_off`, or `re_power_type`. Outage choices are `always_on`,
`always_off`, `last_states`, and `toggle_states`, subject to the device's reported capabilities.
A custom state accepts brightness 1 to 100, color temperature 1500 to 9000 kelvin (zero for color),
hue 0 to 360 and saturation 0 to 100 together. White bulbs may use
`brightness: {"type": "custom", "value": 30}` instead of `type` and `state`.

UNVERIFIED: The default-state fields are decoded. The `set_device_info` write path needs confirmation
on a real bulb, strip, and ceiling-light child. Sanitized L430C read fixtures arrived during this build
and were matched in the fakes. All writes and strip/child operations were tested only with fakes.
No device or network host was contacted by this implementation agent.

## On-device rules

`kind` is `schedule`, `countdown`, or `antitheft` (Away Mode). All rule writes require level 2.
Rules take a complete object, with `enable` as a boolean. Edit also requires `id`.
Times are minutes after midnight, from 0 to 1439, in the device's local time zone.
Use `mode: "REPEAT"` and a `week_day` mask from 1 to 127.
UNVERIFIED: bit 0 is taken to be Sunday and bit 6 Saturday. No real rule has confirmed the order.
For `mode: "ONCE"`, use `week_day: 0` and a valid `year`, `month`, and `day`.

Schedules use `s_type` (`NORMAL`, `SUNRISE`, or `SUNSET`), `s_min`, and `desired_states`
with `on` plus the light-state fields above. The older `s_action: "on"` or `"off"` is also accepted.
Optional `e_type`, `e_min`, and `e_action` describe an end action. `e_type: "NONE"` means no end action.
`time_offset` is a solar offset in minutes, from -720 to 720. A normal time has zero offset.
Example: `{"enable":true,"mode":"REPEAT","week_day":62,"s_type":"NORMAL","s_min":480,
"desired_states":{"on":true,"brightness":40}}`.

Countdown rules use `delay` in seconds, from 1 to 86400, plus `desired_states` with `on`,
or the older `action: "on"` or `"off"`. `remain` is read-only.
Away rules use `start_min`, `end_min`, `start_time_type`, `end_time_type`, and `frequency` in minutes
from 1 to 1440. Overnight windows are accepted. Solar times accept `start_time_offset` and
`end_time_offset`. The frequency bound is a conservative local validation bound, not a reported device limit.

UNVERIFIED: The exact wire serialization of schedule and Away Mode enums needs a real fixture.
Uppercase values follow the decoded enum beans. Check those values and weekday ordering on a SMART bulb.
UNVERIFIED: `remove_antitheft_rules` is inferred from a misspelled app string.
Countdown and Away Mode removal use the decoded schedule removal object. Confirm both on-device.
No method name for Auto Biorhythm is guessed or exposed.

## Python-kasa controls

The existing feature permissions apply. These tools do not change `allow_write` or the default writable list.
Effects require `light_effect`. Fade options use `smooth_transition_on`, `smooth_transition_off`,
`smooth_transition_change`, or the older `smooth_transitions`. Strip length uses `strip_segments`. LED uses `led`.
Only supported features are listed. Device bounds also apply. A zero fade duration disables that fade.
Built-in effects come from python-kasa's `Module.LightEffect`, including its 17 classic strip definitions
and the bulb's Party and Relax choices. Exact names come from `light_features`, including any renamed bulb effects.
Leave out brightness to keep the light's current brightness, and transition to keep the effect's own timing.

Custom effects require `id`, a single-line `name`, and `type` (`static`, `sequence`, `random`, or `pulse`).
`custom` is 1 and `enable` is 0 or 1. Brightness defaults to 50. Animated transitions default to 1000 milliseconds.
HSV colors use `[hue, saturation, brightness]` triplets in `sequence`, `display_colors`, `init_states`, or `backgrounds`.
Hue is 0 to 360. Saturation and brightness are 0 to 100. Lists contain at most 128 entries.
Static paintings require increasing `segments` boundaries and one sequence color per boundary.
Example: `{"id":"my-paint","name":"My paint","type":"static","segments":[0,1],
"sequence":[[0,100,40],[240,100,40]]}`.
UNVERIFIED: Check static painting segment boundaries on a real strip. The app confirms static type and sequence
colors, but no real painting fixture was available.
Random effects require `hue_range`. Range fields contain ordered minimum and maximum values.
Optional classic timing, direction, spread, repeat, expansion, seed and segment-length fields are bounded.
Only an empty `trans_sequence` is accepted. Unknown fields and nested objects are rejected.
The tool applies an effect locally. It does not store a cloud library or overwrite a stored custom effect.

## Auto-White, presets, and Lighting Effect V2

UNVERIFIED: Auto-White uses `get_auto_light_info` and `set_auto_light_info` with `enable` and `mode`, as named
in the app. No real device has answered them yet.
Enabling requires one of `circadian`, `light_compensate`, or `light_track`. Disabling can omit the mode.
These writes require level 2. Auto Biorhythm node editing is skipped because its local methods are unconfirmed.

Preset editing requires level 2. It takes `index` and a `state` with brightness plus optional color temperature
or hue and saturation. It returns the previous value so the caller can restore it. Brightness-only devices
update their brightness list without losing other slots. Color devices update one indexed state.
UNVERIFIED: Per-child preset calls rely on python-kasa's child protocol. Confirm the ring and downlight on
a real ceiling light. Combined parent preset lists and combined schedules are not implemented without fixtures.

Lighting Effect V2 reads only id, cleaned name, type, enable, custom, brightness, segments, speed, carousel,
and device type. It omits mesh tokens and the undecoded `states` and `display_colors` arrays.
V2 template application is skipped: the report has no complete decoded local template or template ids.
The 43 recommended templates come from a cloud catalog, which is outside this task.
Classic built-in strip templates can be listed and applied through the python-kasa effect tools above.

The L430C fixture reports default states and seven state presets. It exposes on/off fades with a 60 second
maximum, but no effect or LED feature. Its `get_auto_light_info` call returns an unknown-method error.
It still lists an `auto_light` component, so a component flag is not proof of support.
`light_set_auto_white` reads the mode first and refuses before writing when the light does not know the method.
Any tool says plainly that the light does not support a method when the light answers that it is unknown.
V2 metadata is read from `get_device_info.segment_effect` when present, as on this bulb,
and otherwise from `get_segment_effect_rule`.

BLE, BCCP, mesh, encrypted Sync-to-Sound, Smart Sensing accessories, cloud libraries, groups, Smart Actions,
pairing, Wi-Fi changes, firmware, and key material are outside this area.

## Validation and integration

The area tests cover all 19 tools and all 19 declared methods through fakes.
L430C read shapes match the sanitized fixtures. Every write needs a later hardware check.
Check default-state writes and preset saves on a bulb, rule enums and removal on a SMART light,
effects and painting boundaries on a strip, and child defaults, rules, and presets on a ceiling light.
Check Auto-White on a model that supports its methods. The L430C reports the read method as unknown.

Twenty-three deliberate scratch mutations were caught by failing assertions. Each change was confirmed
with a diff. Checks covered method levels, feature permissions, integer and boolean validation,
unknown fields, identifiers, weekday masks, calendar dates, outage capabilities, hidden values,
text cleaning, device kind and child targeting, default and preset preservation, complete preset lists,
custom effect validation and support, device refusals, refresh failures, and omitted V2 encodings.
Scratch copies were removed. There are no level 3 or 4 methods in this area.
