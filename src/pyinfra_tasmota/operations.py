"""Operations for the Tasmota connector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyinfra import logger
from pyinfra.api.command import FunctionCommand
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.operation import operation
from pyinfra.context import host

from .compare import adapter_for, is_rule
from .connector import NO_READ_COMMANDS, TasmotaConnector, _canonical, is_secret, redact

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

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
    if _ensure_all(_connector(), kwargs):
        yield _marker()


@operation()
def ensure_host_data(
    key: str = "tasmota", send_passwords: bool = True
) -> Generator[PyinfraCommand, None, None]:
    """Ensure the commands declared under a host-data key.

    The commands are read here, rather than passed as arguments, so the
    operation is identical on every host. pyinfra runs one operation for every
    host that registered it, so passing host-specific arguments would create
    one operation per host and print "Skipped" for all the others.

    ``Rule<x>`` keys are handled like the ``rule`` operation: the value is the
    rule text, or a mapping of its arguments. Their text is sent directly,
    because it can contain semicolons.

    Args:
        key: The host-data key holding a mapping of command to value.
        send_passwords: When False, password commands are left out.

    Yields:
        At most one command per changed rule, or a marker for other changes.
    """
    commands = host.data.get(key, {})
    if not isinstance(commands, dict):
        raise TypeError(f"host data {key!r} must be a mapping of command to value")
    if not send_passwords:
        commands = {name: value for name, value in commands.items() if not is_secret(name)}

    connector = _connector()
    rule_sends: list[tuple[str, str]] = []
    changed = False
    for subject, value in commands.items():
        if is_rule(subject):
            index = _rule_index(subject)
            send_code, rule_changed = _ensure_rule(connector, index, **_rule_spec(value))
            if send_code is not None:
                rule_sends.append((f"Rule{index}", send_code))
            changed = rule_changed or changed
        elif isinstance(value, (list, tuple)):
            changed = _queue_sequence(connector, subject, value) or changed
        else:
            changed = _ensure(connector, subject, value) or changed

    for subject, value in rule_sends:
        yield _send_rule(subject, value)
    if changed and not rule_sends:
        yield _marker()


@operation()
def shutter(
    *,
    open_duration: float,
    close_duration: float,
    interlock: str = "1,2",
    mode: int = 1,
    relay: int = 1,
    button: str | int | None = None,
    index: int = 1,
) -> Generator[PyinfraCommand, None, None]:
    """Configure a Tasmota shutter.

    Shutter support must be enabled before the other commands work, and the
    device has to restart for that to take effect. If it is not enabled, the
    connector enables ``SetOption80``, restarts the device, and waits for it
    to return before the remaining settings are sent.

    Args:
        open_duration: Seconds to open fully (``ShutterOpenDuration``).
        close_duration: Seconds to close fully (``ShutterCloseDuration``).
        interlock: Relay pair to interlock, sent first (``Interlock``).
        mode: Shutter mode (``ShutterMode``), default ``1``.
        relay: First relay of the pair (``ShutterRelay``), default ``1``.
        button: Optional ``ShutterButton`` argument, for example ``"1 up"``.
        index: Shutter number, default ``1``.

    Yields:
        At most one command.
    """
    connector = _connector()
    settings: dict[str, Any] = {
        "ShutterMode": mode,
        f"ShutterRelay{index}": relay,
        f"ShutterOpenDuration{index}": open_duration,
        f"ShutterCloseDuration{index}": close_duration,
    }
    if button is not None:
        settings[f"ShutterButton{index}"] = button

    to_send = [
        subject for subject, value in settings.items() if _needs_change(connector, subject, value)
    ]
    interlock_changed = _needs_change(connector, "Interlock", interlock) or _needs_change(
        connector, "Interlock", "ON"
    )
    enabled = connector.shutter_enabled()
    if enabled and not to_send and not interlock_changed:
        return

    # The interlock must be set before the shutter mode that requires it.
    _ensure(connector, "Interlock", interlock)
    _ensure(connector, "Interlock", "ON")
    for subject in to_send:
        connector.record(subject, str(settings[subject]))

    if not enabled:
        yield _enable_shutter()
    else:
        yield _marker()


@operation()
def rule(
    code: str,
    *,
    index: int = 1,
    enabled: bool | None = None,
    once: bool | None = None,
    stop_on_error: bool | None = None,
    append: bool = False,
) -> Generator[PyinfraCommand, None, None]:
    """Set a Tasmota rule set.

    Rule text contains semicolons, which separate backlog commands, so it is
    sent as its own request rather than through the batch flush. The flags are
    sent afterwards as ordinary commands.

    Args:
        code: The rule text, for example ``"ON ... DO ... ENDON"``.
        index: Rule set number, default ``1``.
        enabled: Enable or disable the set (``Rule<x> 1`` or ``0``).
        once: Whether the set runs once (``Rule<x> 5`` or ``4``).
        stop_on_error: Whether to stop on error (``Rule<x> 9`` or ``8``).
        append: Append the code instead of replacing the set.

    Yields:
        At most one command.
    """
    connector = _connector()
    send_code, changed = _ensure_rule(
        connector,
        index,
        code,
        enabled=enabled,
        once=once,
        stop_on_error=stop_on_error,
        append=append,
    )
    if send_code is not None:
        yield _send_rule(f"Rule{index}", send_code)
    elif changed:
        yield _marker()


def _ensure_rule(
    connector: TasmotaConnector,
    index: int,
    code: str,
    *,
    enabled: bool | None = None,
    once: bool | None = None,
    stop_on_error: bool | None = None,
    append: bool = False,
) -> tuple[str | None, bool]:
    """Ensure one rule set.

    Returns the rule text to send directly, if it must change, and whether a
    flag changed. The text is returned rather than queued because it can
    contain semicolons, which cannot go into a backlog.
    """
    subject = f"Rule{index}"
    code_value = f"+{code}" if append else code
    send_code = code_value if (append or _needs_change(connector, subject, code_value)) else None

    changed = False
    if enabled is not None:
        changed = _ensure(connector, subject, "1" if enabled else "0") or changed
    if once is not None:
        changed = _ensure(connector, subject, "5" if once else "4") or changed
    if stop_on_error is not None:
        changed = _ensure(connector, subject, "9" if stop_on_error else "8") or changed
    return send_code, changed


def _rule_index(subject: str) -> int:
    """Return the rule set number from a ``Rule<x>`` command name."""
    return int(_canonical(subject)[len("rule") :])


def _rule_spec(value: Any) -> dict[str, Any]:
    """Return the ``rule`` arguments for a host-data value.

    A mapping is used as keyword arguments; anything else is the rule text.
    """
    if isinstance(value, dict):
        return dict(value)
    return {"code": value}


def _send_rule(subject: str, value: str) -> PyinfraCommand:
    """Return a command that sends rule text directly, outside the backlog."""

    def send(state: Any, host: Any) -> bool:
        connector = host.connector
        assert isinstance(connector, TasmotaConnector)
        connector.send_command(subject, value)
        return True

    return FunctionCommand(send, (), {})


def _ensure_all(connector: TasmotaConnector, commands: Mapping[str, Any]) -> bool:
    """Ensure a mapping of commands, returning whether any change was recorded."""
    changed = False
    for subject, value in commands.items():
        if isinstance(value, (list, tuple)):
            changed = _queue_sequence(connector, subject, value) or changed
        else:
            changed = _ensure(connector, subject, value) or changed
    return changed


def _queue_sequence(
    connector: TasmotaConnector, subject: str, values: list[Any] | tuple[Any, ...]
) -> bool:
    """Queue a command once per value, in order, without diffing."""
    for one in values:
        connector.record(subject, str(one))
        if connector.executing:
            connector.log_at(
                2, "%s: %s queued %s", connector.host.name, subject, redact(subject, one)
            )
    return bool(values)


def _needs_change(connector: TasmotaConnector, subject: str, value: Any) -> bool:
    """Return whether a command must be sent, reading the device if needed."""
    desired = str(value)
    adapter = adapter_for(subject)

    if adapter.skip:
        if connector.executing:
            connector.log_at(
                2, "%s: %s is masked, setting unconditionally", connector.host.name, subject
            )
        return True

    observed = connector.raw(subject)
    if observed is None and _canonical(subject) not in NO_READ_COMMANDS:
        observed = _read_current(connector, subject)
        connector.log_at(
            3, "%s: read %s as %s", connector.host.name, subject, redact(subject, observed)
        )

    if observed is not None and adapter.matches(value, observed):
        if connector.executing:
            connector.log_at(
                2,
                "%s: %s is already %s, skipping",
                connector.host.name,
                subject,
                redact(subject, observed),
            )
        return False

    if connector.executing:
        if observed is None:
            connector.log_at(
                2,
                "%s: %s state unknown, will set to %s",
                connector.host.name,
                subject,
                redact(subject, desired),
            )
        else:
            connector.log_at(
                2,
                "%s: %s is %s, will set to %s",
                connector.host.name,
                subject,
                redact(subject, observed),
                redact(subject, desired),
            )
    return True


def _ensure(connector: TasmotaConnector, subject: str, value: Any) -> bool:
    """Ensure one command, returning whether a change was recorded."""
    if _needs_change(connector, subject, value):
        connector.record(subject, str(value))
        return True
    return False


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

    # Commands like Interlock answer with several top-level fields, so the
    # adapter needs the whole response, not the value under the command's key.
    if adapter_for(subject).whole:
        connector.cache(subject, candidates)
        return candidates

    target = _canonical(subject)
    for key, value in candidates.items():
        if _canonical(key) == target:
            connector.cache(key, value)
            return value

    if len(candidates) == 1:
        key, value = next(iter(candidates.items()))
        connector.cache(key, value)
        return value

    return None


def _enable_shutter() -> PyinfraCommand:
    """Return a command that enables shutter support and waits for the reboot."""

    def enable(state: Any, host: Any) -> bool:
        connector = host.connector
        assert isinstance(connector, TasmotaConnector)
        connector.enable_shutter()
        return True

    return FunctionCommand(enable, (), {})


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
