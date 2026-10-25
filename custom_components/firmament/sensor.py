"""Sensors that make the link observable, which is what UniLED never was."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import Sp642eEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    device = config_entry.runtime_data
    async_add_entities(
        [
            Sp642eLinkState(device),
            Sp642eRssi(device),
            Sp642eReconnects(device),
        ]
    )


class Sp642eLinkState(Sp642eEntity, SensorEntity):
    """The supervisor's state, so 'lost connection' becomes a diagnosis."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, device: Any) -> None:
        super().__init__(device, "link_state")
        self._attr_name = "Link state"

    @property
    def native_value(self) -> str:
        return self._device.link_state

    @property
    def available(self) -> bool:
        # This sensor is the thing that must keep reporting during an outage.
        return True


class Sp642eRssi(Sp642eEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "dBm"
    _attr_icon = "mdi:signal-cell-3"

    def __init__(self, device: Any) -> None:
        super().__init__(device, "rssi")
        self._attr_name = "Signal strength"

    @property
    def native_value(self) -> int:
        return self._device.link.rssi

    @property
    def available(self) -> bool:
        return True


class Sp642eReconnects(Sp642eEntity, SensorEntity):
    """Counts drops, so flapping is measurable rather than anecdotal."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:restart"

    def __init__(self, device: Any) -> None:
        super().__init__(device, "drops")
        self._attr_name = "Connection drops"

    @property
    def native_value(self) -> int:
        return self._device.link.stats.drops

    @property
    def available(self) -> bool:
        return True
