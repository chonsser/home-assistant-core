"""Manage the tailcat server process."""

import asyncio
from asyncio.subprocess import create_subprocess_exec
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util.json import json_loads

_LOGGER = logging.getLogger(__name__)

_START_TIMEOUT = 30
_TERMINATE_TIMEOUT = 5
_RESTART_DELAY_INITIAL = 1
_RESTART_DELAY_MAX = 60
# A process running at least this long resets the restart backoff
_STABLE_RUN_TIME = 60
_LOG_BUFFER_SIZE = 50
_KEY_FILE_NAME = "server.private.json"

_ADDRESS_PATTERN = re.compile(r"tc[A-Za-z0-9_-]{20,}")
_PRIVATE_KEY_PATTERN = re.compile(r"privkey:[0-9a-fA-F]+")


def redact_output(line: str) -> str:
    """Redact the tailcat address and private keys from process output."""
    line = _ADDRESS_PATTERN.sub("tc**REDACTED**", line)
    return _PRIVATE_KEY_PATTERN.sub("privkey:**REDACTED**", line)


class TailcatServerStartError(Exception):
    """Raised when the tailcat server does not start."""


def _write_key_file(server_key: dict[str, Any]) -> Path:
    """Write the server key to a private temporary file."""
    key_dir = Path(tempfile.mkdtemp(prefix="tailcat_"))
    key_file = key_dir / _KEY_FILE_NAME
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(server_key, file)
    return key_file


class TailcatServer:
    """A tailcat server process that tunnels a loopback port."""

    def __init__(
        self,
        hass: HomeAssistant,
        binary: str,
        server_key: dict[str, Any],
        port: int,
        get_allowed_keys: Callable[[], Iterable[str]],
    ) -> None:
        """Initialize the server."""
        self._hass = hass
        self._binary = binary
        self._server_key = server_key
        self._port = port
        self._get_allowed_keys = get_allowed_keys
        self._allowed_keys = frozenset(get_allowed_keys())
        self._key_file: Path | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._started_at = 0.0
        self._lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._log_buffer: deque[str] = deque(maxlen=_LOG_BUFFER_SIZE)
        self.address: str | None = None

    @property
    def port(self) -> int:
        """Return the tunnel port."""
        return self._port

    @property
    def allowed_keys(self) -> frozenset[str]:
        """Return the public keys of the clients allowed to connect."""
        return self._allowed_keys

    @property
    def is_running(self) -> bool:
        """Return if the server process is running."""
        return self._process is not None and self._process.returncode is None

    async def async_start(self) -> None:
        """Start the server and keep it running."""
        self._key_file = await self._hass.async_add_executor_job(
            _write_key_file, self._server_key
        )
        async with self._lock:
            try:
                await self._async_spawn()
            except TailcatServerStartError:
                await self._async_remove_key_file()
                raise
        self._watchdog_task = self._hass.async_create_background_task(
            self._async_watchdog(), "Tailcat server watchdog"
        )

    async def async_stop(self) -> None:
        """Stop the server."""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None
        async with self._lock:
            await self._async_terminate()
        await self._async_remove_key_file()

    async def async_update_allowed_keys(self) -> None:
        """Apply the clients allowed to connect, restarting only on a change.

        The allowlist is fixed for the lifetime of a tailcat process, so a
        change restarts it. Raises TailcatServerStartError if it does not
        come back; the watchdog keeps retrying in that case.
        """
        async with self._lock:
            # Read under the lock, so concurrent updates apply the latest set
            allowed = frozenset(self._get_allowed_keys())
            if allowed == self._allowed_keys:
                return
            self._allowed_keys = allowed
            if self._key_file is None:
                return
            _LOGGER.debug("Allowed devices changed, restarting tailcat server")
            await self._async_terminate()
            await self._async_spawn()

    def _allow_argument(self) -> str:
        """Return the value of the --allow flag."""
        if not self._allowed_keys:
            # An empty allowlist would allow anyone knowing the address
            return "none"
        return ",".join(sorted(self._allowed_keys))

    async def _async_spawn(self) -> None:
        """Start a server process and wait until it listens."""
        assert self._key_file is not None
        self._log_buffer.clear()
        process = await create_subprocess_exec(
            self._binary,
            "--json",
            f"--key={self._key_file}",
            "serve",
            f"--allow={self._allow_argument()}",
            str(self._port),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._process = process
        self._started_at = self._hass.loop.time()
        self._hass.async_create_background_task(
            self._async_log_stderr(process), "Tailcat server log output"
        )
        try:
            async with asyncio.timeout(_START_TIMEOUT):
                address = await self._async_read_address(process)
        except TimeoutError:
            await self._async_terminate()
            self._log_server_output(logging.WARNING)
            raise TailcatServerStartError(
                "Tailcat server did not start listening in time"
            ) from None
        if address is None:
            await self._async_terminate()
            self._log_server_output(logging.WARNING)
            raise TailcatServerStartError("Tailcat server exited during startup")
        self.address = address
        _LOGGER.debug("Tailcat server listening")

    async def _async_read_address(
        self, process: asyncio.subprocess.Process
    ) -> str | None:
        """Read stdout until the server reports its address."""
        assert process.stdout is not None
        async for line in process.stdout:
            try:
                data = json_loads(line)
            except ValueError:
                self._log_line(line)
                continue
            if isinstance(data, dict) and isinstance(
                address := data.get("listenAddr"), str
            ):
                self._hass.async_create_background_task(
                    self._async_drain_stdout(process), "Tailcat server stdout"
                )
                return address
            self._log_line(line)
        return None

    async def _async_drain_stdout(self, process: asyncio.subprocess.Process) -> None:
        """Keep reading stdout so the process never blocks on it."""
        assert process.stdout is not None
        async for line in process.stdout:
            self._log_line(line)

    async def _async_log_stderr(self, process: asyncio.subprocess.Process) -> None:
        """Log the human readable output of the process."""
        assert process.stderr is not None
        async for line in process.stderr:
            self._log_line(line)

    def _log_line(self, line: bytes) -> None:
        """Log and buffer a line of process output without secrets."""
        msg = redact_output(line.decode(errors="replace").rstrip())
        self._log_buffer.append(msg)
        _LOGGER.debug(msg)

    def _log_server_output(self, level: int) -> None:
        """Log the buffered process output."""
        for line in self._log_buffer:
            _LOGGER.log(level, line)
        self._log_buffer.clear()

    async def _async_terminate(self) -> None:
        """Terminate the server process."""
        if (process := self._process) is None:
            return
        self._process = None
        self.address = None
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                async with asyncio.timeout(_TERMINATE_TIMEOUT):
                    await process.wait()
            except TimeoutError:
                _LOGGER.warning("Tailcat server did not terminate, killing it")
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def _async_remove_key_file(self) -> None:
        """Remove the key file and its private directory."""
        if (key_file := self._key_file) is None:
            return
        self._key_file = None
        await self._hass.async_add_executor_job(shutil.rmtree, key_file.parent, True)

    async def _async_watchdog(self) -> None:
        """Restart the server with a backoff whenever it stops unexpectedly."""
        failures = 0
        while True:
            process = self._process
            if process is not None:
                await process.wait()
                async with self._lock:
                    if self._process is not process:
                        # Replaced or stopped on purpose
                        continue
                    if self._hass.loop.time() - self._started_at >= _STABLE_RUN_TIME:
                        failures = 0
                    _LOGGER.warning(
                        "Tailcat server exited unexpectedly with code %s",
                        process.returncode,
                    )
                    self._log_server_output(logging.WARNING)
                    self._process = None
                    self.address = None
            delay = min(_RESTART_DELAY_INITIAL * 2**failures, _RESTART_DELAY_MAX)
            failures += 1
            await asyncio.sleep(delay)
            async with self._lock:
                if self._process is not None:
                    # Restarted meanwhile by an allowlist change
                    continue
                try:
                    await self._async_spawn()
                except TailcatServerStartError as err:
                    _LOGGER.warning("Restarting the tailcat server failed: %s", err)
