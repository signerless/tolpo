<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/signerless/tolpo/main/assets/logo-dark.svg">
    <img alt="tolpo" src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/logo-light.svg" width="280">
  </picture>
</p>

<p align="center"><strong>Local-first control of TP-Link Tapo smart-home devices for AI agents.</strong></p>

<p align="center">

[![CI](https://github.com/signerless/tolpo/actions/workflows/ci.yml/badge.svg)](https://github.com/signerless/tolpo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/signerless/tolpo/blob/main/LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

</p>

`tolpo` turns the Tapo devices on your network into a single interface that an AI agent, or
your own code, can read and control. It talks to each device directly over the LAN through the open
source [python-kasa](https://github.com/python-kasa/python-kasa) library, so no request ever leaves
your home and no vendor cloud sits in the path. The only credential it needs is the Tapo account the
devices already belong to.

It ships two interfaces over the same core:

- An **MCP server** that assistants such as Claude use as tools (`turn off the office lights`,
  `is any door open?`).
- An **HTTP API** with an OpenAPI description at `/docs`, for scripts and dashboards.

The aim is that everything the Tapo app can do on your network can also be done by the agent: lights, plugs,
hubs and their sensors, cameras, doorbells, robot vacuums, schedules, firmware, rooms, pairing, and even what
the devices say to the internet. That is a lot of power, so every action has a level, and only the first
levels are on until you raise them. See [What an agent can do](#what-an-agent-can-do) and the
[Security model](#security-model).

## Features

- Lights, plugs, the H100/H200 hub and its sensors, cameras and the doorbell, each with live state and a list of what
  can be changed.
- The parts of the Tapo app are split into areas, such as lights, plugs, cameras, doorbells and the network. tolpo
  remembers which kinds of device it found in earlier runs, and an area for a kind you have never had stays off.
- Every action has a level from 0 to 4. Reading and reversible control (power, brightness, color) are on. Settings,
  security changes and things that are hard to undo, such as pairing or a factory reset, stay off until you raise the
  level, and the last two kinds also need a confirmation phrase on every call. Wi-Fi names, setup codes, passwords
  and tokens are never returned.
- A camera you name in the config gives an agent a still picture, a short run of pictures or a saved clip, through the
  same video channel the Tapo app uses. No extra camera account is needed.
- Discovery combines a broadcast with a direct per-address sweep, remembers devices between runs and retries the ones
  that are slow to wake. When a device cannot be reached, the answer says which one and why.
- State is cached briefly and device names resolve at once, so an agent is not blocked on a slow hub.
- A full test suite runs against simulated devices, so it needs no hardware.

## Supported devices

tolpo works with whatever [python-kasa](https://github.com/python-kasa/python-kasa) can talk to, so the list is
longer than the hardware we own. The table keeps two things apart: what we have run against real devices, and
what python-kasa documents as supported but we have not tried.

| | Device | Tested here | Documented by python-kasa, not tried here | What an agent gets |
|---|---|---|---|---|
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/light.svg" width="36" alt="Light bulb"> | Color bulbs | L430C | L430P, L530B, L530E, L530EA, L535E | State, brightness, white temperature and color |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/plug.svg" width="36" alt="Smart plug"> | Smart plugs | P115 | P110, P110M | State, plus power, voltage and current readings |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/hub.svg" width="36" alt="Hub"> | Hubs | H200 | H100, and S200B and S200D buttons paired to a hub | The sensors paired to it show up as devices of their own, and its event log is readable. With a higher level: siren, sensor settings, pairing |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/contact.svg" width="36" alt="Door and window sensor"> | Door and window sensors | T110 |  | Open or closed, low-battery flag |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/climate.svg" width="36" alt="Temperature and humidity sensor"> | Temperature and humidity sensors | T310 | T315 (the one with a display) | Temperature, humidity, low-battery flag |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/motion.svg" width="36" alt="Motion sensor"> | Motion sensors |  | T100 | Motion, as python-kasa documents it. Not checked on real hardware |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/leak.svg" width="36" alt="Water leak sensor"> | Water leak sensors | T300 |  | Leak alert and battery level |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/camera.svg" width="36" alt="Camera"> | Cameras | C225, C610, C465 | C100, C101, C110, C200, C210, C220, C325WB, C460, C520WS, C720, TC40, TC65, TC70 | State, battery where there is one, last alert and detection switches. With a higher level: detection tuning, image and audio settings, pan and tilt, the memory card. Pictures and clips, for the cameras you enable |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/doorbell.svg" width="36" alt="Video doorbell"> | Doorbells | D230 | D100C, D130 | State and battery, pictures and live video through the hub. With a higher level: ring sounds, quick replies, anti-theft, wake-up |
|  | Robot vacuums |  | Models python-kasa has a cleaning module for | Status, start and stop, rooms and zones, suction and water, schedules. Built from the app's protocol and not tried on real hardware |
|  | Door locks |  | Not supported by python-kasa | Not usable yet. The DL100, DL105, DL110, DL130 and DLW10 speak a protocol python-kasa does not know. tolpo can parse their discovery record, and nothing is connected to it yet |

The C610 and the C465 are not on python-kasa's list yet, but both connected fine. The C465 needs the small
certificate fix in `compat.py`. The full upstream list is in
[python-kasa's SUPPORTED.md](https://github.com/python-kasa/python-kasa/blob/master/SUPPORTED.md).

A model that is not listed may still work, because tolpo hands the device to python-kasa and shows whatever it
reports. Run `tolpo scan` to find out. If a device works or does not, tell us through a
[device support issue](https://github.com/signerless/tolpo/issues/new?template=device_support.yml) so the table can improve.

## Requirements

- Python 3.11 or newer
- One or more TP-Link Tapo devices on the same network, already set up in the Tapo app
- Your Tapo account email and password
- [ffmpeg](https://ffmpeg.org), only if you want pictures from cameras
- [uv](https://docs.astral.sh/uv/) (or plain `pip`) to install

## Installation

### With npx

You need [Node](https://nodejs.org) and [uv](https://docs.astral.sh/uv/). Nothing is installed globally:

```bash
npx -y tolpo scan      # find your devices
npx -y tolpo mcp       # run as an MCP server
```

tolpo is a Python program. The npm package carries the Python code and runs it with uv, so the first run
downloads about forty Python packages and takes longer than later runs. If an AI client gives up waiting, run
`npx -y tolpo --help` once and start the client again.

### With Docker

For a machine that stays on, such as a Raspberry Pi or a Linux home server. The image is public, so pulling it
needs no login, and it is built for amd64 and arm64.

Keep the credentials in a file that only you can read, and pass it with `--env-file`:

```bash
(umask 077; printf 'TAPO_EMAIL=you@example.com\nTAPO_PASSWORD=your-password\n' > ~/.tolpo.env)

docker run --rm -i --network host --env-file ~/.tolpo.env ghcr.io/signerless/tolpo
```

That runs the MCP server over stdio. For the HTTP API, add a token, because it is no longer bound to loopback:

```bash
echo "TOLPO_TOKEN=$(openssl rand -hex 24)" >> ~/.tolpo.env

docker run -d --name tolpo --restart unless-stopped --network host --env-file ~/.tolpo.env \
  ghcr.io/signerless/tolpo serve --host 0.0.0.0
```

`--network host` lets the container find devices by UDP broadcast, and it only works on Linux. Docker Desktop on
macOS and Windows runs containers in a virtual machine that cannot see your devices that way, so use the npx
install there.

CI builds the image for both architectures and checks the MCP handshake inside it. Finding real devices from
inside a container has not been tried yet. The image has no ffmpeg, so camera pictures do not work in it yet.

### From source

```bash
git clone https://github.com/signerless/tolpo && cd tolpo
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
```

## Configuration

Credentials can come from environment variables or a config file. The server reads the file first,
then lets the environment override it.

```bash
export TAPO_EMAIL='you@example.com'
export TAPO_PASSWORD='your-password'
```

Or `~/.config/tolpo/config.toml` (run `chmod 600` on it, since it holds a password):

```toml
[tapo]
email = "you@example.com"
password = "your-password"
# or point at an existing JSON file instead of copying the secret:
# credentials_file = "~/.tapo_config.json"

[server]
host = "127.0.0.1"          # loopback only; a token is required to bind elsewhere
port = 8765
# token = "a-long-random-string"
# cache_ttl_seconds = 10
# areas = ["lights", "plugs", "hub"]   # which parts offer tools; empty means every part for the devices you own

[discovery]
# subnets = ["192.168.1.0/24"]   # default: the /24 of your default-route interface
# hosts   = ["192.168.10.20"]    # devices that ignore broadcast or live on another VLAN
# sweep   = true                 # also probe every address in the subnet (networks up to /22)
interval_seconds = 600

[policy]
# The highest level of action an agent may use, from 0 to 4. See the Security model.
tier = 1
# Single device methods an agent may use above `tier`, for example ["setLedStatus"].
allow_methods = []
# Features an agent may write beyond power, brightness and color.
# Plain ids ("pan_left") or kind-qualified ones ("camera.state").
allow_write = []

[cameras]
# Pictures are off for every camera until you name it here, as it is called in the Tapo app.
snapshots = []                    # for example ["Hallway Cam"]
# Optional. A camera account made in the Tapo app switches pictures to the standard RTSP stream.
# username = "camera-account-name"
# password = "camera-account-password"
# ffmpeg = "/opt/homebrew/bin/ffmpeg"   # only if ffmpeg is not on your PATH
# clip_dir = "/Users/you/tolpo-clips"   # the one folder where camera clips may be saved; no folder, no clips
```

Environment variables override the file: `TOLPO_CONFIG` (the file itself), `TOLPO_CREDENTIALS_FILE`, `TOLPO_TIER`,
`TOLPO_AREAS` (names separated by commas), `TOLPO_SNAPSHOTS`, `TOLPO_CLIP_DIR`, `TOLPO_FFMPEG`,
`TOLPO_CAMERA_USERNAME` and `TOLPO_CAMERA_PASSWORD`. A `tier` that is not a whole number from 0 to 4 stops the
server from starting.

Some parts keep their own files, so that no secret ends up in the main one. Automation keeps its rules and alert
outbox, and the local rooms and groups are kept in `~/.local/state/tolpo` (change it with `TOLPO_STATE_DIR`). The
network area reads `~/.config/tolpo/network.toml` (change it with `TOLPO_NETWORK_CONFIG`), which names the
environment variable that holds each router password and never holds the password itself.

Verify your setup with a read-only scan: `npx -y tolpo scan`, or `.venv/bin/tolpo scan` from a source checkout.

## Usage with Claude (MCP)

Put your credentials in `~/.config/tolpo/config.toml` (see Configuration, and `chmod 600` it). The server
reads that file by itself, so nothing secret ends up in your AI client's settings. Then register the server
once:

```bash
claude mcp add tolpo -- npx -y tolpo mcp
```

From a source checkout, point at your own copy instead:

```bash
claude mcp add tolpo -- $PWD/.venv/bin/tolpo mcp
```

If you would rather keep the credentials out of a file, pass them with `-e TAPO_EMAIL=... -e TAPO_PASSWORD=...`,
but your client will then store them in its own settings in plain text.

After the next restart you can ask things like "dim office-1 to 30%", "is any door open?" or "what is the
temperature upstairs?".

| Tool | What it does |
|---|---|
| `home_overview` | The whole house at a glance: what is on, open doors and windows, water alerts, offline devices, low batteries, temperatures |
| `list_devices` | Devices with state; filter by `kind` or name |
| `get_device` | Every feature of one device, and which ones are writable |
| `camera_snapshot` | One still picture from a camera you enabled |
| `set_power` | Switch lights and plugs; accepts several targets and globs like `office*` |
| `set_light` | Brightness, white temperature, or hue and saturation |
| `set_feature` | Any other feature, only when the policy allows it |
| `rescan` | Look for new or moved devices |

Read tools are marked read-only and `set_feature` is marked destructive, so MCP clients can ask for
confirmation where it matters. Responses are compact: all the devices of a typical home fit in under a
thousand tokens.

## What an agent can do

The Tapo app is split into areas. Each area is a group of MCP tools with its own page that lists every tool.
tolpo remembers the kinds of device it found in earlier runs, and an area for a kind you have never had is off, so a
house without a vacuum does not load vacuum tools. On a first run, or when a model is not recognized, every area is
on. Pick the areas yourself with `areas` under `[server]`, or `TOLPO_AREAS`. The tool `tolpo_areas` tells an agent
what is on.

| Area | What it covers | Tools |
|---|---|---:|
| [lights](https://github.com/signerless/tolpo/blob/main/docs/areas/lights.md) | Bulbs and strips: effects, presets, schedules, countdowns, Away Mode, power-on defaults | 19 |
| [plugs](https://github.com/signerless/tolpo/blob/main/docs/areas/plugs.md) | Plugs and switches: schedules, countdowns, energy history, electricity price, auto-off, protections | 21 |
| [hub](https://github.com/signerless/tolpo/blob/main/docs/areas/hub.md) | Hubs, sensors and radiator valves: siren, sensor settings, the door, leak, motion and button event log | 29 |
| [doorbell](https://github.com/signerless/tolpo/blob/main/docs/areas/doorbell.md) | Video doorbells and chimes: ring sounds, quick replies, anti-theft, wake-up, battery mode | 17 |
| [camera_detection](https://github.com/signerless/tolpo/blob/main/docs/areas/camera_detection.md) | Motion, person, pet and vehicle detection, zones, schedules, alarm and spotlight, what warns you | 17 |
| [camera_system](https://github.com/signerless/tolpo/blob/main/docs/areas/camera_system.md) | Pan and tilt, image, audio, battery, privacy mode, UPnP, firmware, reboot | 33 |
| [camera_media](https://github.com/signerless/tolpo/blob/main/docs/areas/camera_media.md) | Memory card, record plan, recording index, thumbnails of past events, clips saved to a folder | 11 |
| [vacuum](https://github.com/signerless/tolpo/blob/main/docs/areas/vacuum.md) | Robot vacuums: status, start and stop, rooms and zones, suction and water, schedules, wear counters | 27 |
| [lock](https://github.com/signerless/tolpo/blob/main/docs/areas/lock.md) | Door locks: read the state from the discovery record. Not usable yet | 1 |
| [device_admin](https://github.com/signerless/tolpo/blob/main/docs/areas/device_admin.md) | Names, clock, LED, firmware, and tolpo's own rooms and groups | 24 |
| [automation](https://github.com/signerless/tolpo/blob/main/docs/areas/automation.md) | A local rule engine in place of Smart Actions: triggers, conditions, actions and an alert outbox | 10 |
| [pairing](https://github.com/signerless/tolpo/blob/main/docs/areas/pairing.md) | Pair and remove hub devices, join Wi-Fi, reboot and factory reset. Most of it is level 4, a reboot is level 2 | 9 |
| [home_plan](https://github.com/signerless/tolpo/blob/main/docs/areas/home_plan.md) | The floor plan of the 3D view: draft it from your devices, read it, save it, and where to see it | 4 |
| [history](https://github.com/signerless/tolpo/blob/main/docs/areas/history.md) | How often a door or window opened and for how long, from what `tolpo record` saved | 1 |
| [network](https://github.com/signerless/tolpo/blob/main/docs/areas/network.md) | What each device talks to, seen through your router, DNS server or firewall, and optional blocking. Blocking needs level 4, `mode = "enforce"` in `network.toml` and a Linux host that routes the traffic | 10 |

On top of these, `home_overview`, `list_devices`, `get_device`, `set_power`, `set_light`, `set_feature`,
`camera_snapshot` and `rescan` work for every device.

All the areas together are 242 tools. Their names, descriptions and parameter lists come to about 40,000 tokens. The
standard setup switches on only the areas that match the devices found at home. A client that loads every tool at once
can use `areas` to keep only what it needs.

Most of this was built from analysis of the Tapo app and tested against simulated devices. Each area page says
which parts were tried on real devices and which were not. Where the exact form of a request was a guess, the
page marks it `UNVERIFIED`, and the tool refuses when the device does not report support for it.

## Usage over HTTP

```bash
npx -y tolpo serve          # or .venv/bin/tolpo serve from a source checkout
# http://127.0.0.1:8765, interactive docs at /docs
```

```bash
# Read
curl -s localhost:8765/devices                 # everything, with state and capabilities
curl -s localhost:8765/devices/front-door       # one device, every feature
curl -s "localhost:8765/devices?kind=sensor"    # doors, windows, water, temperature

# Write
curl -s localhost:8765/devices/hallway-cam/snapshot -o picture.jpg    # a camera you enabled
curl -s -X POST localhost:8765/devices/desk-lamp/power \
  -H 'content-type: application/json' -d '{"on": true}'
curl -s -X POST localhost:8765/devices/office-1/light \
  -H 'content-type: application/json' -d '{"brightness": 40, "color_temp": 3000}'
curl -s -X POST localhost:8765/batch/light \
  -H 'content-type: application/json' -d '{"targets": ["office*"], "brightness": 60}'
```

A device is addressed by its short `id` (`office-1`), by its name in the Tapo app (case-insensitive),
or, in the batch endpoints, by a glob such as `office*`. If a name matches several devices the server
returns `409` with the candidates instead of guessing.

### The house in 3D

`tolpo serve` also serves a 3D view of the house at `http://127.0.0.1:8765/home`. It draws the rooms, puts every
device in its room and shows its state: lights glow in their real color, a door or window drawn on the wall opens when
its sensor reports open, a water leak turns red, the temperature floats over its sensor. Click a device to switch it,
dim it or change its color. Click a camera or the doorbell to watch it live. Rooms without a device are left out.

The floor plan is yours and stays on your machine, in `~/.config/tolpo/home.json` (`server.home_layout` or
`TOLPO_HOME_LAYOUT` to move it). The easiest way to make one is to ask your agent to "build my home plan": with the
`home_plan_draft`, `home_plan_get` and `home_plan_set` tools it drafts a plan from your devices, asks you about your
rooms and saves it. You can also press `Edit layout` in the view, drag devices where they belong, and save. The format
is in [docs/areas/home_plan.md](https://github.com/signerless/tolpo/blob/main/docs/areas/home_plan.md):

```json
{"version": 1,
 "rooms": [{"id": "office", "name": "Office", "x": -7, "z": -5, "w": 5, "d": 5},
           {"id": "garden", "name": "Garden", "x": -9, "z": -12, "w": 21, "d": 7, "outside": true}],
 "devices": {"office-1": {"room": "office", "x": -5.75, "z": -3.75},
             "garden-cam": {"room": "garden", "x": 1.5, "z": -5.6, "yaw": 0}}}
```

Live video comes from `GET /devices/{id}/live`, a stream of JPEG pictures (about 10 per second) from a camera you listed
in `cameras.snapshots`. One stream per camera and two cameras at once. A view ends after 15 minutes, or after 2 minutes
on a battery camera, because live video keeps it awake. The page loads three.js from one pinned jsDelivr path and may
talk to this server only. It asks battery cameras for their state every five minutes, not every few seconds, so it does
not wake them.

### Door and window history

A Tapo hub keeps no history of its own for sensors connected to TP-Link's cloud: asked the way the app asks, an H200
answers with an empty list for a door sensor, and the app reads that history from the cloud instead. So tolpo records
its own:

```sh
tolpo record garage
```

It reads the sensor every 5 seconds (`--every` to change it) and saves each opening and closing, with its time, in
`~/.local/share/tolpo/history.jsonl` (`server.history_file` or `TOLPO_HISTORY_FILE`), a file only you can read. Leave
out the name to record every door and window sensor. It records only while it runs, so run it as a background service
for a history without holes; times when the computer slept or the sensor was out of reach are written down as gaps.
The `recorded_history` tool then tells an agent how often a door opened, for how long, per day and per hour, and
lists the times nothing was recorded instead of guessing about them.

## Camera pictures

An agent can ask a camera for one still picture. Every camera is off until you name it in the config, with the
name it has in the Tapo app:

```toml
[cameras]
snapshots = ["Hallway Cam"]
```

You also need [ffmpeg](https://ffmpeg.org) on the machine that runs tolpo. Check the setup yourself before an
agent uses it. This saves one picture to a file that only you can read:

```bash
npx -y tolpo snapshot "Hallway Cam" -o hallway.jpg
```

To watch a camera live on your own screen, use `npx -y tolpo watch "Hallway Cam"`. It opens ffplay, which comes with
ffmpeg, and closes when you close the window. `--seconds 30` stops it sooner. Nothing is recorded and nothing goes to
an agent.

tolpo takes one frame from the camera's live stream with ffmpeg and returns it as a JPEG, at most 1280 pixels
wide. `--quality high` asks for the main stream instead of the small one. There are two ways to reach the stream:

- The video channel of the Tapo app, on port 8800 of the camera. It uses the Tapo account tolpo already has, so
  there is nothing more to set up.
- RTSP, if you create a camera account in the Tapo app and add it under `[cameras]` as `username` and `password`.
  When a camera account is configured, tolpo uses RTSP.

What to know before you turn it on:

- A picture goes to the AI model your agent runs on. Enable only the cameras you are happy for it to see.
- A camera in privacy mode gives no picture. An agent cannot switch privacy mode off unless you allow
  `camera.state` in `allow_write`.
- Every picture is logged with the camera and the time, never with the picture itself.
- If a camera rejects the login, tolpo leaves it alone for five minutes, because repeated failures can lock a
  camera out.
- A picture is one frame from now, not a recording. Battery cameras can be asleep and may not answer.
- With RTSP, ffmpeg receives the camera account on its command line, so other users on the same machine can see
  it for a moment.
- A doorbell behind the hub has no address of its own. Its pictures and live video come through the hub, like the
  app's, and the first picture takes about 5 s while a battery doorbell wakes up. Its recordings are not reachable yet.

The `camera_media` area builds on this. It can read the memory card and its recording index, return the thumbnail
of a past event, take a short run of pictures from the live stream, and save a recording to the one folder you set
as `clip_dir`. Thumbnails, runs of pictures and clips follow the same rules as pictures: only cameras you named, never
in privacy mode, and the tool that saves a clip returns the path and never the video. Reading the card status, the
record plan and the list of recordings and events works for any camera. Formatting a card is a level 4 action.

## Security model

This server controls physical things, so it refuses by default and widens only on request.

Every action on a device has a level. You choose the highest level an agent may use with `tier` under `[policy]`
(1 by default), and you can allow single device methods above it with `allow_methods`.

| Level | Name | What it is | Examples |
|---|---|---|---|
| 0 | Read | Look at anything that is not secret | State, settings, energy, event logs, a memory card index |
| 1 | Control | Reversible, as you would do by hand | Power, brightness, color, a camera sweep, vacuum start and stop |
| 2 | Setting | Changes behavior | Schedules, detection tuning, defaults, renaming, automation rules |
| 3 | Security | Matters for safety or privacy | Camera privacy mode, a siren, alarm and arm state, a camera light turned off, UPnP, audio out |
| 4 | Lifecycle | Hard to undo | Pairing and removing devices, factory reset, firmware install, Wi-Fi change, firewall changes, formatting a memory card |

Levels 3 and 4 also need a confirmation phrase on every call, such as `setLedStatus hallway-cam`. The refusal says
what the phrase is, so the phrase is a pause for the agent to ask the person, not proof that the person agreed. The
phrase names the action and the device (a few name a change or a plan instead), so one confirmation cannot be reused
for another device or another action. A refusal says what the action is, and never how to widen the permission.

| Scope | Allowed |
|---|---|
| Read | Everything that is not secret. The Wi-Fi name, setup codes, passwords, tokens, keys and MAC addresses are never returned |
| Control | Power, brightness, color temperature, color and light presets, on lights and plugs, at level 1 |
| More | Whatever level you allow with `tier`, or single methods named in `allow_methods` |
| Features | Anything else written through `set_feature`, named in `policy.allow_write` (alarm test, pan/tilt, detection switches) |
| Pictures | Only from the cameras named in `cameras.snapshots`. Off by default. The other camera tools (settings, card index, event lists) work for every camera at the level you allow |
| Pairing | Only through the pairing tools, at level 4. The hub `pair` and `unpair` features stay blocked in `set_feature` |

Cameras are read-only through `set_feature`: the `state` switch on a camera is its privacy shutter, not a power
relay, so it is not treated as one. Enable it deliberately with `allow_write = ["camera.state"]`, or use the
privacy tool at level 3. A feature named in `allow_write` is allowed at any level and needs no phrase, so name only
what you mean to give away.

Rules that run without anyone asking (automation) are held to the same levels at the moment they run, not only when
they are made. Setting them up needs level 2. An agent can stop all rules at any time, but starting them again
needs level 3 and a phrase. You can do it yourself with `tolpo automation resume`, which a server that is already
running picks up when it restarts.

The server listens on `127.0.0.1` only. It refuses to start on any other address without a bearer
token, and without a token it also rejects requests whose `Host` header is not local, which blocks
DNS-rebinding attacks from a web page.

Device names are free text typed by whoever set the device up. The server flattens and shortens them
before an agent sees them, and the instructions it sends tell the agent to treat names and readings as
data, not commands. A shared Tapo account still means anyone in it can rename a device, so keep the
account to people you trust.

## How discovery handles a busy network

Homes often have many devices, some battery-powered and slow to answer. Discovery is designed for that:

- A UDP broadcast, followed by a direct query to each address in the subnet (`discovery.sweep`),
  because some battery cameras ignore broadcasts yet always answer a direct question.
- Devices seen before are remembered in `~/.cache/tolpo/hosts.json` (address, name and model,
  no secrets) and queried directly at startup. A device missing from one scan is never forgotten;
  entries unseen for 30 days are dropped.
- Devices that do not answer are retried after 5, 15 and 45 seconds, and again on every rescan.

Startup is staged so the server is useful immediately: the first scan runs in the background, and each
device is connected the moment it answers. A device you name exactly is usable as soon as it is up,
without waiting for the hub, which can take about ten seconds from cold. Operations that span the whole
house wait for the scan, but never longer than fifteen seconds; anything still connecting finishes in
the background.

When something cannot be reached, the answers say so. `home_overview` and `list_devices` carry
`complete: false` and a `not_connected` list with the name, model and reason for each unreachable
device, so an agent does not mistake an offline door sensor for a closed door.

## For AI agents

Call `tolpo_areas` first. It lists the parts of tolpo that are on, what each one offers, and the level the owner
allows. If you are an AI agent asked to set tolpo up or to use it, start with
[llms.txt](https://raw.githubusercontent.com/signerless/tolpo/main/llms.txt). If you are asked to change the code,
read [AGENTS.md](https://github.com/signerless/tolpo/blob/main/AGENTS.md) first.

## Development

```bash
.venv/bin/python -m pytest        # runs against simulated devices, no hardware required
```

Project layout:

```
src/tolpo/
  registry.py    device discovery, state cache, control operations
  policy.py      what may be read and written (deny-by-default)
  views.py       shared presentation for the HTTP and MCP layers
  api.py         FastAPI HTTP server
  mcp_server.py  MCP server (stdio)
  config.py      config file and environment loading
  compat.py      small fixes for devices python-kasa cannot reach yet
  snapshot.py    one picture from a camera, taken with ffmpeg
  media.py       the video channel of the Tapo app (port 8800)
  tsvideo.py     finds the video track in a camera's stream
  calls.py       the one way an area sends a method to a device, checked against its level
  provision.py   Wi-Fi setup of a new device from this machine (SoftAP), behind a plan
  areas/         one file per area of the Tapo app; adding an area adds a file
  automation/    the local rule engine
  network/       providers for routers, DNS servers and firewalls
  cli.py         serve / mcp / scan / snapshot / watch / automation entry points
tests/           full suite against simulated devices
docs/protocols.md  how Tapo devices talk on the local network
docs/areas/      one page per area, with every tool and what was only tried with simulated devices
assets/          logo, device icons and social preview
```

See [CONTRIBUTING.md](https://github.com/signerless/tolpo/blob/main/CONTRIBUTING.md) for the ground rules, [docs/protocols.md](https://github.com/signerless/tolpo/blob/main/docs/protocols.md) for the protocol notes and
[SECURITY.md](https://github.com/signerless/tolpo/blob/main/SECURITY.md) to report a vulnerability.

## Background

The protocols behind Tapo devices are not documented by the vendor. Part of this project is independent
analysis of the official Tapo app and of real devices, done so that software can interoperate with hardware
its owners already have. The result is written down in [docs/protocols.md](https://github.com/signerless/tolpo/blob/main/docs/protocols.md): which
transport each model uses, how discovery works, what the newer TPAP protocol looks like, and what we could not
establish. The notes are in our own words. They contain no vendor code, keys or credentials, and tolpo itself
only ever signs in with your own Tapo account.

## Compatibility and status

This is a development version after 0.1. Lights, plugs, hubs and sensors are supported over the KLAP and SSL-AES
transports through `python-kasa`, plus a small patch (`compat.py`) for cameras with an ECDSA certificate, such as
the C465, that `python-kasa` cannot reach on its own.

The areas beyond the first release were built from analysis of the Tapo app and tested with simulated devices.
Reading was tried on a real home with four bulbs, a hub, eight sensors, a doorbell, three cameras and a plug. A few
reversible writes were tried there too: the bulbs (power, brightness, color, power-on defaults, saved presets and
disabled rules), the plug (power, countdown, LED, power-on default and disabled rules), a camera light, the hub light, a
microphone volume, a detection switch and an automation rule, each set back afterwards. Most other writes have not been
tried on real hardware, and the area pages say which. Robot vacuums, door locks, radiator valves and the firewall providers were never
tried on the real thing.

Known limits: cameras give still pictures, short runs of pictures and saved clips, but no continuous video to an
agent, and a doorbell behind the hub gives pictures but not its recordings; the cloud-only parts of the app (account, homes, sharing, push notifications,
Tapo Care, voice assistants) are not reachable, and the automation area replaces some of them locally; the MCP
server runs over stdio, one process per client, with no shared HTTP transport yet; the HTTP API has the original
routes plus a few for the camera memory card (`/devices/{id}/storage`, `/recordings/...`) and the door lock state, and
the other areas are MCP tools only; and devices on a second subnet have not been tested.

## License

MIT, see [LICENSE](https://github.com/signerless/tolpo/blob/main/LICENSE).

This project is not affiliated with or endorsed by TP-Link. Tapo is a trademark of TP-Link. All device
communication goes through the open source
[python-kasa](https://github.com/python-kasa/python-kasa) library.

<!-- mcp-name: io.github.signerless/tolpo -->
