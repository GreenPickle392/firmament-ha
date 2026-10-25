"""Configuration flow for the SP642E CCT Controller integration."""
from __future__ import annotations

from typing import Any

import re
import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback

from .const import (
    CONF_MANUFACTURER,
    CONF_MAX_KELVIN,
    CONF_MIN_KELVIN,
    CONF_MODEL_ID,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    DOMAIN,
    MANUFACTURER,
)

ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
BANLANX_MANUFACTURER_ID = 20563
BANLANX_DATA_TAG = 0x10

STEP_USER = vol.Schema(
    {
        vol.Required(CONF_ADDRESS): str,
        vol.Optional(CONF_NAME, default="SP642E"): str,
    }
)


def model_id_from_manufacturer_data(info: BluetoothServiceInfoBleak) -> int | None:
    """BanlanX puts [model_id, 0x10] in manufacturer data 20563."""
    for mfr_id, data in (info.manufacturer_data or {}).items():
        if mfr_id != BANLANX_MANUFACTURER_ID:
            continue
        if len(data) >= 2 and data[1] == BANLANX_DATA_TAG:
            return int(data[0])
    return None


class Sp642eConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discover the controller over BLE, or add it by MAC address."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str = "SP642E"
        self._model_id: int | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a bluetooth discovery."""
        address = discovery_info.address.upper()
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        self._address = address
        self._name = discovery_info.name or "SP642E"
        self._model_id = model_id_from_manufacturer_data(discovery_info)
        self.context["title_placeholders"] = {"name": self._name, "address": address}
        return await self.async_step_confirm()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual entry by MAC address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            if not ADDRESS_RE.match(address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                self._address = address
                self._name = user_input.get(CONF_NAME) or "SP642E"
                info = bluetooth.async_last_service_info(
                    self.hass, address, connectable=True
                )
                if info is not None:
                    self._model_id = model_id_from_manufacturer_data(info)
                return self._async_create_entry()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER, errors=errors
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered controller before creating the entry."""
        if user_input is not None:
            return self._async_create_entry()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": self._name,
                "address": self._address or "",
            },
        )

    def _async_create_entry(self) -> ConfigFlowResult:
        assert self._address is not None
        return self.async_create_entry(
            title=self._name,
            data={
                CONF_ADDRESS: self._address,
                CONF_NAME: self._name,
                CONF_MODEL_ID: self._model_id,
                CONF_MANUFACTURER: MANUFACTURER,
            },
            options={
                CONF_MIN_KELVIN: DEFAULT_MIN_KELVIN,
                CONF_MAX_KELVIN: DEFAULT_MAX_KELVIN,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: Any,
    ) -> OptionsFlow:  # pragma: no cover - wiring only
        return Sp642eOptionsFlow()


class Sp642eOptionsFlow(OptionsFlow):
    """Tune the colour temperature range the strip is wired for."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MIN_KELVIN,
                        default=options.get(CONF_MIN_KELVIN, DEFAULT_MIN_KELVIN),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1000, max=6500)),
                    vol.Required(
                        CONF_MAX_KELVIN,
                        default=options.get(CONF_MAX_KELVIN, DEFAULT_MAX_KELVIN),
                    ): vol.All(vol.Coerce(int), vol.Range(min=2000, max=10000)),
                }
            ),
        )
