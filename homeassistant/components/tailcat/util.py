"""Helpers to run the tailcat binary for key generation."""

import asyncio
from asyncio.subprocess import create_subprocess_exec
from contextlib import suppress
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import socket
import tempfile
from typing import Any
from urllib.parse import quote, urlencode

from homeassistant.core import HomeAssistant
from homeassistant.util.json import json_loads_object

from .const import BINARY_NAME

_LOGGER = logging.getLogger(__name__)

_GENKEY_TIMEOUT = 30
_KEY_FILE_NAME = "key.json"

PAIRING_URL = "homeassistant://tailcat/pair"


class TailcatKeyError(Exception):
    """Raised when tailcat fails to generate a key."""


@dataclass(frozen=True, slots=True)
class ClientKey:
    """A client identity key generated for a device."""

    private_key: str
    public_key: str


async def async_find_binary(hass: HomeAssistant) -> str | None:
    """Return the path of the tailcat binary, if installed."""
    return await hass.async_add_executor_job(shutil.which, BINARY_NAME)


def find_free_port() -> int:
    """Return a currently free TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _read_key_file(path: Path) -> dict[str, Any]:
    """Read a key file written by tailcat."""
    return json_loads_object(path.read_bytes())


async def _async_genkey(
    hass: HomeAssistant, binary: str, *args: str
) -> tuple[dict[str, Any], str]:
    """Run tailcat genkey and return the key file content and stdout.

    The key file is written to a private temporary directory, which is removed
    afterwards, so no key is left on disk.
    """
    key_dir = Path(await hass.async_add_executor_job(tempfile.mkdtemp))
    key_file = key_dir / _KEY_FILE_NAME
    try:
        process = await create_subprocess_exec(
            binary,
            "genkey",
            f"--key={key_file}",
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(_GENKEY_TIMEOUT):
                stdout, _ = await process.communicate()
        except TimeoutError as err:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise TailcatKeyError("tailcat genkey timed out") from err
        if process.returncode != 0:
            # stderr may contain the address of a server key, so not logged
            _LOGGER.debug("tailcat genkey failed with exit code %s", process.returncode)
            raise TailcatKeyError(
                f"tailcat genkey failed with exit code {process.returncode}"
            )
        try:
            key = await hass.async_add_executor_job(_read_key_file, key_file)
        except (OSError, ValueError) as err:
            raise TailcatKeyError("tailcat genkey wrote no valid key file") from err
    finally:
        await hass.async_add_executor_job(shutil.rmtree, key_dir, True)
    return key, stdout.decode(errors="replace").strip()


async def async_generate_server_key(hass: HomeAssistant, binary: str) -> dict[str, Any]:
    """Generate a server key with a fixed relay region.

    The fixed region keeps the tailcat address stable across restarts.
    """
    key, _ = await _async_genkey(hass, binary, "--fixed-region")
    if not isinstance(key.get("Private"), str) or not isinstance(
        key.get("Public"), dict
    ):
        raise TailcatKeyError("tailcat genkey wrote an unexpected server key")
    return key


async def async_generate_client_key(hass: HomeAssistant, binary: str) -> ClientKey:
    """Generate a client identity key for a device."""
    key, public_key = await _async_genkey(hass, binary, "--client")
    private_key = key.get("Private")
    if (
        not isinstance(private_key, str)
        or not private_key.startswith("privkey:")
        or not public_key.startswith("nodekey:")
    ):
        raise TailcatKeyError("tailcat genkey wrote an unexpected client key")
    return ClientKey(private_key=private_key, public_key=public_key)


def build_pairing_url(address: str, private_key: str, port: int, name: str) -> str:
    """Build the URL a device scans to pair with this server."""
    query = urlencode(
        {"address": address, "key": private_key, "port": port, "name": name},
        quote_via=quote,
        safe="",
    )
    return f"{PAIRING_URL}?{query}"
