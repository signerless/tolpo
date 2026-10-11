import asyncio
import json

import kasa
import pytest

from tolpo.config import Config
from tolpo.registry import Registry

from . import fakes

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def patch_kasa(monkeypatch, log):
    async def broadcast(**kwargs):
        log.append("broadcast")
        await asyncio.sleep(0.2)
        return {}

    async def single(host, **kwargs):
        log.append(f"single:{host}")
        return fakes.bulb("b1", "Office 1") if host == "192.0.2.10" else None

    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(broadcast))
    monkeypatch.setattr(kasa.Discover, "discover_single", staticmethod(single))


def config(tmp_path, **extra):
    extra.setdefault("sweep", False)  # the sweep has its own test; the others count exact calls
    return Config(email="a", password="b", cache_file=str(tmp_path / "hosts.json"), subnets=("192.0.2.0/24",),
                  discovery_interval=3600, **extra)


async def test_remembered_hosts_make_the_first_answer_fast(tmp_path, monkeypatch):
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": ["192.0.2.10"]}))
    log = []
    patch_kasa(monkeypatch, log)
    reg = Registry(config(tmp_path))
    await reg.start(discover=True, wait=True)
    assert reg.ready.is_set() and [e.slug for e in reg.entries()] == ["office-1"]
    await asyncio.sleep(0.5)  # let the background broadcast pass run
    await reg.stop()
    assert log[0] == "single:192.0.2.10" and "broadcast" in log and log.index("broadcast") > 0


async def test_first_run_without_cache_does_the_full_scan(tmp_path, monkeypatch):
    log = []
    patch_kasa(monkeypatch, log)
    reg = Registry(config(tmp_path))
    await reg.start(discover=True, wait=True)
    await reg.stop()
    assert log == ["broadcast"] and reg.entries() == []


async def test_silent_remembered_host_is_reported(tmp_path, monkeypatch):
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": ["192.0.2.99"]}))
    patch_kasa(monkeypatch, [])
    reg = Registry(config(tmp_path))
    await reg.discover(quick=True)
    assert reg.problems == {"192.0.2.99": "remembered host did not answer"}


async def test_hosts_are_remembered_between_runs(tmp_path, monkeypatch):
    patch_kasa(monkeypatch, [])
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": ["192.0.2.10"]}))
    reg = Registry(config(tmp_path))
    await reg.discover(quick=True)
    saved = json.loads((tmp_path / "hosts.json").read_text())
    assert list(saved["hosts"]) == ["192.0.2.10"] and saved["hosts"]["192.0.2.10"]["name"] == "Office 1"


async def test_concurrent_discoveries_do_not_overlap(tmp_path, monkeypatch):
    running, peak = 0, 0

    async def broadcast(**kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.1)
        running -= 1
        return {}

    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(broadcast))
    reg = Registry(config(tmp_path))
    await asyncio.gather(reg.discover(), reg.discover(), reg.discover())
    assert peak == 1


async def test_a_missed_scan_never_makes_the_cache_forget_a_device(tmp_path, monkeypatch):
    """The bug: one transient miss used to drop the host from the cache, so it was never probed again."""
    seen = 1_700_000_000.0
    (tmp_path / "hosts.json").write_text(json.dumps({"version": 2, "hosts": {
        "192.0.2.10": {"seen": seen, "name": "Office 1", "model": "L430C"},
        "192.0.2.77": {"seen": seen, "name": "hallway-cam", "model": "C225"},
    }}))
    monkeypatch.setattr("tolpo.registry.CACHE_MAX_AGE", 10 ** 12)
    patch_kasa(monkeypatch, [])
    reg = Registry(config(tmp_path))
    await reg.discover(quick=True)  # .10 answers, .77 does not
    saved = json.loads((tmp_path / "hosts.json").read_text())["hosts"]
    assert set(saved) == {"192.0.2.10", "192.0.2.77"} and saved["192.0.2.77"]["seen"] == seen
    assert reg.not_connected() == [{"host": "192.0.2.77", "name": "hallway-cam", "model": "C225",
                                    "reason": "remembered host did not answer"}]


async def test_old_cache_format_is_migrated(tmp_path, monkeypatch):
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": ["192.0.2.10", "192.0.2.99"]}))
    patch_kasa(monkeypatch, [])
    reg = Registry(config(tmp_path))
    assert set(reg._known) == {"192.0.2.10", "192.0.2.99"}
    await reg.discover(quick=True)
    saved = json.loads((tmp_path / "hosts.json").read_text())
    assert saved["version"] == 2 and set(saved["hosts"]) == {"192.0.2.10", "192.0.2.99"}


async def test_hosts_unseen_for_a_month_are_pruned(tmp_path, monkeypatch):
    old = 1.0  # 1970
    (tmp_path / "hosts.json").write_text(json.dumps({"version": 2, "hosts": {
        "192.0.2.10": {"seen": old, "name": "Office 1", "model": "L430C"},
        "192.0.2.50": {"seen": old, "name": "gone", "model": "P115"},
    }}))
    patch_kasa(monkeypatch, [])
    reg = Registry(config(tmp_path))
    await reg.discover(quick=True)  # .10 answers and is refreshed; .50 is ancient and silent
    assert list(json.loads((tmp_path / "hosts.json").read_text())["hosts"]) == ["192.0.2.10"]


async def test_unreachable_hosts_are_retried_soon(tmp_path, monkeypatch):
    (tmp_path / "hosts.json").write_text(json.dumps({"version": 2, "hosts": {
        "192.0.2.10": {"seen": 1e12, "name": "Office 1", "model": "L430C"}}}))
    attempts = []

    async def flaky(host, **kwargs):
        attempts.append(host)
        return fakes.bulb("b1", "Office 1") if len(attempts) >= 3 else None  # fails twice, then answers

    async def broadcast(**kwargs):
        return {}

    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(broadcast))
    monkeypatch.setattr(kasa.Discover, "discover_single", staticmethod(flaky))
    reg = Registry(config(tmp_path))
    reg.retry_delays = (0.01, 0.01, 0.01)
    await reg.start(discover=True, wait=True)
    for _ in range(100):
        if not reg.not_connected():
            break
        await asyncio.sleep(0.02)
    await reg.stop()
    assert len(attempts) >= 3 and reg.not_connected() == [] and [e.slug for e in reg.entries()] == ["office-1"]
    assert reg.health()["complete"] is True


async def test_sweep_finds_devices_that_ignore_broadcast(tmp_path, monkeypatch):
    asked = []

    async def broadcast(**kwargs):
        return {}  # the battery camera stayed silent

    async def single(host, **kwargs):
        asked.append(host)
        return fakes.bulb("cam", "back camera") if host == "192.0.2.30" else None

    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(broadcast))
    monkeypatch.setattr(kasa.Discover, "discover_single", staticmethod(single))
    reg = Registry(config(tmp_path, sweep=True))
    await reg.discover()
    assert [e.slug for e in reg.entries()] == ["back-camera"]
    assert len(asked) == 254 and set(reg._known) == {"192.0.2.10"}  # fakes share one host; real devices do not

    asked.clear()
    quick = Registry(config(tmp_path, sweep=True))
    await quick.discover(quick=True)
    assert len(asked) <= 1  # quick passes never sweep


async def test_sweep_skips_networks_that_are_too_big(tmp_path, monkeypatch):
    asked = []

    async def single(host, **kwargs):
        asked.append(host)

    async def broadcast(**kwargs):
        return {}

    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(broadcast))
    monkeypatch.setattr(kasa.Discover, "discover_single", staticmethod(single))
    reg = Registry(Config(email="a", password="b", cache_file="", subnets=("10.0.0.0/16",), sweep=True))
    await reg.discover()
    assert asked == []


async def test_devices_that_answer_but_cannot_connect_are_reported(tmp_path, monkeypatch):
    class Broken(fakes.FakeDevice):
        async def update(self):
            raise OSError("tls handshake failed")

    broken = Broken("cam", "x", "C465", fakes.DeviceType.Camera, host="192.0.2.80")

    async def broadcast(**kwargs):
        return {"192.0.2.80": broken}

    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(broadcast))
    reg = Registry(config(tmp_path))
    await reg.discover()
    assert reg.entries() == [] and "192.0.2.80" not in reg._known
    assert reg.not_connected() == [{"host": "192.0.2.80", "name": "C465", "model": "C465", "reason": "OSError"}]
    assert reg.health()["complete"] is False and broken.disconnected


def test_ecdsa_cameras_patch_is_additive_and_idempotent():
    from kasa.transports.sslaestransport import SslAesTransport

    from tolpo import compat

    before = SslAesTransport.CIPHERS.split(":")
    assert compat.allow_ecdsa_cameras() and compat.allow_ecdsa_cameras()
    after = SslAesTransport.CIPHERS.split(":")
    assert all(suite in after for suite in before)  # nothing the library offered is removed
    assert all(after.count(suite) == 1 for suite in compat.ECDSA_SUITES)  # applying twice adds nothing


class Gated(fakes.FakeDevice):
    """A device whose first connection hangs until the test lets it finish, like a sleeping battery camera."""

    def __init__(self, gate, **kwargs):
        super().__init__(**kwargs)
        self.gate, self.updates = gate, 0

    async def update(self):
        self.updates += 1
        await self.gate.wait()


def staggered_house(tmp_path, monkeypatch):
    gate = asyncio.Event()
    slow = Gated(gate, device_id="cam", alias="hallway-cam", model="C225", device_type=fakes.DeviceType.Camera,
                 host="192.0.2.20", features=[fakes.FakeFeature("state", fakes.Feature.Type.Switch, fakes.Feature.Category.Primary, True)])

    async def single(host, **kwargs):
        return {"192.0.2.10": fakes.bulb("b1", "Office 1"), "192.0.2.20": slow}.get(host)

    async def broadcast(**kwargs):
        return {}

    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(broadcast))
    monkeypatch.setattr(kasa.Discover, "discover_single", staticmethod(single))
    (tmp_path / "hosts.json").write_text(json.dumps({"version": 2, "hosts": {
        "192.0.2.10": {"seen": 1e12, "name": "Office 1", "model": "L430C"},
        "192.0.2.20": {"seen": 1e12, "name": "hallway-cam", "model": "C225"}}}))
    reg = Registry(config(tmp_path))
    reg.boot_grace, reg.retry_delays = 0.2, ()
    return reg, slow, gate


async def test_a_stuck_device_does_not_hold_up_startup(tmp_path, monkeypatch):
    reg, slow, gate = staggered_house(tmp_path, monkeypatch)
    await asyncio.wait_for(reg.start(discover=True, wait=True), 3)  # fails instead of hanging if the grace is broken
    assert [e.slug for e in reg.entries()] == ["office-1"]
    assert reg.not_connected() == [{"host": "192.0.2.20", "name": "hallway-cam", "model": "C225", "reason": "connecting"}]
    assert reg.health()["complete"] is False

    await reg.discover(quick=True)  # another pass while it is still connecting must not open a second session
    assert slow.updates == 1

    gate.set()  # the camera finally wakes up
    for _ in range(100):
        if not reg.not_connected():
            break
        await asyncio.sleep(0.02)
    await reg.stop()
    assert sorted(e.slug for e in reg.entries()) == ["hallway-cam", "office-1"] and reg.health()["complete"] is True
    assert slow.updates == 1


async def test_stop_cancels_devices_that_are_still_connecting(tmp_path, monkeypatch):
    reg, slow, gate = staggered_house(tmp_path, monkeypatch)
    await asyncio.wait_for(reg.start(discover=True, wait=True), 3)
    await asyncio.wait_for(reg.stop(), 3)  # the gate is never opened
    assert not reg._stragglers and not gate.is_set()


async def test_a_silent_device_does_not_delay_the_others(tmp_path, monkeypatch):
    """A battery camera that never answers its probe must not hold back the lights that answered at once."""
    (tmp_path / "hosts.json").write_text(json.dumps({"version": 2, "hosts": {
        "192.0.2.10": {"seen": 1e12, "name": "Office 1", "model": "L430C"},
        "192.0.2.99": {"seen": 1e12, "name": "camera", "model": "C610"}}}))

    async def single(host, **kwargs):
        if host == "192.0.2.99":
            await asyncio.sleep(1.0)  # never answers within the probe window
            return None
        return fakes.bulb("b1", "Office 1")

    monkeypatch.setattr(kasa.Discover, "discover_single", staticmethod(single))
    reg = Registry(config(tmp_path))
    loop = asyncio.get_running_loop()
    started = loop.time()
    scan = asyncio.create_task(reg.discover(quick=True))
    while not reg.entries():
        await asyncio.sleep(0.01)
    light_up_at = loop.time() - started
    still_scanning = not scan.done()
    await scan
    assert light_up_at < 0.5 and still_scanning
    assert reg.problems == {"192.0.2.99": "remembered host did not answer"}
