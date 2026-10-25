"""Offline checks for the wire protocol.

These need no Home Assistant and no radio: framing and reassembly are the
layers whose bugs showed up as 'loses connection', so they are testable in
isolation on purpose.
"""
from __future__ import annotations

import sys
from pathlib import Path

# protocol.py has no Home Assistant or relative imports on purpose, so it is
# importable as a bare module and testable without a running HA install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "firmament"))

from protocol import (  # noqa: E402
    Cmd,
    CommandBuilder,
    EncryptedPacketError,
    FrameReader,
    Mode,
    ProtocolError,
    cct_to_kelvin,
    encode,
    kelvin_to_cct,
    parse_status,
)

STATUS_TOTAL = 59


def status_frame(
    *, power=1, mode=int(Mode.STATIC_WHITE), effect=1, level=200,
    cold=255, warm=0, speed=2, firmware=b"V1.2.3",
) -> bytearray:
    frame = bytearray(STATUS_TOTAL)
    frame[0] = 0x53
    frame[1] = int(Cmd.STATUS)
    frame[2] = 0x00
    frame[3] = 0x01
    frame[4] = 0x00
    frame[5] = STATUS_TOTAL - 6
    frame[11:19] = firmware.ljust(8, b"\x00")
    frame[19] = 0x03
    frame[29] = power
    frame[30] = 0
    frame[31] = 0
    frame[32] = mode
    frame[33] = effect
    frame[34] = 1
    frame[36] = level
    frame[40] = cold
    frame[41] = warm
    frame[42] = speed
    frame[50] = cold
    frame[51] = warm
    return frame


def test_encode_matches_known_good_bytes() -> None:
    assert bytes(encode(Cmd.STATUS, bytearray([0x01]))) == bytes(
        [0x53, 0x02, 0x00, 0x01, 0x00, 0x01, 0x01]
    )
    assert bytes(CommandBuilder.power(True)) == bytes(
        [0x53, 0x50, 0x00, 0x01, 0x00, 0x01, 0x01]
    )
    assert bytes(CommandBuilder.cct(255, 0, static=True)) == bytes(
        [0x53, 0x61, 0x00, 0x01, 0x00, 0x02, 0xFF, 0x00]
    )
    assert bytes(CommandBuilder.cct(255, 0, static=False)) == bytes(
        [0x53, 0x60, 0x00, 0x01, 0x00, 0x02, 0xFF, 0x00]
    )
    assert bytes(CommandBuilder.level(128)) == bytes(
        [0x53, 0x51, 0x00, 0x01, 0x00, 0x02, 0x01, 0x80]
    )


def test_reassembles_frame_split_across_notifications() -> None:
    """A 59-byte status reply arrives as 23 + 23 + 13 byte notifications."""
    frame = status_frame()
    reader = FrameReader()
    assert reader.feed(frame[:23]) == []
    assert reader.feed(frame[23:46]) == []
    frames = reader.feed(frame[46:])
    assert len(frames) == 1
    assert frames[0] == frame
    status = parse_status(frames[0])
    assert status.power is True
    assert status.level == 200
    assert status.firmware == "V1.2.3"
    assert status.effect_name == "Solid"


def test_splits_two_frames_in_one_chunk() -> None:
    reader = FrameReader()
    one = CommandBuilder.power(True)
    two = CommandBuilder.status_query()
    frames = reader.feed(one + two)
    assert len(frames) == 2
    assert frames[0][1] == int(Cmd.POWER)
    assert frames[1][1] == int(Cmd.STATUS)


def test_resyncs_after_garbage() -> None:
    reader = FrameReader()
    assert reader.feed(b"\x00\x11\x22") == []
    frames = reader.feed(CommandBuilder.power(False))
    assert len(frames) == 1
    assert frames[0][1] == int(Cmd.POWER)
    assert reader.pending == 0


def test_truncated_status_is_an_error_not_a_crash() -> None:
    frame = status_frame()[:40]
    try:
        parse_status(frame)
    except ProtocolError as err:
        assert "truncated" in str(err)
    else:
        raise AssertionError("truncated frame should raise ProtocolError")


def test_encrypted_packet_is_reported() -> None:
    frame = status_frame()
    frame[2] = 0x07
    try:
        parse_status(frame)
    except EncryptedPacketError:
        return
    raise AssertionError("nonzero key byte should raise EncryptedPacketError")


def test_kelvin_round_trips() -> None:
    for kelvin in range(2000, 6501, 100):
        cold, warm = kelvin_to_cct(kelvin, 2000, 6500)
        back = cct_to_kelvin(cold, warm, 2000, 6500)
        assert abs(back - kelvin) <= 40, (kelvin, cold, warm, back)


def test_kelvin_extremes_drive_one_channel() -> None:
    assert kelvin_to_cct(6500, 2000, 6500) == (255, 0)
    assert kelvin_to_cct(2000, 2000, 6500) == (0, 255)


def test_dynamic_mode_reads_the_second_cct_pair() -> None:
    frame = status_frame(
        mode=int(Mode.DYNAMIC_WHITE), effect=1, cold=100, warm=155
    )
    status = parse_status(frame)
    assert (status.cold, status.warm) == (100, 155)
    assert status.effect_name == "Breath"
    assert status.is_dynamic is True


def run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {type(err).__name__}: {err}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
