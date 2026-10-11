"""Validated local rules. No cloud record or credentials are accepted."""
from __future__ import annotations

import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from tolpo import policy


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


Ref = Annotated[str, Field(min_length=1, max_length=128)]
Minute = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
Seconds = Annotated[float, Field(ge=0, le=86400)]
MIN_INTERVAL = 5  # a relay switched every second can be ruined; two rules that undo each other would do exactly that


class Predicate(Model):
    device: Ref
    field: Ref
    op: Literal["eq", "ne", "gt", "ge", "lt", "le"] = "eq"
    value: JsonValue
    hold_seconds: Seconds = 0

    @field_validator("field")
    @classmethod
    def visible(cls, value):
        if value in policy.HIDDEN:
            raise ValueError("hidden features cannot be used in rules")
        return value


class Trigger(Model):
    kind: Literal["clock", "sunrise", "sunset", "state", "sensor", "camera", "rule"]
    time: Minute | None = None
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    offset_minutes: Annotated[int, Field(ge=-720, le=720)] = 0
    predicate: Predicate | None = None
    device: Ref | None = None
    event: Ref | None = None
    rule: Ref | None = None

    @model_validator(mode="after")
    def required(self):
        if self.kind == "clock" and self.time is None:
            raise ValueError("clock needs time")
        if self.kind == "state" and self.predicate is None:
            raise ValueError("state needs predicate")
        if self.kind in ("sensor", "camera") and self.device is None:
            raise ValueError("event trigger needs device")
        if self.kind == "rule" and self.rule is None:
            raise ValueError("rule trigger needs rule id")
        return self


class Window(Model):
    start: Minute
    end: Minute
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)


class Light(Model):
    on: bool | None = None
    brightness: Annotated[int, Field(ge=1, le=100)] | None = None
    color_temp: Annotated[int, Field(ge=1000, le=10000)] | None = None
    hue: Annotated[int, Field(ge=0, le=360)] | None = None
    saturation: Annotated[int, Field(ge=0, le=100)] | None = None

    @model_validator(mode="after")
    def combinations(self):
        if (self.hue is None) != (self.saturation is None):
            raise ValueError("hue and saturation must be given together")
        if self.hue is not None and self.color_temp is not None:
            raise ValueError("choose color temperature or hue")
        if not self.model_dump(exclude_none=True):
            raise ValueError("light action needs settings")
        if self.on is False and len(self.model_dump(exclude_none=True)) > 1:
            raise ValueError("off cannot be combined with light settings")
        return self


def check_params(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r"password|passwd|token|secret|credential|ssid|mac|setup|rtsp|confirm|key", key, re.I):
                raise ValueError("private fields cannot be stored in automation actions")
            check_params(item)
    elif isinstance(value, list):
        for item in value:
            check_params(item)
    elif isinstance(value, str) and "rtsp://" in value.casefold():
        raise ValueError("RTSP addresses cannot be stored in automation actions")


class Action(Model):
    kind: Literal["power", "light", "method", "tool", "delay", "message"]
    device: Ref | None = None
    on: bool | None = None
    light: Light | None = None
    method: Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,127}$")] | None = None
    tool: Ref | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)
    seconds: Seconds = 0
    message: Annotated[str, Field(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def required(self):
        check_params(self.params)
        if self.kind in ("power", "light", "method") and self.device is None:
            raise ValueError("device action needs device")
        if self.kind == "power" and self.on is None:
            raise ValueError("power needs on")
        if self.kind == "light" and self.light is None:
            raise ValueError("light needs settings")
        if self.kind == "method" and self.method is None:
            raise ValueError("method action needs method")
        if self.kind == "tool" and self.tool is None:
            raise ValueError("tool action needs tool name")
        if self.kind == "message" and self.message is None:
            raise ValueError("message action needs message")
        return self


class Rule(Model):
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
    name: Ref
    enabled: bool = False
    triggers: list[Trigger] = Field(min_length=1, max_length=16)
    conditions: list[Predicate] = Field(default_factory=list, max_length=16)
    condition_mode: Literal["all", "any"] = "all"
    windows: list[Window] = Field(default_factory=list, max_length=16)
    actions: list[Action] = Field(min_length=1, max_length=32)
    minimum_interval: Annotated[float, Field(ge=MIN_INTERVAL, le=86400)] = 60

    @model_validator(mode="after")
    def no_self(self):
        if any(t.kind == "rule" and t.rule == self.id for t in self.triggers):
            raise ValueError("rule cannot trigger itself")
        if self.actions[-1].kind == "delay":
            raise ValueError("delay cannot be the last action")
        return self

    def device_refs(self) -> set[str]:
        """Every device the rule names, as it was written."""
        refs = set()
        for trigger in self.triggers:
            refs.update(ref for ref in (trigger.device, trigger.predicate and trigger.predicate.device) if ref)
        refs.update(condition.device for condition in self.conditions)
        refs.update(action.device for action in self.actions if action.device)
        return refs

    def with_devices(self, names: dict[str, str]) -> "Rule":
        """A copy in which each device the rule names is replaced by `names[ref]`. A rule keeps pointing at the device it
        was written for, also after the device is renamed or another device takes its name."""

        def swap(ref):
            return names.get(ref, ref) if ref else ref

        data = self.model_dump()
        for trigger in data["triggers"]:
            trigger["device"] = swap(trigger["device"])
            if trigger["predicate"]:
                trigger["predicate"]["device"] = swap(trigger["predicate"]["device"])
        for condition in data["conditions"]:
            condition["device"] = swap(condition["device"])
        for action in data["actions"]:
            action["device"] = swap(action["device"])
        return type(self).model_validate(data)


class Settings(Model):
    timezone: str = "UTC"
    latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[float, Field(ge=-180, le=180)] | None = None
    poll_seconds: Annotated[float, Field(ge=1, le=3600)] = 5
    killed: bool = False
    quiet_hours: Window | None = None

    @field_validator("timezone")
    @classmethod
    def zone(cls, value):
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be given together")
        return self
