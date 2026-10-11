"""A device keeps the short id it was first given. Rooms, rules and baselines point at it, so it must not drift."""

import json

from tolpo.config import Config
from tolpo.registry import SLUGS_MAX, Registry, device_key

from . import fakes


def registry(tmp_path):
    return Registry(Config(email="a", password="b", cache_file=str(tmp_path / "hosts.json")))


def test_a_device_keeps_its_slug_after_a_rename_and_a_restart(tmp_path):
    first = registry(tmp_path)
    first._adopt(fakes.plug("id-1", "Desk Lamp"))
    first._save_cache()
    second = registry(tmp_path)
    second._adopt(fakes.plug("id-1", "Renamed In The App"))
    assert [e.slug for e in second.entries()] == ["desk-lamp"]
    assert second.resolve_one("renamed in the app").slug == "desk-lamp"  # the new name still finds it


def test_devices_with_the_same_name_keep_their_slugs_when_discovery_order_changes(tmp_path):
    first = registry(tmp_path)
    first._adopt(fakes.plug("id-a", "Bulb"))
    first._adopt(fakes.plug("id-b", "Bulb"))
    first._save_cache()
    assert {e.device.device_id: e.slug for e in first.entries()} == {"id-a": "bulb", "id-b": "bulb-2"}
    second = registry(tmp_path)
    second._adopt(fakes.plug("id-b", "Bulb"))
    second._adopt(fakes.plug("id-a", "Bulb"))
    assert {e.device.device_id: e.slug for e in second.entries()} == {"id-a": "bulb", "id-b": "bulb-2"}


def test_a_new_device_never_takes_a_slug_that_is_in_use(tmp_path):
    reg = registry(tmp_path)
    reg._adopt(fakes.plug("id-a", "Bulb"))
    reg._adopt(fakes.plug("id-b", "Bulb"))
    reg._adopt(fakes.plug("id-c", "Bulb"))
    assert sorted(e.slug for e in reg.entries()) == ["bulb", "bulb-2", "bulb-3"]


def test_the_file_holds_a_hash_of_the_id_and_never_the_id(tmp_path):
    reg = registry(tmp_path)
    device = fakes.plug("secret-device-id", "Desk Lamp")
    reg._adopt(device)
    reg._save_cache()
    text = (tmp_path / "hosts.json").read_text()
    assert "secret-device-id" not in text
    assert json.loads(text)["slugs"] == {device_key(device): "desk-lamp"}


def test_damaged_or_hostile_slugs_in_the_file_are_ignored(tmp_path):
    good = device_key(fakes.plug("id-1", "x"))
    (tmp_path / "hosts.json").write_text(
        json.dumps(
            {
                "hosts": {},
                "slugs": {
                    good: "desk-lamp",
                    "k2": "../etc/passwd",
                    "k3": "Has Capitals",
                    "k4": 7,
                    "k5": "x" * 200,
                    "k6": "desk-lamp",  # a second device cannot claim a slug that is already taken
                    "k7": {"nested": "dict"},
                },
            }
        )
    )
    assert registry(tmp_path)._slugs == {good: "desk-lamp"}


def test_a_slugs_section_that_is_not_a_mapping_is_ignored(tmp_path):
    for damaged in (["a"], "text", 3, None):
        (tmp_path / "hosts.json").write_text(json.dumps({"hosts": {}, "slugs": damaged}))
        assert registry(tmp_path)._slugs == {}
    (tmp_path / "hosts.json").write_text("{not json")
    assert registry(tmp_path)._slugs == {}


def test_only_a_bounded_number_of_remembered_slugs_is_read(tmp_path):
    many = {f"key{i:05d}": f"device-{i}" for i in range(SLUGS_MAX + 500)}
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": {}, "slugs": many}))
    assert len(registry(tmp_path)._slugs) == SLUGS_MAX


def test_without_a_cache_file_slugs_are_remembered_for_the_run_only():
    reg = Registry(Config(email="a", password="b", cache_file=""))
    reg._adopt(fakes.plug("id-1", "Desk Lamp"))
    assert reg._slugs == {device_key(fakes.plug("id-1", "x")): "desk-lamp"}
    assert Registry(Config(email="a", password="b", cache_file=""))._slugs == {}


def test_a_new_device_leaves_the_slug_of_a_device_not_seen_yet_this_run(tmp_path):
    first = registry(tmp_path)
    first._adopt(fakes.plug("id-b", "Bulb"))
    first._save_cache()
    second = registry(tmp_path)
    second._adopt(fakes.plug("id-a", "Bulb"))  # new, and found before the old one
    assert {e.device.device_id: e.slug for e in second.entries()} == {"id-a": "bulb-2"}
    second._adopt(fakes.plug("id-b", "Bulb"))
    assert {e.device.device_id: e.slug for e in second.entries()} == {"id-a": "bulb-2", "id-b": "bulb"}


def test_a_remembered_slug_that_is_in_use_by_another_device_is_not_reused(tmp_path):
    reg = registry(tmp_path)
    reg._adopt(fakes.plug("id-a", "Bulb"))
    reg._slugs[device_key(fakes.plug("id-b", "x"))] = "bulb"  # damage: the same slug remembered for two devices
    reg._adopt(fakes.plug("id-b", "Bulb"))
    assert {e.device.device_id: e.slug for e in reg.entries()} == {"id-a": "bulb", "id-b": "bulb-2"}
