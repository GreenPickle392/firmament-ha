"""Diagnostics for the SP642E CCT Controller integration."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: Any
) -> dict[str, Any]:
    """Redact the MAC beyond its first octet: it identifies the radio."""
    device = config_entry.runtime_data
    data = device.diagnostics()
    parts = str(data.get("address", "")).split(":")
    data["address"] = f"{parts[0]}:**:**:**:**:**" if len(parts) == 6 else "*"
    data["config_entry"] = {
        "title": config_entry.title,
        "version": config_entry.version,
        "options": dict(config_entry.options),
    }
    return data
