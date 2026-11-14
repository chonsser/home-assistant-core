"""Tests for the Tailcat config flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.tailcat.const import (
    CONF_PUBLIC_KEY,
    CONF_SERVER_KEY,
    CONF_TUNNEL_PORT,
    DOMAIN,
    SUBENTRY_TYPE_DEVICE,
)
from homeassistant.config_entries import SOURCE_USER, ConfigSubentry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import (
    ADDRESS,
    BINARY,
    CLIENT_PRIVATE_KEY,
    CLIENT_PUBLIC_KEY,
    PAIRED_DEVICE,
    PAIRED_PUBLIC_KEY,
    SERVER_KEY,
    TUNNEL_PORT,
    GenkeyMock,
    ServeMock,
)

PAIRING_URL = (
    "homeassistant://tailcat/pair"
    f"?address={ADDRESS}"
    f"&key=privkey%3A{'55' * 32}"
    f"&port={TUNNEL_PORT}"
    "&name=My%20Home%20%26%20Garden%2F%C3%A9"
)


@pytest.mark.usefixtures("mock_which", "mock_setup_entry")
async def test_user_flow(hass: HomeAssistant, mock_genkey: GenkeyMock) -> None:
    """Test the full user flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "homeassistant.components.tailcat.config_flow.find_free_port",
        return_value=TUNNEL_PORT,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Tailcat"
    assert result["data"] == {
        CONF_SERVER_KEY: SERVER_KEY,
        CONF_TUNNEL_PORT: TUNNEL_PORT,
    }
    assert len(mock_genkey.calls) == 1
    assert mock_genkey.calls[0][:2] == (BINARY, "genkey")
    assert mock_genkey.calls[0][3:] == ("--fixed-region",)


@pytest.mark.usefixtures("socket_enabled")
async def test_user_flow_free_port(hass: HomeAssistant) -> None:
    """Test the stored tunnel port is a usable port."""
    with (
        patch(
            "homeassistant.components.tailcat.util.shutil.which", return_value=BINARY
        ),
        patch(
            "homeassistant.components.tailcat.util.create_subprocess_exec",
            new=GenkeyMock(),
        ),
        patch("homeassistant.components.tailcat.async_setup_entry", return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert 0 < result["data"][CONF_TUNNEL_PORT] < 65536


async def test_user_flow_binary_not_found(hass: HomeAssistant) -> None:
    """Test the flow aborts without the tailcat binary."""
    with patch("homeassistant.components.tailcat.util.shutil.which", return_value=None):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "binary_not_found"


@pytest.mark.parametrize(
    "genkey",
    [
        pytest.param(GenkeyMock(returncode=1), id="exit_code"),
        pytest.param(GenkeyMock(write_file=False), id="no_key_file"),
        pytest.param(GenkeyMock(hang=True), id="timeout"),
        pytest.param(GenkeyMock(server_key={"Private": 1}), id="invalid_key"),
    ],
)
@pytest.mark.usefixtures("mock_which", "mock_setup_entry")
async def test_user_flow_genkey_failed(hass: HomeAssistant, genkey: GenkeyMock) -> None:
    """Test the flow aborts when generating the key fails."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with (
        patch(
            "homeassistant.components.tailcat.util.create_subprocess_exec",
            new=genkey,
        ),
        patch("homeassistant.components.tailcat.util._GENKEY_TIMEOUT", 0),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "key_generation_failed"


@pytest.mark.usefixtures("mock_which")
async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test only one Tailcat entry can be set up."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def _start_pairing(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Start pairing a device and enter its name."""
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_DEVICE),
        context={"source": SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Tablet"}
    )


@pytest.mark.parametrize("subentries_data", [[PAIRED_DEVICE]])
@pytest.mark.usefixtures("mock_genkey")
async def test_pair_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_serve: ServeMock
) -> None:
    """Test pairing a device."""
    hass.config.location_name = "My Home & Garden/é"
    assert mock_serve.allow == PAIRED_PUBLIC_KEY

    result = await _start_pairing(hass, init_integration)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    assert result["description_placeholders"] == {
        "name": "Tablet",
        "pairing_url": PAIRING_URL,
    }
    qr_selector = result["data_schema"].schema["qr_code"]
    assert qr_selector.config["data"] == PAIRING_URL
    # The device may connect while the code is shown
    assert len(mock_serve.processes) == 2
    assert mock_serve.processes[0].terminated
    assert mock_serve.allow == ",".join(sorted([CLIENT_PUBLIC_KEY, PAIRED_PUBLIC_KEY]))

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Tablet"
    assert result["data"] == {CONF_NAME: "Tablet", CONF_PUBLIC_KEY: CLIENT_PUBLIC_KEY}
    subentry = next(
        subentry
        for subentry in init_integration.subentries.values()
        if subentry.unique_id == CLIENT_PUBLIC_KEY
    )
    assert isinstance(subentry, ConfigSubentry)
    assert dict(subentry.data) == {
        CONF_NAME: "Tablet",
        CONF_PUBLIC_KEY: CLIENT_PUBLIC_KEY,
    }
    assert CLIENT_PRIVATE_KEY not in str(init_integration.as_dict())
    # The paired device stays allowed without another restart
    assert len(mock_serve.processes) == 2
    assert init_integration.runtime_data.pending_keys == set()


@pytest.mark.usefixtures("mock_genkey")
async def test_pair_device_aborted(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_serve: ServeMock
) -> None:
    """Test an abandoned pairing stops allowing the device."""
    result = await _start_pairing(hass, init_integration)
    assert result["step_id"] == "pair"
    assert mock_serve.allow == CLIENT_PUBLIC_KEY

    hass.config_entries.subentries.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert mock_serve.allow == "none"
    assert len(mock_serve.processes) == 3
    assert init_integration.subentries == {}
    assert init_integration.runtime_data.pending_keys == set()


@pytest.mark.usefixtures("mock_genkey")
async def test_pair_device_aborted_after_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_serve: ServeMock
) -> None:
    """Test aborting a pairing after the entry was unloaded."""
    result = await _start_pairing(hass, init_integration)
    await hass.config_entries.async_unload(init_integration.entry_id)
    processes = len(mock_serve.processes)

    hass.config_entries.subentries.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert len(mock_serve.processes) == processes


async def test_pair_device_not_loaded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test pairing needs a running server."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry.entry_id, SUBENTRY_TYPE_DEVICE),
        context={"source": SOURCE_USER},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"


@pytest.mark.parametrize(
    "genkey",
    [
        pytest.param(GenkeyMock(returncode=1), id="exit_code"),
        pytest.param(GenkeyMock(client_private_key="invalid"), id="private_key"),
        pytest.param(GenkeyMock(client_public_key="invalid"), id="public_key"),
    ],
)
async def test_pair_device_genkey_failed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_serve: ServeMock,
    genkey: GenkeyMock,
) -> None:
    """Test pairing aborts when generating the client key fails."""
    with patch(
        "homeassistant.components.tailcat.util.create_subprocess_exec",
        new=genkey,
    ):
        result = await _start_pairing(hass, init_integration)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "key_generation_failed"
    assert len(mock_serve.processes) == 1


@pytest.mark.usefixtures("mock_genkey")
async def test_pair_device_server_restart_failed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_serve: ServeMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test pairing aborts when the server does not come back."""
    mock_serve.exit_immediately = True
    with patch("homeassistant.components.tailcat.server._RESTART_DELAY_INITIAL", 3600):
        result = await _start_pairing(hass, init_integration)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "server_not_running"
    assert init_integration.runtime_data.pending_keys == set()
    assert ADDRESS not in caplog.text
    await hass.config_entries.async_unload(init_integration.entry_id)


@pytest.mark.usefixtures("mock_genkey")
async def test_pair_device_without_address(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Test pairing aborts while the server has no address."""
    with patch.object(
        init_integration.runtime_data.server,
        "async_update_allowed_keys",
        AsyncMock(),
    ):
        init_integration.runtime_data.server.address = None
        result = await _start_pairing(hass, init_integration)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "server_not_running"


@pytest.mark.usefixtures("mock_which")
async def test_supported_subentry_types(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_setup_entry: MagicMock
) -> None:
    """Test the device subentry type is offered."""
    mock_config_entry.add_to_hass(hass)
    assert mock_config_entry.supported_subentry_types == {
        SUBENTRY_TYPE_DEVICE: {"supports_reconfigure": False}
    }
