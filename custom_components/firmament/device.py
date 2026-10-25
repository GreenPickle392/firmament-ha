"""Device model: state cache, optimistic writes, command orchestration."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
from typing import Any, Callable

from .const import (
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    STATUS_CACHE_WINDOW,
)
from .protocol import (
    Cmd,
    CommandBuilder,
    EncryptedPacketError,
    EFFECT_LIST,
    EFFECTS,
    MODEL_NAMES,
    Mode,
    ProtocolError,
    cct_to_kelvin,
    kelvin_to_cct,
    parse_status,
)
from .transport import LinkError, LinkState, Sp642eLink

_LOGGER = logging.getLogger(__name__)

# Only the newest queued command per key survives, so dragging a brightness
# slider does not queue dozens of writes against a one-frame-at-a-time radio.
KEY_CCT = "cct"
KEY_LEVEL = "level"
KEY_MODE = "mode"
KEY_POWER = "power"
KEY_SPEED = "speed"
KEY_STATUS = "status"

REFRESH_DEBOUNCE = 0.6


@dataclass(slots=True)
class Sp642eState:
    """Merged device-reported and optimistically-set light state."""

    power: bool = False
    brightness: int = 255
    kelvin: int = DEFAULT_MAX_KELVIN
    effect: str = "Solid"
    speed: int = 2
    firmware: str | None = None
    light_type: int | None = None
    known: bool = False
    error: str | None = None

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "power": self.power,
            "brightness": self.brightness,
            "kelvin": self.kelvin,
            "effect": self.effect,
            "speed": self.speed,
            "firmware": self.firmware,
            "known": self.known,
            "error": self.error,
        }


class Sp642eDevice:
    """One SP642E controller."""

    def __init__(
        self, hass: Any, address: str, name: str, entry_options: dict[str, Any]
    ) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self.min_kelvin = int(entry_options.get(CONF_MIN_KELVIN, DEFAULT_MIN_KELVIN))
        self.max_kelvin = int(entry_options.get(CONF_MAX_KELVIN, DEFAULT_MAX_KELVIN))

        self.link = Sp642eLink(
            hass, address, name, self._on_frame, self._on_link_state
        )
        self.state = Sp642eState(
            kelvin=round((self.min_kelvin + self.max_kelvin) / 2)
        )
        self.model_id: int | None = None

        self._listeners: list[Callable[[], None]] = []
        self._refresh_task: asyncio.Task | None = None
        self._last_status_mono: float = 0.0
        self._encrypt_warned = False

    # ------------------------------------------------------------------ state

    @property
    def available(self) -> bool:
        return self.link.state == LinkState.READY

    @property
    def entity_available(self) -> bool:
        """Whether entities should accept commands.

        Kept available while the supervisor is recovering, so a queued turn-on
        survives a brief reconnect. Only EVICTED - another central holds the
        controller, so no amount of retrying will help - greys the entities
        out and points the user at the real cause.
        """
        return self.link.state != LinkState.EVICTED

    @property
    def link_state(self) -> str:
        return self.link.state

    @property
    def supported_effects(self) -> list[str]:
        return EFFECT_LIST

    @property
    def model_name(self) -> str:
        if self.model_id is not None:
            return MODEL_NAMES.get(self.model_id, f"BanlanX 0x{self.model_id:02x}")
        return "SP642E"

    def listen(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def unsub() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsub

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("%s: entity listener raised", self.name)

    # ---------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        await self.link.async_start()

    async def async_stop(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        await self.link.async_stop()
        self._listeners.clear()

    async def async_startup_status(self, budget: float) -> bool:
        """Fetch status once, within `budget` seconds, for entry setup."""
        deadline = asyncio.get_running_loop().time() + budget
        while asyncio.get_running_loop().time() < deadline:
            if await self.async_refresh():
                return True
            await asyncio.sleep(1.0)
        return False

    # ---------------------------------------------------------------- commands

    async def async_turn_on(
        self,
        brightness: int | None = None,
        kelvin: int | None = None,
        effect: str | None = None,
        speed: int | None = None,
    ) -> None:
        """Apply a turn-on, optimistically, then reconcile from the device."""
        target_mode, target_effect = self._resolve_effect(effect)
        next_state = replace(
            self.state,
            power=True,
            brightness=self.state.brightness if brightness is None else brightness,
            kelvin=self.state.kelvin if kelvin is None else kelvin,
            effect=target_effect if effect is not None else self.state.effect,
            speed=self.state.speed if speed is None else speed,
            error=None,
        )
        self._apply(next_state)

        frames: list[tuple[bytearray, str]] = []

        if effect is not None or speed is not None:
            frames.append(
                (
                    CommandBuilder.mode_effect(target_mode, EFFECTS[target_effect][1]),
                    KEY_MODE,
                )
            )
        if kelvin is not None or effect is not None:
            cold, warm = kelvin_to_cct(next_state.kelvin, self.min_kelvin, self.max_kelvin)
            frames.append(
                (
                    CommandBuilder.cct(cold, warm, static=target_mode == Mode.STATIC_WHITE),
                    KEY_CCT,
                )
            )
        if brightness is not None or effect is not None:
            frames.append(
                (CommandBuilder.level(next_state.brightness), KEY_LEVEL)
            )
        if speed is not None:
            frames.append((CommandBuilder.effect_speed(speed), KEY_SPEED))
        frames.append((CommandBuilder.power(True), KEY_POWER))

        await self._send_frames(frames)

    async def async_turn_off(self) -> None:
        self._apply(replace(self.state, power=False, error=None))
        await self._send_frames([(CommandBuilder.power(False), KEY_POWER)])

    async def async_set_speed(self, speed: int) -> None:
        self._apply(replace(self.state, speed=speed, error=None))
        await self._send_frames(
            [(CommandBuilder.effect_speed(speed), KEY_SPEED)]
        )

    async def async_refresh(self, force: bool = False) -> bool:
        """Query device status; reuses a recently received frame."""
        now = asyncio.get_running_loop().time()
        if (
            not force
            and self.state.known
            and now - self._last_status_mono < STATUS_CACHE_WINDOW
        ):
            return True
        try:
            reply = await self.link.async_send(
                CommandBuilder.status_query(),
                expect_cmd=int(Cmd.STATUS),
                coalesce_key=KEY_STATUS,
            )
        except LinkError as err:
            self._set_error(str(err))
            return False
        if reply is None:
            # Superseded by a newer query; that one carries the state.
            return self.state.known
        # _on_frame already ingested this frame; a second parse would notify
        # every entity twice for one device reply.
        return True

    # ----------------------------------------------------------------- internal

    async def _send_frames(self, frames: list[tuple[bytearray, str]]) -> None:
        failed: str | None = None
        for frame, key in frames:
            try:
                await self.link.async_send(frame, coalesce_key=key)
            except LinkError as err:
                # Optimistic state already reflects the intent, and the link
                # supervisor is retrying; report it on the entity rather than
                # failing the service call into a hung UI.
                failed = str(err)
                _LOGGER.debug("%s: command deferred: %s", self.name, err)
                break
        self._set_error(failed)
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()

        async def _later() -> None:
            try:
                await asyncio.sleep(REFRESH_DEBOUNCE)
            except asyncio.CancelledError:
                return
            await self.async_refresh(force=True)

        self._refresh_task = self.hass.async_create_task(
            _later(), name=f"{self.name}-reconcile"
        )

    def apply_restored(
        self,
        power: bool,
        brightness: int | None,
        kelvin: int | None,
        effect: str | None,
    ) -> None:
        """Seed optimistic state after an HA restart, without touching the radio."""
        self._apply(
            replace(
                self.state,
                power=power,
                brightness=self.state.brightness if brightness is None else brightness,
                kelvin=self.state.kelvin if kelvin is None else kelvin,
                effect=(
                    self.state.effect
                    if effect is None or effect not in EFFECTS
                    else effect
                ),
            )
        )

    def _resolve_effect(self, effect: str | None) -> tuple[Mode, str]:
        """Return the (mode, effect name) to write, falling back to Solid."""
        for name in (effect, self.state.effect):
            if name is not None and name in EFFECTS:
                return EFFECTS[name][0], name
        return EFFECTS["Solid"][0], "Solid"

    def _apply(self, state: Sp642eState) -> None:
        self.state = state
        self._notify()

    def _set_error(self, error: str | None) -> None:
        if error == self.state.error:
            return
        self.state = replace(self.state, error=error)
        self._notify()

    def _on_link_state(self) -> None:
        if self.link.ready:
            self._schedule_refresh()
        self._notify()

    def _on_frame(self, frame: bytearray) -> None:
        if len(frame) < 2:
            return
        if frame[1] != int(Cmd.STATUS):
            _LOGGER.debug("%s: ignoring reply to 0x%02x", self.name, frame[1])
            return
        self._ingest(frame)

    def _ingest(self, frame: bytearray) -> bool:
        try:
            status = parse_status(frame)
        except EncryptedPacketError as err:
            if not self._encrypt_warned:
                self._encrypt_warned = True
                _LOGGER.error(
                    "%s: controller is using an encrypted session (%s); this "
                    "integration only speaks the plaintext BanlanX protocol - "
                    "reset the controller to factory state",
                    self.name,
                    err,
                )
            self._set_error("encrypted session - controller needs factory reset")
            return False
        except ProtocolError as err:
            self._set_error(f"bad status frame: {err}")
            return False

        self._last_status_mono = asyncio.get_running_loop().time()
        kelvin = cct_to_kelvin(status.cold, status.warm, self.min_kelvin, self.max_kelvin)
        effect = status.effect_name or self.state.effect
        brightness = status.level if status.level else self.state.brightness

        self.state = replace(
            self.state,
            power=status.power,
            brightness=brightness,
            kelvin=kelvin,
            effect=effect,
            speed=status.speed,
            firmware=status.firmware or self.state.firmware,
            light_type=status.light_type,
            known=True,
            error=None,
        )
        self._notify()
        return True

    def diagnostics(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "min_kelvin": self.min_kelvin,
            "max_kelvin": self.max_kelvin,
            "state": self.state.to_status_dict(),
            "link": self.link.snapshot(),
        }
