"""Tests for setting up the Tailcat integration and its server process."""

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.tailcat.const import CONF_TUNNEL_PORT
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .conftest import (
    ADDRESS,
    BINARY,
    PAIRED_DEVICE,
    PAIRED_PUBLIC_KEY,
    SERVER_KEY,
    TUNNEL_PORT,
    ServeMock,
)


@pytest.fixture(autouse=True)
def fast_restart() -> None:
    """Restart crashed servers without delay."""
    with (
        patch("homeassistant.components.tailcat.server._RESTART_DELAY_INITIAL", 0),
        patch("homeassistant.components.tailcat.server._TERMINATE_TIMEOUT", 0.01),
        patch("homeassistant.components.tailcat.server._START_TIMEOUT", 0.05),
    ):
        yield


@pytest.mark.parametrize("subentries_data", [[PAIRED_DEVICE]])
async def test_setup_and_unload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_serve: ServeMock,
    mock_listener: AsyncMock,
) -> None:
    """Test the server is started for the paired devices and stopped again."""
    assert init_integration.state is ConfigEntryState.LOADED
    mock_listener.assert_awaited_once_with(hass.http, TUNNEL_PORT)

    args = mock_serve.calls[0]
    key_arg = args[2]
    assert args[:2] == (BINARY, "--json")
    assert key_arg.startswith("--key=")
    assert args[3:] == ("serve", f"--allow={PAIRED_PUBLIC_KEY}", str(TUNNEL_PORT))
    assert mock_serve.key_files == [SERVER_KEY]
    key_file = Path(key_arg.removeprefix("--key="))
    assert key_file.stat().st_mode & 0o777 == 0o600

    server = init_integration.runtime_data.server
    assert server.address == ADDRESS
    assert server.is_running

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert init_integration.state is ConfigEntryState.NOT_LOADED
    assert mock_serve.process.terminated
    assert not key_file.parent.exists()
    mock_listener.return_value.close.assert_called_once()


async def test_stop_with_home_assistant(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_serve: ServeMock
) -> None:
    """Test the server stops when Home Assistant stops."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert mock_serve.process.terminated
    assert not init_integration.runtime_data.server.is_running


async def test_kill_when_not_terminating(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_serve: ServeMock,
) -> None:
    """Test a server that ignores termination is killed."""
    mock_serve.process._exit_on_terminate = False
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    assert mock_serve.process.killed


@pytest.mark.usefixtures("mock_serve", "mock_listener")
async def test_setup_binary_not_found(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Test setup is retried without the binary."""
    mock_config_entry.add_to_hass(hass)
    with patch("homeassistant.components.tailcat.util.shutil.which", return_value=None):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.usefixtures("mock_which", "mock_serve")
async def test_setup_http_not_running(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_listener: AsyncMock
) -> None:
    """Test setup is retried until the HTTP server runs."""
    mock_listener.side_effect = HomeAssistantError("The HTTP server is not running")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.usefixtures("mock_which")
async def test_setup_port_in_use(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_listener: AsyncMock,
    mock_serve: ServeMock,
) -> None:
    """Test a busy tunnel port is replaced by a free one."""
    listener = mock_listener.return_value
    listener.sockets[0].getsockname.return_value = ("127.0.0.1", 23456)
    mock_listener.side_effect = [OSError("in use"), listener]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert mock_listener.await_args_list[1].args == (hass.http, 0)
    assert mock_config_entry.data[CONF_TUNNEL_PORT] == 23456
    assert mock_serve.calls[0][-1] == "23456"
    # Updating the entry data does not restart the server
    await hass.async_block_till_done()
    assert len(mock_serve.processes) == 1


@pytest.mark.parametrize(
    ("stdout", "exit_immediately"),
    [
        pytest.param(b"", False, id="timeout"),
        pytest.param(b"", True, id="exited"),
    ],
)
@pytest.mark.usefixtures("mock_which")
async def test_setup_server_not_started(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_listener: AsyncMock,
    mock_serve: ServeMock,
    stdout: bytes,
    exit_immediately: bool,
) -> None:
    """Test setup is retried when the server does not start."""
    mock_serve.stdout = stdout
    mock_serve.exit_immediately = exit_immediately
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_serve.process.returncode is not None
    mock_listener.return_value.close.assert_called_once()
    key_file = Path(mock_serve.calls[0][2].removeprefix("--key="))
    assert not key_file.parent.exists()


@pytest.mark.parametrize("subentries_data", [[PAIRED_DEVICE]])
async def test_remove_device_revokes_it(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_serve: ServeMock
) -> None:
    """Test removing a paired device restarts the server without it."""
    assert mock_serve.allow == PAIRED_PUBLIC_KEY
    subentry_id = next(iter(init_integration.subentries))

    assert hass.config_entries.async_remove_subentry(init_integration, subentry_id)
    await hass.async_block_till_done()

    assert len(mock_serve.processes) == 2
    assert mock_serve.processes[0].terminated
    assert mock_serve.allow == "none"
    assert init_integration.runtime_data.server.address == ADDRESS


async def test_unchanged_allowlist_does_not_restart(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_serve: ServeMock
) -> None:
    """Test entry updates without device changes keep the server running."""
    hass.config_entries.async_update_entry(init_integration, title="Renamed")
    await hass.async_block_till_done()
    assert len(mock_serve.processes) == 1


@pytest.mark.parametrize("subentries_data", [[PAIRED_DEVICE]])
async def test_failed_restart_on_device_removal(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_serve: ServeMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a failed restart after removing a device is retried."""
    mock_serve.exit_immediately = True
    subentry_id = next(iter(init_integration.subentries))
    with patch("homeassistant.components.tailcat.server._RESTART_DELAY_INITIAL", 3600):
        hass.config_entries.async_remove_subentry(init_integration, subentry_id)
        await hass.async_block_till_done()
    assert "Tailcat server did not restart" in caplog.text
    assert not init_integration.runtime_data.server.is_running
    assert await hass.config_entries.async_unload(init_integration.entry_id)


async def test_restart_after_crash(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_serve: ServeMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the server is restarted when it exits unexpectedly."""
    server = init_integration.runtime_data.server
    mock_serve.process.exit(2)
    await hass.async_block_till_done()
    for _ in range(10):
        await asyncio.sleep(0)
    await hass.async_block_till_done()

    assert "Tailcat server exited unexpectedly with code 2" in caplog.text
    assert ADDRESS not in caplog.text
    assert len(mock_serve.processes) == 2
    assert server.is_running
    assert server.address == ADDRESS


async def test_restart_retries_with_backoff(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_serve: ServeMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test failing restarts are retried until the server runs again."""
    server = init_integration.runtime_data.server
    mock_serve.exit_immediately = True
    with patch("homeassistant.components.tailcat.server._STABLE_RUN_TIME", 0):
        mock_serve.process.exit(2)
        for _ in range(20):
            await asyncio.sleep(0)
            if len(mock_serve.processes) >= 3:
                break
        assert "Restarting the tailcat server failed" in caplog.text
        assert not server.is_running

        mock_serve.exit_immediately = False
        for _ in range(50):
            await asyncio.sleep(0)
            if server.is_running:
                break
    assert server.is_running
    assert server.address == ADDRESS


async def test_watchdog_skips_restart_after_allowlist_update(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_serve: ServeMock,
) -> None:
    """Test a server restarted by an allowlist change is not restarted again."""
    server = init_integration.runtime_data.server
    mock_serve.exit_immediately = True
    with patch("homeassistant.components.tailcat.server._RESTART_DELAY_INITIAL", 0.05):
        mock_serve.process.exit(2)
        await hass.async_block_till_done()
        # Restarted by a pairing while the watchdog waits for its delay
        mock_serve.exit_immediately = False
        init_integration.runtime_data.pending_keys.add(PAIRED_PUBLIC_KEY)
        await server.async_update_allowed_keys()
        processes = len(mock_serve.processes)
        await asyncio.sleep(0.1)
    assert len(mock_serve.processes) == processes
    assert server.is_running


async def test_update_after_stop(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_serve: ServeMock,
) -> None:
    """Test allowlist changes after stopping do not start the server."""
    server = init_integration.runtime_data.server
    await server.async_stop()
    init_integration.runtime_data.pending_keys.add(PAIRED_PUBLIC_KEY)
    await server.async_update_allowed_keys()
    assert server.allowed_keys == {PAIRED_PUBLIC_KEY}
    assert len(mock_serve.processes) == 1
    assert not server.is_running


@pytest.mark.usefixtures("mock_which", "mock_listener")
async def test_redact_output(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_serve: ServeMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test the address and private keys in the server output are not logged."""
    caplog.set_level(logging.DEBUG)
    mock_serve.stderr = f"{ADDRESS} loaded privkey:abcdef0123456789\n".encode()
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert "tc**REDACTED** loaded privkey:**REDACTED**" in caplog.text
    assert "abcdef0123456789" not in caplog.text
    assert ADDRESS not in caplog.text
