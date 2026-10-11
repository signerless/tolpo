"""Stand-ins for python-kasa devices so the API can be tested without hardware."""

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
