# Door locks

Tapo door locks (DL100, DL105, DL110, DL130, DLW10) speak their own protocol. python-kasa does not know them, so
tolpo cannot log in to a lock yet. This page covers the one thing tolpo can do today, and a design for the rest.

Nothing on this page has been tested on a real lock. The owner has none. Every test uses canned records shaped
after the field names in the app audit. Treat the whole area as untested on hardware.

## What it does today

The audit names lock status, door sensor and battery fields in TDP discovery (UDP port 20002). The parser accepts
an assumed reply layout and keeps the latest state in memory. Neither discovery feed is connected automatically.
Normal use therefore returns an empty cache. This area sends nothing to the lock and cannot lock or unlock.

- `lock_state`: shows cached lock records matching an UNVERIFIED account hash assumption. Returns guessed locked/unlocked words, raw door codes, battery and record age. Says when no lock was heard. Discovery feeds are not connected automatically.

The same answer is at `GET /locks` on the HTTP API, behind the usual token.

### What the answer means

UNVERIFIED: the parser assumes a 16-byte binary header followed by plain UTF-8 JSON, with an integer
`error_code` of zero and lock fields flat in `result` next to `device_model`. This follows other TDP families,
not a captured lock reply. The record's `device_type` was never seen. `SMART.DOORLOCK` comes from app analytics;
the parser also recognizes records carrying `lock_status`. No lock fixture is available.

- `lock`: `locked` or `unlocked`. UNVERIFIED. The audit records the lock command's values as 0 and 1, with 1
  presumably meaning locked. That the discovery record uses the same values is a further guess. Other values show as
  `unknown code N`.
- `door_code`, `battery_code`, `backup_battery_code`, `power_code`, `last_lock_code`: raw codes. UNVERIFIED. Their
  meanings are not documented in what we read, so tolpo passes the number on instead of guessing a word.
- `last_change`: the lock's `last_onoff_time`. UNVERIFIED. Probably a Unix time of the last lock change.
- `battery`, `backup_battery`: percent. `low_battery`: true or false.
- `app_unlock`: UNVERIFIED presence. Shown only if the record carries `switch_appkey`. The audit does not list it in the discovery record,
  so normally it is missing and the answer says that reading the app lock and unlock switch needs the lock client.
- `heard_s_ago`: seconds since the record arrived. Records older than one hour are dropped.

### Privacy and safety rules

- Only locks whose discovery record names this account as owner are described. The owner hash is MD5 of the account
  email, as python-kasa computes it for other devices. UNVERIFIED for locks. Other locks are only counted
  (`other_locks`), because a neighbour's lock on a shared network would tell when their door is open. If your own lock
  shows up only in that count, the hash rule or the record may be wrong. This is a provisional filter, not proof
  of account ownership. The parser does not authenticate records; a forged record can claim the same hash.
- The answer never carries an identifier, MAC, IP address, port or owner hash. Full device-id digests are kept only
  for internal deduplication.
- Model text must match one of the audit's model names (optionally with EU, US or UK in parentheses), then goes
  through `views.clean`. Other text becomes `door lock`. This prevents identifiers hidden in model text from leaking.
- Numeric fields require integers, excluding booleans. Percentages must be 0 to 100. Codes must fit a signed
  32-bit integer; timestamps must be nonnegative signed 64-bit integers. These are defensive bounds, not verified
  protocol ranges. Boolean fields also accept integer 0 and 1. Invalid fields are dropped.
- At most 32 locks are remembered. Datagrams over 4 KB and JSON deeper than 16 containers are ignored before
  decoding. Non-finite JSON numbers are rejected. Stored identifiers and owner hashes have fixed length bounds.
- The area declares no device methods. Native lock methods such as `setLockStatus`, including batched calls, are
  refused, even with a method allowance and confirmation. `areas/common.py` does not declare any method for locks.

### Where the records come from

The parser is a pure function. Two ways to feed it exist, and neither is switched on yet:

1. python-kasa's own discovery. Its `on_discovered_raw` callback hands over every TDP reply before python-kasa gives
   up on the unknown lock type. `LockBook.note_raw` takes exactly that shape. `LockBook.note_unsupported` takes the
   error python-kasa raises for an unknown type. This is a proposed path; the callback shapes match python-kasa,
   but no lock has been checked. TDP replies normally go back to the probing port. Wiring needs a change in
   `registry.py` (see the end of this page).
2. A passive listener on UDP 20002 (`TDPLockListener`, started with `listen`). UNVERIFIED usefulness: it only hears
   a lock if the lock sends its record without being asked. It is not started anywhere.

Until one of them is wired, `lock_state` always answers that no lock was heard.

## Design for the rest

This part is a plan, not code. It records only what the audit found. Where the audit stops, this page stops.

### The lock's own client

The lock uses a JSON request and response format of its own: a method name and params in, an error code, the method
and a result out. Many calls can travel in one `multipleRequest`. The app picks one of three transports, in this
order:

1. KLAP over HTTP to the lock's address, on the port from its discovery record. The app does this only when the
   lock's owner hash matches the signed-in account. The endpoints are `app/handshake0`, `app/handshake1`,
   `app/handshake2` and `app/request` with a `seq` query. python-kasa's KLAP has no `handshake0` step. The body of
   that step and the cipher setup were skipped in the audit as key material, so we do not know what `handshake0`
   sends or proves. tolpo will not guess it.
2. Bluetooth LE with TP-Link's own framing. Needs a radio near the lock and the same account. The service layout
   was not analyzed.
3. The cloud "Thing" service `passthrough`, with MQTT shadow updates. The REST path or MQTT topic of the passthrough
   call and how it authenticates were not captured.

The lock keeps its Wi-Fi off much of the time to save battery (a soft Wi-Fi switch, a Wi-Fi schedule and a "Remote
Control" mode). So the LAN path works only while Wi-Fi is on.

### Why unlock is blocked

Lock and unlock (`setLockStatus`) carry an `access_info` value, a cached per-device "control key". Its origin sits in
code that holds key material, which the audit skipped on purpose and tolpo will not read. Without it there is no
lock or unlock, local or cloud. Whether the owner can get this value without the TP-Link cloud decides whether a
LAN-only unlock can exist at all. Unlock also fails when the app lock and unlock switch is off, or when the lock is
not calibrated.

### Levels, once a client exists

Every lock method would be declared for the `lock` kind with a level. Reads are level 0. The rest follow the
audit's opt-in tiers:

- Level 3, with the confirmation phrase on every call: lock and unlock. Lock is lower risk than unlock but can still
  shut someone out, so it gets the same gate.
- Level 3: creating or changing access. PIN codes (normal, one-time, temporary), users, fingerprint, palm and NFC
  enrolment. A PIN (`digital_code`) or a user's password field is never logged or returned.
- Level 3: lowering the lock's guard. The app lock and unlock switch and its confirmation switch, wrong-try
  protection, trace protection and PIN scramble, the door jam and left-open alarms, auto-lock off, Wi-Fi off.
- Level 4: calibration, reset and reboot, firmware install, onboarding and cloud binding.
- Never exposed: the debug server override, which can point the lock at another server.

Every level 3 or 4 call needs the confirmation phrase, including access creation and changes to protections.

Order of work once a client exists: device info and running info, battery and event log reads, then settings, then
lock and unlock, then credentials.

### Open questions

1. Where does `access_info` come from, and can the owner get it without the TP-Link cloud?
2. Does the lock accept a KLAP login without `handshake0`, or is it required? What does that step carry?
3. Does the lock answer on the LAN while its Wi-Fi is in power save?
4. Is the TDP lock record sent without authentication, and is it refreshed on every state change?
5. What do the codes in the record mean: `lock_status` beyond 0 and 1 (jammed, not calibrated), `door_state`,
   `battery_status`, `backup_battery_status`, `power_source`? Is `last_onoff_time` a Unix time?
6. Does the lock's `owner` field use the same hash as other Tapo devices (MD5 of the account email)?
7. What is the lock's `device_type` in the record? The app uses `SMART.DOORLOCK` in analytics only. Are fields flat
   in `result`, and is there a 16-byte header followed by plain JSON?
8. What is the default of the app lock and unlock switch on a new lock, and does its confirmation switch apply to
   local calls?
9. The REST path or MQTT topic of the cloud passthrough call, and how it authenticates.
10. The BLE service layout.

## Checks on a real lock

When a lock is available, look at:

- One TDP reply from the lock (redacted): the `device_type`, whether the lock fields sit in `result` next to
  `device_model`, and whether the reply is plain or encrypted.
- `lock_state` before and after locking by hand: does `lock` flip between `locked` and `unlocked` the right way round?
- Open and close the door: which `door_code` values appear?
- Whether the lock shows under `locks` or only under `other_locks`. The second may mean the owner hash rule or the
  record layout is wrong.

## Not connected yet

To feed `lock_state` from tolpo's own discovery, `registry.py` would pass python-kasa an `on_discovered_raw` callback
that calls `lock.book_for(registry).note_raw(event)`, and call `note_unsupported(exc)` from its existing
`on_unsupported` handler. That is not done, because there is no lock to test it against.
