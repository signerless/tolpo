# Plugs, strip sockets and switches

Select a strip socket by its own id or name. Its methods go through python-kasa's child protocol.
The strip parent is not a socket. Rule tools also accept wall switches.
Reads use level 0. Rules change unattended behavior and use level 2.
Timers, schedules and Away Mode can energize a load while nobody watches.

- `plug_rules`: List one page of countdown, schedule or Away Mode rules, with ids and page counts.
- `plug_countdown_set`: Add a countdown, or replace one by id. Set its delay in seconds and its power action.
- `plug_schedule_set`: Add or replace a fixed-time power schedule. Supply unique weekdays or one date.
- `plug_away_set`: Add or replace a random power window. Windows may cross midnight.
- `plug_rule_delete`: Delete one rule by id. Does not delete other rules.
- `plug_rule_enable`: Enable or disable one rule by id, or all schedules or Away Mode rules.
- `plug_settings`: Read one setting or status. Sections: `status` (overcurrent, overheat, protection trips, charging,
  running auto-off timer), `default_state`, `auto_off`, `charge_guard`, `power_protection`, `max_power`, `runtime`
  (minutes on today and over 7 and 30 days), `led` and `price`. Level 0.
- `plug_default_state_set`: Set last state, always on, always off or a reported toggle choice. Level 2.
- `plug_energy_usage`: Read energy, generation, runtime, cost and savings figures in their native structure.
- `plug_energy_history`: Read one page of energy or power history. Includes returned range timestamps.
- `plug_electricity_price_set`: Set a constant price in the device's integer scale. Level 2. UNVERIFIED price scale.
- `plug_energy_clear`: Permanently clear energy statistics. Level 3. Requires `clear_energy_data <device id>`.
- `plug_auto_off_cancel`: Cancel the running timer. Level 3. Requires `cancel_auto_off <device id>`.
- `plug_identity_set`: Rename a device or socket, or change its icon. Level 2.
- `plug_location_set`: Store latitude and longitude without returning coordinates. Level 2. UNVERIFIED write scale.
- `switch_settings`: Read state, delay, calibration, button actions, double click, sleep interval or LED settings.
- `switch_delay_off_set`: Configure delayed shut-off with the candidate v1 shape. Level 2. UNVERIFIED.
- `switch_double_click_set`: Enable double click and set its recognition interval. Level 2. UNVERIFIED unit and limits.
- `switch_calibration_set`: Set calibration thresholds and an optional curve id. Level 2. UNVERIFIED units and curve ids.
- `switch_low_power_set`: Set normal or low-power mode if the device reports the setting. Level 2. UNVERIFIED write.
- `plug_led_set`: Set the LED mode or a fixed night window. Level 2. UNVERIFIED fixed-window enum.

Times are device-local, in strict `HH:MM` form. Weekdays run from Sunday 0 to Saturday 6.
A date must be a valid `YYYY-MM-DD`. Countdown delays are 1 to 86400 seconds.
That is a conservative tool limit, not a claimed device limit.
An edit replaces the rule's supplied settings. The enable tool preserves the known fields of the existing rule.
Use the returned `sum` and `start_index` to request another page. Enable-by-id searches at most 64 pages.
Only schedule replies carry `sum` and `start_index` on the P115. Countdown and Away Mode replies are one page.
A reply's top-level `enable` is the family switch. The P115 reported it false with no rules.
UNVERIFIED: whether a new countdown runs while that switch is false. There is no countdown family switch tool.
No cloud fallback is used. An unsupported method or unreachable device is reported as an error.

Always-on after power loss can energize a heater. Select each socket deliberately.
Defaults with a reported `re_power_type_capability` use that capability. Legacy defaults use a custom on/off state.
History requests use UTC seconds and minute intervals. Energy accepts 60, 1440 or 43200; power accepts 5 or 60.
Each request covers at most 512 intervals. Use the returned timestamps for the next request.
Use `mwh` precision only on a device that supports `get_energy_data_mwh`.
Cost arrays retain their device ordering. Their period mapping, currency and price scaling are not established.
The price setter replaces time-of-use mode. Time-of-use writes are deferred because season ranges and slot encoding
are not verified. Reads keep summer and winter prices, periods and weekday/weekend slot arrays.
Protection polling returns a single current sample. It does not subscribe to push alerts or reset a tripped protection.
Select `charge_guard`, `power_protection` or `max_power` only when the model supports the corresponding read.
Cancelling auto-off may leave the load on longer. It needs level 3 and the exact confirmation phrase.
Names are encoded as python-kasa expects. Icon ids are model-specific.
UNVERIFIED: location is written in ten-thousandths of a degree, the scale python-kasa reads. No write was captured.
Only latitude and longitude are changed. Clock, region and timezone settings belong to another area.

Switch hardware tools accept wall switches and devices python-kasa classes as dimmers, including dimmer plugs.
Ordinary bulbs and plug sockets are refused for these tools. LED tools accept plugs and switches too.
Calibration writes accept thresholds from 1 to 100 and optional curve ids from 0 to 255.
These are conservative tool limits. Check the model's supported curve before supplying an id.
No calibration test is started. Double-click intervals are limited to 100 to 2000 in the candidate millisecond unit.
Delay writes only send the v1 enabled/state/time fields. Delay trigger selection is deferred.
Cooldown, DC supply, wiring state, button action mappings and sleep interval are read-only.
Their writable units or value enums are not established. Fan sleep state is included when reported.

## Verification

All tools were tested only with fakes. P115 fixtures arrived during implementation and determine the read shapes.
Rules use `rule_list`, `enable` and their family-specific `*_rule_max_count`.
Empty rule lists are omitted. A reported zero `sum` remains in the output.
Price reads preserve the nested seasonal configuration and 24 hourly slots per weekday/weekend array.
No switch, dimmer or power-strip fixtures were available.
The method names and field names come from the audit and the app's data beans.
UNVERIFIED: SMART rule mode and time enum numbers, the Away Mode frequency value and unit,
and rule write acceptance need device checks. Check a plug first, then each strip child and a switch.
UNVERIFIED: legacy `default_states.state.on`, v2 `smart_status` with `re_power_type`, history timestamp boundaries,
price scale and the cost-array period mapping need real plug checks. Verify each strip child separately.
Check that clearing energy data removes history and totals only after the exact confirmation phrase is supplied.
UNVERIFIED: `clear_energy_data` and `cancel_auto_off` are sent without parameters.
UNVERIFIED: delay-action v1 time units, calibration response and threshold units, curve ids, button action enums,
sleep interval units, `wiring_status` key, double-click interval units and LED `night_mode_type="time"`.
Check these on a wall switch and dimmer. Check LED writes on the P115 and low-power writes on a supported switch.
The supplied P115 fixtures cover reads only. Every write, Charge Guard read, history read and child routing
still needs lead verification on hardware. The child wrapper itself is tested with a fake parent protocol.
Confirm that add returns an id, edits retain the requested action, list paging advances,
delete affects only that id, and enable changes the requested rule.
Sunrise, sunset, paired start/end schedule actions and schedule runtime statistics are deferred.

Each safety rule (levels, confirmations, unknown fields, text cleaning, numeric and boolean validation, times and weekday
masks, rule ids, device and child targets, location and history bounds, strict types, refresh failures) was broken on
purpose, and a test failed each time.

Checked on a real P115 on 2026-10-12: the reads of rules, LED, default state and status; power off and on; a countdown
rule (created disabled, with the action equal to the current state, listed, then deleted); the LED mode (changed and set
back); and the power-on default (set to always on, which the device stores as type `custom` with `state.on` true, and set
back to `last_states`). Schedules, Away Mode, the electricity price, energy clearing, auto-off, protections, location and
the switch and dimmer tools were not tried on real devices.
