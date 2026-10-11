"""Automation tests use fake clocks, fake observations and the real caller policy."""
import asyncio
import json
from datetime import datetime, timezone
from dataclasses import replace

import pytest
from kasa import Feature
from pydantic import ValidationError

from tolpo import policy
from tolpo.areas import AreaContext
from tolpo.areas.automation import engine_for, install_lifecycle
from tolpo.automation import Engine, FakeClock, FakeSource, Rule, Sample, Settings, Store
from tolpo.automation.engine import in_window, public_rule
from tolpo.automation.events import RegistrySource
from tolpo.automation.models import Window
from tolpo.automation.sun import sun_time
from tolpo.policy import Tier
from tolpo.registry import Forbidden
from tests.fakes import FakeFeature

pytestmark = pytest.mark.usefixtures("clean_catalog")


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOLPO_STATE_DIR", str(tmp_path))


@pytest.fixture
def setup(make_house, tmp_path, monkeypatch):
    def build(tier=1, allow_methods=(), sample=None):
        reg, caller, devices = make_house(tier=tier, allow_methods=allow_methods)
        source = FakeSource(sample)
        clock = FakeClock(datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc))
        eng = Engine(reg, caller, store=Store(tmp_path), source=source, clock=clock)
        monkeypatch.setattr(eng, "start", lambda: None)
        return eng, source, clock, reg, caller, devices
    return build


def rule(ref="r1", triggers=None, actions=None, **extra):
    return Rule(id=ref, name="Evening\n  lights", enabled=True,
                triggers=triggers or [{"kind": "clock", "time": "12:00"}],
                actions=actions or [{"kind": "power", "device": "desk-lamp", "on": True}], **extra)


def declare(name="set_alarm", tier=Tier.SECURITY):
    policy.register_methods([policy.method(name, tier, ["plug"], "Change the alarm.")])


@pytest.mark.parametrize("change", [
    {"triggers": [{"kind": "clock", "time": "25:00"}]},
    {"triggers": [{"kind": "state"}]},
    {"triggers": [{"kind": "state", "predicate": {"device": "desk-lamp", "field": "ssid", "value": "x"}}]},
    {"triggers": [{"kind": "rule", "rule": "r1"}]},
    {"minimum_interval": 0},
    {"actions": [{"kind": "light", "device": "office-1", "light": {"brightness": 101}}]},
    {"actions": [{"kind": "light", "device": "office-1", "light": {"hue": 10}}]},
    {"actions": [{"kind": "delay", "seconds": 5}]},
    {"actions": [{"kind": "method", "device": "desk-lamp", "method": "set_device_info", "params": {"password": "private"}}]},
    {"actions": [{"kind": "method", "device": "desk-lamp", "method": "set_device_info", "params": {"nested": [{"homekit_setup_code": "private"}]}}]},
    {"triggers": [{"kind": "clock", "time": "12:00", "weekdays": [7]}]},
    {"actions": [{"kind": "tool", "tool": "snapshot", "params": {"url": "rtsp://fake:private@example.invalid/live"}}]},
])
def test_validation(change):
    with pytest.raises(ValidationError):
        rule(**change)


def test_settings_validation():
    for args in ({"timezone": "Not/AZone"}, {"latitude": 91, "longitude": 1}, {"latitude": 1}, {"poll_seconds": 0}):
        with pytest.raises((ValueError, KeyError)):
            Settings(**args)


def test_store_version_atomic_and_private(setup, tmp_path):
    eng, *_ = setup()
    eng.create(rule())
    path = tmp_path / "automation.json"
    assert Store(tmp_path).load().rules[0].id == "r1"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".automation-*"))
    raw = json.loads(path.read_text())
    raw["version"] = 99
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="version"):
        Store(tmp_path).load()
    path.write_text("{")
    with pytest.raises(ValueError):
        Store(tmp_path).load()


def test_rule_graph_rejects_cycles_and_duplicate_ids(setup):
    eng, *_ = setup()
    eng.create(rule("a", triggers=[{"kind": "rule", "rule": "b"}]))
    with pytest.raises(ValueError, match="cycle"):
        eng.create(rule("b", triggers=[{"kind": "rule", "rule": "a"}]))
    with pytest.raises(ValueError, match="already exists"):
        eng.create(rule("a"))
    with pytest.raises(ValueError, match="latitude"):
        eng.create(rule("sun", triggers=[{"kind": "sunrise"}]))


@pytest.mark.anyio
async def test_clock_once_dry_run_and_policy_change(setup):
    eng, source, clock, reg, caller, devices = setup()
    eng.create(rule())
    before = eng.store.path.read_bytes()
    assert (await eng.tick(dry_run=True))["fired"] == ["r1"]
    assert devices["lamp"].is_on is False
    assert eng.store.path.read_bytes() == before
    assert (await eng.tick())["fired"] == ["r1"]
    assert devices["lamp"].is_on is True
    assert (await eng.tick())["fired"] == []
    clock.advance(86400)
    reg.cfg = replace(reg.cfg, tier=0)
    result = await eng.tick()
    assert result["errors"][0]["error"] == "Forbidden"
    assert caller.sent == []


@pytest.mark.anyio
async def test_guarded_light_actions_and_privacy_denial(setup):
    eng, source, clock, reg, caller, devices = setup()
    eng.create(rule(actions=[{"kind": "light", "device": "office-1", "light": {"brightness": 44}}]))
    await eng.tick()
    assert devices["office1"].features["brightness"].value == 44
    eng.create(rule("privacy", actions=[{"kind": "power", "device": "hallway-cam", "on": False}]))
    result = await eng.tick()
    assert result["errors"][0]["device"] == "hallway-cam"
    assert devices["cam"].is_on is True


@pytest.mark.anyio
@pytest.mark.parametrize("tier", [2, 3, 4])
async def test_method_level_confirmation_and_no_saved_phrase(setup, tier):
    declare(tier=Tier(tier))
    eng, source, clock, reg, caller, devices = setup(tier=4)
    eng.create(rule(actions=[{"kind": "method", "device": "desk-lamp", "method": "set_alarm"}]))
    result = await eng.tick()
    assert bool(result.get("errors")) == (tier >= 3)
    assert len(caller.sent) == (1 if tier == 2 else 0)
    clock.advance(86400)
    result = await eng.tick(confirm="set_alarm desk-lamp")
    assert "errors" not in result
    assert "confirm" not in eng.store.path.read_text()
    clock.advance(86400)
    reg.cfg = replace(reg.cfg, tier=1)
    result = await eng.tick(confirm="set_alarm desk-lamp")
    assert result["errors"][0]["error"] == "Forbidden"


@pytest.mark.anyio
async def test_undeclared_method_and_error_do_not_leak(setup):
    eng, source, clock, reg, caller, devices = setup(tier=4)
    eng.create(rule(actions=[{"kind": "method", "device": "desk-lamp", "method": "unknown"}]))
    result = await eng.tick()
    assert result["errors"][0]["error"] == "Forbidden"
    assert caller.sent == []
    eng.delete("r1")
    eng.create(rule(actions=[{"kind": "method", "device": "desk-lamp", "method": "get_device_info"}]))
    caller.respond("get_device_info", RuntimeError("password secret ssid MAC"))
    result = await eng.tick()
    assert result["errors"][0]["error"] == "RuntimeError"
    assert "secret" not in json.dumps(result) + eng.store.outbox.read_text()


@pytest.mark.anyio
async def test_action_responses_never_return_private_values(setup):
    eng, source, clock, reg, caller, devices = setup()
    caller.respond("get_device_info", {"ssid": "private", "token": "private", "mac": "private"})
    eng.create(rule(actions=[{"kind": "method", "device": "desk-lamp", "method": "get_device_info"}]))
    result = await eng.tick()
    assert result["fired"] == ["r1"] and result["actions"][0]["ok"] is True
    assert "private" not in json.dumps(result) + json.dumps(public_rule(eng.rule("r1")))


@pytest.mark.anyio
async def test_state_edge_hold_and_minimum_interval(setup):
    p = {"device": "main-door", "field": "is_open", "value": True, "hold_seconds": 10}
    eng, source, clock, *_ = setup(sample=Sample(values={"main-door": {"is_open": False}}))
    eng.create(rule(triggers=[{"kind": "state", "predicate": p}]))
    assert (await eng.tick())["fired"] == []
    source.current.values["main-door"]["is_open"] = True
    assert (await eng.tick())["fired"] == []
    clock.advance(9)
    assert (await eng.tick())["fired"] == []
    clock.advance(1)
    assert (await eng.tick())["fired"] == ["r1"]
    clock.advance(10)
    assert (await eng.tick())["fired"] == []
    source.current.values["main-door"]["is_open"] = False
    await eng.tick()
    source.current.values["main-door"]["is_open"] = True
    await eng.tick()
    clock.advance(10)
    assert (await eng.tick())["fired"] == []  # minimum interval


@pytest.mark.anyio
async def test_hold_resets_on_unknown_readings(setup):
    p = {"device": "main-door", "field": "is_open", "value": True, "hold_seconds": 10}
    eng, source, clock, *_ = setup(sample=Sample(values={"main-door": {"is_open": False}}))
    eng.create(rule(triggers=[{"kind": "state", "predicate": p}]))
    await eng.tick()
    source.current.values["main-door"]["is_open"] = True
    await eng.tick()
    source.current.values = {}
    clock.advance(11)
    assert (await eng.tick())["fired"] == []
    source.current.values = {"main-door": {"is_open": True}}
    assert (await eng.tick())["fired"] == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode,expected", [("all", False), ("any", True)])
async def test_conditions_all_any_and_hold(setup, mode, expected):
    eng, source, clock, *_ = setup(sample=Sample(values={"main-door": {"is_open": True}}))
    eng.create(rule(condition_mode=mode, conditions=[
        {"device": "main-door", "field": "is_open", "value": True},
        {"device": "absent", "field": "temperature", "op": "gt", "value": 20}]))
    assert bool((await eng.tick())["fired"]) == expected


def test_windows_weekdays_and_overnight():
    window = Window(start="22:00", end="06:00", weekdays=[0])
    monday = datetime(2026, 3, 23, 23, tzinfo=timezone.utc)
    assert in_window(window, monday)
    assert in_window(window, monday.replace(day=24, hour=2))
    assert not in_window(window, monday.replace(day=23, hour=2))
    assert not in_window(window, monday.replace(day=24, hour=6))


@pytest.mark.anyio
async def test_sensor_camera_baseline_dedup_and_rule_chain(setup):
    sample = Sample(values={"camera": {"last_alert_timestamp": 1, "last_alert_type": "motion"}},
                    logs={"sensor": [{"id": 1, "event": "singleClick", "eventId": "e1", "timestamp": 1}]})
    eng, source, clock, *_ = setup(sample=sample)
    message = [{"kind": "message", "message": "hello\n world"}]
    eng.create(rule("sensor", triggers=[{"kind": "sensor", "device": "sensor", "event": "singleClick"}], actions=message))
    eng.create(rule("camera", triggers=[{"kind": "camera", "device": "camera", "event": "motion"}], actions=message))
    eng.create(rule("chain", triggers=[{"kind": "rule", "rule": "sensor"}], actions=message))
    assert (await eng.tick())["fired"] == []
    source.current.logs["sensor"].append({"id": 2, "event": "singleClick", "eventId": "e2", "timestamp": 2})
    source.current.values["camera"]["last_alert_timestamp"] = 2
    assert (await eng.tick())["fired"] == ["sensor", "camera", "chain"]
    assert len(eng.store.alerts()) == 2
    assert (await eng.tick())["fired"] == []
    assert len(eng.store.alerts()) == 3
    assert eng.store.alerts()[0]["message"] == "hello world"


@pytest.mark.anyio
async def test_delay_restart_disable_and_at_most_once(setup, tmp_path):
    eng, source, clock, reg, caller, devices = setup()
    eng.create(rule(actions=[{"kind": "power", "device": "desk-lamp", "on": True},
                             {"kind": "delay", "seconds": 30},
                             {"kind": "power", "device": "desk-lamp", "on": False}]))
    await eng.tick()
    assert devices["lamp"].is_on and len(eng.state.runs) == 1
    restarted = Engine(reg, caller, store=Store(tmp_path), source=source, clock=clock)
    await restarted.tick()
    assert devices["lamp"].is_on
    clock.advance(30)
    await restarted.tick()
    assert not devices["lamp"].is_on and restarted.state.runs == []
    clock.advance(86400 - 30)
    await restarted.tick()
    restarted.enabled("r1", False)
    assert restarted.state.runs == []
    clock.advance(30)
    await restarted.tick()
    assert devices["lamp"].is_on  # canceled off action


@pytest.mark.anyio
async def test_confirmation_does_not_survive_delay(setup):
    declare()
    eng, source, clock, reg, caller, _ = setup(tier=4)
    eng.create(rule(actions=[{"kind": "delay", "seconds": 5}, {"kind": "method", "device": "desk-lamp", "method": "set_alarm"}]))
    await eng.tick(confirm="set_alarm desk-lamp")
    clock.advance(5)
    result = await eng.tick()
    assert result["errors"][0]["error"] == "Forbidden" and caller.sent == []


@pytest.mark.anyio
async def test_quiet_outbox_partial_clear_and_kill(setup):
    eng, source, clock, *_ = setup()
    eng.state.settings.quiet_hours = Window(start="11:00", end="13:00")
    eng.create(rule(actions=[{"kind": "message", "message": "Door\n open"}]))
    await eng.tick()
    assert eng.store.alerts()[0]["quiet"] is True
    assert eng.store.outbox.stat().st_mode & 0o777 == 0o600
    eng.store.alert({"message": "second"})
    assert len(eng.store.alerts(clear=True, limit=1)) == 1
    assert eng.store.alerts() == [{"message": "second"}]
    eng.state.settings.killed = True
    clock.advance(86400)
    assert await eng.tick() == {"killed": True, "fired": []}


@pytest.mark.anyio
async def test_registry_source_shapes_and_errors(make_house):
    reg, caller, devices = make_house(responses={"get_trigger_logs": {"logs": [{"id": 1, "eventId": "e1", "event": "open", "timestamp": 5}]}})
    devices["cam"].features["last_alert_timestamp"] = FakeFeature("last_alert_timestamp", Feature.Type.Sensor, value=5)
    source = RegistrySource(reg, caller)
    rules = [rule(triggers=[{"kind": "sensor", "device": "main-door"}, {"kind": "camera", "device": "hallway-cam"}]),
             rule("power", triggers=[{"kind": "state", "predicate": {"device": "desk-lamp", "field": "state", "value": True}}])]
    sample = await source.sample(rules)
    assert sample.logs["main-door"][0]["event"] == "open"
    assert sample.values["desk-lamp"]["state"] is False
    assert sample.values["hallway-cam"]["last_alert_timestamp"] == 5
    assert caller.sent == [("main-door", "get_trigger_logs", {"start_id": 0})]
    caller.respond("get_trigger_logs", RuntimeError("secret"))
    sample = await source.sample(rules)
    assert sample.errors == [{"device": "main-door", "error": "RuntimeError"}]
    assert "main-door" not in sample.values


@pytest.mark.anyio
async def test_sunrise_sunset_offsets_and_polar_night(setup):
    eng, source, clock, *_ = setup()
    eng.state.settings = Settings(latitude=0, longitude=0)
    moment = sun_time(clock.now().date(), 0, 0)
    assert moment.hour in (5, 6)
    sunset = sun_time(clock.now().date(), 0, 0, True)
    assert sunset.hour in (18, 19)
    assert sun_time(datetime(2026, 12, 21).date(), 89, 0) is None
    eng.create(rule(triggers=[{"kind": "sunrise", "offset_minutes": 10}]))
    clock.value = moment.replace(second=0, microsecond=0)
    clock.advance(600)
    assert (await eng.tick())["fired"] == ["r1"]
    assert (await eng.tick())["fired"] == []


@pytest.mark.anyio
async def test_arbitrary_tool_is_refused_without_adapter(setup):
    eng, *_ = setup()
    eng.create(rule(actions=[{"kind": "tool", "tool": "set_power", "params": {"on": True}}]))
    assert (await eng.tick())["errors"][0]["error"] == "Forbidden"


@pytest.mark.anyio
async def test_adapter_obeys_owner_level_and_per_call_confirmation(setup):
    class Adapter:
        def __init__(self):
            self.calls = []
        def plan(self, name, params):
            return {"level": 3, "allowed": True, "confirm": "alarm desk-lamp"}
        async def execute(self, name, params, *, confirm):
            self.calls.append((name, params, confirm))
    eng, source, clock, reg, *_ = setup(tier=1)
    adapter = eng.tool_executor = Adapter()
    eng.create(rule(actions=[{"kind": "tool", "tool": "alarm"}]))
    assert (await eng.tick(confirm="alarm desk-lamp"))["errors"]
    assert adapter.calls == []
    clock.advance(86400)
    reg.cfg = replace(reg.cfg, tier=3)
    assert (await eng.tick())["errors"]
    clock.advance(86400)
    assert "errors" not in await eng.tick(confirm="alarm desk-lamp")
    assert len(adapter.calls) == 1


@pytest.mark.anyio
async def test_mcp_all_tools_and_lifecycle(make_house, mcp, monkeypatch):
    reg, caller, devices = make_house()
    clock = FakeClock(datetime(2026, 3, 20, 12, tzinfo=timezone.utc))
    async with mcp(reg, caller) as client:
        eng = reg._automation_engine
        eng.clock = clock
        monkeypatch.setattr(eng, "start", lambda: None)
        listed = (await client.list_tools()).tools
        assert len([t for t in listed if t.name.startswith("automation_")]) == 10
        payload = rule().model_dump(mode="json")
        payload["enabled"] = False
        for name, args in [
            ("automation_create", {"rule": payload}),
            ("automation_list", {}), ("automation_show", {"rule_id": "r1"}),
            ("automation_enable", {"rule_id": "r1"}), ("automation_dry_run", {}),
            ("automation_tick", {}), ("automation_alerts", {"clear": True}),
            ("automation_disable", {"rule_id": "r1"}),
            ("automation_settings", {"settings": {"killed": True}}),
            ("automation_delete", {"rule_id": "r1"}),
        ]:
            result = await client.call_tool(name, args)
            assert not result.is_error, (name, result)
        missing = await client.call_tool("automation_show", {"rule_id": "missing"})
        assert missing.is_error
    assert eng.closed
    assert devices["lamp"].disconnected


@pytest.mark.anyio
async def test_continuous_engine_starts_lazily_and_stops(make_house):
    reg, caller, devices = make_house()
    ctx = AreaContext(registry=reg, caller=caller)
    install_lifecycle(ctx)
    await reg.start(discover=False)
    eng = engine_for(ctx)
    assert eng.task is None
    eng.clock = FakeClock(datetime(2026, 3, 20, 12, tzinfo=timezone.utc))
    eng.create(rule())
    assert eng.task is not None
    # Yield to the scheduler, never sleep against a real clock.
    for _ in range(30):
        if devices["lamp"].is_on:
            break
        await asyncio.sleep(0)
    assert devices["lamp"].is_on
    await reg.stop()
    assert eng.task is None and eng.closed


@pytest.mark.anyio
async def test_multiple_clock_triggers_do_not_replay_same_minute(setup):
    eng, source, clock, *_ = setup()
    eng.create(rule(triggers=[{"kind": "clock", "time": "12:00"}, {"kind": "clock", "time": "12:00"}], minimum_interval=1))
    assert (await eng.tick())["fired"] == ["r1"]
    clock.advance(2)
    assert (await eng.tick())["fired"] == []


@pytest.mark.anyio
async def test_conditions_hold_observed_each_poll_and_reset_outside_window(setup):
    eng, source, clock, *_ = setup(sample=Sample(values={"main-door": {"is_open": True}}))
    eng.create(rule(triggers=[{"kind": "clock", "time": "12:01"}], conditions=[
        {"device": "main-door", "field": "is_open", "value": True, "hold_seconds": 30}]))
    await eng.tick()
    clock.advance(60)
    assert (await eng.tick())["fired"] == ["r1"]
    eng.delete("r1")
    eng.create(rule(triggers=[{"kind": "clock", "time": "12:02"}], windows=[{"start": "12:02", "end": "12:03"}], conditions=[
        {"device": "main-door", "field": "is_open", "value": True, "hold_seconds": 30}]))
    await eng.tick()
    clock.advance(30)
    source.current.values["main-door"]["is_open"] = False
    await eng.tick()
    clock.advance(30)
    source.current.values["main-door"]["is_open"] = True
    assert (await eng.tick())["fired"] == []


@pytest.mark.anyio
async def test_log_high_water_ignores_old_page_reappearance(setup):
    eng, source, clock, *_ = setup(sample=Sample(logs={"sensor": [{"id": 10, "event": "open"}]}))
    eng.create(rule(triggers=[{"kind": "sensor", "device": "sensor"}], actions=[{"kind": "message", "message": "new event"}]))
    await eng.tick()
    source.current.logs["sensor"] = [{"id": 9, "event": "open"}]
    clock.advance(60)
    assert (await eng.tick())["fired"] == []
    source.current.logs["sensor"] = [{"id": 10, "event": "open"}]
    assert (await eng.tick())["fired"] == []


@pytest.mark.anyio
async def test_mcp_tool_dispatch_with_levels_errors_and_validation(make_house, mcp, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    reg, caller, devices = make_house()
    async with mcp(reg, caller) as client:
        eng = reg._automation_engine
        eng.clock = FakeClock(datetime(2026, 3, 20, 12, tzinfo=timezone.utc))
        monkeypatch.setattr(eng, "start", lambda: None)
        eng.create(rule(actions=[{"kind": "tool", "tool": "set_power", "params": {"targets": "desk-lamp", "on": True}}]))
        assert "errors" not in await eng.tick()
        assert devices["lamp"].is_on
        eng.delete("r1")
        eng.create(rule(actions=[{"kind": "tool", "tool": "set_light", "params": {"targets": "office-1", "brightness": 200}}]))
        assert (await eng.tick())["errors"][0]["error"] == "ValidationError"
        eng.delete("r1")
        eng.create(rule(actions=[{"kind": "tool", "tool": "automation_tick"}]))
        assert (await eng.tick())["errors"][0]["error"] == "Forbidden"
        eng.delete("r1")
        reg.cfg = replace(reg.cfg, tier=0)
        eng.create(rule(actions=[{"kind": "tool", "tool": "set_power", "params": {"targets": "desk-lamp", "on": False}}]))
        assert (await eng.tick())["errors"][0]["error"] == "Forbidden"
        assert devices["lamp"].is_on
        eng.delete("r1")
        reg.cfg = replace(reg.cfg, tier=3)
        async def guarded_alarm(device: str, confirm: str | None = None):
            await caller.call(reg.resolve_one(device), "set_alarm", confirm=confirm)
            await reg.refresh(reg.resolve_one(device), force=True)
            return {}
        declare()
        eng.tool_executor.record("guarded_alarm", Tier.SECURITY)
        eng.tool_executor.ctx.server.tool()(guarded_alarm)
        eng.create(rule(actions=[{"kind": "tool", "tool": "guarded_alarm", "params": {"device": "desk-lamp"}}]))
        assert (await eng.tick())["errors"][0]["error"] == "Forbidden"
        eng.clock.advance(86400)
        assert "errors" not in await eng.tick(confirm="set_alarm desk-lamp")
        assert caller.methods_sent() == ["set_alarm"]
        eng.clock.advance(86400)
        assert (await eng.tick(confirm="wrong"))["errors"][0]["error"] == "NeedsConfirmation"


@pytest.mark.anyio
async def test_kill_cancels_live_task_pending_work_and_restart(make_house, mcp):
    reg, caller, devices = make_house()
    async with mcp(reg, caller) as client:
        eng = reg._automation_engine
        eng.clock = FakeClock(datetime(2026, 3, 20, 12, tzinfo=timezone.utc))
        eng.create(rule(actions=[{"kind": "delay", "seconds": 100}, {"kind": "power", "device": "desk-lamp", "on": True}]))
        for _ in range(10):
            if eng.state.runs:
                break
            await asyncio.sleep(0)
        assert eng.task is not None and eng.state.runs
        result = await client.call_tool("automation_settings", {"settings": {"killed": True}})
        assert not result.is_error
        assert eng.task is None and eng.state.runs == []
        assert await eng.tick() == {"killed": True, "fired": []}
        result = await client.call_tool("automation_settings", {"settings": {"killed": False}})
        assert not result.is_error and eng.task is not None
        eng.clock.advance(100)
        assert (await eng.tick()).get("pending", 0) == 0
        assert not devices["lamp"].is_on
    assert eng.task is None


@pytest.mark.anyio
async def test_persisted_enabled_rule_restarts_continuous_loop(make_house):
    reg, caller, _ = make_house()
    eng = Engine(reg, caller)
    eng.state.rules = [rule(actions=[{"kind": "message", "message": "scheduled"}])]
    eng.save()
    second_reg, second_caller, _ = make_house()
    ctx = AreaContext(registry=second_reg, caller=second_caller)
    install_lifecycle(ctx)
    await second_reg.start(discover=False)
    assert second_reg._automation_engine.task is not None
    await second_reg.stop()
    assert second_reg._automation_engine.task is None


@pytest.mark.anyio
async def test_refreshed_state_failure_is_reported_and_stops_run(setup):
    eng, source, clock, reg, caller, devices = setup()
    async def broken_update():
        raise TimeoutError("private")
    devices["lamp"].update = broken_update
    eng.create(rule(actions=[{"kind": "power", "device": "desk-lamp", "on": True}, {"kind": "message", "message": "done"}]))
    result = await eng.tick()
    assert result["errors"][0]["error"] == "DeviceUnavailableAfterWrite"
    assert result["errors"][0]["device"] == "desk-lamp"
    assert eng.store.alerts()[0]["error"] == "DeviceUnavailableAfterWrite"
    assert len(eng.store.alerts()) == 1


@pytest.mark.anyio
async def test_delay_cursor_reserved_before_call_and_never_replayed(setup):
    eng, source, clock, reg, caller, _ = setup()
    observed = []
    def respond(entry, params):
        persisted = eng.store.load()
        observed.append(persisted.runs[0].cursor)
        return {}
    caller.respond("get_device_info", respond)
    eng.create(rule(actions=[{"kind": "method", "device": "desk-lamp", "method": "get_device_info"}]))
    await eng.tick()
    assert observed == [1]
    restarted = Engine(reg, caller, store=eng.store, source=source, clock=clock)
    await restarted.tick()
    assert observed == [1]


@pytest.mark.anyio
async def test_dry_run_does_not_consume_device_edges(setup):
    eng, source, clock, *_ = setup(sample=Sample(values={"main-door": {"is_open": False}}))
    eng.create(rule(triggers=[{"kind": "state", "predicate": {"device": "main-door", "field": "is_open", "value": True}}]))
    await eng.tick()
    source.current.values["main-door"]["is_open"] = True
    assert (await eng.tick(dry_run=True))["fired"] == ["r1"]
    assert (await eng.tick(dry_run=True))["fired"] == ["r1"]
    assert (await eng.tick())["fired"] == ["r1"]


def test_atomic_failure_keeps_previous_store(setup, monkeypatch):
    import tolpo.automation.store as store_module
    eng, *_ = setup()
    eng.create(rule())
    previous = eng.store.path.read_bytes()
    def failed_replace(*args):
        raise OSError("disk problem")
    monkeypatch.setattr(store_module.os, "replace", failed_replace)
    with pytest.raises(OSError):
        eng.create(rule("second"))
    assert [r.id for r in eng.state.rules] == ["r1"]
    assert eng.store.path.read_bytes() == previous
    assert not list(eng.store.directory.glob(".automation-*"))


def test_compact_summary_flattens_names_and_omits_parameters():
    created = rule(actions=[{"kind": "method", "device": "desk-lamp", "method": "get_device_info", "params": {"untrusted_text": "private"}}])
    out = public_rule(created)
    assert out["name"] == "Evening lights"
    assert "private" not in json.dumps(out) and "params" not in json.dumps(out)


@pytest.mark.anyio
async def test_due_security_action_accepts_only_a_new_tick_confirmation(setup):
    declare()
    eng, source, clock, reg, caller, _ = setup(tier=4)
    eng.create(rule(actions=[{"kind": "delay", "seconds": 5}, {"kind": "method", "device": "desk-lamp", "method": "set_alarm"}]))
    await eng.tick()
    clock.advance(6)
    assert "errors" not in await eng.tick(confirm="set_alarm desk-lamp")
    assert caller.methods_sent() == ["set_alarm"]


@pytest.mark.anyio
async def test_tick_and_dry_run_report_requested_changes(setup):
    eng, *_ = setup()
    eng.create(rule())
    preview = await eng.tick(dry_run=True)
    assert preview["actions"][0]["device"] == "desk-lamp"
    assert preview["actions"][0]["on"] is True
    result = await eng.tick()
    assert result["actions"][0] == {"rule": "r1", "action": 0, "kind": "power", "device": "desk-lamp", "on": True, "ok": True}


@pytest.mark.anyio
async def test_corrupt_automation_store_refuses_tools_without_breaking_server(make_house, mcp, tmp_path, caplog):
    (tmp_path / "automation.json").write_text("{")
    reg, caller, _ = make_house()
    async with mcp(reg, caller) as client:
        assert "automation store could not be loaded" in caplog.text
        result = await client.call_tool("automation_list", {})
        assert result.is_error
        result = await client.call_tool("list_devices", {})
        assert not result.is_error


@pytest.mark.anyio
async def test_mcp_validation_never_echoes_private_input(make_house, mcp):
    reg, caller, _ = make_house()
    async with mcp(reg, caller) as client:
        payload = rule().model_dump(mode="json")
        payload["actions"] = [{"kind": "method", "method": "set_device_info", "device": "desk-lamp", "params": {"password": "FAKE-PRIVATE-INPUT"}}]
        result = await client.call_tool("automation_create", {"rule": payload})
        assert result.is_error
        assert "FAKE-PRIVATE-INPUT" not in str(result)
        result = await client.call_tool("automation_settings", {"settings": {"timezone": "FAKE-PRIVATE-INPUT"}})
        assert result.is_error
        assert "FAKE-PRIVATE-INPUT" not in str(result)


def test_store_refuses_invalid_cursor_and_missing_rule(setup):
    eng, *_ = setup()
    eng.create(rule())
    original = json.loads(eng.store.path.read_text())
    for run in ({"rule": "r1", "cursor": -1, "due": 1}, {"rule": "missing", "cursor": 0, "due": 1}):
        modified = dict(original, runs=[run])
        eng.store.path.write_text(json.dumps(modified))
        with pytest.raises(ValueError):
            eng.store.load()


@pytest.mark.anyio
async def test_camera_privacy_cannot_use_power_even_when_feature_opted_in(setup):
    eng, source, clock, reg, caller, devices = setup(tier=4)
    reg.cfg = replace(reg.cfg, allow_write=frozenset({"camera.state"}))
    eng.create(rule(actions=[{"kind": "power", "device": "hallway-cam", "on": False}]))
    assert (await eng.tick())["errors"][0]["error"] == "Forbidden"
    assert devices["cam"].is_on


@pytest.mark.anyio
async def test_camera_privacy_cannot_use_power_tool_even_when_feature_opted_in(make_house, mcp, monkeypatch):
    reg, caller, devices = make_house(tier=4, allow_write=frozenset({"camera.state"}))
    async with mcp(reg, caller) as client:
        eng = reg._automation_engine
        eng.clock = FakeClock(datetime(2026, 3, 20, 12, tzinfo=timezone.utc))
        monkeypatch.setattr(eng, "start", lambda: None)
        eng.create(rule(actions=[{"kind": "tool", "tool": "set_power", "params": {"targets": "hallway-cam", "on": False}}]))
        assert (await eng.tick())["errors"][0]["error"] == "Forbidden"
        assert devices["cam"].is_on


# ---- review: file safety, bounded work, kill switch, one engine, policy namespaces ----------------------------------


def test_outbox_and_store_refuse_symlinks_and_force_private_mode(tmp_path):
    import os
    victim = tmp_path / "victim.jsonl"
    victim.write_text('{"secret": "FAKE-PRIVATE"}\n')
    store = Store(tmp_path)
    os.symlink(victim, store.outbox)
    with pytest.raises(ValueError, match="symbolic link"):
        store.alerts()
    with pytest.raises(ValueError, match="symbolic link"):
        store.alert({"message": "x"})
    assert victim.read_text() == '{"secret": "FAKE-PRIVATE"}\n'
    os.unlink(store.outbox)
    os.symlink(victim, store.path)
    with pytest.raises(ValueError, match="symbolic link"):
        store.load()
    os.unlink(store.path)
    store.outbox.write_text("")
    os.chmod(store.outbox, 0o644)
    store.alert({"message": "x"})
    assert store.outbox.stat().st_mode & 0o777 == 0o600


def test_outbox_is_bounded_and_survives_a_corrupt_line(tmp_path, monkeypatch):
    import tolpo.automation.store as store_module
    monkeypatch.setattr(store_module, "MAX_OUTBOX_BYTES", 2000)
    store = Store(tmp_path)
    store.outbox.write_text("not json\n")
    assert store.alerts() == [{"error": "UnreadableAlert"}]
    for i in range(200):
        store.alert({"message": f"alert {i}"})
    assert store.outbox.stat().st_size <= 2000
    rows = store.alerts(limit=1000)
    assert rows[0]["error"] == "AlertsDropped" and rows[0]["dropped"] > 0
    assert rows[-1] == {"message": "alert 199"}


def test_long_rule_chains_are_checked_in_linear_time(setup):
    import time
    eng, *_ = setup()
    started = time.monotonic()
    for i in range(200):
        triggers = [{"kind": "rule", "rule": f"r{j}"} for j in range(max(0, i - 16), i)] or [{"kind": "clock", "time": "12:00"}]
        eng.create(rule(f"r{i}", triggers=triggers, actions=[{"kind": "message", "message": "m"}]))
    assert time.monotonic() - started < 10
    eng.delete("r0")
    with pytest.raises(ValueError, match="cycle"):
        eng.create(rule("r0", triggers=[{"kind": "rule", "rule": "r199"}]))


def test_store_refuses_a_saved_cycle(setup):
    eng, *_ = setup()
    eng.create(rule("a", triggers=[{"kind": "rule", "rule": "b"}]))
    eng.create(rule("b", triggers=[{"kind": "rule", "rule": "c"}]))
    raw = json.loads(eng.store.path.read_text())
    raw["rules"].append(rule("c", triggers=[{"kind": "rule", "rule": "a"}]).model_dump(mode="json"))
    eng.store.path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="cycle"):
        Store(eng.store.directory).load()


@pytest.mark.anyio
async def test_kill_switch_stops_a_tick_that_is_stuck_in_an_action(make_house, mcp):
    reg, caller, devices = make_house()
    entered, never = asyncio.Event(), asyncio.Event()

    async def stuck_turn_on():
        entered.set()
        await never.wait()

    devices["lamp"].turn_on = stuck_turn_on
    async with mcp(reg, caller) as client:
        eng = reg._automation_engine
        eng.clock = FakeClock(datetime(2026, 3, 20, 12, tzinfo=timezone.utc))
        eng.create(rule(actions=[{"kind": "power", "device": "desk-lamp", "on": True},
                                 {"kind": "message", "message": "after"}]))
        await asyncio.wait_for(entered.wait(), 5)
        result = await asyncio.wait_for(client.call_tool("automation_settings", {"settings": {"killed": True}}), 5)
        assert not result.is_error
        assert eng.task is None and eng.state.runs == [] and eng.state.settings.killed
        assert eng.store.alerts() == []  # the action after the stuck one never ran
        assert Store(eng.store.directory).load().settings.killed


@pytest.mark.anyio
async def test_halt_stops_remaining_actions_of_a_running_tick(setup):
    eng, source, clock, reg, caller, devices = setup()

    class Halting:
        def plan(self, name, params):
            return {"level": 0, "allowed": True}

        async def execute(self, name, params, *, confirm):
            await eng.halt()

    eng.tool_executor = Halting()
    eng.create(rule(actions=[{"kind": "tool", "tool": "stop"}, {"kind": "power", "device": "desk-lamp", "on": True}]))
    await eng.tick()
    assert devices["lamp"].is_on is False


@pytest.mark.anyio
async def test_kill_from_another_process_stops_this_engine(setup):
    eng, source, clock, reg, caller, devices = setup()
    eng.create(rule(actions=[{"kind": "power", "device": "desk-lamp", "on": True}]))
    other = Engine(reg, caller, store=Store(eng.store.directory), source=source, clock=clock)
    other.kill()
    other.save()
    assert await eng.tick() == {"killed": True, "fired": []}
    assert devices["lamp"].is_on is False
    assert eng.state.settings.killed


@pytest.mark.anyio
async def test_only_one_engine_runs_per_state_directory(make_house, tmp_path):
    reg, caller, _ = make_house()
    first = Engine(reg, caller, store=Store(tmp_path))
    second = Engine(reg, caller, store=Store(tmp_path))
    for eng in (first, second):
        eng.state.rules = [rule()]
        eng.start()
    assert first.task is not None
    assert second.task is None
    await first.stop()
    second.start()
    assert second.task is not None
    await second.stop()


@pytest.mark.anyio
async def test_allowed_methods_do_not_widen_a_tool_of_the_same_name(make_house, mcp, monkeypatch):
    reg, caller, devices = make_house(tier=1, allow_methods={"set_alarm", "guarded_alarm"})
    async with mcp(reg, caller) as client:
        eng = reg._automation_engine
        eng.clock = FakeClock(datetime(2026, 3, 20, 12, tzinfo=timezone.utc))
        monkeypatch.setattr(eng, "start", lambda: None)

        async def guarded_alarm(device: str, confirm: str | None = None):
            await caller.call(reg.resolve_one(device), "set_alarm", confirm=confirm)
            return {}

        declare()
        eng.tool_executor.record("guarded_alarm", Tier.SECURITY)
        eng.tool_executor.ctx.server.tool()(guarded_alarm)
        eng.create(rule(actions=[{"kind": "tool", "tool": "guarded_alarm", "params": {"device": "desk-lamp"}}]))
        assert (await eng.tick(confirm="set_alarm desk-lamp"))["errors"][0]["error"] == "Forbidden"
        assert caller.sent == []


@pytest.mark.anyio
async def test_one_confirmation_runs_one_security_action(setup):
    declare()
    eng, source, clock, reg, caller, _ = setup(tier=4)
    alarm = [{"kind": "method", "device": "desk-lamp", "method": "set_alarm"}]
    eng.create(rule("a", actions=alarm))
    eng.create(rule("b", actions=alarm))
    result = await eng.tick(confirm="set_alarm desk-lamp")
    assert caller.methods_sent() == ["set_alarm"]
    assert result["errors"][0]["rule"] == "b"


@pytest.mark.anyio
async def test_camera_event_matches_the_registry_spelling(setup):
    # registry.jsonable turns python-kasa's LastAlertType.Motion into "Motion".
    sample = Sample(values={"camera": {"last_alert_timestamp": "2026-03-20T11:59:00", "last_alert_type": "Motion"}})
    eng, source, clock, *_ = setup(sample=sample)
    eng.create(rule(triggers=[{"kind": "camera", "device": "camera", "event": "motion"}], actions=[{"kind": "message", "message": "m"}]))
    await eng.tick()
    source.current.values["camera"]["last_alert_timestamp"] = "2026-03-20T12:00:00"
    assert (await eng.tick())["fired"] == ["r1"]


@pytest.mark.anyio
async def test_trigger_log_matches_the_sensor_fixture_and_is_refused_on_the_hub(make_house):
    # The exact answer of a T110 behind an H200 (tapo-re fixtures, sensor/T110/trigger_logs.json).
    reg, caller, _ = make_house(responses={"get_trigger_logs": {"logs": [], "start_id": 0, "sum": 0}})
    sample = await RegistrySource(reg, caller).sample([rule(triggers=[{"kind": "sensor", "device": "main-door"}])])
    assert sample.logs == {"main-door": []} and sample.errors == []
    with pytest.raises(Forbidden):
        await caller.call(reg.resolve_one("tapo-h200"), "get_trigger_logs", {"start_id": 0})


@pytest.mark.anyio
async def test_idle_ticks_do_not_rewrite_the_store(setup):
    eng, *_ = setup()
    eng.create(rule(triggers=[{"kind": "clock", "time": "03:00"}]))
    before = eng.store.path.stat().st_ino
    for _ in range(3):
        await eng.tick()
    assert eng.store.path.stat().st_ino == before
