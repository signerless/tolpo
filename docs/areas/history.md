# Door and window history

A Tapo hub keeps no event history of its own for sensors that are connected to TP-Link's cloud. Asked the way the app
asks (`get_trigger_logs`), our H200 answered with an empty list and a sum of 0 for a T110 door sensor, and the app
then reads that sensor's history from the cloud. tolpo stays local, so it records the history itself.

- `recorded_history`: how often one door or window sensor opened and for how long, per day and per hour, the latest
  openings, whether it is open now, and the times when nothing was recorded. Reads the file only; no device is
  contacted.

## Recording

`tolpo record [SENSOR ...] [--every SECONDS]` reads the sensors every 5 seconds by default and appends each change to
`~/.local/share/tolpo/history.jsonl` (`server.history_file` or `TOLPO_HISTORY_FILE`). The file has mode 0600 and its
folder 0700. Without names it records every door and window sensor it finds. It runs until it is stopped and writes a
line when it starts, every ten minutes while it runs, and when it stops.

An opening counts only when the recording saw the door go from closed to open, and its length only when it also saw it
close again. A door that closed while nothing was being recorded has an opening of unknown length. The times when
nothing was recorded are a stopped recording, a computer asleep, or a sensor out of reach for over a minute, and the
tool lists them. Every time in the file is within `--every` seconds of the real change.

## What was checked

The tests cover the lines a recording writes, gaps after a missed reading, the file and folder modes, only door and
window sensors being accepted, counting only what was seen, the hole a crashed recording leaves, and the tool over MCP.
On the owner's home on 2026-10-13 the hub's empty log for the garage sensor was confirmed with the app's own parameters,
and `tolpo record garage` ran against the real sensor.
