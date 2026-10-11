# Local Smart Actions

Rules run on the tolpo host. They do not appear in the Tapo app. No cloud account or push service is used.
Keep tolpo running for rules to fire. Evaluation starts when the first rule is enabled. Enabled rules resume when the server restarts. Shutdown cancels the loop before the registry disconnects.

- `automation_create`: Create a validated local rule. The default is disabled.
- `automation_list`: List compact rule summaries and the kill switch state.
- `automation_show`: Show a rule summary without action parameters or condition values.
- `automation_enable`: Enable a rule and start the evaluation loop lazily.
- `automation_disable`: Disable a rule and cancel its pending actions.
- `automation_delete`: Delete a rule and cancel its pending actions.
- `automation_dry_run`: Read current observations and report what would fire now without writes or consumed events.
- `automation_tick`: Evaluate once and run due actions through the current owner policy.
- `automation_alerts`: Read the alert outbox. Optionally clear only the returned records.
- `automation_settings`: Replace timezone, location, polling interval, quiet hours and kill switch settings.

No HTTP routes are added.

## Rule format

Pass a `rule` object to `automation_create`. Its `id` is unique. `name` is descriptive text.
Triggers are ORed. Conditions use `condition_mode: "all"` or `"any"`. No conditions means true.
Each condition is a device predicate with `device`, `field`, `op`, `value` and optional `hold_seconds`.
Supported operators are `eq`, `ne`, `gt`, `ge`, `lt`, and `le`. Missing or unreachable readings never satisfy a predicate, including `ne`.
Hold times are observed on every poll. Missing observations reset them. Hold observations restart after server restart.

Trigger kinds:

- `clock`: `time` is `HH:MM`. `weekdays` uses Monday 0 through Sunday 6.
- `sunrise` and `sunset`: Use configured latitude, longitude and timezone. `offset_minutes` shifts the event. Polar days without the selected event do not fire. The offline calculation is approximate.
- `state`: A `predicate` fires on a changed reading that satisfies it. Optional hold time waits for the changed state to remain true.
- `sensor`: `device` identifies the sensor or button whose hub log is read. Optional `event` matches the exact log event string.
- `camera`: `device` identifies the camera. A changed `last_alert_timestamp` fires; optional `event` matches `last_alert_type`.
- `rule`: `rule` identifies another rule. It fires after that rule finishes successfully.

The first state, camera and log poll establishes a baseline. Historical events are not replayed.
Polling can miss short state changes or intermediate camera alerts. Sensor logs are read with `get_trigger_logs` and `start_id: 0`. Log ids must increase; after a device resets its counter, restart tolpo to establish a new baseline.

`windows` limits a rule to one or more `{start, end, weekdays}` ranges. Ends are exclusive.
An overnight range belongs to the weekday on which it begins. Equal start and end is an empty range.
The default `minimum_interval` is 60 seconds and the minimum is one second.
Clock events fire once in their selected minute. Missed minutes are not replayed. DST repeated minutes fire once per local date. Choose a polling interval below one minute for time rules.

Actions run in order:

- `power`: `device` and Boolean `on`, through the registry power policy.
- `light`: `device` and a `light` object with `on`, `brightness`, `color_temp`, or paired `hue` and `saturation`.
- `method`: `device`, a declared `method`, and optional `params`, through the guarded caller. Undeclared methods are refused.
- `tool`: Registered MCP `tool` name and `params`. The tool keeps its normal guards and input validation. Unknown tools and recursive `automation_` calls are refused.
- `delay`: Wait `seconds` before the following action. A delay cannot be the last action.
- `message`: Add a short `message` to the owner's outbox.

For tool actions, this area records tiers as later areas register their tools. Earlier tools without an exact tier use conservative levels: read 0, ordinary write 2, destructive write 4. Built-in power and light use level 1. Tools needing request context or resolver interaction are unavailable to unattended rules. A custom `ToolExecutor` adapter can integrate other host tools, but must recheck their policy on execution.
Security and lifecycle actions require a fresh confirmation passed to `automation_tick`. For methods, the phrase is `<method> <device slug>`. For tools, pass the phrase required by the tool's guarded caller. Confirmations are never stored. Background execution cannot confirm these actions. Delayed execution does not retain a previous confirmation. A direct tick can supply a new confirmation for a due action.
Power and light also require level 1 and the registry's feature policy. Their targets must be lights, plugs or switches. An opt-in camera state feature cannot turn these actions into a privacy change. A method or tool exception stops that run, appears in tick results, and is recorded in the outbox by exception type and target. Raw device errors and method responses are not exposed.

Example:

```json
{
  "id": "door-light",
  "name": "Door light",
  "enabled": true,
  "triggers": [{"kind": "state", "predicate": {"device": "main-door", "field": "is_open", "value": true}}],
  "actions": [
    {"kind": "power", "device": "desk-lamp", "on": true},
    {"kind": "delay", "seconds": 30},
    {"kind": "power", "device": "desk-lamp", "on": false},
    {"kind": "message", "message": "Door light completed"}
  ]
}
```

## State, safety and alerts

The version 1 rule store is `~/.local/state/tolpo/automation.json`. `TOLPO_STATE_DIR` overrides its directory.
Writes use a private temporary file, fsync and atomic replace. Invalid or unknown store versions fail closed.
Delayed action cursors, due times, cooldowns and consumed clock minutes survive restart.
Actions reserve their cursor before execution. A crash can skip an action, but does not deliberately replay it. This is at-most-once attempt behavior, not guaranteed delivery.
Use one tolpo server process per state directory. Concurrent processes and external edits are not coordinated.

Rule reference cycles are rejected at creation. Runtime ancestor chains and minimum intervals provide another guard.
A rule with pending delayed actions cannot queue another run. Disabling or deleting it cancels those actions.
Set `killed: true` with `automation_settings` to stop the loop, discard all pending runs and block manual ticks.
Settings replace the full settings object, so include any timezone, location or quiet hours you want to keep.

Messages and failures append to `automation-alerts.jsonl` in the same directory.
An MCP client can poll `automation_alerts` with `clear: true` and `limit: 100`, then deliver the returned records through its own owner-approved channel. There are no Tapo push notifications.
Quiet hours use the same window shape. Messages generated during quiet hours carry `quiet: true`; they remain in the outbox. The client should defer their delivery until the configured quiet period ends. Operational failures remain visible during quiet hours.
The JSONL file also lets a local owner process consume alerts. Coordinate consumers so that only one clears records.

Everything here was tested with fake devices and injectable clocks. No live network calls were made.
Hub logs follow python-kasa's sensor log shape (`logs` with `id`, `eventId`, `timestamp`, `event`). Real fixture files were unavailable at build time. UNVERIFIED: direct hub and switch support for `get_trigger_logs` needs model-specific hardware checks. Verify `get_trigger_logs` on hub children and button event names on hardware. Verify camera last-alert feature availability and power/light writes for each model.
The local engine substitutes for cloud Smart Actions. Device-native schedules, countdowns, Away mode, heating schedules and camera schedules are outside this package. Their writes need captured parameter shapes or their owning areas.
Geofencing, cloud sync, voice assistants, AI actions and push delivery are not implemented.

## Verification

Safety mutations in isolated scratch copies were caught by tests: action levels, fresh confirmations, hidden features, private parameter keys, light bounds, name flattening, raw error hiding, response hiding, cycles, the kill switch, cooldowns, dry-run isolation, private file modes, cursor reservation, camera privacy boundaries, RTSP refusal, MCP validation privacy, store versions and queue validation. Every mutation was checked with a diff before testing.
The prescribed Python suite reached a pre-existing localhost ffmpeg socket test whose expected error differs under the sandbox. With that additional socket test deselected, the allowed suite passed. Node launcher tests and release metadata checks also passed.
The npm file list needs explicit `src/tolpo/automation/*.py` and `src/tolpo/areas/*.py` entries before this package ships. No shared file was edited.
