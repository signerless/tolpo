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


def test_a_slug_two_devices_claim_goes_to_the_one_seen_more_recently(tmp_path):
    """Whatever order the file lists them in, a hostile or damaged file cannot give one slug to two devices."""
    import time

    now = time.time()
    for order in ({"older": "same-slug", "newer": "same-slug"}, {"newer": "same-slug", "older": "same-slug"}):
        (tmp_path / "hosts.json").write_text(json.dumps({"hosts": {}, "slugs": order, "slug_seen": {"older": now - 9999, "newer": now - 1}}))
        assert registry(tmp_path)._slugs == {"newer": "same-slug"}


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


# ---- children that leave a hub -----------------------------------------------------------------------------------------


import asyncio  # noqa: E402

import pytest  # noqa: E402


def hub_registry(tmp_path):
    reg = registry(tmp_path)
    reg._adopt(fakes.hub())
    return reg


def update(reg, top):
    asyncio.run(reg._update_locked(top))


def test_a_child_that_is_unpaired_from_the_hub_leaves_the_registry_after_two_updates_and_keeps_its_slug(tmp_path):
    reg = hub_registry(tmp_path)
    top = reg.resolve_one("tapo-h200")
    door = next(c for c in top.device.children if c.device_id == "child-door")
    top.device.children.remove(door)
    update(reg, top)
    assert sorted(e.slug for e in reg.entries() if e.root is top) == ["main-door", "temp-sensor"]  # one list can be partial
    update(reg, top)
    assert sorted(e.slug for e in reg.entries() if e.root is top) == ["temp-sensor"]
    top.device.children.append(door)  # paired again
    update(reg, top)
    assert reg.resolve_one("main door").slug == "main-door"  # the same short id as before


def test_a_child_missing_from_one_update_only_is_kept_and_its_count_starts_again(tmp_path):
    reg = hub_registry(tmp_path)
    top = reg.resolve_one("tapo-h200")
    door = next(c for c in top.device.children if c.device_id == "child-door")
    entry = reg.resolve_one("main-door")
    for _ in range(3):  # missing, back, missing, back ...
        top.device.children.remove(door)
        update(reg, top)
        top.device.children.append(door)
        update(reg, top)
    assert reg.resolve_one("main-door") is entry  # never dropped and made again


def test_forget_removes_a_device_at_once(tmp_path):
    reg = hub_registry(tmp_path)
    door = reg.resolve_one("main-door")
    reg.forget(door)
    assert "main-door" not in [e.slug for e in reg.entries()]
    reg.forget(door)  # forgetting twice is harmless


def test_a_failed_update_never_removes_children(tmp_path):
    reg = hub_registry(tmp_path)
    top = reg.resolve_one("tapo-h200")

    async def broken():
        raise TimeoutError

    top.device.update = broken
    top.device.children.clear()  # what a half-answered update could look like
    for _ in range(3):
        update(reg, top)
    assert sorted(e.slug for e in reg.entries() if e.root is top) == ["main-door", "temp-sensor"]
    assert not top.online


def test_children_of_another_hub_and_standalone_devices_are_untouched(tmp_path):
    reg = hub_registry(tmp_path)
    reg._adopt(fakes.plug("id-plug", "Desk Lamp"))
    top = reg.resolve_one("tapo-h200")
    top.device.children.clear()
    update(reg, top)
    update(reg, top)
    assert [e.slug for e in reg.entries()] == ["desk-lamp", "tapo-h200"]


# ---- the cache file --------------------------------------------------------------------------------------------------


def test_the_cache_is_written_in_one_piece_and_a_failed_write_keeps_the_old_one(tmp_path, monkeypatch):
    import os

    reg = registry(tmp_path)
    reg._adopt(fakes.plug("id-1", "Desk Lamp"))
    reg._save_cache()
    good = (tmp_path / "hosts.json").read_text()
    assert json.loads(good)["slugs"] and not list(tmp_path.glob(".hosts-*"))

    def broken(*args, **kwargs):
        raise OSError("disk full")

    reg._adopt(fakes.plug("id-2", "Other"))
    monkeypatch.setattr(os, "replace", broken)
    reg._save_cache()
    assert (tmp_path / "hosts.json").read_text() == good and not list(tmp_path.glob(".hosts-*"))


def test_short_ids_another_process_gave_are_kept_when_this_one_saves(tmp_path):
    early = registry(tmp_path)  # started before the other one saved anything
    other = registry(tmp_path)
    other._adopt(fakes.plug("id-x", "Lamp"))
    other._save_cache()
    early._adopt(fakes.plug("id-y", "Other"))
    early._save_cache()
    third = registry(tmp_path)
    assert sorted(third._slugs.values()) == ["lamp", "other"]


def test_a_short_id_is_given_up_only_after_its_device_was_away_for_a_month(tmp_path):
    import time

    now = time.time()
    here = device_key(fakes.plug("id-present", "x"))
    (tmp_path / "hosts.json").write_text(json.dumps({
        "hosts": {},
        "slugs": {"recent": "recent-bulb", "old": "old-bulb", here: "present-bulb"},
        "slug_seen": {"recent": now - 86400, "old": now - 40 * 86400, here: now - 90 * 86400},
    }))
    reg = registry(tmp_path)
    reg._adopt(fakes.plug("id-present", "Present Bulb"))  # here again, so its old record does not matter
    reg._save_cache()
    kept = json.loads((tmp_path / "hosts.json").read_text())["slugs"]
    assert sorted(kept.values()) == ["present-bulb", "recent-bulb"]  # the one away for 40 days is gone
    assert reg.resolve_one("present bulb").slug == "present-bulb"


def test_only_the_newest_short_ids_are_kept_when_there_are_too_many(tmp_path, monkeypatch):
    import time

    from tolpo import registry as registry_module

    monkeypatch.setattr(registry_module, "SLUGS_MAX", 3)
    now = time.time()
    slugs = {f"k{i}": f"device-{i}" for i in range(6)}
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": {}, "slugs": slugs, "slug_seen": {f"k{i}": now - i * 3600 for i in range(6)}}))
    reg = registry(tmp_path)
    assert sorted(reg._slugs.values()) == ["device-0", "device-1", "device-2"]  # the three seen most recently
    reg._save_cache()
    assert sorted(json.loads((tmp_path / "hosts.json").read_text())["slugs"].values()) == ["device-0", "device-1", "device-2"]


# ---- looking devices up when short ids outlive names --------------------------------------------------------------------


def test_a_short_id_and_an_exact_name_that_point_at_different_devices_are_ambiguous(tmp_path):
    from tolpo.registry import Ambiguous

    first = registry(tmp_path)
    first._adopt(fakes.plug("id-1", "Office"))
    first._save_cache()
    second = registry(tmp_path)
    second._adopt(fakes.plug("id-1", "Heater"))  # renamed in the Tapo app: it keeps the short id "office"
    second._adopt(fakes.plug("id-2", "Office"))  # a new device now has the name
    with pytest.raises(Ambiguous) as caught:
        second.resolve_one("office")
    assert sorted(caught.value.candidates) == ["office", "office-2"]
    assert second.resolve_one("heater").device.device_id == "id-1" and second.resolve_one("office-2").device.device_id == "id-2"


def test_a_name_and_a_short_id_of_the_same_device_are_not_ambiguous(tmp_path):
    reg = registry(tmp_path)
    reg._adopt(fakes.plug("id-1", "Office"))
    assert reg.resolve_one("office").device.device_id == "id-1" and reg.resolve_one("OFFICE").device.device_id == "id-1"


def test_by_slug_is_exact_and_never_falls_back_to_a_name_or_a_pattern(tmp_path):
    from tolpo.registry import NotFound

    reg = registry(tmp_path)
    reg._adopt(fakes.plug("id-2", "Office"))
    assert reg.by_slug("office").device.device_id == "id-2"
    for ref in ("Office", "OFFICE", "off*", "id-2", "office-2"):
        with pytest.raises(NotFound):
            reg.by_slug(ref)


def test_the_cache_holds_no_more_short_ids_than_the_limit_when_it_is_saved(tmp_path, monkeypatch):
    from tolpo import registry as registry_module

    monkeypatch.setattr(registry_module, "SLUGS_MAX", 3)
    reg = registry(tmp_path)
    for i in range(5):
        reg._adopt(fakes.plug(f"id-{i}", f"Plug {i}"))
    reg._save_cache()
    assert len(json.loads((tmp_path / "hosts.json").read_text())["slugs"]) == 3


def test_a_hub_that_lists_fewer_children_does_not_take_the_children_of_another_hub(tmp_path):
    reg = hub_registry(tmp_path)
    second = fakes.hub()
    second.device_id, second.alias, second.host = "hub-2", "Second Hub", "192.0.2.77"
    for child in second.children:
        child.device_id = child.device_id.replace("child", "other")
        child.alias = f"{child.alias} two"
    reg._adopt(second)
    first = reg.resolve_one("tapo-h200")
    first.device.children.clear()
    for _ in range(3):
        update(reg, first)
    mine = {e.slug for e in reg.entries() if e.root is first}
    theirs = {e.slug for e in reg.entries() if e.root is reg.resolve_one("second-hub")}
    assert mine == set() and theirs == {"main-door-two", "temp-sensor-two"}
