"""Owner-chosen, private local history and append-only hash-chained change logs."""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import tempfile

from .config import private_open
from .models import NetworkError

MAX_STORE_BYTES = 8 * 1024 * 1024
MAX_AUDIT_BYTES = 8 * 1024 * 1024


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read(path: Path) -> dict:
    if not path.exists() and not path.is_symlink():
        return {}
    with os.fdopen(private_open(path), "r") as stream:
        data = stream.read(MAX_STORE_BYTES + 1)
    try:
        if len(data) > MAX_STORE_BYTES:
            raise ValueError
        return json.loads(data)
    except ValueError:
        raise NetworkError("Network history is invalid or exceeds its limit") from None


class History:
    def __init__(self, filename: str, retention_days: int):
        self.path = Path(filename).expanduser() if filename else None
        self.retention = retention_days * 86400

    def compare(self, device: str, observations: list[dict], now: float, since: float) -> dict:
        if self.path is None:
            raise NetworkError("Destination history is not enabled")
        data = read(self.path)
        if not isinstance(data, dict):
            raise NetworkError("Network history is invalid")
        try:
            for state in data.values():
                if not isinstance(state, dict) or not isinstance(state["seen"], dict):
                    raise ValueError
                for at in (state["baseline"], state["updated"]):
                    if not isinstance(at, (int, float)):
                        raise ValueError
                for item in state["seen"].values():
                    if not isinstance(item, dict) or not isinstance(item["destination"], str):
                        raise ValueError
                    for at in (item["first_seen"], item["last_seen"]):
                        if not isinstance(at, (int, float)):
                            raise ValueError
        except (ValueError, TypeError, KeyError):
            raise NetworkError("Network history is invalid") from None
        # Retire devices and destinations, not just event rows.
        data = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("updated", 0) >= now - self.retention}
        first = device not in data
        state = data.setdefault(device, {"baseline": now, "updated": now, "seen": {}})
        seen = {k: v for k, v in state["seen"].items() if v["last_seen"] >= now - self.retention}
        result = []
        reported = set()
        for item in observations:
            key = digest([item["destination"], item.get("address", ""), item.get("port", 0), item.get("protocol", ""), item["source"]])
            if key not in seen:
                same_name = bool(item.get("address")) and any(v["destination"] == item["destination"] for v in seen.values())
                seen[key] = {**item, "first_seen": now, "last_seen": now,
                             "status": "known name, new address" if same_name else "new"}
            else:
                seen[key]["last_seen"] = now
            entry = seen[key]
            if not first and key not in reported and entry["first_seen"] > state["baseline"] and entry["first_seen"] >= since:
                result.append(entry)
                reported.add(key)
        state.update(seen=seen, updated=now)
        encoded = json.dumps(data, sort_keys=True)
        if len(encoded.encode()) > MAX_STORE_BYTES:
            raise NetworkError("Network history limit exceeded")
        # Atomic replacement, private from creation. Never create a default history directory.
        try:
            fd, temporary = tempfile.mkstemp(prefix=".tolpo-network-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            raise NetworkError("Network history cannot be saved") from None
        return {"baseline_created": first, "new": result[:50], "omitted": max(0, len(result) - 50)}


class Audit:
    def __init__(self, filename: str):
        self.path = Path(filename).expanduser() if filename else None

    def append(self, event: dict) -> None:
        if self.path is None:
            raise NetworkError("Firewall audit log is unavailable")
        fd = private_open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT)
        # A full log refuses new plans. Records that finish or revert an existing change are always written.
        if event.get("event") == "plan" and os.fstat(fd).st_size > MAX_AUDIT_BYTES:
            os.close(fd)
            raise NetworkError("Firewall audit log is full; the owner must archive it before new plans")
        try:
            with os.fdopen(fd, "r+") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                previous = ""
                # Verify the chain before appending. A valid tail deletion needs an external anchor to detect.
                for line in stream:
                    try:
                        record = json.loads(line)
                        got = record.pop("hash")
                        if record.get("previous") != previous or digest(record) != got:
                            raise ValueError
                        previous = got
                    except (ValueError, KeyError, TypeError):
                        raise NetworkError("Firewall audit chain is invalid") from None
                record = {**event, "previous": previous}
                record["hash"] = digest(record)
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise NetworkError("Firewall audit log cannot be saved") from None
