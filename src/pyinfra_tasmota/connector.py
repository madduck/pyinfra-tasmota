"""A Pyinfra execution connector for Tasmota devices.

The connector talks to a Tasmota device through its HTTP API. On connect it
pulls a status dump to populate a cache of the device settings. Operations
record their intent against that cache, and the pending commands are flushed
to the device as ``Backlog0`` batches, either through the ``apply`` operation
or on disconnect.
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

# Tasmota's Backlog accepts at most this many commands. decode-config caps the
# full Backlog command line at this many characters; both limits are enforced
# when grouping commands into batches.
BACKLOG_MAX_COMMANDS = 30
BACKLOG_MAX_LENGTH = 320
BACKLOG_PREFIX = "Backlog0 "

# Tasmota reports booleans as ON/OFF but accepts other spellings, so values
# are folded to these sets before comparison.
_TRUE_VALUES = frozenset({"1", "on", "true", "yes"})
_FALSE_VALUES = frozenset({"0", "off", "false", "no"})

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
        # Maps (canonical command, value) to the subject as given. A command
        # may therefore be queued with several values (in order), while a
        # repeated identical command collapses to one.
        self._pending: dict[tuple[str, str], str] = {}

    @property
    def verbosity(self) -> int:
        """Return the verbosity implied by pyinfra's ``-v`` flags.

        pyinfra does not raise the logging level for ``-v``; it sets display
        flags on the state. ``-v`` sets ``print_noop_info``, ``-vv`` adds
        ``print_input``, and ``-vvv`` adds ``print_output``.
        """
        state = self.state
        if getattr(state, "print_output", False):
            return 3
        if getattr(state, "print_input", False):
            return 2
        if getattr(state, "print_noop_info", False):
            return 1
        return 0

    @property
    def executing(self) -> bool:
        """Return whether operations are executing, not just being prepared.

        pyinfra runs an operation's command generator once during change
        detection and again during execution. Logging only while executing
        avoids emitting the same line twice.
        """
        return bool(getattr(self.state, "is_executing", False))

    def log_at(self, level: int, message: str, *args: Any) -> None:
        """Log ``message`` when the verbosity is at least ``level``.

        Level 1 (``-v``) reports commands sent, level 2 (``-vv``) reports
        observed state and skipped commands, and level 3 (``-vvv``) reports
        internal detail such as batching.
        """
        if self.verbosity >= level:
            log.info(message, *args)

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
        self._merge(status)

        shutters = self._request("Status 13", allow_missing=True)
        if isinstance(shutters, dict):
            self._merge(shutters)

        self.log_at(3, "%s: cached %d settings", self.host.name, len(self._cache))

    def _merge(self, sections: dict[str, Any]) -> None:
        """Merge numbered status sections into the cache, keyed canonically."""
        for section in sections.values():
            if isinstance(section, dict):
                for key, value in section.items():
                    self._cache[_canonical(key)] = value

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
        """Record a command to be sent during the next flush.

        Commands are keyed canonically and by value, so one command can be
        queued with several values while a repeated identical command
        collapses. This also stops the two generator passes (change detection
        and execution) from queueing the same command twice. The subject is
        kept as given for the wire command.
        """
        self._pending[(_canonical(subject), str(value))] = str(subject)

    def has_pending(self) -> bool:
        """Return whether any command is waiting to be flushed."""
        return bool(self._pending)

    def current(self, subject: str) -> str | None:
        """Return the cached value of ``subject`` or ``None`` if unknown.

        The lookup is canonical, so it ignores case and the ``SO`` alias.
        """
        value = self.raw(subject)
        return None if value is None else str(value)

    def raw(self, subject: str) -> Any:
        """Return the raw cached value of ``subject``, or ``None``.

        Struct responses are kept as dicts so comparison adapters can read
        their fields.
        """
        return self._cache.get(_canonical(subject))

    def cache(self, subject: str, value: Any) -> None:
        """Store ``value`` for ``subject`` in the settings cache."""
        self._cache[_canonical(subject)] = value

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

        immediate = [
            (subject, value)
            for (canonical, value), subject in pending.items()
            if canonical not in REBOOT_COMMANDS
        ]
        reboots = [
            (subject, value)
            for (canonical, value), subject in pending.items()
            if canonical in REBOOT_COMMANDS
        ]

        self.log_at(
            3,
            "%s: flushing %d commands (%d batched, %d reboot)",
            self.host.name,
            len(pending),
            len(immediate),
            len(reboots),
        )

        for batch in _backlog_batches(immediate):
            self._send_batch(batch)

        for subject, value in reboots:
            self._send_reboot(subject, value)

    def _send_batch(self, batch: list[tuple[str, str]]) -> None:
        """Send a batch of non-rebooting commands as a single Backlog0."""
        commands = ";".join(f"{subject} {value}" for subject, value in batch)
        self._request(f"{BACKLOG_PREFIX}{commands}")
        self.log_at(
            3,
            "%s: sent a batch of %d commands (%d characters)",
            self.host.name,
            len(batch),
            len(BACKLOG_PREFIX) + len(commands),
        )
        for subject, value in batch:
            self.log_at(1, "%s: sent %s %s", self.host.name, subject, value)
            self.cache(subject, value)

    def _send_reboot(self, subject: str, value: str) -> None:
        """Send a single reboot-triggering command and wait for the device.

        A reboot drains or aborts the backlog, so the command must stand alone.
        """
        self._request(f"{subject} {value}")
        self.log_at(1, "%s: sent %s %s", self.host.name, subject, value)
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
        previous_boot = self.current("BootCount")
        timeout = float(self.data.get("tasmota_reboot_timeout", DEFAULT_REBOOT_TIMEOUT))
        deadline = time.monotonic() + timeout
        saw_down = False

        self.log_at(3, "%s: waiting for the device to answer after reboot", self.host.name)

        while time.monotonic() < deadline:
            reply = self._request("Status 1", allow_missing=True)
            if reply is None:
                if not saw_down:
                    self.log_at(3, "%s: device is down", self.host.name)
                saw_down = True
            else:
                boot = _extract_bootcount(reply)
                if saw_down or (
                    previous_boot is not None and boot is not None and str(boot) != previous_boot
                ):
                    self.log_at(3, "%s: device is back after reboot", self.host.name)
                    self._ingest()
                    return
            time.sleep(REBOOT_POLL_INTERVAL)

        raise PyinfraError(
            f"{self.host.name}: device did not return within {timeout:g}s after reboot"
        )

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


def _canonical(subject: str) -> str:
    """Return the canonical form of a Tasmota command name.

    Command names are case-insensitive, and ``SO`` is a documented synonym for
    ``SetOption``, so ``SO114`` and ``SetOption114`` are the same command.
    """
    name = subject.strip().lower()
    if name.startswith("so") and (name == "so" or name[2:].isdigit()):
        return "setoption" + name[2:]
    return name


def _normalize(value: Any) -> str:
    """Fold a Tasmota value to a canonical form for comparison.

    Tasmota reports booleans as ``ON``/``OFF`` but accepts ``1``/``0`` and
    ``on``/``off``, so both sides of a comparison are folded to ``1``/``0``.
    Other values become lower-cased text.
    """
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return "1"
    if text in _FALSE_VALUES:
        return "0"
    return text


def _extract_bootcount(reply: Any) -> Any:
    """Return ``BootCount`` from a ``Status 1`` reply, or ``None``."""
    if isinstance(reply, dict):
        prm = reply.get("StatusPRM")
        if isinstance(prm, dict):
            return prm.get("BootCount")
    return None


def _backlog_batches(
    commands: list[tuple[str, str]],
) -> Iterator[list[tuple[str, str]]]:
    """Group commands into Backlog0 batches within the count and length limits.

    Tasmota's Backlog accepts at most ``BACKLOG_MAX_COMMANDS`` commands, and
    decode-config caps the full command line at ``BACKLOG_MAX_LENGTH``
    characters. A single command longer than the length limit is still sent on
    its own, so that batching always makes progress.
    """
    batch: list[tuple[str, str]] = []
    length = len(BACKLOG_PREFIX)
    for subject, value in commands:
        command = f"{subject} {value}"
        addition = len(command) + (1 if batch else 0)  # separator ";"
        if batch and (len(batch) >= BACKLOG_MAX_COMMANDS or length + addition > BACKLOG_MAX_LENGTH):
            yield batch
            batch = []
            length = len(BACKLOG_PREFIX)
            addition = len(command)
        batch.append((subject, value))
        length += addition
    if batch:
        yield batch
