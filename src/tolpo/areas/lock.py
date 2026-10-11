"""Tapo door locks (DL100, DL105, DL110, DL130, DLW10): read-only state from the lock's discovery record.

python-kasa does not know the lock family, so tolpo cannot log in to a lock. The audit names lock, door and battery
fields in TDP discovery. UNVERIFIED: their wire layout and whether discovery is unauthenticated. This module parses
an assumed reply into a typed state, keeps a bounded cache, and offers one read-only tool. Neither discovery feed
is connected automatically, so normal use sees an empty cache.

It sends no device methods at all, so METHODS is empty. Lock, unlock, codes, users and every setting need the lock's
own client (KLAP with an extra handshake step, BLE or the cloud) and, for lock and unlock, a per-device control key.
None of that is here. See docs/areas/lock.md for the design of the rest.

Nothing here has been run against a real lock. The owner has none; every test uses canned records.

Do NOT add `from __future__ import annotations` here: the MCP SDK reads the tool signatures at runtime.
"""

import asyncio
import hashlib
import json
import re
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..policy import Tier
from ..views import clean

METHODS: list = []  # this area sends nothing; only explicitly fed discovery records enter the cache

TDP_PORT = 20002
HEADER_LEN = 16  # UNVERIFIED for locks: assume python-kasa's 16-byte header followed by plain JSON
MAX_DATAGRAM = 4096  # defensive limit, not a verified lock reply size
MAX_JSON_DEPTH = 16  # reject excessive nesting before the JSON decoder allocates containers
MAX_ID_LEN = 128
MAX_LOCKS = 32  # a house has a handful; this bounds what spoofed datagrams can make us remember
STALE_AFTER = 3600.0  # seconds; an older record is dropped, because the door may have changed since

# The fields of the lock's discovery record, as named in the audit (row L1). Their meanings beyond the name are
# not documented anywhere we have read; the codes are passed on raw where the meaning is unknown.
LOCK_FIELDS = (
    "lock_status",
    "last_lock_status",
    "last_onoff_time",
    "door_state",
    "battery_percentage",
    "battery_status",
    "at_low_battery",
    "backup_battery_per",
    "backup_battery_status",
    "power_source",
)

# UNVERIFIED: the audit records the lock command's enum as OFF 0 and ON 1, with ON presumably meaning locked
# (inferred from the app's lock helper). That the discovery record uses the same codes is a further guess.
LOCK_WORDS = {0: "unlocked", 1: "locked"}

NOTE_APP_SWITCH = (
    "The app lock and unlock switch is not listed in the discovery audit. Reading it needs the lock's own client, "
    "which tolpo does not have yet."
)
NO_LOCK_NOTE = (
    "No door lock on this account has been heard on the network. Discovery feeds are not connected automatically."
)
UNVERIFIED_NOTE = (
    "UNVERIFIED: wire layout, account matching, code meanings and locked/unlocked words, last_change time, "
    "and app switch presence are guesses. Discovery records are not authenticated by this parser."
)
MODEL_PATTERN = re.compile(r"(?:DL100|DL105|DL110|DL130|DLW10)(?:\((?:EU|US|UK)\))?")


# ---- the record ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LockState:
    """What one lock said about itself in its discovery record. None means the record did not carry the field."""

    key: str  # full digest for internal deduplication; no identifier is returned
    model: str
    lock: str | None  # "locked", "unlocked" or "unknown code N" (UNVERIFIED mapping)
    lock_code: int | None
    last_lock_code: int | None
    last_change: int | None  # UNVERIFIED: `last_onoff_time`, probably a Unix time of the last lock change
    door_code: int | None  # UNVERIFIED: `door_state` values are not documented (open, closed, no sensor...)
    battery: int | None  # percent
    battery_code: int | None  # UNVERIFIED meaning
    low_battery: bool | None
    backup_battery: int | None  # percent
    backup_battery_code: int | None  # UNVERIFIED meaning
    power_code: int | None  # UNVERIFIED meaning of `power_source`
    app_unlock: bool | None  # UNVERIFIED: only if the record ever carries `switch_appkey`; the audit does not list it
    owner: str | None  # the owner hash from the record; kept inside, never returned


def _int(value: Any, low: int = -(2**31), high: int = 2**31 - 1) -> int | None:
    if type(value) is not int:
        return None
    if not low <= value <= high:
        return None
    return value


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _key(record: Mapping[str, Any]) -> str | None:
    """A bounded identifier used only inside the cache."""
    raw = record.get("device_id") or record.get("device_id_md5")
    if not isinstance(raw, str) or not 0 < len(raw) <= MAX_ID_LEN or not raw.strip():
        return None
    try:
        return "lock-" + hashlib.sha256(raw.strip().encode()).hexdigest()
    except UnicodeEncodeError:
        return None


def is_lock_record(record: Mapping[str, Any]) -> bool:
    """A discovery result describes a lock when its type says so or it carries the lock status.
    UNVERIFIED: the app names the type SMART.DOORLOCK in analytics; the record's own device_type was never seen."""
    kind = record.get("device_type")
    return (isinstance(kind, str) and len(kind) <= 64 and "DOORLOCK" in kind.upper()) or "lock_status" in record


def parse_lock_record(record: Any) -> LockState | None:
    """The `result` object of a TDP reply, as a typed lock state. Anything that is not a lock record gives None.

    Pure: no I/O, no clock. Values of the wrong type or out of range become None instead of being trusted."""
    # UNVERIFIED: lock fields are assumed flat in result alongside device_model and owner; no lock fixture exists.
    if not isinstance(record, Mapping) or not is_lock_record(record):
        return None
    key = _key(record)
    if key is None:
        return None
    model = record.get("device_model")
    code = _int(record.get("lock_status"))
    owner = record.get("owner")
    return LockState(
        key=key,
        # Use only known model labels. Free text can carry identifiers or secrets even after clean().
        model=clean(model, 32) if isinstance(model, str) and MODEL_PATTERN.fullmatch(model) else "door lock",
        lock=None if code is None else LOCK_WORDS.get(code, f"unknown code {code}"),
        lock_code=code,
        last_lock_code=_int(record.get("last_lock_status")),
        last_change=_int(record.get("last_onoff_time"), low=0, high=2**63 - 1),
        door_code=_int(record.get("door_state")),
        battery=_int(record.get("battery_percentage"), 0, 100),
        battery_code=_int(record.get("battery_status")),
        low_battery=_bool(record.get("at_low_battery")),
        backup_battery=_int(record.get("backup_battery_per"), 0, 100),
        backup_battery_code=_int(record.get("backup_battery_status")),
        power_code=_int(record.get("power_source")),
        app_unlock=_bool(record.get("switch_appkey")),
        # UNVERIFIED: assume an MD5 hex owner. Keep only this bounded form internally.
        owner=owner.lower() if isinstance(owner, str) and re.fullmatch(r"[0-9a-fA-F]{32}", owner) else None,
    )


def _bounded_json_depth(data: bytes) -> bool:
    """Scan bounded bytes, ignoring braces and escapes inside JSON strings."""
    depth, quoted, escaped = 0, False, False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif byte in (93, 125):
            depth -= 1
    return True


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def parse_tdp_reply(data: bytes) -> LockState | None:
    """Parse the UNVERIFIED layout: a header, then JSON `{"error_code": 0, "result": {...}}`.
    Probes, broken datagrams and replies from other devices give None. This parser does not decrypt anything."""
    if not isinstance(data, (bytes, bytearray)) or not HEADER_LEN < len(data) <= MAX_DATAGRAM:
        return None
    # UNVERIFIED: assume a 16-byte header and plain UTF-8 JSON, as for other TDP families. No lock capture exists.
    payload = bytes(data[HEADER_LEN:])
    if not _bounded_json_depth(payload):
        return None
    try:
        body = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return parse_discovery_json(body)


def parse_discovery_json(body: Any) -> LockState | None:
    """The decoded JSON of a TDP reply, as python-kasa hands it over."""
    if not isinstance(body, Mapping) or type(body.get("error_code")) is not int or body["error_code"] != 0:
        return None
    return parse_lock_record(body.get("result"))


def owner_hash(email: str) -> str | None:
    """The owner hash python-kasa sends for an account: MD5 of the user name, in hex.
    UNVERIFIED for locks: that the lock's `owner` field uses the same hash."""
    return hashlib.md5(email.encode()).hexdigest() if email else None


# ---- what was seen -----------------------------------------------------------------------------------------------


class LockBook:
    """The latest state of each lock heard on the network. In memory only; nothing is written to disk."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._seen: dict[str, tuple[LockState, float]] = {}

    def note(self, state: LockState | None) -> bool:
        if state is None:
            return False
        if state.key not in self._seen and len(self._seen) >= MAX_LOCKS:
            self._forget_stale()
            if len(self._seen) >= MAX_LOCKS:
                return False
        self._seen[state.key] = (state, self.clock())
        return True

    def note_datagram(self, data: bytes) -> bool:
        return self.note(parse_tdp_reply(data))

    def note_raw(self, event: Mapping[str, Any]) -> bool:
        """The shape of python-kasa's `on_discovered_raw` callback: {"discovery_response": json, "meta": {...}}."""
        return self.note(parse_discovery_json(event.get("discovery_response") if isinstance(event, Mapping) else None))

    def note_unsupported(self, exc: Exception) -> bool:
        """python-kasa's `on_unsupported` callback: the error carries the discovery JSON or its `result`."""
        found = getattr(exc, "discovery_result", None)
        if isinstance(found, Mapping) and "result" in found:
            return self.note(parse_discovery_json(found))
        return self.note(parse_lock_record(found))

    def _forget_stale(self) -> None:
        now = self.clock()
        for key in [k for k, (_, at) in self._seen.items() if now - at > STALE_AFTER]:
            del self._seen[key]

    def current(self) -> list[tuple[LockState, float]]:
        """Fresh records, most recent first. Each with how many seconds ago it was heard."""
        self._forget_stale()
        now = self.clock()
        return sorted(((s, max(0.0, now - at)) for s, at in self._seen.values()), key=lambda pair: (pair[1], pair[0].key))


_BOOKS: "weakref.WeakKeyDictionary[Any, LockBook]" = weakref.WeakKeyDictionary()


def book_for(registry: Any) -> LockBook:
    """One book per registry, so tests and servers never share what they heard."""
    book = _BOOKS.get(registry)
    if book is None:
        book = _BOOKS[registry] = LockBook()
    return book


# ---- an optional listener (not started by default) -------------------------------------------------------------


class TDPLockListener(asyncio.DatagramProtocol):
    """Feeds every datagram it gets into a LockBook. Nothing is sent and nothing else is kept.

    UNVERIFIED: whether a lock ever sends its record without being asked. TDP replies normally go back to the port
    that sent the probe. Feeding python-kasa's discovery callback is a proposed source, untested for locks."""

    def __init__(self, book: LockBook):
        self.book = book

    def datagram_received(self, data: bytes, addr: Any) -> None:  # addr is ignored on purpose
        self.book.note_datagram(data)


async def listen(book: LockBook, *, host: str = "0.0.0.0", port: int = TDP_PORT, loop: Any = None):
    """Start a passive listener. Not called anywhere in tolpo yet: it stays out of the default discovery path."""
    loop = loop or asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: TDPLockListener(book), local_addr=(host, port))
    return transport


# ---- the answer --------------------------------------------------------------------------------------------------


def lock_view(state: LockState, age: float) -> dict[str, Any]:
    """Compact: only allowed fields. Never an identifier, MAC, address, port or owner hash."""
    out: dict[str, Any] = {"model": state.model}
    for name, value in (
        ("lock", state.lock),
        ("door_code", state.door_code),
        ("battery", state.battery),
        ("low_battery", state.low_battery),
        ("battery_code", state.battery_code),
        ("backup_battery", state.backup_battery),
        ("backup_battery_code", state.backup_battery_code),
        ("power_code", state.power_code),
        ("last_lock_code", state.last_lock_code),
        ("last_change", state.last_change),
        ("app_unlock", state.app_unlock),
    ):
        if value is not None:
            out[name] = value
    out["heard_s_ago"] = int(age)
    return out


def lock_states(registry: Any, book: LockBook | None = None) -> dict[str, Any]:
    """What `lock_state` answers. Locks that do not carry this account's owner hash are counted, not described:
    a neighbour's lock on a shared network says when their door is open."""
    book = book or book_for(registry)
    mine = owner_hash(getattr(registry.cfg, "email", "") or "")
    shown, others = [], 0
    for state, age in book.current():
        if mine is not None and state.owner == mine:
            shown.append(lock_view(state, age))
        else:
            others += 1
    out: dict[str, Any] = {}
    if shown:
        out["locks"] = shown
        out["unverified"] = UNVERIFIED_NOTE
        if all("app_unlock" not in row for row in shown):
            out["note"] = NOTE_APP_SWITCH
    else:
        out["locks"] = []
        out["note"] = NO_LOCK_NOTE
    if others:
        out["other_locks"] = others
        out["other_note"] = "Records not matching this account under the assumed hash rule are counted only."
    return out


# ---- tools and routes --------------------------------------------------------------------------------------------


def tools(ctx):
    @ctx.tool(title="Door lock state", tier=Tier.READ)
    async def lock_state() -> dict[str, Any]:
        """Show cached door lock discovery state whose owner field matches this account under an UNVERIFIED hash
        assumption: guessed locked/unlocked words, raw door code, battery, and seconds since each record arrived.

        Read only. It cannot lock or unlock. Wire layout and field meanings have only fake tests. Records are not
        authenticated here. Discovery feeds are not connected automatically. If no lock was heard, it says so."""
        return lock_states(ctx.registry)


def routes(ctx):
    @ctx.route("/locks")
    async def locks() -> dict[str, Any]:
        return lock_states(ctx.registry)
