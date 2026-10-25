"""Light platform: on/off, brightness, colour temperature, effects."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.color import color_temperature_mired_to_kelvin
import logging

from .entity import Sp642eEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([Sp642eLight(config_entry.runtime_data)])


class Sp642eLight(Sp642eEntity, RestoreEntity, LightEntity):
    """The CCT light the SP642E physically drives."""

    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_name = None

    def __init__(self, device: Any) -> None:
        super().__init__(device, "light")
        self._attr_min_color_temp_kelvin = device.min_kelvin
        self._attr_max_color_temp_kelvin = device.max_kelvin
        self._attr_effect_list = list(device.supported_effects)

    @property
    def is_on(self) -> bool:
        return self._device.state.power

    @property
    def brightness(self) -> int:
        return self._device.state.brightness

    @property
    def color_temp_kelvin(self) -> int:
        return self._device.state.kelvin

    @property
    def effect(self) -> str | None:
        return self._device.state.effect

    async def async_added_to_hass(self) -> None:
        """Seed from the last known state so an HA restart is not a blackout."""
        await super().async_added_to_hass()
        if self._device.state.known:
            return
        restored = await self.async_get_last_state()
        if restored is None:
            return
        attrs = restored.attributes
        self._device.apply_restored(
            power=restored.state == "on",
            brightness=attrs.get(ATTR_BRIGHTNESS),
            kelvin=attrs.get(ATTR_COLOR_TEMP_KELVIN)
            or (
                color_temperature_mired_to_kelvin(attrs[ATTR_COLOR_TEMP])
                if attrs.get(ATTR_COLOR_TEMP)
                else None
            ),
            effect=attrs.get(ATTR_EFFECT),
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        if kelvin is None and ATTR_COLOR_TEMP in kwargs:
            kelvin = color_temperature_mired_to_kelvin(kwargs[ATTR_COLOR_TEMP])
        effect = kwargs.get(ATTR_EFFECT)
        if effect is not None and effect not in self._device.supported_effects:
            _LOGGER.warning(
                "%s: ignoring unknown effect %r; supported: %s",
                self._device.name,
                effect,
                ", ".join(self._device.supported_effects),
            )
            effect = None

        await self._device.async_turn_on(
            brightness=brightness, kelvin=kelvin, effect=effect
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._device.async_turn_off()
