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

## Features

- **One view of the whole house.** Lights, plugs, the H100/H200 hub and its sensors, cameras and the
  doorbell, each with live state and a clear list of what can be changed.
- **Safe by default.** Reads are broad; writes are limited to power, brightness and color unless you
  opt in. Cameras, pairing and hub alarms stay read-only. Wi-Fi names and Matter setup codes are
  never returned. An agent sees a camera only if you name it.
- **Pictures from cameras.** A camera you enable gives an agent one still picture on request, through the same
  video channel the Tapo app uses. No extra camera account is needed.
- **Built for real homes.** Discovery combines broadcast with a direct per-address sweep, remembers
  devices between runs, retries the ones that are slow to wake, and reports honestly when something
  cannot be reached instead of pretending it is fine.
- **Fast to answer.** State is cached briefly and device names resolve instantly, so an agent is not
  blocked on a slow hub.
- **Tested.** A full suite runs against simulated devices, so it needs no hardware.

## Supported devices

tolpo works with whatever [python-kasa](https://github.com/python-kasa/python-kasa) can talk to, so the list is
longer than the hardware we own. The table keeps two things apart: what we have run against real devices, and
what python-kasa documents as supported but we have not tried.

| | Device | Tested here | Documented by python-kasa, not tried here | What an agent gets |
|---|---|---|---|---|
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/light.svg" width="36" alt="Light bulb"> | Color bulbs | L430C | L430P, L530B, L530E, L530EA, L535E | State, brightness, white temperature and color |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/plug.svg" width="36" alt="Smart plug"> | Smart plugs | P115 | P110, P110M | State, plus power, voltage and current readings |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/hub.svg" width="36" alt="Hub"> | Hubs | H200 | H100, and S200B and S200D buttons paired to a hub | The sensors paired to it show up as devices of their own. Its alarm and pairing stay blocked |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/contact.svg" width="36" alt="Door and window sensor"> | Door and window sensors | T110 |  | Open or closed, low-battery flag |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/climate.svg" width="36" alt="Temperature and humidity sensor"> | Temperature and humidity sensors | T310 | T315 (the one with a display) | Temperature, humidity, low-battery flag |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/motion.svg" width="36" alt="Motion sensor"> | Motion sensors |  | T100 | Motion, as python-kasa documents it. Not checked on real hardware |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/leak.svg" width="36" alt="Water leak sensor"> | Water leak sensors | T300 |  | Leak alert and battery level |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/camera.svg" width="36" alt="Camera"> | Cameras | C225, C610, C465 | C100, C101, C110, C200, C210, C220, C325WB, C460, C520WS, C720, TC40, TC65, TC70 | State, battery where there is one, last alert and detection switches. Settings are read-only. A still picture, for the cameras you enable |
| <img src="https://raw.githubusercontent.com/signerless/tolpo/main/assets/devices/doorbell.svg" width="36" alt="Video doorbell"> | Doorbells | D230 | D100C, D130 | State and battery. Read-only |

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

[discovery]
# subnets = ["192.168.1.0/24"]   # default: the /24 of your default-route interface
# hosts   = ["192.168.10.20"]    # devices that ignore broadcast or live on another VLAN
# sweep   = true                 # also probe every address in the subnet (networks up to /22)
interval_seconds = 600

[policy]
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
```

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
- The doorbell sits behind the hub and has no stream of its own, so it gives no picture.

## Security model

This server controls physical things, so it refuses by default and widens only on request.

| Scope | Allowed |
|---|---|
| Read | Every feature of every device, except the Wi-Fi name and Matter setup codes, which are never returned |
| Write by default | Power, brightness, color temperature, color and light presets, on lights and plugs |
| Write on opt-in | Anything else, named in `policy.allow_write` (alarm test, pan/tilt, detection switches, reboot) |
| Pictures | Only from the cameras named in `cameras.snapshots`. Off by default |
| Never | `pair` and `unpair` on the hub |

Cameras are read-only: the `state` switch on a camera is its privacy shutter, not a power relay, so it
is not treated as one. Enable it deliberately with `allow_write = ["camera.state"]`.

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

If you are an AI agent asked to set tolpo up or to use it, start with
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
  cli.py         serve / mcp / scan / snapshot entry points
tests/           full suite against simulated devices
docs/protocols.md  how Tapo devices talk on the local network
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

Version 0.1. Lights, plugs, hubs and sensors are fully supported over the KLAP and SSL-AES transports
through `python-kasa`, plus a small patch (`compat.py`) for cameras with an ECDSA certificate, such as
the C465, that `python-kasa` cannot reach on its own.

Known limits: cameras give still pictures but no continuous video, and the doorbell gives none; the settings of
cameras and the doorbell are read-only; the MCP server runs over stdio, one process per client, with no shared
HTTP transport yet; and devices on a second subnet have not been tested.

## License

MIT, see [LICENSE](https://github.com/signerless/tolpo/blob/main/LICENSE).

This project is not affiliated with or endorsed by TP-Link. Tapo is a trademark of TP-Link. All device
communication goes through the open source
[python-kasa](https://github.com/python-kasa/python-kasa) library.

<!-- mcp-name: io.github.signerless/tolpo -->
