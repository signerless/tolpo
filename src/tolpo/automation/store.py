"""Versioned private JSON state and a JSONL owner outbox."""
from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from pathlib import Path

from pydantic import Field

from .models import Model, Rule, Settings


class Run(Model):
    rule: str
    cursor: int = Field(default=0, ge=0, le=32)
    due: float = Field(ge=0)
    ancestors: list[str] = Field(default_factory=list, max_length=200)


class State(Model):
    version: int = 1
    settings: Settings = Field(default_factory=Settings)
    rules: list[Rule] = Field(default_factory=list, max_length=200)
    runs: list[Run] = Field(default_factory=list, max_length=200)
    last_run: dict[str, float] = Field(default_factory=dict)
    clock_marks: dict[str, str] = Field(default_factory=dict)


# Never follow a symbolic link planted in the state directory. Windows has no O_NOFOLLOW.
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
MAX_OUTBOX_BYTES = 1 << 20


def _open_private(path: Path, flags: int, mode: int = 0o600) -> int:
    try:
        fd = os.open(path, flags | NOFOLLOW, mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"{path.name} is a symbolic link; automation refuses to use it") from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"{path.name} is not a regular file")
    return fd


def read_private(path: Path) -> str | None:
    try:
        fd = _open_private(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as stream:
        return stream.read()


def _try_lock(fd: int) -> bool:
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def atomic(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".automation-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Store:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or Path(os.environ.get("TOLPO_STATE_DIR", "~/.local/state/tolpo")).expanduser()
        self.path = self.directory / "automation.json"
        self.outbox = self.directory / "automation-alerts.jsonl"
        self.lockfile = self.directory / "automation.lock"
        self._lock_fd = None
        self._saved = None

    def lock(self) -> bool:
        """Take the one-engine lock for this state directory. False when another process holds it."""
        if self._lock_fd is not None:
            return True
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = _open_private(self.lockfile, os.O_RDWR | os.O_CREAT)
        if not _try_lock(fd):
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def unlock(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing releases the lock
            self._lock_fd = None

    def killed_on_disk(self) -> bool:
        """Whether the saved settings say killed. Another process may have set it."""
        content = read_private(self.path)
        if content is None:
            return False
        return bool(json.loads(content).get("settings", {}).get("killed"))

    def load(self):
        content = read_private(self.path)
        if content is None:
            return State()
        state = State.model_validate_json(content)
        if state.version != 1:
            raise ValueError("unsupported automation store version")
        ids = {r.id for r in state.rules}
        if len(ids) != len(state.rules):
            raise ValueError("duplicate automation rule ids")
        if any(run.rule not in ids for run in state.runs):
            raise ValueError("automation queue refers to a missing rule")
        from .graph import check_rule_graph

        check_rule_graph(state.rules)
        self._saved = content
        return state

    def save(self, state: State):
        content = state.model_dump_json(indent=2)
        if content == self._saved:
            return  # nothing changed; do not fsync on every poll
        atomic(self.path, content)
        self._saved = content

    def alert(self, row: dict):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = _open_private(self.outbox, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        with os.fdopen(fd, "a") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            size = os.fstat(stream.fileno()).st_size
        if size > MAX_OUTBOX_BYTES:
            self._trim()

    def _trim(self):
        """Keep the newest records when nobody reads the outbox, and say how many were dropped."""
        lines = (read_private(self.outbox) or "").splitlines()
        kept, total = [], 0
        for line in reversed(lines):
            total += len(line) + 1
            if total > MAX_OUTBOX_BYTES // 2:
                break
            kept.append(line)
        kept.reverse()
        marker = json.dumps({"error": "AlertsDropped", "dropped": len(lines) - len(kept)})
        atomic(self.outbox, "".join(line + "\n" for line in [marker, *kept]))

    def alerts(self, clear=False, limit=100):
        lines = (read_private(self.outbox) or "").splitlines()
        rows = []
        for line in lines[:limit]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"error": "UnreadableAlert"})
        if clear:
            atomic(self.outbox, "".join(line + "\n" for line in lines[limit:]))
        return rows
