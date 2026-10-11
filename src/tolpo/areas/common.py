"""Device methods that many areas need. Declared once here, so no two areas can disagree about their level.

Rule for every area: a method that only reads (`get_*`) is level 0. `set_device_info` carries many different actions
in its parameters (power, brightness, default state, name), so the method as a whole is level 2; reversible power and
brightness control does not go through the caller at all (it uses python-kasa's features in registry.py).
"""

from .. import policy
from ..policy import Tier

SMART_KINDS = ["light", "plug", "switch", "sensor", "hub", "vacuum", "thermostat", "valve", "chime", "lock"]
CAMERA_KINDS = ["camera", "doorbell"]

METHODS = [
    policy.method("get_device_info", Tier.READ, SMART_KINDS, "Read the device's basic information."),
    policy.method("get_device_usage", Tier.READ, SMART_KINDS, "Read how long the device has been on and its usage counters."),
    policy.method("get_component_list", Tier.READ, SMART_KINDS, "Read which features this device model supports."),
    policy.method("get_connect_cloud_state", Tier.READ, SMART_KINDS, "Read whether the device is connected to the TP-Link cloud."),
    policy.method("get_child_device_list", Tier.READ, ["hub"], "List the devices paired to a hub."),
    policy.method("set_device_info", Tier.SETTING, SMART_KINDS, "Change a device setting such as its name or its power-on default."),
    policy.method("getDeviceInfo", Tier.READ, CAMERA_KINDS, "Read the camera's basic information."),
    policy.method("getAppComponentList", Tier.READ, CAMERA_KINDS, "Read which features this camera model supports."),
]
