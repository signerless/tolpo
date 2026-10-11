# agentic-home

A small local server that lets AI agents read and control the smart devices in a home.
It speaks to TP-Link Tapo devices directly over the local network (lights, plugs, the H100/H200 hub
and its sensors, cameras, the doorbell) and exposes them two ways: as an **MCP server** that agents such
as Claude call as tools, and as an HTTP API with an OpenAPI description.

Nothing goes through a third-party cloud. The server only needs the Tapo account the devices already
belong to, because that is how Tapo devices authenticate on the LAN.

## Run it

```bash
git clone https://github.com/signerless/agentic-home && cd agentic-home
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e ".[dev]"

export TAPO_EMAIL='you@example.com'
export TAPO_PASSWORD='...'
.venv/bin/agentic-home scan     # print what it finds
.venv/bin/agentic-home serve    # http://127.0.0.1:8765  (docs at /docs)
```

Credentials can also live in `~/.config/agentic-home/config.toml` (run `chmod 600` on it):

```toml
[tapo]
email = "you@example.com"
password = "..."
# or read an existing JSON file in place: credentials_file = "~/.tapo_config.json"

[server]
host = "127.0.0.1"
port = 8765
# token = "..."            # required if host is not loopback

[discovery]
# subnets = ["192.168.1.0/24"]   # default: the /24 of your default-route interface
# hosts = ["192.168.10.20"]      # devices that do not answer broadcast, or live on another VLAN
# sweep = true                   # also ask every address of the subnet directly (subnets up to /22)
interval_seconds = 600

[policy]
# Features an agent may write besides power, brightness and color.
# Plain ids ("pan_left") or kind-qualified ones ("camera.state").
allow_write = []
```

## Use it from Claude (MCP)

```bash
claude mcp add agentic-home \
  -e AGENTIC_HOME_CREDENTIALS_FILE=$HOME/.tapo_config.json \
  -- $PWD/.venv/bin/agentic-home mcp
```

Use `-e TAPO_EMAIL=... -e TAPO_PASSWORD=...` instead if you keep the credentials elsewhere. Then ask things like
"turn off the office lights", "dim office-1 to 30%", "is any door open?" or "what is the temperature?".

| Tool | What it does |
|---|---|
| `home_overview` | The whole house at a glance: what is on, open doors and windows, water alerts, offline devices, low batteries, temperatures |
| `list_devices` | Devices with state; filter by `kind` or name |
| `get_device` | Every feature of one device, and which ones are writable |
| `set_power` | Switch lights and plugs; accepts several targets and globs like `office*` |
| `set_light` | Brightness, white temperature, or hue and saturation |
| `set_feature` | Any other feature, only if the owner's policy allows it |
| `rescan` | Look for new or moved devices |

Read tools are marked read-only and `set_feature` is marked destructive, so MCP clients can ask for
confirmation where it matters. Answers are compact (all 17 devices of a real home take about 730 tokens).

Startup is staged and honest. The first scan runs in the background, so the handshake is instant, and each device
is connected the moment it answers. A device you name exactly is usable as soon as it is up (about 1.5 seconds in
the author's home), without waiting for the hub, which can take 10 seconds cold. Globs, `home_overview` and unknown
names wait for the scan, but never longer than 15 seconds: a device that is still connecting finishes in the
background and is reported meanwhile.

Whenever something cannot be reached, the answers say so instead of staying silent. `home_overview` carries
`complete: false` and a `not_connected` list with the name, model and reason of each device, and `list_devices`
adds the same list. The agent's instructions tell it not to treat an unreachable door sensor as a closed door.

Discovery is built for homes with many devices, some of them battery powered and slow to answer:

- A UDP broadcast, then a direct question to every address of the subnet (`discovery.sweep`), because battery
  cameras ignore broadcasts at times yet answer a direct question.
- Devices seen before are remembered in `~/.cache/agentic-home/hosts.json` (address, name and model) and asked
  directly at startup. A device missing from one scan is never forgotten; entries unseen for 30 days are dropped.
- Devices that do not answer are retried after 5, 15 and 45 seconds, and again at every rescan.

Device names are text typed by whoever set the device up. The server flattens and shortens them, and the
instructions it sends tell the agent to treat names and readings as data, but a shared Tapo account means anyone
in it can rename a device. Keep the account to people you trust.

## Talking to the HTTP API

```bash
curl -s localhost:8765/devices                       # everything, with state and what you can do
curl -s localhost:8765/devices/front-door            # one device, every feature
curl -s localhost:8765/devices?kind=sensor           # doors, windows, water, temperature

curl -s -X POST localhost:8765/devices/desk-lamp/power -H 'content-type: application/json' -d '{"on": true}'
curl -s -X POST localhost:8765/devices/office-1/light  -H 'content-type: application/json' -d '{"brightness": 40, "color_temp": 3000}'
curl -s -X POST localhost:8765/batch/light             -H 'content-type: application/json' -d '{"targets": ["office*"], "brightness": 60}'
```

A device is addressed by its short `id` (`office-1`), by its name in the Tapo app (any case), or, in the
batch endpoints, by a glob such as `office*`. If a name matches several devices the server answers 409 and
lists the candidates instead of guessing.

## What an agent can and cannot do

This server controls physical things, so it is deny-by-default.

| | |
|---|---|
| Read | every feature of every device, except the Wi-Fi name and Matter setup codes, which are never returned |
| Write by default | power, brightness, color temperature, color, light presets, on lights and plugs |
| Write if you opt in | anything else, by id, through `policy.allow_write` (alarm test, pan/tilt, detection switches, reboot) |
| Never | `pair` and `unpair` on the hub |

Cameras are read-only: the `state` switch on a camera is its privacy shutter, so it is not treated as a power
relay. Enable it deliberately with `allow_write = ["camera.state"]`.

The server listens on `127.0.0.1` only. It refuses to start on any other address without a bearer token, and
without a token it also rejects requests whose `Host` header is not local, which blocks DNS rebinding from a
web page. State is cached for ten seconds (`server.cache_ttl_seconds`) so an agent polling in a loop does not hammer the devices; ask for `refresh` when you need it live.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests use simulated devices, so they run without any hardware.

## Status

Version 0.1. Covers the KLAP and SSL-AES transports through `python-kasa`, plus a small patch for cameras with an
ECDSA certificate such as the C465, which `python-kasa` cannot reach by itself (see `compat.py`). Cameras and the doorbell are
read-only for now, with no video. Not yet tested against devices on a second subnet. The MCP server runs over
stdio, one process per client; a shared HTTP transport is not built yet.

## License

MIT, see [LICENSE](LICENSE). This project is not affiliated with or endorsed by TP-Link. Tapo is a trademark of
TP-Link. Device communication goes through the open source [python-kasa](https://github.com/python-kasa/python-kasa)
library.
