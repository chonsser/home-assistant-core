"""The Tailcat integration."""

import asyncio
from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError

from .const import CONF_PUBLIC_KEY, CONF_SERVER_KEY, CONF_TUNNEL_PORT, DOMAIN
from .server import TailcatServer, TailcatServerStartError
from .util import async_find_binary

_LOGGER = logging.getLogger(__name__)

type TailcatConfigEntry = ConfigEntry[TailcatData]


@dataclass
class TailcatData:
    """Runtime data of a Tailcat config entry."""

    binary: str
    server: TailcatServer
    listener: asyncio.Server


async def _async_start_listener(
    hass: HomeAssistant, entry: TailcatConfigEntry
) -> asyncio.Server:
    """Start the loopback listener the tailcat server tunnels to."""
    port: int = entry.data[CONF_TUNNEL_PORT]
    try:
        return await hass.http.async_start_tunnel_listener(port)
    except HomeAssistantError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="http_not_running"
        ) from err
    except OSError:
        pass
    listener = await hass.http.async_start_tunnel_listener(0)
    new_port: int = listener.sockets[0].getsockname()[1]
    _LOGGER.warning(
        "Tunnel port %s is in use, switched to port %s; paired devices"
        " must be paired again",
        port,
        new_port,
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_TUNNEL_PORT: new_port}
    )
    return listener


async def async_setup_entry(hass: HomeAssistant, entry: TailcatConfigEntry) -> None:
    """Set up Tailcat from a config entry."""
    if (binary := await async_find_binary(hass)) is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="binary_not_found"
        )

    listener = await _async_start_listener(hass, entry)

    def _allowed_keys() -> set[str]:
        """Return the public keys of all paired devices."""
        return {
            subentry.data[CONF_PUBLIC_KEY] for subentry in entry.subentries.values()
        }

    server = TailcatServer(
        hass,
        binary,
        entry.data[CONF_SERVER_KEY],
        entry.data[CONF_TUNNEL_PORT],
        _allowed_keys,
    )
    try:
        await server.async_start()
    except TailcatServerStartError as err:
        listener.close()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="server_not_started"
        ) from err

    entry.runtime_data = TailcatData(binary=binary, server=server, listener=listener)

    async def _async_stop(event: Event) -> None:
        """Stop the server when Home Assistant stops."""
        await server.async_stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))


async def _async_update_listener(
    hass: HomeAssistant, entry: TailcatConfigEntry
) -> None:
    """Apply paired or removed devices."""
    try:
        await entry.runtime_data.server.async_update_allowed_keys()
    except TailcatServerStartError as err:
        _LOGGER.warning("Tailcat server did not restart: %s", err)


async def async_unload_entry(hass: HomeAssistant, entry: TailcatConfigEntry) -> None:
    """Unload a config entry."""
    await entry.runtime_data.server.async_stop()
    entry.runtime_data.listener.close()
