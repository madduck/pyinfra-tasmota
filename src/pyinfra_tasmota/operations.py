"""Operations for the Tasmota connector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyinfra.api.command import FunctionCommand
from pyinfra.api.operation import operation
from pyinfra.context import host

from .connector import TasmotaConnector

if TYPE_CHECKING:
    from collections.abc import Generator

    from pyinfra.api.command import PyinfraCommand


@operation()
def cmnd(subject: str, value: Any) -> Generator[PyinfraCommand, None, None]:
    """Set a Tasmota command value, if it differs from the device.

    The operation records the command against the connector's settings cache.
    If the cached value already equals ``value``, the operation yields no
    command and pyinfra reports "No changes". If the value is not cached, the
    current value is read from the device over HTTP and cached before the
    comparison. Any command that must be sent is applied when the connector
    flushes, on disconnect.

    Args:
        subject: The Tasmota command name, for example ``SetOption114``.
        value: The desired value, for example ``1`` or ``on``.

    Yields:
        At most one command that records the pending change.
    """
    connector = _connector()
    desired = str(value)

    current = connector.current(subject)
    if current is None:
        current = _read_current(connector, subject)

    if current is not None and str(current).lower() == desired.lower():
        return

    yield _record_command(subject, desired)


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


def _record_command(subject: str, value: str) -> PyinfraCommand:
    """Return a command that records the pending change on the connector."""

    def record(state: Any, host: Any) -> bool:
        connector = host.connector
        assert isinstance(connector, TasmotaConnector)
        connector.record(subject, value)
        return True

    return FunctionCommand(record, (), {})
