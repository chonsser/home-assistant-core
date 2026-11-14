"""Diagnostics support for Tailcat."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import TailcatConfigEntry
from .const import CONF_PUBLIC_KEY, CONF_SERVER_KEY

TO_REDACT = {CONF_SERVER_KEY, CONF_PUBLIC_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TailcatConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    server = entry.runtime_data.server
    return {
        "entry_data": async_redact_data(entry.data, TO_REDACT),
        "devices": [
            async_redact_data(subentry.data, TO_REDACT)
            for subentry in entry.subentries.values()
        ],
        "server": {
            "running": server.is_running,
            "has_address": server.address is not None,
            "port": server.port,
            "allowed_devices": len(server.allowed_keys),
        },
    }
