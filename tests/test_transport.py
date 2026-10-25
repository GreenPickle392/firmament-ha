"""Offline checks for the link supervisor.

Home Assistant and bleak are stubbed just enough to let the real transport code
run: serialisation, notification correlation, advertisement gating, coalescing
and reconnection are the behaviours UniLED gets wrong, so they are exercised
here without a radio.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "firmament"
WRITE_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

CURRENT: dict = {}


class FakeAdvertisement:
    rssi = -55


class FakeServiceInfo:
    advertisement = FakeAdvertisement()


class FakeChar:
    uuid = WRITE_UUID


class FakeServices:
    def get_service(self, uuid: str):
        if uuid not in (
            "0000ffe0-0000-1000-8000-00805f9b34fb",
            "0000e0ff-0000-1000-8000-00805f9b34fb",
        ):
            return None

        class Service:
            characteristics = [FakeChar()]

        return Service()


class FakeClient:
    def __init__(self, hub, disconnect_cb) -> None:
        self._hub = hub
        self._disconnect_cb = disconnect_cb
        self.is_connected = True
        self.services = FakeServices()
        self.notify_cb = None

    async def write_gatt_char(self, char, data, response=True) -> None:
        await self._hub.write(bytearray(data))

    async def start_notify(self, char, callback) -> None:
        self.notify_cb = callback

    async def clear_cache(self) -> None:
        pass

    async def disconnect(self) -> None:
        self.is_connected = False

    def simulate_drop(self) -> None:
        self.is_connected = False
        self._disconnect_cb(self)


class FakeHub:
    def __init__(self, status_frame: bytearray) -> None:
        self.status_frame = status_frame
        self.device: object | None = object()
        self.connects = 0
        self.writes: list[bytearray] = []
        self.inflight = 0
        self.max_inflight = 0
        self.client: FakeClient | None = None

    async def establish_connection(self, client_cls, device, name, cb=None, **kwargs):
        self.connects += 1
        self.client = FakeClient(self, cb)
        return self.client

    async def close_stale_connections(self, device) -> None:
        pass

    async def get_device(self, address) -> object | None:
        return self.device

    async def write(self, frame: bytearray) -> None:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0.01)
            self.writes.append(frame)
            if len(frame) > 1 and frame[1] == 0x02:
                self.reply(self.status_frame)
        finally:
            self.inflight -= 1

    def reply(self, frame: bytearray) -> None:
        """Deliver a status reply split at the default 23-byte MTU."""
        assert self.client and self.client.notify_cb
        for offset in range(0, len(frame), 23):
            self.client.notify_cb(FakeChar(), bytearray(frame[offset : offset + 23]))


def _install_stubs() -> None:
    ha = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    core = types.ModuleType("homeassistant.core")

    bluetooth.async_ble_device_from_address = (
        lambda hass, address, connectable=True: CURRENT["hub"].device
    )
    bluetooth.async_last_service_info = (
        lambda hass, address, connectable=True: FakeServiceInfo()
    )
    bluetooth.async_rediscover_address = lambda hass, address: None
    core.HomeAssistant = object
    core.callback = lambda func: func

    ha.components = components
    ha.core = core
    components.bluetooth = bluetooth

    brc = types.ModuleType("bleak_retry_connector")
    brc.BLEAK_RETRY_EXCEPTIONS = (ConnectionError,)
    brc.BleakNotFoundError = type("BleakNotFoundError", (Exception,), {})
    brc.BleakClientWithServiceCache = type("BleakClientWithServiceCache", (object,), {})
    brc.close_stale_connections = lambda device: CURRENT["hub"].close_stale_connections(device)
    brc.establish_connection = lambda *a, **k: CURRENT["hub"].establish_connection(*a, **k)
    brc.get_device = lambda address: CURRENT["hub"].get_device(address)

    bleak = types.ModuleType("bleak")
    exc = types.ModuleType("bleak.exc")
    exc.BleakDBusError = type("BleakDBusError", (Exception,), {})
    bleak.exc = exc

    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.components": components,
            "homeassistant.components.bluetooth": bluetooth,
            "homeassistant.core": core,
            "bleak_retry_connector": brc,
            "bleak": bleak,
            "bleak.exc": exc,
        }
    )


def env(**timings):
    """Load a fresh copy of the modules with test timings applied."""
    for name in [k for k in list(sys.modules) if k.startswith(("homeassistant", "bleak", "spt"))]:
        del sys.modules[name]
    _install_stubs()

    pkg = types.ModuleType("spt")
    pkg.__path__ = [str(SRC)]
    sys.modules["spt"] = pkg

    def load(name: str):
        spec = importlib.util.spec_from_file_location(f"spt.{name}", SRC / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"spt.{name}"] = module
        spec.loader.exec_module(module)
        return module

    load("const")
    protocol = load("protocol")
    trans = load("transport")
    for key, value in timings.items():
        setattr(trans, key, value)
    return protocol, trans


def status_frame_59(level: int = 120) -> bytearray:
    frame = bytearray(59)
    frame[0], frame[1], frame[5], frame[32], frame[33], frame[36] = 0x53, 0x02, 53, 0x02, 0x01, level
    return frame


async def build_link(trans, hub, on_frame, advertise=True):
    link = trans.Sp642eLink(
        object(), "AA:BB:CC:DD:EE:FF", "test", on_frame, lambda: None
    )
    CURRENT["hub"] = hub
    if advertise:
        link.async_advertisement(True)
    await link.async_start()
    return link


FAST = {"BACKOFF_CAP": 0.05, "WRITE_TIMEOUT": 2.0, "NOTIFY_TIMEOUT": 1.0, "COMMAND_SETTLE": 0.0}


async def test_one_write_at_a_time() -> None:
    """Concurrent service calls must never interleave on the characteristic."""
    protocol, trans = env(**FAST)
    hub = FakeHub(bytearray(59))
    link = await build_link(trans, hub, lambda frame: None)

    await asyncio.gather(*(link.async_send(protocol.CommandBuilder.power(i % 2)) for i in range(8)))
    assert hub.max_inflight == 1, f"overlapping writes: {hub.max_inflight}"
    assert len(hub.writes) == 8
    await link.async_stop()


async def test_status_reply_correlates_over_mtu_splits() -> None:
    """A 59-byte reply arrives in 3 notifications and still resolves the caller."""
    protocol, trans = env(**FAST)
    frame = status_frame_59()
    hub = FakeHub(frame)
    seen: list[bytearray] = []
    link = await build_link(trans, hub, seen.append)

    reply = await link.async_send(protocol.CommandBuilder.status_query(), expect_cmd=0x02)
    assert reply == frame
    assert len(seen) == 1, "one frame delivered, not one per notification chunk"
    await link.async_stop()


async def test_advertisement_gates_connecting() -> None:
    """A controller that is not seen must not be hammered with connects."""
    protocol, trans = env(**FAST)
    hub = FakeHub(bytearray(59))
    hub.device = None
    link = await build_link(trans, hub, lambda f: None, advertise=False)

    delivered = False
    try:
        await asyncio.wait_for(
            link.async_send(protocol.CommandBuilder.status_query(), expect_cmd=0x02), 0.3
        )
        delivered = True
    except (trans.LinkError, asyncio.TimeoutError):
        pass
    assert not delivered
    assert hub.connects == 0, "must not connect to a device HA cannot see"
    assert link.state == trans.LinkState.SCANNING

    hub.device = object()
    link.async_advertisement(True)
    for _ in range(40):
        if link.state == trans.LinkState.READY:
            break
        await asyncio.sleep(0.02)
    assert hub.connects >= 1
    assert link.state == trans.LinkState.READY
    await link.async_stop()


async def test_recovers_without_waiting_for_a_poll() -> None:
    """The disconnect callback, not the next poll, drives recovery."""
    protocol, trans = env(**FAST)
    frame = status_frame_59()
    hub = FakeHub(frame)
    link = await build_link(trans, hub, lambda f: None)
    await link.async_send(protocol.CommandBuilder.status_query(), expect_cmd=0x02)
    assert link.state == trans.LinkState.READY

    assert hub.client is not None
    hub.client.simulate_drop()
    # No poll and no service call: recovery is already running on its own.
    for _ in range(50):
        if link.state == trans.LinkState.READY:
            break
        await asyncio.sleep(0.02)
    assert link.state == trans.LinkState.READY, f"stuck in {link.state}"
    assert hub.connects == 2

    reply = await link.async_send(protocol.CommandBuilder.status_query(), expect_cmd=0x02)
    assert reply == frame
    await link.async_stop()


async def test_newer_value_supersedes_queued_older_one() -> None:
    """Slider drags must not queue every intermediate value against the radio."""
    protocol, trans = env(**{**FAST, "BACKOFF_CAP": 30.0, "WRITE_TIMEOUT": 6.0})
    hub = FakeHub(bytearray(59))
    hub.device = None
    link = await build_link(trans, hub, lambda f: None, advertise=False)

    first = asyncio.create_task(
        link.async_send(protocol.CommandBuilder.level(1), coalesce_key="level")
    )
    await asyncio.sleep(0.05)
    sends = [
        asyncio.create_task(
            link.async_send(protocol.CommandBuilder.level(value), coalesce_key="level")
        )
        for value in (2, 3, 4, 5)
    ]
    await asyncio.sleep(0.05)

    # The invariant, checked while the link is still down: only the newest
    # value for a parameter is left queued.
    queued = [tx for tx in link._queue if tx.coalesce_key == "level"]
    assert len(queued) == 1 and queued[0].frame[7] == 5, [tx.frame[7] for tx in queued]
    assert link.stats.superseded >= 2, f"superseded={link.stats.superseded}"

    hub.device = object()
    link.async_advertisement(True)
    results = await asyncio.gather(*sends, return_exceptions=True)
    assert any(r is None for r in results), (
        "a superseded command resolves without hitting the radio"
    )
    try:
        await first
    except trans.LinkError:
        pass
    for _ in range(60):
        if any(w[1] == 0x51 and w[7] == 5 for w in hub.writes):
            break
        await asyncio.sleep(0.02)

    levels = [w[7] for w in hub.writes if len(w) > 1 and w[1] == 0x51]
    assert 5 in levels, levels
    assert len(levels) < 5, f"intermediate values still reached the radio: {levels}"
    await link.async_stop()


async def test_eviction_by_another_central_is_named_not_mystery() -> None:
    """Repeated connect-then-instant-drop is reported as eviction."""
    protocol, trans = env(**FAST)
    hub = FakeHub(bytearray(59))
    states: list[str] = []
    link = trans.Sp642eLink(
        object(), "AA:BB:CC:DD:EE:FF", "test", lambda f: None, lambda: None
    )
    original_set_state = link._set_state

    def record_state(state: str) -> None:
        states.append(state)
        original_set_state(state)

    link._set_state = record_state  # type: ignore[method-assign]
    CURRENT["hub"] = hub
    link.async_advertisement(True)
    await link.async_start()

    for _ in range(trans.QUICK_DROP_LIMIT):
        for _ in range(60):
            if link.state == trans.LinkState.READY:
                break
            await asyncio.sleep(0.02)
        assert hub.client is not None
        hub.client.simulate_drop()
        link.async_advertisement(True)
        await asyncio.sleep(0.02)

    # The supervisor may already have reconnected, so assert on the history
    # rather than the instantaneous state.
    assert trans.LinkState.EVICTED in states, states
    assert hub.connects >= trans.QUICK_DROP_LIMIT
    await link.async_stop()


def run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            asyncio.run(test())
        except Exception:  # noqa: BLE001
            failures += 1
            import traceback

            traceback.print_exc()
            print(f"FAIL {test.__name__}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
