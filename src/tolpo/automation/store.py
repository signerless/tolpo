"""Versioned private JSON state and a JSONL owner outbox."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import Field

from .models import Model, Rule, Settings


class Run(Model):
    rule: str
    cursor: int = Field(default=0, ge=0, le=32)
    due: float = Field(ge=0)
    ancestors: list[str] = Field(default_factory=list)


class State(Model):
    version: int = 1
    settings: Settings = Field(default_factory=Settings)
    rules: list[Rule] = Field(default_factory=list, max_length=200)
    runs: list[Run] = Field(default_factory=list, max_length=200)
    last_run: dict[str, float] = Field(default_factory=dict)
    clock_marks: dict[str, str] = Field(default_factory=dict)


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

    def load(self):
        if not self.path.exists():
            return State()
        state = State.model_validate_json(self.path.read_text())
        if state.version != 1:
            raise ValueError("unsupported automation store version")
        ids = {r.id for r in state.rules}
        if len(ids) != len(state.rules):
            raise ValueError("duplicate automation rule ids")
        if any(run.rule not in ids for run in state.runs):
            raise ValueError("automation queue refers to a missing rule")
        return state

    def save(self, state: State):
        atomic(self.path, state.model_dump_json(indent=2))

    def alert(self, row: dict):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.outbox, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def alerts(self, clear=False, limit=100):
        if not self.outbox.exists():
            return []
        lines = self.outbox.read_text().splitlines()
        rows = [json.loads(line) for line in lines[:limit]]
        if clear:
            atomic(self.outbox, "".join(line + "\n" for line in lines[limit:]))
        return rows
