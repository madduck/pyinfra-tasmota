"""Operations for the Tasmota connector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyinfra import logger
from pyinfra.api.command import FunctionCommand
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.operation import operation
from pyinfra.context import host

from .compare import adapter_for
from .connector import NO_READ_COMMANDS, TasmotaConnector, _canonical

if TYPE_CHECKING:
    from collections.abc import Generator

    from pyinfra.api.command import PyinfraCommand


@operation()
def cmnd(**kwargs: Any) -> Generator[PyinfraCommand, None, None]:
    """Set Tasmota command values, if they differ from the device.

    Each keyword argument is a command name and its desired value, for
    example ``cmnd(SetOption114=1, Power="toggle")``. Command names are
    matched case-insensitively and resolve the ``SO`` alias of ``SetOption``.

    A value is either a single value or a list of values. A list sends the
    command once per value, in order, which suits commands that must be sent
    more than once, such as ``Rule1``. List values are never diffed, because a
    sequence has no single cached value to compare against.

    For each command whose cached value already equals the desired value, no
    change is recorded. If a value is not cached, the current value is read
    from the device over HTTP and cached before the comparison. Recording
    happens while the command generator runs, so changes are visible during
    the prepare step and to ``--dry``.

    Commands whose bare form acts on the device or reports no value, such as
    ``Backlog`` or ``Restart``, are never read: a requested value is always
    sent. Most commands, including reboot-triggering settings, are read.

    The recorded commands are sent by the ``apply`` operation, or on
    disconnect.

    Args:
        **kwargs: Tasmota command names mapped to a value or list of values.

    Yields:
        At most one marker command when any value must change.
    """
    connector = _connector()
    changed = False
    for subject, value in kwargs.items():
        if isinstance(value, (list, tuple)):
            changed = _queue_sequence(connector, subject, value) or changed
        else:
            changed = _ensure(connector, subject, value) or changed

    if changed:
        yield _marker()


def _queue_sequence(
    connector: TasmotaConnector, subject: str, values: list[Any] | tuple[Any, ...]
) -> bool:
    """Queue a command once per value, in order, without diffing."""
    for one in values:
        connector.record(subject, str(one))
        if connector.executing:
            connector.log_at(2, "%s: %s queued %s", connector.host.name, subject, one)
    return bool(values)


def _ensure(connector: TasmotaConnector, subject: str, value: Any) -> bool:
    """Ensure one command, returning whether a change was recorded."""
    desired = str(value)
    adapter = adapter_for(subject)

    if adapter.skip:
        connector.record(subject, desired)
        if connector.executing:
            connector.log_at(
                2, "%s: %s is masked, setting unconditionally", connector.host.name, subject
            )
        return True

    observed = connector.raw(subject)
    if observed is None and _canonical(subject) not in NO_READ_COMMANDS:
        observed = _read_current(connector, subject)
        connector.log_at(3, "%s: read %s as %s", connector.host.name, subject, observed)

    if observed is not None and adapter.matches(value, observed):
        if connector.executing:
            connector.log_at(
                2, "%s: %s is already %s, skipping", connector.host.name, subject, observed
            )
        return False

    if connector.executing:
        if observed is None:
            connector.log_at(
                2, "%s: %s state unknown, will set to %s", connector.host.name, subject, desired
            )
        else:
            connector.log_at(
                2,
                "%s: %s is %s, will set to %s",
                connector.host.name,
                subject,
                observed,
                desired,
            )
    connector.record(subject, desired)
    return True


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


def _read_current(connector: TasmotaConnector, subject: str) -> Any:
    """Read and cache the current value of ``subject`` from the device.

    Tasmota answers a bare command with a single ``{key: value}`` object. The
    key usually matches the command, but not always: ``so114`` answers with
    ``SetOption114``. An exact canonical match wins; otherwise a lone value is
    accepted, which resolves aliases. The raw value is returned, so struct
    responses reach the comparison adapter intact.
    """
    response = connector.request(subject, allow_missing=True)
    if not isinstance(response, dict):
        return None

    candidates = {key: value for key, value in response.items() if key.lower() != "command"}
    if not candidates:
        return None

    target = _canonical(subject)
    for key, value in candidates.items():
        if _canonical(key) == target:
            connector.cache(key, value)
            return value

    if adapter_for(subject).unwrapped:
        connector.cache(subject, candidates)
        return candidates

    if len(candidates) == 1:
        key, value = next(iter(candidates.items()))
        connector.cache(key, value)
        return value

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
