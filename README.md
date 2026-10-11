# agentic-home

Local-first control of TP-Link Tapo smart-home devices for AI agents.

[![CI](https://github.com/signerless/agentic-home/actions/workflows/ci.yml/badge.svg)](https://github.com/signerless/agentic-home/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

`agentic-home` turns the Tapo devices on your network into a single interface that an AI agent, or
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
  never returned.
- **Built for real homes.** Discovery combines broadcast with a direct per-address sweep, remembers
  devices between runs, retries the ones that are slow to wake, and reports honestly when something
  cannot be reached instead of pretending it is fine.
- **Fast to answer.** State is cached briefly and device names resolve instantly, so an agent is not
  blocked on a slow hub.
- **Tested.** A full suite runs against simulated devices, so it needs no hardware.

## Requirements

- Python 3.11 or newer
- One or more TP-Link Tapo devices on the same network, already set up in the Tapo app
- Your Tapo account email and password
- [uv](https://docs.astral.sh/uv/) (or plain `pip`) to install

## Installation

```bash
git clone https://github.com/signerless/agentic-home && cd agentic-home
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

Or `~/.config/agentic-home/config.toml` (run `chmod 600` on it, since it holds a password):

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
```

Verify your setup with a read-only scan:

```bash
.venv/bin/agentic-home scan
```

## Usage with Claude (MCP)

Register the server once:

```bash
claude mcp add agentic-home \
  -e AGENTIC_HOME_CREDENTIALS_FILE=$HOME/.tapo_config.json \
  -- $PWD/.venv/bin/agentic-home mcp
```

Use `-e TAPO_EMAIL=... -e TAPO_PASSWORD=...` instead if your credentials live elsewhere. After the
next restart you can ask things like "dim office-1 to 30%", "is any door open?" or "what is the
temperature upstairs?".

| Tool | What it does |
|---|---|
| `home_overview` | The whole house at a glance: what is on, open doors and windows, water alerts, offline devices, low batteries, temperatures |
| `list_devices` | Devices with state; filter by `kind` or name |
| `get_device` | Every feature of one device, and which ones are writable |
| `set_power` | Switch lights and plugs; accepts several targets and globs like `office*` |
| `set_light` | Brightness, white temperature, or hue and saturation |
| `set_feature` | Any other feature, only when the policy allows it |
| `rescan` | Look for new or moved devices |

Read tools are marked read-only and `set_feature` is marked destructive, so MCP clients can ask for
confirmation where it matters. Responses are compact: all the devices of a typical home fit in under a
thousand tokens.

## Usage over HTTP

```bash
.venv/bin/agentic-home serve        # http://127.0.0.1:8765, interactive docs at /docs
```

```bash
# Read
curl -s localhost:8765/devices                 # everything, with state and capabilities
curl -s localhost:8765/devices/front-door       # one device, every feature
curl -s "localhost:8765/devices?kind=sensor"    # doors, windows, water, temperature

# Write
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

## Security model

This server controls physical things, so it refuses by default and widens only on request.

| Scope | Allowed |
|---|---|
| Read | Every feature of every device, except the Wi-Fi name and Matter setup codes, which are never returned |
| Write by default | Power, brightness, color temperature, color and light presets, on lights and plugs |
| Write on opt-in | Anything else, named in `policy.allow_write` (alarm test, pan/tilt, detection switches, reboot) |
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
- Devices seen before are remembered in `~/.cache/agentic-home/hosts.json` (address, name and model,
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

## Development

```bash
.venv/bin/python -m pytest        # runs against simulated devices, no hardware required
```

Project layout:

```
src/agentic_home/
  registry.py    device discovery, state cache, control operations
  policy.py      what may be read and written (deny-by-default)
  views.py       shared presentation for the HTTP and MCP layers
  api.py         FastAPI HTTP server
  mcp_server.py  MCP server (stdio)
  config.py      config file and environment loading
  compat.py      small fixes for devices python-kasa cannot reach yet
  cli.py         serve / mcp / scan entry points
tests/           full suite against simulated devices
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the ground rules and [SECURITY.md](SECURITY.md) to report a vulnerability.

## Compatibility and status

Version 0.1. Lights, plugs, hubs and sensors are fully supported over the KLAP and SSL-AES transports
through `python-kasa`, plus a small patch (`compat.py`) for cameras with an ECDSA certificate, such as
the C465, that `python-kasa` cannot reach on its own.

Known limits: cameras and the doorbell are read-only, with no video; the MCP server runs over stdio,
one process per client, with no shared HTTP transport yet; and devices on a second subnet have not
been tested.

## License

MIT, see [LICENSE](LICENSE).

This project is not affiliated with or endorsed by TP-Link. Tapo is a trademark of TP-Link. All device
communication goes through the open source
[python-kasa](https://github.com/python-kasa/python-kasa) library.
