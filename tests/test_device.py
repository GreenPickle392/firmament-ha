"""Offline checks for command composition and state reconciliation.

This is the layer that decides which frames a single light turn-on becomes, so
the ordering, the effect -> (mode, code) mapping and the optimistic state are
asserted here against the same fake radio the transport tests use.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys

from test_transport import CURRENT, FAST, FakeHub, SRC, env, status_frame_59


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"spt.{name}", SRC / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"spt.{name}"] = module
    spec.loader.exec_module(module)
    return module


def env2(**timings) -> tuple:
    protocol, trans = env(**{**FAST, **timings})
    return protocol, trans, load("device")


class StubHass:
    """Only async_create_task is used by the device layer."""

    def async_create_task(self, target, name=None):  # noqa: ANN001, ARG002
        return asyncio.create_task(target)


async def build(device_mod, hub, options=None):
    CURRENT["hub"] = hub
    device = device_mod.Sp642eDevice(
        StubHass(), "AA:BB:CC:DD:EE:FF", "test", options or {}
    )
    device.link.async_advertisement(True)
    await device.async_start()
    return device


def commands(hub) -> list[int]:
    return [frame[1] for frame in hub.writes]


async def test_turn_on_with_effect_writes_mode_then_cct_level_power() -> None:
    """One service call must become the right frames, in the right order."""
    protocol, _, device_mod = env2()
    hub = FakeHub(bytearray(59))
    device = await build(device_mod, hub)

    await device.async_turn_on(brightness=128, kelvin=3000, effect="Breath")

    assert commands(hub) == [0x53, 0x60, 0x51, 0x50], hub.writes
    mode_frame = hub.writes[0]
    assert mode_frame[6] == int(protocol.Mode.DYNAMIC_WHITE)
    assert mode_frame[7] == 0x01, "Breath is effect 0x01 inside dynamic white"
    assert abs(hub.writes[1][6] + hub.writes[1][7] - 255) <= 1, "CCT split stays full-scale"
    assert hub.writes[2][7] == 128
    assert hub.writes[3][6] == 0x01

    assert device.state.power is True
    assert device.state.brightness == 128
    assert device.state.effect == "Breath"
    await device.async_stop()


async def test_solid_effect_writes_the_static_cct_command() -> None:
    """Static white and dynamic white take different CCT opcodes."""
    _, _, device_mod = env2()
    hub = FakeHub(bytearray(59))
    device = await build(device_mod, hub)

    await device.async_turn_on(effect="Solid")
    assert 0x61 in commands(hub), hub.writes

    await device.async_turn_on(effect="Breath")
    assert 0x60 in commands(hub), hub.writes
    await device.async_stop()


async def test_unknown_effect_is_ignored_rather_than_raising() -> None:
    """A typo in an automation must not blow up the service call."""
    _, _, device_mod = env2()
    hub = FakeHub(bytearray(59))
    device = await build(device_mod, hub)

    await device.async_turn_on(effect="Disco Inferno")

    assert device.state.effect == "Solid"
    assert 0x50 in commands(hub), "the light still turns on"
    await device.async_stop()


async def test_restore_seeds_state_without_radio_traffic() -> None:
    """After an HA restart the light should look right before the radio answers."""
    _, _, device_mod = env2()
    hub = FakeHub(bytearray(59))
    device = await build(device_mod, hub)
    hub.writes.clear()

    device.apply_restored(power=True, brightness=90, kelvin=2700, effect="Breath")

    assert hub.writes == []
    assert device.state.brightness == 90
    assert device.state.kelvin == 2700
    assert device.state.effect == "Breath"
    await device.async_stop()


async def test_status_reply_updates_the_cached_state() -> None:
    """A pushed frame is what moves the entity, not a poll."""
    _, _, device_mod = env2()
    hub = FakeHub(status_frame_59(level=200))
    device = await build(device_mod, hub)

    await device.async_refresh()

    assert device.state.known is True
    assert device.state.brightness == 200
    assert device.state.effect == "Solid"
    await device.async_stop()


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
