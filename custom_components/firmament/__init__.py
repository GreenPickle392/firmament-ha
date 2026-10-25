"""SP642E CCT Controller integration for Home Assistant."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .config_flow import model_id_from_manufacturer_data
from .const import (
    CONF_MODEL_ID,
    CONF_RECONCILE_INTERVAL,
    RECONCILE_INTERVAL,
    STARTUP_STATUS_BUDGET,
)
from .device import Sp642eDevice

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.NUMBER, Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry[Sp642eDevice]) -> bool:
    """Set up one controller and keep its link supervised."""
    address = str(entry.data[CONF_ADDRESS]).upper()
    name = str(entry.data.get(CONF_NAME) or entry.title)
    device = Sp642eDevice(hass, address, name, dict(entry.options))
    device.model_id = entry.data.get(CONF_MODEL_ID)

    @callback
    def _async_on_advertisement(
        service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """Tell the supervisor the controller is present, and what model it is."""
        device.link.async_advertisement(change is not BluetoothChange.DISAPPEARED)
        model_id = model_id_from_manufacturer_data(service_info)
        if model_id is not None:
            device.model_id = model_id

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_on_advertisement,
            BluetoothCallbackMatcher(address=address, connectable=True),
            BluetoothScanningMode.PASSIVE,
        )
    )

    await device.async_start()
    if not await device.async_startup_status(STARTUP_STATUS_BUDGET):
        # Deliberately not ConfigEntryNotReady: the entities need to exist so
        # the link sensor can report what the supervisor is doing.
        _LOGGER.warning(
            "%s: no status from controller within %ss; retrying in the "
            "background, so entities may be stale until it answers",
            name,
            STARTUP_STATUS_BUDGET,
        )

    entry.runtime_data = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    interval = int(entry.options.get(CONF_RECONCILE_INTERVAL, RECONCILE_INTERVAL))

    async def _async_reconcile(_now: datetime | None = None) -> None:
        await device.async_refresh(force=True)

    entry.async_on_unload(
        async_track_time_interval(hass, _async_reconcile, timedelta(seconds=interval))
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry[Sp642eDevice]
) -> None:
    """Apply kelvin range options by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry[Sp642eDevice]) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
