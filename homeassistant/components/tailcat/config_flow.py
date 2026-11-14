"""Config flow for the Tailcat integration."""

import logging
from typing import Any, override

import probatio

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
)

from . import TailcatConfigEntry
from .const import (
    CONF_PUBLIC_KEY,
    CONF_SERVER_KEY,
    CONF_TUNNEL_PORT,
    DOMAIN,
    SUBENTRY_TYPE_DEVICE,
)
from .util import (
    TailcatKeyError,
    async_find_binary,
    async_generate_client_key,
    async_generate_server_key,
    build_pairing_url,
    find_free_port,
)

_LOGGER = logging.getLogger(__name__)

STEP_DEVICE_DATA_SCHEMA = probatio.Schema(
    {
        # The name identifies the paired device, which has no other name
        # pylint: disable-next=home-assistant-config-flow-name-field
        probatio.Required(CONF_NAME): str,
    }
)


class TailcatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tailcat."""

    VERSION = 1

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the setup and generate the server key."""
        if (binary := await async_find_binary(self.hass)) is None:
            return self.async_abort(reason="binary_not_found")
        if user_input is None:
            return self.async_show_form(step_id="user")

        try:
            server_key = await async_generate_server_key(self.hass, binary)
        except TailcatKeyError:
            _LOGGER.exception("Generating the tailcat server key failed")
            return self.async_abort(reason="key_generation_failed")
        port = await self.hass.async_add_executor_job(find_free_port)
        # The key is stored in the entry so backups keep the address stable
        return self.async_create_entry(
            title="Tailcat",
            data={CONF_SERVER_KEY: server_key, CONF_TUNNEL_PORT: port},
        )

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: TailcatConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {SUBENTRY_TYPE_DEVICE: DeviceSubentryFlowHandler}


class DeviceSubentryFlowHandler(ConfigSubentryFlow):
    """Pair a device with the Tailcat server."""

    _name: str
    _pairing_url: str
    _public_key: str

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask for the device name and generate its key."""
        entry: TailcatConfigEntry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_DEVICE_DATA_SCHEMA
            )

        data = entry.runtime_data
        if (address := data.server.address) is None:
            return self.async_abort(reason="server_not_running")
        try:
            client_key = await async_generate_client_key(self.hass, data.binary)
        except TailcatKeyError:
            _LOGGER.exception("Generating a tailcat client key failed")
            return self.async_abort(reason="key_generation_failed")

        # Allowed only once submitted: the restart this needs would cut off a
        # client showing this flow through the tunnel
        self._public_key = client_key.public_key
        self._name = user_input[CONF_NAME]
        self._pairing_url = build_pairing_url(
            address,
            client_key.private_key,
            data.server.port,
            self.hass.config.location_name,
        )
        return await self.async_step_pair()

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the pairing code and allow the device once submitted."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={CONF_NAME: self._name, CONF_PUBLIC_KEY: self._public_key},
                unique_id=self._public_key,
            )

        return self.async_show_form(
            step_id="pair",
            data_schema=probatio.Schema(
                {
                    probatio.Optional("qr_code"): QrCodeSelector(
                        config=QrCodeSelectorConfig(
                            data=self._pairing_url,
                            scale=6,
                            error_correction_level=QrErrorCorrectionLevel.MEDIUM,
                        )
                    )
                }
            ),
            description_placeholders={
                "name": self._name,
                "pairing_url": self._pairing_url,
            },
        )
