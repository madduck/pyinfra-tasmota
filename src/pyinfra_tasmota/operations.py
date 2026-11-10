"""Operations for the Tasmota connector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyinfra import logger
from pyinfra.api.command import FunctionCommand
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.operation import operation
from pyinfra.context import host

from .connector import REBOOT_COMMANDS, TasmotaConnector, _canonical, _normalize

if TYPE_CHECKING:
    from collections.abc import Generator

    from pyinfra.api.command import PyinfraCommand


@operation()
def cmnd(**kwargs: Any) -> Generator[PyinfraCommand, None, None]:
    """Set Tasmota command values, if they differ from the device.

    Each keyword argument is a command name and its desired value, for
    example ``cmnd(SetOption114=1, Power="toggle")``. Command names are
    matched case-insensitively and resolve the ``SO`` alias of ``SetOption``.

    For each command whose cached value already equals the desired value, no
    change is recorded. If a value is not cached, the current value is read
    from the device over HTTP and cached before the comparison. Recording
    happens while the command generator runs, so changes are visible during
    the prepare step and to ``--dry``.

    Reboot-triggering commands are never read: sending such a command to learn
    its value is meaningless and may act on the device.

    The recorded commands are sent by the ``apply`` operation, or on
    disconnect.

    Args:
        **kwargs: Tasmota command names mapped to desired values.

    Yields:
        At most one marker command when any value must change.
    """
    connector = _connector()
    changed = False
    for subject, value in kwargs.items():
        desired = str(value)

        current = connector.current(subject)
        if current is None and _canonical(subject) not in REBOOT_COMMANDS:
            current = _read_current(connector, subject)
            connector.log_at(3, "%s: read %s as %s", connector.host.name, subject, current)

        if current is not None and _normalize(current) == _normalize(desired):
            if connector.executing:
                connector.log_at(
                    2, "%s: %s is already %s, skipping", connector.host.name, subject, current
                )
            continue

        if connector.executing:
            if current is None:
                connector.log_at(
                    2,
                    "%s: %s state unknown, will set to %s",
                    connector.host.name,
                    subject,
                    desired,
                )
            else:
                connector.log_at(
                    2,
                    "%s: %s is %s, will set to %s",
                    connector.host.name,
                    subject,
                    current,
                    desired,
                )
        connector.record(subject, desired)
        changed = True

    if changed:
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
    """Read and cache the current value of ``subject`` from the device.

    Tasmota answers a bare command with a single ``{key: value}`` object. The
    key usually matches the command, but not always: ``so114`` answers with
    ``SetOption114``. An exact canonical match wins; otherwise a lone scalar
    value is accepted, which resolves aliases.
    """
    response = connector.request(subject, allow_missing=True)
    if not isinstance(response, dict):
        return None

    scalars = {
        key: value
        for key, value in response.items()
        if not isinstance(value, (dict, list)) and key.lower() != "command"
    }
    if not scalars:
        return None

    target = _canonical(subject)
    for key, value in scalars.items():
        if _canonical(key) == target:
            connector.cache(key, value)
            return str(value)

    if len(scalars) == 1:
        key, value = next(iter(scalars.items()))
        connector.cache(key, value)
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
