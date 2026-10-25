"""Base entity for the SP642E integration."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, MANUFACTURER
from .device import Sp642eDevice


class Sp642eEntity:
    """Shares one controller connection across all its entities."""

    _attr_has_entity_name = True

    def __init__(self, device: Sp642eDevice, key: str) -> None:
        # This is a mixin, so super() walks into RestoreEntity / the platform
        # entity and gives them their own attribute initialisation.
        super().__init__()
        self._device = device
        self._unsub: Callable[[], None] | None = None
        self._attr_unique_id = f"{device.address.lower()}-{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.address.lower())},
            name=self._device.name,
            manufacturer=MANUFACTURER,
            model=self._device.model_name,
            sw_version=self._device.state.firmware,
            connections={(dr.CONNECTION_BLUETOOTH, self._device.address.lower())},
        )

    @property
    def available(self) -> bool:
        return self._device.entity_available

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"link_state": self._device.link_state}

    async def async_added_to_hass(self) -> None:
        # Continue up the MRO: RestoreEntity and Entity both have work to do
        # here, and stopping at this class would silently disable state
        # restore for the light.
        self._unsub = self._device.listen(self._schedule_update)
        await super().async_added_to_hass()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()

    @callback
    def _schedule_update(self) -> None:
        # State arrives from GATT notifications, so there is nothing to poll;
        # force_update would only add radio traffic.
        self.async_schedule_update_ha_state(False)
