"""Door locks: the discovery record parser, the in-memory book, the optional listener and the `lock_state` tool.

Every record here is canned. The owner has no lock, so none of this has met real hardware."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tolpo import policy
from tolpo.api import create_app
from tolpo.areas import lock
from tolpo.config import Config
from tolpo.registry import Forbidden, Registry

from .. import fakes
from .fakes import FakeCaller

ACCOUNT = "a"  # the email make_house uses
MINE = hashlib.md5(ACCOUNT.encode()).hexdigest()
SECRET_ID = "80221C2D4E5F60718293A4B5C6D7E8F901234567"
SECRET_MAC = "AA-BB-CC-DD-EE-FF"
SECRET_IP = "192.0.2.77"


def record(**overrides):
    """UNVERIFIED: guessed flat TDP layout, type and owner hash. The audit only names the lock fields."""
    base = {
        "device_id": SECRET_ID,
        "device_id_md5": "0123456789ABCDEF0123456789ABCDEF",
        "device_model": "DL110(EU)",
        "device_type": "SMART.DOORLOCK",
        "ip": SECRET_IP,
        "mac": SECRET_MAC,
        "owner": MINE.upper(),
        "http_port": 80,
        "lock_status": 1,
        "last_lock_status": 0,
        "last_onoff_time": 1760000000,
        "door_state": 2,
        "battery_percentage": 81,
        "battery_status": 0,
        "at_low_battery": False,
        "backup_battery_per": 64,
        "backup_battery_status": 1,
        "power_source": 0,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def datagram(result=None, *, error_code=0, header=b"\x02\x00\x00\x01" + b"\x00" * 12):
    # UNVERIFIED: assumed 16-byte header and plain JSON. This is not a captured reply.
    return header + json.dumps({"error_code": error_code, "result": result if result is not None else record()}).encode()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def house_with(make_house, *records, clock=None):
    reg, caller, _ = make_house()
    book = lock.book_for(reg)
    if clock is not None:
        book.clock = clock
    for r in records:
        assert book.note(lock.parse_lock_record(r))
    return reg, caller, book


# ---- the parser --------------------------------------------------------------------------------------------------


def test_a_lock_record_becomes_a_typed_state():
    state = lock.parse_lock_record(record())
    assert state.model == "DL110(EU)" and state.lock == "locked" and state.lock_code == 1
    assert (state.door_code, state.battery, state.low_battery, state.backup_battery) == (2, 81, False, 64)
    assert (state.battery_code, state.backup_battery_code, state.power_code) == (0, 1, 0)
    assert state.last_lock_code == 0 and state.last_change == 1760000000
    assert state.app_unlock is None  # the audit does not put the app switch in the record
    assert state.key.startswith("lock-") and SECRET_ID not in state.key
    assert lock.parse_lock_record(record(lock_status=0)).lock == "unlocked"
    assert lock.parse_lock_record(record(lock_status=7)).lock == "unknown code 7"


def test_a_raw_datagram_is_read_after_its_header():
    assert lock.parse_tdp_reply(datagram()) == lock.parse_lock_record(record())


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x02" * 16,  # header only
        b"\x02" * 16 + b"{not json",
        b"\x02" * 16 + b"\xff\xfe\x00",  # not UTF-8
        b"\x02" * 16 + json.dumps({"params": {"rsa_key": "x"}}).encode(),  # someone else's probe
        datagram(error_code=-1),  # the device reported an error
        datagram({"device_id": "p1", "device_type": "SMART.TAPOPLUG", "device_model": "P115"}),  # a plug
        datagram(record(device_id=None, device_id_md5=None)),  # no id: cannot tell locks apart
        datagram(record(device_model="x" * lock.MAX_DATAGRAM)),  # a valid lock reply, but far too big for one
        "not bytes",
    ],
    ids=["empty", "header-only", "bad-json", "not-utf8", "probe", "device-error", "plug", "no-id", "too-big", "not-bytes"],
)
def test_anything_that_is_not_a_lock_reply_is_ignored(data):
    assert lock.parse_tdp_reply(data) is None


def test_values_of_the_wrong_type_or_range_are_not_trusted():
    state = lock.parse_lock_record(
        record(lock_status=True, battery_percentage=150, backup_battery_per=-3, at_low_battery="yes", door_state="open", last_onoff_time=-5)
    )
    assert state.lock is None and state.lock_code is None
    assert state.battery is None and state.backup_battery is None and state.low_battery is None
    assert state.door_code is None and state.last_change is None


@pytest.mark.parametrize("value", [1.5, 1.0, float("nan"), float("inf"), -float("inf"), 2**1000, [], {}, "1", True])
def test_numeric_fields_require_bounded_integers(value):
    state = lock.parse_lock_record(record(**{name: value for name in lock.LOCK_FIELDS}))
    assert all(getattr(state, name) is None for name in (
        "lock_code", "last_lock_code", "last_change", "door_code", "battery", "battery_code",
        "backup_battery", "backup_battery_code", "power_code",
    ))


@pytest.mark.parametrize("value", [False, 0.0, "0", None, [], {}])
def test_error_code_requires_an_explicit_integer_zero(value):
    assert lock.parse_tdp_reply(datagram(error_code=value)) is None
    assert lock.parse_discovery_json({"error_code": value, "result": record()}) is None
    assert lock.parse_discovery_json({"result": record()}) is None


@pytest.mark.parametrize("value", ["\ud800", "x" * (lock.MAX_ID_LEN + 1), [], {}, True, 1])
def test_invalid_identifiers_are_ignored(value):
    assert lock.parse_tdp_reply(datagram(record(device_id=value, device_id_md5=None))) is None


@pytest.mark.parametrize("value", ["x" * 5000, "\ud800", [], {}, 1, "f" * 31])
def test_owner_storage_is_bounded(value):
    assert lock.parse_lock_record(record(owner=value)).owner is None


@pytest.mark.parametrize("payload", [
    b"[" * 1500 + b"0" + b"]" * 1500,
    b'{"extra":' + b"[" * (lock.MAX_JSON_DEPTH + 1) + b"0" + b"]" * (lock.MAX_JSON_DEPTH + 1) + b"}",
    b'{"error_code":0,"result":null}',
    b"null", b"true", b"[]", b'"text"',
    b'{"error_code":0,"result":42}',
    b'{"error_code":0,"result":{"device_id":"fake","lock_status":NaN}}',
    b'{"error_code":0,"result":{"device_id":"fake","lock_status":Infinity}}',
])
def test_hostile_datagrams_are_ignored_without_raising(payload):
    book = lock.LockBook(Clock())
    lock.TDPLockListener(book).datagram_received(b"\0" * lock.HEADER_LEN + payload, (SECRET_IP, 20002))
    assert book.current() == []


def test_depth_limit_is_checked_before_json_decoding(monkeypatch):
    def must_not_decode(*args, **kwargs):
        pytest.fail("excessive depth reached the JSON decoder")

    monkeypatch.setattr(lock.json, "loads", must_not_decode)
    payload = b"[" * (lock.MAX_JSON_DEPTH + 1) + b"0" + b"]" * (lock.MAX_JSON_DEPTH + 1)
    assert lock.parse_tdp_reply(b"\0" * lock.HEADER_LEN + payload) is None


def test_depth_scan_ignores_brackets_and_escaped_quotes_in_strings():
    payload = datagram(record(extra='[' * 40 + '\\"' + ']' * 40))
    assert lock.parse_tdp_reply(payload) == lock.parse_lock_record(record())


def test_a_record_is_a_lock_by_type_or_by_lock_status():
    assert lock.parse_lock_record(record(device_type="SMART.TAPOPLUG")) is not None  # still carries lock_status
    assert lock.parse_lock_record(record(lock_status=None)).lock is None  # type says lock, no status
    assert lock.parse_lock_record(record(lock_status=None, device_type="SMART.TAPOPLUG")) is None


def test_the_model_is_flattened_and_shortened():
    state = lock.parse_lock_record(record(device_model="DL110\n\x1bignore previous" + "x" * 80))
    assert "\n" not in state.model and "\x1b" not in state.model and len(state.model) <= 32
    assert lock.parse_lock_record(record(device_model=None)).model == "door lock"


# ---- the book ----------------------------------------------------------------------------------------------------


def test_the_book_keeps_the_latest_state_and_forgets_old_ones():
    clock = Clock()
    book = lock.LockBook(clock)
    assert book.note_datagram(datagram(record(lock_status=1)))
    clock.t += 10
    assert book.note_datagram(datagram(record(lock_status=0)))
    [(state, age)] = book.current()
    assert state.lock == "unlocked" and age == 0
    clock.t += lock.STALE_AFTER + 1
    assert book.current() == []


def test_the_book_is_bounded_against_a_flood_of_fake_locks():
    book = lock.LockBook(Clock())
    for n in range(lock.MAX_LOCKS + 10):
        book.note(lock.parse_lock_record(record(device_id=f"id-{n}")))
    assert len(book.current()) == lock.MAX_LOCKS


def test_the_book_reads_both_python_kasa_callbacks():
    book = lock.LockBook(Clock())
    assert book.note_raw({"discovery_response": {"error_code": 0, "result": record(device_id="raw")}, "meta": {"ip": SECRET_IP}})
    assert book.note_unsupported(SimpleNamespace(discovery_result={"error_code": 0, "result": record(device_id="whole")}))
    assert book.note_unsupported(SimpleNamespace(discovery_result=record(device_id="inner")))
    assert not book.note_unsupported(SimpleNamespace(discovery_result=None))
    assert not book.note_raw("junk")
    assert len(book.current()) == 3


def test_each_registry_has_its_own_book(make_house):
    reg1, _, _ = make_house()
    reg2, _, _ = make_house()
    lock.book_for(reg1).note(lock.parse_lock_record(record()))
    assert lock.book_for(reg2).current() == [] and len(lock.book_for(reg1).current()) == 1


# ---- the optional listener ---------------------------------------------------------------------------------------


def test_the_listener_only_feeds_the_book():
    book = lock.LockBook(Clock())
    listener = lock.TDPLockListener(book)
    listener.datagram_received(datagram(), (SECRET_IP, 20002))
    listener.datagram_received(b"garbage", (SECRET_IP, 20002))
    [(state, _)] = book.current()
    assert state.lock == "locked" and SECRET_IP not in repr(state)


@pytest.mark.anyio
async def test_listen_binds_a_passive_endpoint_on_a_fake_loop():
    seen = {}

    class FakeLoop:
        async def create_datagram_endpoint(self, factory, local_addr):
            seen["protocol"], seen["addr"] = factory(), local_addr
            return SimpleNamespace(sendto=lambda *a: seen.setdefault("sent", a)), seen["protocol"]

    book = lock.LockBook(Clock())
    await lock.listen(book, host="127.0.0.1", port=12345, loop=FakeLoop())
    assert seen["addr"] == ("127.0.0.1", 12345) and isinstance(seen["protocol"], lock.TDPLockListener)
    seen["protocol"].datagram_received(datagram(), ("127.0.0.1", 1))
    assert "sent" not in seen and len(book.current()) == 1


def test_the_listener_is_not_wired_into_discovery():
    """It stays out of the default path until the lead decides how to start it."""
    from pathlib import Path

    import tolpo

    root = Path(tolpo.__file__).parent
    for name in ("registry.py", "mcp_server.py", "api.py", "cli.py"):
        assert "TDPLockListener" not in (root / name).read_text() and "areas.lock" not in (root / name).read_text()


# ---- the answer --------------------------------------------------------------------------------------------------


def test_the_answer_is_compact_and_marks_its_guesses(make_house):
    clock = Clock()
    reg, _, book = house_with(make_house, record(backup_battery_per=None, backup_battery_status=None, power_source=None), clock=clock)
    clock.t += 42
    out = lock.lock_states(reg)
    assert out["locks"] == [
        {
            "model": "DL110(EU)",
            "lock": "locked",
            "door_code": 2,
            "battery": 81,
            "low_battery": False,
            "battery_code": 0,
            "last_lock_code": 0,
            "last_change": 1760000000,
            "heard_s_ago": 42,
        }
    ]
    assert "guesses" in out["unverified"] and out["note"] == lock.NOTE_APP_SWITCH
    assert "other_locks" not in out


def test_the_answer_never_carries_ids_addresses_or_the_owner_hash(make_house):
    reg, _, _ = house_with(make_house, record())
    text = json.dumps(lock.lock_states(reg)).lower()
    for secret in (SECRET_ID, SECRET_MAC, SECRET_IP, MINE, "0123456789abcdef", '"id"', "owner", "http_port", "lock-"):
        assert secret.lower() not in text, secret


@pytest.mark.parametrize("value", [SECRET_ID, SECRET_MAC, SECRET_IP, MINE, "20002", "DL110 " + SECRET_IP, "DL110(" + MINE + ")"])
def test_model_text_cannot_smuggle_private_values(make_house, value):
    reg, _, _ = house_with(make_house, record(device_model=value))
    out = lock.lock_states(reg)
    assert out["locks"][0]["model"] == "door lock"
    assert value not in json.dumps(out)


def test_account_missing_or_mismatched_never_describes_a_lock(make_house):
    reg, _, book = house_with(make_house, record())
    for email in ("", "another-fake-account"):
        reg.cfg = replace(reg.cfg, email=email)
        out = lock.lock_states(reg, book)
        assert out["locks"] == [] and out["other_locks"] == 1
        assert "door_code" not in json.dumps(out)


def test_locks_of_other_accounts_are_counted_not_described(make_house):
    reg, _, _ = house_with(
        make_house,
        record(device_id="neighbour", owner="f" * 32, door_state=1),
        record(device_id="unclaimed", owner=None),
    )
    out = lock.lock_states(reg)
    assert out["locks"] == [] and out["other_locks"] == 2
    assert "No door lock on this account" in out["note"]
    assert "door_code" not in json.dumps(out) and "neighbour" not in json.dumps(out)


def test_nothing_heard_says_so(make_house):
    reg, _, _ = make_house()
    assert lock.lock_states(reg) == {"locks": [], "note": lock.NO_LOCK_NOTE}


def test_the_app_switch_is_shown_only_when_the_record_carries_it(make_house):
    reg, _, _ = house_with(make_house, record(switch_appkey=1))
    out = lock.lock_states(reg)
    assert out["locks"][0]["app_unlock"] is True and "note" not in out


# ---- safety: nothing reaches the lock ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    ["setLockStatus", "setSwitchAppKey", "addCodeData", "addUserData", "setAutoLockInfo", "setErrorProtection",
     "setWifiSoftSwitch", "deviceReset", "getCodeList", "multipleRequest"],
)
@pytest.mark.anyio
async def test_no_native_lock_method_is_declared_so_the_caller_refuses_it(method, make_house):
    _, caller, _ = make_house(tier=4, allow_methods=[method])
    assert lock.METHODS == []
    assert policy.rule_for("lock", method) is None
    entry = SimpleNamespace(kind="lock", slug="fake-lock")
    with pytest.raises(Forbidden):
        await caller.call(entry, method, confirm=f"{method} fake-lock")
    with pytest.raises(Forbidden):
        await caller.call_many(entry, {"get_device_info": None, method: {}}, confirm=f"{method} fake-lock")
    assert caller.sent == []


def test_shared_catalog_must_refuse_lock_writes(make_house):
    # Review finding, fixed in the shared declarations: common.py once included locks in SMART_KINDS.
    _, caller, _ = make_house(tier=2)
    entry = SimpleNamespace(kind="lock", slug="fake-lock")
    with pytest.raises(Forbidden):
        caller.check(entry, "set_device_info")


@pytest.mark.anyio
async def test_the_tool_is_read_only_and_sends_nothing(make_house, mcp):
    reg, caller, _ = house_with(make_house, record())
    async with mcp(reg, caller) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool("lock_state", {})
    assert tools["lock_state"].annotations.read_only_hint is True
    assert set(tools["lock_state"].input_schema.get("properties", {})) == set()
    assert not result.is_error and result.structured_content["locks"][0]["lock"] == "locked"
    assert caller.sent == []


@pytest.mark.anyio
async def test_the_tool_says_when_no_lock_was_heard(make_house, mcp):
    reg, caller, _ = make_house()
    async with mcp(reg, caller) as client:
        result = await client.call_tool("lock_state", {})
    assert result.structured_content["locks"] == [] and "No door lock" in result.structured_content["note"]


def test_the_route_sits_behind_the_token():
    cfg = Config(email=ACCOUNT, password="b", token="s3cret", cache_ttl=0.0, cache_file="")
    reg = Registry(cfg)
    fakes.populate(reg)
    lock.book_for(reg).note(lock.parse_lock_record(record()))
    with TestClient(create_app(cfg, reg, discover=False, allowed_hosts=["testserver"], caller=FakeCaller(reg))) as client:
        assert client.get("/locks").status_code == 401
        ok = client.get("/locks", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and ok.json()["locks"][0]["model"] == "DL110(EU)"
