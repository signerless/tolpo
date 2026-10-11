"""`tolpo automation`: the owner's switch. It needs no Tapo account and never contacts a device."""

import json
import os
import stat

import pytest

from tolpo import cli
from tolpo.automation import Rule
from tolpo.automation.store import State, Store


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOLPO_STATE_DIR", str(tmp_path))
    for name in ("TAPO_EMAIL", "TAPO_PASSWORD"):
        monkeypatch.delenv(name, raising=False)  # no account: the command must not ask for one


def run(*args):
    with pytest.raises(SystemExit) as stop:
        cli.main(["automation", *args])
    return stop.value.code


def saved(tmp_path):
    return json.loads((tmp_path / "automation.json").read_text())


def one_rule():
    return Rule(id="r1", name="Evening", enabled=True, triggers=[{"kind": "clock", "time": "12:00"}],
                actions=[{"kind": "message", "message": "hi"}])


def test_status_of_an_empty_house_says_running(tmp_path, capsys):
    assert run("status") == 0
    assert "0 rule(s), 0 enabled. Automation is running." in capsys.readouterr().out
    assert not (tmp_path / "automation.json").exists()  # reading changes nothing


def test_kill_then_resume_flips_the_saved_switch_and_keeps_the_rules(tmp_path, capsys):
    store = Store(tmp_path)
    store.save(State(rules=[one_rule()]))
    assert run("kill") == 0
    assert "STOPPED" in capsys.readouterr().out and saved(tmp_path)["settings"]["killed"] is True
    assert Store(tmp_path).killed_on_disk() is True  # a running engine in another process stops by this
    assert run("status") == 0
    assert "1 rule(s), 1 enabled. Automation is STOPPED." in capsys.readouterr().out
    assert run("resume") == 0
    out = capsys.readouterr().out
    assert "running" in out and "restarts" in out and saved(tmp_path)["settings"]["killed"] is False
    assert [r["id"] for r in saved(tmp_path)["rules"]] == ["r1"]


def test_the_state_file_stays_private(tmp_path):
    Store(tmp_path).save(State(rules=[one_rule()]))
    run("kill")
    assert stat.S_IMODE(os.stat(tmp_path / "automation.json").st_mode) == 0o600


def test_a_damaged_state_file_is_reported_by_status_and_resume_and_left_alone(tmp_path, capsys):
    (tmp_path / "automation.json").write_text("{not json")
    for action in ("status", "resume"):
        assert run(action) == 1
    assert "could not be used" in capsys.readouterr().err
    assert (tmp_path / "automation.json").read_text() == "{not json"


def test_a_stop_lands_even_when_the_state_file_is_damaged(tmp_path, capsys):
    (tmp_path / "automation.json").write_text("{not json")
    assert run("kill") == 0
    assert "kept as automation.damaged.json" in capsys.readouterr().err
    assert (tmp_path / "automation.damaged.json").read_text() == "{not json"  # nothing is lost
    assert saved(tmp_path)["settings"]["killed"] is True and Store(tmp_path).killed_on_disk()
    assert stat.S_IMODE(os.stat(tmp_path / "automation.damaged.json").st_mode) == 0o600


def test_a_stop_also_empties_what_was_waiting_to_run(tmp_path):
    from tolpo.automation.store import Run

    store = Store(tmp_path)
    store.save(State(rules=[one_rule()], runs=[Run(rule="r1", cursor=1, due=1e12)]))
    assert saved(tmp_path)["runs"]
    assert run("kill") == 0
    assert saved(tmp_path)["runs"] == []  # after a resume, nothing old starts at once


def test_an_unknown_action_is_refused():
    with pytest.raises(SystemExit) as stop:
        cli.main(["automation", "delete-everything"])
    assert stop.value.code == 2
