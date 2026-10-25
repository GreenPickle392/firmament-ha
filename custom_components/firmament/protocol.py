"""BanlanX 6xx wire protocol, as spoken by the SP642E (2 CH PWM CCT).

No Home Assistant imports: framing is testable on its own, and it is the layer
that UniLED got wrong in ways that show up as dropped connections.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Final

HEADER: Final = 0x53
HEADER_LEN: Final = 6
MIN_FRAME_LEN: Final = HEADER_LEN
STATUS_MIN_LEN: Final = 53


class Cmd(IntEnum):
    """Command opcodes used by the CCT (CFG_83) light type."""

    STATUS = 0x02
    ONOFF_EFFECT = 0x08
    COEXISTENCE = 0x0A
    ON_POWER = 0x0B
    POWER = 0x50
    LEVEL = 0x51
    MODE_EFFECT = 0x53
    EFFECT_SPEED = 0x54
    EFFECT_LENGTH = 0x55
    EFFECT_DIRECTION = 0x56
    EFFECT_LOOP = 0x58
    AUDIO_INPUT = 0x59
    SENSITIVITY = 0x5A
    EFFECT_PLAY = 0x5D
    CCT_DYNAMIC = 0x60
    CCT_STATIC = 0x61
    LIGHT_TYPE = 0x6A
    CHIP_ORDER = 0x6B


class Mode(IntEnum):
    """Light modes available on a CCT controller."""

    STATIC_WHITE = 0x02
    DYNAMIC_WHITE = 0x04
    SOUND_WHITE = 0x06


MAX_EFFECT_SPEED: Final = 0xFF
MIN_EFFECT_SPEED: Final = 1

# effect name -> (mode, effect code)
EFFECTS: Final[dict[str, tuple[Mode, int]]] = {
    "Solid": (Mode.STATIC_WHITE, 0x01),
    "Breath": (Mode.DYNAMIC_WHITE, 0x01),
    "Strobe": (Mode.DYNAMIC_WHITE, 0x02),
    "Heartbeat": (Mode.DYNAMIC_WHITE, 0x03),
    "Music": (Mode.SOUND_WHITE, 0x01),
}
EFFECT_LIST: Final[list[str]] = list(EFFECTS)
_EFFECT_BY_CODE: Final[dict[tuple[int, int], str]] = {
    (int(mode), code): name for name, (mode, code) in EFFECTS.items()
}

WHITE_CHANNEL = 0x01
COLOR_CHANNEL = 0x00

# Advertising manufacturer data carries the model id; only the CCT pair is
# implemented here, everything else in this family needs a different light type.
MODEL_NAMES: Final[dict[int, str]] = {0x21: "SP632E", 0x2D: "SP642E"}


class ProtocolError(Exception):
    """Raised when a frame cannot be interpreted."""


class EncryptedPacketError(ProtocolError):
    """The controller negotiated a key; the payload byte at index 2 is nonzero."""


def encode(cmd: Cmd | int, payload: bytes | bytearray = b"") -> bytearray:
    """Frame one command: 53 <cmd> <key> 01 00 <len> <payload...>."""
    body = bytearray(payload)
    if len(body) > 0xFF:
        raise ProtocolError(f"payload too long for one frame: {len(body)}")
    return bytearray(
        [HEADER, int(cmd) & 0xFF, 0x00, 0x01, 0x00, len(body) & 0xFF]
    ) + body


class FrameReader:
    """Reassemble frames from GATT notifications.

    Status replies are 53+ bytes and arrive split across several notifications
    at the default 23-byte MTU. Parsing each chunk on its own - which is what
    reads `data[52]` positionally and breaks - is avoided here by buffering
    until a whole frame is present.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    @property
    def pending(self) -> int:
        return len(self._buf)

    def feed(self, chunk: bytes | bytearray) -> list[bytearray]:
        """Return every complete frame available after appending `chunk`."""
        self._buf.extend(chunk)
        frames: list[bytearray] = []
        while True:
            if len(self._buf) < MIN_FRAME_LEN:
                return frames
            if self._buf[0] != HEADER:
                # Lost a chunk mid-frame: resync on the next header rather than
                # let a bad byte poison the buffer forever.
                del self._buf[0]
                continue
            total = self._buf[5] + HEADER_LEN
            if total < MIN_FRAME_LEN:
                del self._buf[0]
                continue
            if len(self._buf) < total:
                return frames
            frames.append(bytearray(self._buf[:total]))
            del self._buf[:total]


@dataclass(slots=True)
class Status:
    """Decoded device status."""

    power: bool = False
    mode: int = Mode.STATIC_WHITE
    effect: int = 0x01
    level: int = 0xFF
    cold: int = 0xFF
    warm: int = 0
    speed: int = 0x02
    length: int = 0
    direction: bool = False
    loop: bool = False
    play: bool = True
    firmware: str = ""
    light_type: int = 0x03
    chip_order: int = 0
    raw: bytes = field(default=b"", repr=False)

    @property
    def effect_name(self) -> str | None:
        return _EFFECT_BY_CODE.get((int(self.mode), int(self.effect)))

    @property
    def is_dynamic(self) -> bool:
        return self.mode != Mode.STATIC_WHITE


def parse_status(frame: bytearray) -> Status:
    """Decode a 0x02 status frame."""
    if len(frame) < HEADER_LEN:
        raise ProtocolError("frame is undersized")
    key = frame[2]
    if key:
        raise EncryptedPacketError(f"encrypted packet, key byte 0x{key:02x}")
    cmd = frame[1]
    if cmd != Cmd.STATUS:
        raise ProtocolError(f"not a status frame: cmd 0x{cmd:02x}")
    if len(frame) < STATUS_MIN_LEN:
        raise ProtocolError(
            f"status frame truncated: {len(frame)} bytes, need {STATUS_MIN_LEN}"
        )

    mode = frame[32]
    # Dynamic and sound white modes report their balance in the second CCT
    # pair; static white uses the first.
    if mode == Mode.STATIC_WHITE:
        cold, warm = frame[40], frame[41]
    else:
        cold, warm = frame[50], frame[51]

    return Status(
        power=frame[29] > 0x00,
        mode=mode,
        effect=frame[33],
        level=frame[36],
        cold=cold,
        warm=warm,
        speed=frame[42],
        length=frame[43],
        direction=bool(frame[44]),
        loop=bool(frame[30]),
        play=bool(frame[34]),
        firmware=bytes(frame[11:18]).decode("utf-8", "replace").strip("\x00 "),
        light_type=frame[19],
        chip_order=frame[31],
        raw=bytes(frame),
    )


def kelvin_to_cct(
    kelvin: int, min_kelvin: int, max_kelvin: int
) -> tuple[int, int]:
    """Split a colour temperature into (cold, warm) PWM levels.

    Interpolated in mired space, which is the perceptually linear axis these
    two-PWM CCT strips are wired for: at max_kelvin only the cold channel is
    lit, at min_kelvin only the warm one.
    """
    kelvin = max(min_kelvin, min(max_kelvin, kelvin))
    warm_ratio = _mired_ratio(kelvin, min_kelvin, max_kelvin)
    return (
        round(255 * (1.0 - warm_ratio)),
        round(255 * warm_ratio),
    )


def cct_to_kelvin(
    cold: int, warm: int, min_kelvin: int, max_kelvin: int
) -> int:
    """Inverse of kelvin_to_cct, so a write/read round trip is stable."""
    total = cold + warm
    if total <= 0:
        return round((min_kelvin + max_kelvin) / 2)
    min_mireds = 1_000_000 / max_kelvin
    max_mireds = 1_000_000 / min_kelvin
    warm_ratio = warm / total
    mireds = min_mireds + warm_ratio * (max_mireds - min_mireds)
    return round(1_000_000 / mireds)


def _mired_ratio(kelvin: int, min_kelvin: int, max_kelvin: int) -> float:
    """0.0 at max_kelvin (coldest), 1.0 at min_kelvin (warmest)."""
    min_mireds = 1_000_000 / max_kelvin
    max_mireds = 1_000_000 / min_kelvin
    mireds = 1_000_000 / kelvin
    span = max_mireds - min_mireds
    if span <= 0:
        return 0.5
    return (mireds - min_mireds) / span


class CommandBuilder:
    """Builds the byte sequences the CCT light type expects."""

    @staticmethod
    def status_query() -> bytearray:
        return encode(Cmd.STATUS, bytearray([0x01]))

    @staticmethod
    def power(state: bool) -> bytearray:
        return encode(Cmd.POWER, bytearray([0x01 if state else 0x00]))

    @staticmethod
    def level(value: int, white: bool = True) -> bytearray:
        which = WHITE_CHANNEL if white else COLOR_CHANNEL
        return encode(Cmd.LEVEL, bytearray([which, max(0, min(255, value))]))

    @staticmethod
    def cct(cold: int, warm: int, static: bool) -> bytearray:
        cmd = Cmd.CCT_STATIC if static else Cmd.CCT_DYNAMIC
        return encode(cmd, bytearray([max(0, min(255, cold)), max(0, min(255, warm))]))

    @staticmethod
    def mode_effect(mode: Mode | int, effect: int) -> bytearray:
        return encode(Cmd.MODE_EFFECT, bytearray([int(mode) & 0xFF, effect & 0xFF]))

    @staticmethod
    def effect_speed(value: int) -> bytearray:
        return encode(
            Cmd.EFFECT_SPEED,
            bytearray([max(MIN_EFFECT_SPEED, min(MAX_EFFECT_SPEED, value))]),
        )
