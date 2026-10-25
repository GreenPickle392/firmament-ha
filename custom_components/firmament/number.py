"""Number platform: effect speed for the dynamic white modes."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import Sp642eEntity
from .protocol import MAX_EFFECT_SPEED, MIN_EFFECT_SPEED


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = config_entry.runtime_data
    async_add_entities([Sp642eEffectSpeed(device)])


class Sp642eEffectSpeed(Sp642eEntity, NumberEntity):
    """Only meaningful in a dynamic mode, but always writable."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:speedometer"
    _attr_mode = NumberMode.SLIDER
    _attr_min_value = MIN_EFFECT_SPEED
    _attr_max_value = MAX_EFFECT_SPEED
    _attr_step = 1

    def __init__(self, device: Any) -> None:
        super().__init__(device, "effect_speed")
        self._attr_name = "Effect speed"

    @property
    def native_value(self) -> float:
        return self._device.state.speed

    async def async_set_native_value(self, value: float) -> None:
        await self._device.async_set_speed(int(value))
