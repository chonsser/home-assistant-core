"""Common fixtures for the Tailcat tests."""

import asyncio
from collections.abc import Generator
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
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
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant

BINARY = "/usr/local/bin/tailcat"
ADDRESS = "tcAbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_"
TUNNEL_PORT = 18123
SERVER_KEY: dict[str, Any] = {
    "Private": "privkey:" + "11" * 32,
    "Public": {
        "ServerPublic": "nodekey:" + "22" * 32,
        "ServerDiscoPublic": "discokey:" + "33" * 32,
        "PresharedKey": "psk:" + "44" * 32,
        "RegionID": 303,
    },
}
CLIENT_PRIVATE_KEY = "privkey:" + "55" * 32
CLIENT_PUBLIC_KEY = "nodekey:" + "66" * 32
PAIRED_PUBLIC_KEY = "nodekey:" + "77" * 32


class FakeProcess:
    """A fake asyncio subprocess."""

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        *,
        exit_on_terminate: bool = True,
    ) -> None:
        """Initialize the process, feeding its output streams."""
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stderr.feed_data(stderr)
        self.returncode: int | None = None
        self._exit_on_terminate = exit_on_terminate
        self._exited = asyncio.Event()
        self.terminated = False
        self.killed = False

    def exit(self, returncode: int) -> None:
        """Let the process exit."""
        if self.returncode is not None:
            return
        self.returncode = returncode
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exited.set()

    async def wait(self) -> int:
        """Wait for the process to exit."""
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        """Terminate the process."""
        self.terminated = True
        if self._exit_on_terminate:
            self.exit(-15)

    def kill(self) -> None:
        """Kill the process."""
        self.killed = True
        self.exit(-9)


@dataclass
class ServeMock:
    """Records tailcat serve invocations."""

    processes: list[FakeProcess] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    key_files: list[dict[str, Any]] = field(default_factory=list)
    stdout: bytes = (
        b"not json\n"
        b'{"other": 1}\n'
        + json.dumps({"listenAddr": ADDRESS}).encode()
        + b"\nmore output\n"
    )
    stderr: bytes = f"# Server listening: {ADDRESS}\n".encode()
    exit_on_terminate: bool = True
    exit_immediately: bool = False

    @property
    def allow(self) -> str:
        """Return the --allow argument of the latest invocation."""
        return next(
            arg.removeprefix("--allow=")
            for arg in self.calls[-1]
            if arg.startswith("--allow=")
        )

    @property
    def process(self) -> FakeProcess:
        """Return the latest process."""
        return self.processes[-1]

    async def __call__(self, *args: str, **kwargs: Any) -> FakeProcess:
        """Start a fake tailcat serve process."""
        self.calls.append(args)
        key_file = Path(next(a for a in args if a.startswith("--key=")).split("=")[1])
        self.key_files.append(json.loads(key_file.read_text(encoding="utf-8")))
        process = FakeProcess(
            b"" if self.exit_immediately else self.stdout,
            self.stderr,
            exit_on_terminate=self.exit_on_terminate,
        )
        if self.exit_immediately:
            process.exit(1)
        self.processes.append(process)
        return process


@dataclass
class GenkeyMock:
    """Records tailcat genkey invocations and writes key files."""

    calls: list[tuple[str, ...]] = field(default_factory=list)
    returncode: int = 0
    write_file: bool = True
    hang: bool = False
    server_key: dict[str, Any] = field(default_factory=lambda: SERVER_KEY)
    client_private_key: str = CLIENT_PRIVATE_KEY
    client_public_key: str = CLIENT_PUBLIC_KEY

    async def __call__(self, *args: str, **kwargs: Any) -> MagicMock:
        """Run a fake tailcat genkey."""
        self.calls.append(args)
        key_file = Path(next(a for a in args if a.startswith("--key=")).split("=")[1])
        client = "--client" in args
        if self.write_file:
            content: dict[str, Any] = (
                {"Private": self.client_private_key, "Public": {}}
                if client
                else self.server_key
            )
            key_file.write_text(json.dumps(content), encoding="utf-8")
        stdout = (self.client_public_key if client else ADDRESS).encode() + b"\n"
        process = MagicMock()
        process.returncode = self.returncode
        if self.hang:
            process.communicate = AsyncMock(side_effect=asyncio.Event().wait)
        else:
            process.communicate = AsyncMock(return_value=(stdout, b""))
        process.wait = AsyncMock(return_value=self.returncode)
        return process


@pytest.fixture
def mock_which() -> Generator[MagicMock]:
    """Mock finding the tailcat binary."""
    with patch(
        "homeassistant.components.tailcat.util.shutil.which", return_value=BINARY
    ) as mock:
        yield mock


@pytest.fixture
def mock_genkey() -> Generator[GenkeyMock]:
    """Mock tailcat genkey."""
    genkey = GenkeyMock()
    with patch(
        "homeassistant.components.tailcat.util.create_subprocess_exec",
        new=genkey,
    ):
        yield genkey


@pytest.fixture
def mock_serve() -> Generator[ServeMock]:
    """Mock tailcat serve."""
    serve = ServeMock()
    with patch(
        "homeassistant.components.tailcat.server.create_subprocess_exec",
        new=serve,
    ):
        yield serve


@pytest.fixture
def mock_listener() -> Generator[AsyncMock]:
    """Mock the HTTP tunnel listener."""
    listener = MagicMock(spec=asyncio.Server)
    listener.sockets = [MagicMock()]
    listener.sockets[0].getsockname.return_value = ("127.0.0.1", TUNNEL_PORT)
    with patch(
        "homeassistant.components.http.HomeAssistantHTTP.async_start_tunnel_listener",
        autospec=True,
        return_value=listener,
    ) as mock:
        yield mock


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Override async_setup_entry."""
    with patch(
        "homeassistant.components.tailcat.async_setup_entry", return_value=None
    ) as mock:
        yield mock


@pytest.fixture
def subentries_data() -> list[ConfigSubentryData]:
    """Return the paired devices of the config entry."""
    return []


@pytest.fixture
def mock_config_entry(subentries_data: list[ConfigSubentryData]) -> MockConfigEntry:
    """Return a Tailcat config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tailcat",
        data={CONF_SERVER_KEY: SERVER_KEY, CONF_TUNNEL_PORT: TUNNEL_PORT},
        subentries_data=subentries_data,
    )


PAIRED_DEVICE = ConfigSubentryData(
    data={CONF_NAME: "Phone", CONF_PUBLIC_KEY: PAIRED_PUBLIC_KEY},
    subentry_type=SUBENTRY_TYPE_DEVICE,
    title="Phone",
    unique_id=PAIRED_PUBLIC_KEY,
)


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_which: MagicMock,
    mock_serve: ServeMock,
    mock_listener: AsyncMock,
) -> MockConfigEntry:
    """Set up the Tailcat integration."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
