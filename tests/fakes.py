"""Stand-ins for python-kasa devices so the API can be tested without hardware."""

import json
import sys
from pathlib import Path

from kasa import DeviceType, Feature, Module

T, C = Feature.Type, Feature.Category


class FakeFeature:
    def __init__(self, id, type, category=C.Config, value=None, unit=None, lo=0, hi=100, choices=None, name=None):
        self.id, self.type, self.category = id, type, category
        self.name = name or id.replace("_", " ")
        self.value, self.unit = value, unit
        self.minimum_value, self.maximum_value, self.choices = lo, hi, choices
        self.calls = []

    async def set_value(self, value):
        self.calls.append(value)
        if self.type is not T.Action:
            self.value = value


class FakeLight:
    def __init__(self, device):
        self.device, self.calls = device, []

    async def set_brightness(self, brightness, *, transition=None):
        self.calls.append(("brightness", brightness))
        self.device.features["brightness"].value = brightness

    async def set_color_temp(self, temp, *, brightness=None, transition=None):
        self.calls.append(("color_temp", temp, brightness))
        self.device.features["color_temperature"].value = temp

    async def set_hsv(self, hue, saturation, value=None, *, transition=None):
        self.calls.append(("hsv", hue, saturation, value))
        self.device.features["hsv"].value = (hue, saturation, value)


class FakeDevice:
    def __init__(self, device_id, alias, model, device_type, features=(), children=(), host="192.0.2.10", light=False):
        self.device_id, self.alias, self.model = device_id, alias, model
        self.device_type, self.host = device_type, host
        self.features = {f.id: f for f in features}
        self.children = list(children)
        self.modules = {Module.Light: FakeLight(self)} if light else {}
        self.disconnected = False

    @property
    def is_on(self):
        return self.features["state"].value

    async def turn_on(self):
        self.features["state"].value = True

    async def turn_off(self):
        self.features["state"].value = False

    async def update(self):
        pass

    async def disconnect(self):
        self.disconnected = True


def bulb(device_id, alias):
    return FakeDevice(
        device_id, alias, "L430C", DeviceType.Bulb, light=True,
        features=[
            FakeFeature("state", T.Switch, C.Primary, True),
            FakeFeature("brightness", T.Number, C.Primary, 80, lo=1, hi=100),
            FakeFeature("color_temperature", T.Number, C.Primary, 3000, lo=2500, hi=6500),
            FakeFeature("hsv", T.Unknown, C.Config, (0, 0, 80)),
            FakeFeature("light_preset", T.Choice, C.Config, "Off", choices=["Off", "Warm"]),
            FakeFeature("reboot", T.Action, C.Debug),
            FakeFeature("ssid", T.Sensor, C.Debug, "MyHomeWifi"),
        ],
    )


def plug(device_id, alias):
    return FakeDevice(
        device_id, alias, "P115", DeviceType.Plug,
        features=[
            FakeFeature("state", T.Switch, C.Primary, False),
            FakeFeature("current_consumption", T.Sensor, C.Primary, 12.5, unit="W"),
            FakeFeature("reboot", T.Action, C.Debug),
        ],
    )


def hub():
    door = FakeDevice(
        "child-door", "main door", "T110", DeviceType.Sensor,
        features=[
            FakeFeature("is_open", T.BinarySensor, C.Primary, False),
            FakeFeature("battery_level", T.Sensor, C.Info, 80),
            FakeFeature("unpair", T.Action, C.Debug),
        ],
    )
    temp = FakeDevice(
        "child-temp", "temp sensor", "T310", DeviceType.Sensor,
        features=[
            FakeFeature("temperature", T.Sensor, C.Primary, 21.5, unit="celsius"),
            FakeFeature("humidity", T.Sensor, C.Primary, 48, unit="%"),
        ],
    )
    return FakeDevice(
        "hub-1", "Tapo H200", "H200", DeviceType.Hub, children=[door, temp],
        features=[
            FakeFeature("matter_setup_code", T.Sensor, C.Debug, "SECRET-123"),
            FakeFeature("matter_setup_payload", T.Sensor, C.Debug, "MT:SECRETPAYLOAD"),
            FakeFeature("pair", T.Action, C.Config),
            FakeFeature("test_alarm", T.Action, C.Config),
        ],
    )


def camera():
    return FakeDevice(
        "cam-1", "hallway-cam", "C225", DeviceType.Camera,
        features=[
            FakeFeature("state", T.Switch, C.Primary, True),
            FakeFeature("pan_left", T.Action, C.Config),
        ],
    )


def populate(registry, extra=()):
    """Adopt the standard fake house into a registry and return the devices by short name."""
    devices = {
        "office1": bulb("b1", "Office 1"),
        "office2": bulb("b2", "Office 2"),
        "lamp": plug("p1", "Desk Lamp"),
        "hub": hub(),
        "cam": camera(),
    }
    for device in [*devices.values(), *extra]:
        registry._adopt(device)
    return devices


# A real 16x16 grey JPEG made by ffmpeg, so the picture checks run on genuine bytes.
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010200000100010000fffe00104c61766336322e32382e31303200ffdb004300080a0a0b0a0b0d0d0d0d0d0d"
    "100f101010101010101010101012121215151512121210101212141415151717171515151517171919191e1e1c1c2323242b2b33ffc400"
    "4a000100000000000000000000000000000000010100000000000000000000000000000000100100000000000000000000000000000000"
    "110100000000000000000000000000000000ffc00011080010001003012200021100031100ffda000c03010002110311003f00000fffd9"
)

# A stand-in for ffmpeg. Tests choose what it does through FAKE_FFMPEG_MODE and read what it was asked
# through FAKE_FFMPEG_LOG, so no camera and no video library is involved.
_FAKE_FFMPEG = """\
#!{python}
import json, os, sys, time

mode = os.environ.get("FAKE_FFMPEG_MODE", "ok")
if os.environ.get("FAKE_FFMPEG_LOG"):
    with open(os.environ["FAKE_FFMPEG_LOG"], "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_FFMPEG_PID"):
    with open(os.environ["FAKE_FFMPEG_PID"], "w") as pid:
        pid.write(str(os.getpid()))

url = sys.argv[sys.argv.index("-i") + 1]
if url == "pipe:0" and not sys.stdin.buffer.read(188):
    # Like the real ffmpeg: nothing to show until the first video arrives, and an error when none does.
    print("pipe:0: Invalid data found when processing input", file=sys.stderr)
    sys.exit(1)
JPEG = bytes.fromhex({jpeg!r})
FAILURES = {{
    "unauthorized": "[rtsp @ 0x1] method DESCRIBE failed: 401 Unauthorized",
    "notfound": "[rtsp @ 0x1] method DESCRIBE failed: 404 Not Found",
    "busy": "[rtsp @ 0x1] method DESCRIBE failed: 453 Not Enough Bandwidth",
    "refused": "[tcp @ 0x1] Connection to tcp://192.0.2.10:554?timeout=0 failed: Connection refused",
    "timeout": "[tcp @ 0x1] Connection to tcp://192.0.2.10:554?timeout=0 failed: Connection timed out",
    "crash": "Segmentation fault",
}}
if mode == "ok":
    sys.stdout.buffer.write(JPEG)
elif mode == "garbage":
    sys.stdout.buffer.write(b"this is not a picture")
elif mode == "empty":
    pass
elif mode == "hang":
    time.sleep(60)
elif mode == "slow":
    with open(os.environ["FAKE_FFMPEG_TRACE"], "a") as trace:
        trace.write("start %f\\n" % time.time())
    time.sleep(0.4)
    sys.stdout.buffer.write(JPEG)
    with open(os.environ["FAKE_FFMPEG_TRACE"], "a") as trace:
        trace.write("end %f\\n" % time.time())
else:
    # Real ffmpeg repeats the whole URL, password included, when it fails.
    print(FAILURES[mode] + "\\nError opening input file " + url + ".", file=sys.stderr)
    sys.exit(1)
"""


def fake_ffmpeg(folder: Path) -> str:
    """Write the stand-in into `folder` and return its path."""
    script = folder / "ffmpeg"
    script.write_text(_FAKE_FFMPEG.format(python=sys.executable, jpeg=TINY_JPEG.hex()))
    script.chmod(0o755)
    return str(script)


class FakeFfmpeg:
    """The stand-in above, wired to a test: `mode()` chooses what it does and `calls()` lists what it was asked."""

    def __init__(self, folder: Path, monkeypatch):
        self.path = fake_ffmpeg(folder)
        self.folder, self._patch = folder, monkeypatch
        self._log = folder / "calls.jsonl"
        monkeypatch.setenv("FAKE_FFMPEG_LOG", str(self._log))

    def mode(self, name: str) -> None:
        self._patch.setenv("FAKE_FFMPEG_MODE", name)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self._log.read_text().splitlines()] if self._log.exists() else []

    def urls(self) -> list[str]:
        return [call[call.index("-i") + 1] for call in self.calls()]
