"""A Pyinfra execution connector for Tasmota devices.

The connector talks to a Tasmota device through its HTTP API. On connect it
pulls a status dump to populate a cache of the device settings. Operations
record their intent against that cache, and the pending commands are flushed
to the device as ``Backlog0`` batches on disconnect.

Tasmota writes every changed setting to flash. To avoid wear, a batch of
non-rebooting commands is wrapped in ``SaveData 0`` (no automatic flash
writes) followed by ``SaveData 1`` (restore automatic writes). Reboot
triggering commands are not wrapped: a reboot drains or aborts the backlog, so
a trailing ``SaveData 1`` could be lost. Because ``SaveData`` is persisted
immediately and survives a reboot, the connector also checks and repairs a
stuck value during connect.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import requests
from pyinfra import logger
from pyinfra.api.exceptions import ConnectError, InventoryError, PyinfraError
from pyinfra.connectors.base import BaseConnector, ConnectorData, DataMeta
from pyinfra.connectors.util import CommandOutput
from typing_extensions import Unpack

if TYPE_CHECKING:
    from pyinfra.api.arguments import ConnectorArguments
    from pyinfra.api.command import StringCommand


log = logger.getChild("pyinfra-tasmota")

# Tasmota's Backlog accepts at most this many commands. Two slots are
# reserved for the SaveData 0 and SaveData 1 guard, so a batch carries at most
# ``BACKLOG_MAX_COMMANDS - 2`` real commands.
BACKLOG_MAX_COMMANDS = 30

# How long to wait for a rebooted device to answer again, and how often to poll.
DEFAULT_REBOOT_TIMEOUT = 60.0
REBOOT_POLL_INTERVAL = 0.5

# A handful of Tasmota commands restart the device (or its networking stack).
# The connector cannot apply further commands until the device is back, so
# these are isolated into their own batch and applied last.
REBOOT_COMMANDS = frozenset(
    {
        "ap",
        "devgroupname",
        "hostname",
        "ipaddress",
        "module",
        "mqttclient",
        "mqttfulltopic",
        "mqtthost",
        "mqttpassword",
        "mqttport",
        "mqttuser",
        "prefix",
        "reset",
        "restart",
        "ssid",
        "sta",
        "template",
        "upgrade",
        "upload",
    }
)


class TasmotaConnectorData(ConnectorData):
    """Connector data accepted per host in the inventory."""

    tasmota_friendly_name: str
    tasmota_reboot_timeout: float


data_meta: dict[str, DataMeta] = {
    "tasmota_friendly_name": DataMeta("Optional human-readable device name, for logging only"),
    "tasmota_reboot_timeout": DataMeta(
        "Seconds to wait for a rebooted device to answer again",
        DEFAULT_REBOOT_TIMEOUT,
    ),
}


class TasmotaConnector(BaseConnector):
    """Execute operations against a Tasmota device over HTTP."""

    handles_execution = True
    data_cls = TasmotaConnectorData
    data_meta = data_meta

    def __init__(self, state: Any, host: Any) -> None:
        """Initialise the connector and its HTTP session."""
        super().__init__(state, host)
        self.session = requests.Session()
        self._cache: dict[str, Any] = {}
        self._pending: dict[str, str] = {}

    @staticmethod
    def make_names_data(name: str | None = None) -> Any:
        """Yield the Tasmota host described by ``name``.

        Args:
            name: A ``host`` or ``host:port`` string, optionally prefixed with
                ``http://`` or ``https://``.

        Yields:
            A ``(name, data, groups)`` tuple describing the host.

        Raises:
            InventoryError: If no host name is given.
        """
        if not name:
            raise InventoryError("No Tasmota host provided!")

        host, port = _split_host_port(name)

        yield (
            name,
            {"tasmota_host": host, "tasmota_port": port},
            ["tasmota"],
        )

    # -- Connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Fetch a status dump and populate the settings cache.

        Raises:
            ConnectError: If the device cannot be reached or answers with an
                unexpected payload.
        """
        self._ingest()

    def _ingest(self) -> None:
        """Pull the full status dump and rebuild the settings cache.

        Raises:
            ConnectError: If the device cannot be reached or answers with an
                unexpected payload.
        """
        status = self._request("Status 0")
        if not isinstance(status, dict) or "Status" not in status:
            raise ConnectError(f"Unexpected status response from {self.host.name!r}")

        self._cache = {}
        for value in status.values():
            if isinstance(value, dict):
                self._cache.update(value)

        shutters = self._request("Status 13", allow_missing=True)
        if isinstance(shutters, dict):
            for value in shutters.values():
                if isinstance(value, dict):
                    self._cache.update(value)

        self._repair_savedata()

        log.debug("%s: cached %d settings", self.host.name, len(self._cache))

    def disconnect(self) -> None:
        """Flush any pending commands and close the HTTP session.

        This is a safety net behind the ``apply`` operation. A disconnect must
        not fail, so a flush error is logged rather than raised.
        """
        try:
            self.apply()
        except PyinfraError as err:
            log.error("%s: flush during disconnect failed: %s", self.host.name, err)
        finally:
            self.session.close()

    # -- HTTP helpers ---------------------------------------------------------

    def _url(self, cmnd: str) -> str:
        query = urlencode({"cmnd": cmnd})
        scheme = self.host.data.get("tasmota_scheme", "http")
        host = self.host.data.get("tasmota_host", self.host.name)
        port = self.host.data.get("tasmota_port", 80)
        return f"{scheme}://{host}:{port}/cm?{query}"

    def _request(self, cmnd: str, allow_missing: bool = False) -> Any:
        url = self._url(cmnd)
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as err:
            if allow_missing:
                return None
            raise ConnectError(f"Request to {url!r} failed: {err}") from err

        try:
            return response.json()
        except ValueError as err:
            if allow_missing:
                return None
            raise ConnectError(f"Invalid JSON from {url!r}: {err}") from err

    # -- Intent recording -----------------------------------------------------

    def record(self, subject: str, value: Any) -> None:
        """Record a command to be sent during the next flush."""
        self._pending[str(subject)] = str(value)

    def has_pending(self) -> bool:
        """Return whether any command is waiting to be flushed."""
        return bool(self._pending)

    def current(self, subject: str) -> str | None:
        """Return the cached value of ``subject`` or ``None`` if unknown.

        The lookup is case-insensitive because Tasmota command names are.
        """
        for key, value in self._cache.items():
            if key.lower() == subject.lower():
                return str(value)
        return None

    def cache(self, subject: str, value: Any) -> None:
        """Store ``value`` for ``subject`` in the settings cache."""
        self._cache[subject] = value

    def request(self, cmnd: str, allow_missing: bool = False) -> Any:
        """Send ``cmnd`` to the device and return the parsed JSON reply."""
        return self._request(cmnd, allow_missing=allow_missing)

    # -- Flushing -------------------------------------------------------------

    def apply(self) -> None:
        """Send all pending commands to the device, in safe batches."""
        if not self._pending:
            return

        pending = dict(self._pending)
        self._pending.clear()

        immediate = [(s, v) for s, v in pending.items() if s.lower() not in REBOOT_COMMANDS]
        reboots = [(s, v) for s, v in pending.items() if s.lower() in REBOOT_COMMANDS]

        for batch in _chunk(immediate, BACKLOG_MAX_COMMANDS - 2):
            self._send_batch(batch)

        for subject, value in reboots:
            self._send_reboot(subject, value)

    def _send_batch(self, batch: list[tuple[str, str]]) -> None:
        """Send a batch of non-rebooting commands, guarding against flash wear."""
        parts = ["SaveData 0", *(f"{subject} {value}" for subject, value in batch), "SaveData 1"]
        self._request("Backlog0 " + ";".join(parts))
        for subject, value in batch:
            self._cache[subject] = value

    def _send_reboot(self, subject: str, value: str) -> None:
        """Send a single reboot-triggering command and wait for the device.

        A reboot drains or aborts the backlog, so the command must stand alone.
        It is not wrapped in a ``SaveData`` guard: the reboot persists the
        configuration itself, and a trailing ``SaveData 1`` could be lost.
        """
        self._request(f"{subject} {value}")
        self._await_reboot()

    def _await_reboot(self) -> None:
        """Wait for a rebooted device to answer, then refresh the cache.

        The device is considered back once it either was seen to drop and is
        answering again, or its ``BootCount`` has advanced. The latter covers
        fast reboots during which the HTTP server never appears to go down.

        Raises:
            PyinfraError: If the device does not return within the configured
                timeout.
        """
        previous_boot = self._cache.get("BootCount")
        timeout = float(self.data.get("tasmota_reboot_timeout", DEFAULT_REBOOT_TIMEOUT))
        deadline = time.monotonic() + timeout
        saw_down = False

        while time.monotonic() < deadline:
            reply = self._request("Status 1", allow_missing=True)
            if reply is None:
                saw_down = True
            else:
                boot = _extract_bootcount(reply)
                if saw_down or (
                    previous_boot is not None and boot is not None and boot != previous_boot
                ):
                    log.info("%s: device is back after reboot", self.host.name)
                    self._ingest()
                    return
            time.sleep(REBOOT_POLL_INTERVAL)

        raise PyinfraError(
            f"{self.host.name}: device did not return within {timeout:g}s after reboot"
        )

    def _repair_savedata(self) -> None:
        if self._cache.get("SaveData") == 0:
            log.warning("%s: repairing stuck SaveData 0", self.host.name)
            self._request("SaveData 1")
        elif "SaveData" not in self._cache:
            log.debug("%s: device does not expose SaveData", self.host.name)

    # -- Unsupported execution surface ---------------------------------------

    def run_shell_command(
        self,
        command: StringCommand,
        print_output: bool = False,
        print_input: bool = False,
        **arguments: Unpack[ConnectorArguments],
    ) -> tuple[bool, CommandOutput]:
        """Tasmota devices do not expose a shell."""
        raise NotImplementedError("The Tasmota connector does not support shell commands")

    def put_file(
        self,
        filename_or_io: Any,
        remote_filename: str,
        remote_temp_filename: str | None = None,
        print_output: bool = False,
        print_input: bool = False,
        **arguments: Unpack[ConnectorArguments],
    ) -> bool:
        """Tasmota devices do not accept arbitrary file uploads."""
        raise NotImplementedError("The Tasmota connector does not support file uploads")

    def get_file(
        self,
        remote_filename: str,
        filename_or_io: Any,
        remote_temp_filename: str | None = None,
        print_output: bool = False,
        print_input: bool = False,
        **arguments: Unpack[ConnectorArguments],
    ) -> bool:
        """Tasmota devices do not expose an arbitrary file system."""
        raise NotImplementedError("The Tasmota connector does not support file downloads")


def _split_host_port(name: str) -> tuple[str, int]:
    """Split a connector target into a host and a port."""
    host_part = name
    port = 80
    if "://" in host_part:
        scheme, _, host_part = host_part.partition("://")
        if scheme == "https":
            port = 443
    if host_part.count(":") == 1:
        host_part, _, port_part = host_part.partition(":")
        port = int(port_part)
    return host_part, port


def _extract_bootcount(reply: Any) -> Any:
    """Return ``BootCount`` from a ``Status 1`` reply, or ``None``."""
    if isinstance(reply, dict):
        prm = reply.get("StatusPRM")
        if isinstance(prm, dict):
            return prm.get("BootCount")
    return None


def _chunk(items: list[Any], size: int) -> Iterator[list[Any]]:
    """Yield consecutive chunks of at most ``size`` items."""
    for index in range(0, len(items), size):
        yield items[index : index + size]
