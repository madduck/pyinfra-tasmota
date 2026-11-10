"""Operations for the Tasmota connector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyinfra import logger
from pyinfra.api.command import FunctionCommand
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.operation import operation
from pyinfra.context import host

from .connector import REBOOT_COMMANDS, TasmotaConnector

if TYPE_CHECKING:
    from collections.abc import Generator

    from pyinfra.api.command import PyinfraCommand


@operation()
def cmnd(subject: str, value: Any) -> Generator[PyinfraCommand, None, None]:
    """Set a Tasmota command value, if it differs from the device.

    The operation records the desired value against the connector's pending
    set. If the cached value already equals ``value``, the operation yields no
    command and pyinfra reports "No changes". If the value is not cached, the
    current value is read from the device over HTTP and cached before the
    comparison. Recording happens while the command generator runs, so the
    change is visible during the prepare step and to ``--dry``.

    Reboot-triggering commands are never read: sending such a command to learn
    its value is meaningless and may act on the device.

    The recorded command is sent by the ``apply`` operation, or on disconnect.

    Args:
        subject: The Tasmota command name, for example ``SetOption114``.
        value: The desired value, for example ``1`` or ``on``.

    Yields:
        At most one marker command when the value must change.
    """
    connector = _connector()
    desired = str(value)

    current = connector.current(subject)
    if current is None and subject.lower() not in REBOOT_COMMANDS:
        current = _read_current(connector, subject)

    if current is not None and str(current).lower() == desired.lower():
        return

    connector.record(subject, desired)
    yield _marker()


@operation()
def apply() -> Generator[PyinfraCommand, None, None]:
    """Send all pending Tasmota commands to the device.

    Call this after the ``cmnd`` operations to flush them as batched
    ``Backlog0`` requests. If nothing is pending, the operation yields no
    command and pyinfra reports "No changes". Pending commands not flushed by
    this operation are sent on disconnect as a safety net.

    Yields:
        At most one command that flushes the pending commands.
    """
    connector = _connector()
    if not connector.has_pending():
        return
    yield _flush_command()


def _connector() -> TasmotaConnector:
    """Return the active Tasmota connector for the current host."""
    connector = host.connector
    assert isinstance(connector, TasmotaConnector)
    return connector


def _read_current(connector: TasmotaConnector, subject: str) -> str | None:
    """Read and cache the current value of ``subject`` from the device."""
    response = connector.request(subject, allow_missing=True)
    if isinstance(response, dict):
        for key, value in response.items():
            if key.lower() == subject.lower() and not isinstance(value, dict):
                connector.cache(subject, value)
                return str(value)
    return None


def _marker() -> PyinfraCommand:
    """Return a no-op command that marks an operation as a change."""

    def mark(state: Any, host: Any) -> bool:
        return True

    return FunctionCommand(mark, (), {})


def _flush_command() -> PyinfraCommand:
    """Return a command that flushes the connector's pending commands."""

    def flush(state: Any, host: Any) -> bool:
        connector = host.connector
        assert isinstance(connector, TasmotaConnector)
        try:
            connector.apply()
        except PyinfraError as err:
            logger.error("%s", err)
            return False
        return True

    return FunctionCommand(flush, (), {})
