"""Operations for the Tasmota connector."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from pyinfra import logger
from pyinfra.api.command import FunctionCommand
from pyinfra.api.exceptions import PyinfraError
from pyinfra.api.operation import operation
from pyinfra.context import host

from .compare import adapter_for, is_rule
from .connector import (
    NO_READ_COMMANDS,
    TasmotaConnector,
    _canonical,
    _normalize,
    is_secret,
    redact,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from pyinfra.api.command import PyinfraCommand


@operation()
def cmnd(**kwargs: Any) -> Generator[PyinfraCommand, None, None]:
    """Set Tasmota command values, if they differ from the device.

    Each keyword argument is a command name and its desired value, for
    example ``cmnd(SetOption114=1, Power="toggle")``. Command names are
    matched case-insensitively and resolve the ``SO`` alias of ``SetOption``.

    A value is either a single value or a list of values. A list ensures each
    value in order, which suits commands that carry more than one value, such
    as ``Interlock 1,2`` and ``Interlock ON``.

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
def template(
    value: str | dict[str, Any], *, activate: bool = True
) -> Generator[PyinfraCommand, None, None]:
    """Set a Tasmota template and optionally activate it.

    Template fields are compared as JSON, ignoring ``ARCH`` and ``CMND``.
    ``CMND`` is removed from the template sent to the device. When activation
    is requested, ``Module 0`` and the commands from ``CMND`` are also ensured,
    even if the template already matches. Commands are queued for ``apply``
    or disconnect, using the connector's normal flush order.

    Args:
        value: Template dictionary or JSON object string. ``CMND`` holds
            pipe-separated commands, with an optional ``Backlog`` prefix.
        activate: Select the template with ``Module 0`` and ensure its commands.
            If False, only store the template.

    Yields:
        At most one marker command when any value must change.
    """
    if _ensure_template(_connector(), value, activate=activate):
        yield _marker()


def _ensure_template(
    connector: TasmotaConnector, value: str | dict[str, Any], *, activate: bool = True
) -> bool:
    """Ensure a template and its activation commands, returning whether values changed."""
    data = json.loads(value) if isinstance(value, str) else dict(value)
    commands = data.pop("CMND", "")
    changed = _ensure(connector, "Template", json.dumps(data, separators=(",", ":")))
    if activate:
        changed = _ensure(connector, "Module", 0) or changed
        commands = re.sub(r"^\s*backlog\s+", "", commands, flags=re.IGNORECASE)
        for command in commands.split("|"):
            parts = command.split(None, 1)
            if not parts:
                continue
            subject = parts[0]
            payload = parts[1].strip() if len(parts) > 1 else ""
            changed = _ensure(connector, subject, payload) or changed
    return changed


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

    A ``Template`` entry is handled like the ``template`` operation with
    ``activate=True``, after the other commands. It ensures ``Module 0`` and
    the template's ``CMND`` commands. A separate ``Module 0`` entry is skipped.

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
        if _is_template_activation(subject, value):
            continue
        if is_rule(subject):
            index = _rule_index(subject)
            send_code, rule_changed = _ensure_rule_value(connector, index, value)
            if send_code is not None:
                rule_sends.append((f"Rule{index}", send_code))
            changed = rule_changed or changed
        else:
            changed = _ensure_any(connector, subject, value) or changed

    value = _template_value(commands)
    if value is not None:
        changed = _ensure_template(connector, value) or changed

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
        open_duration: Seconds to open fully, rounded to one decimal
            (``ShutterOpenDuration``).
        close_duration: Seconds to close fully, rounded to one decimal
            (``ShutterCloseDuration``).
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
        connector.record(subject, str(adapter_for(subject).prepare(settings[subject])))

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


def _template_value(commands: Mapping[str, Any]) -> Any | None:
    """Return the ``Template`` value from a host-data mapping, or ``None``."""
    for subject, value in commands.items():
        if _canonical(subject) == "template":
            return value
    return None


def _is_template_activation(subject: str, value: Any) -> bool:
    """Return whether a command belongs to template handling.

    ``Template`` stores a template and ``Module 0`` selects it, so they are
    handled together rather than sent as ordinary commands.
    """
    canonical = _canonical(subject)
    return canonical == "template" or (canonical == "module" and _normalize(value) == "0")


def _rule_index(subject: str) -> int:
    """Return the rule set number from a ``Rule<x>`` command name."""
    return int(_canonical(subject)[len("rule") :])


def _ensure_rule_value(
    connector: TasmotaConnector, index: int, value: Any
) -> tuple[str | None, bool]:
    """Ensure a rule set from a host-data value.

    The value is the rule text, a list of the text followed by flag payloads,
    or a mapping of the ``rule`` arguments.
    """
    if isinstance(value, dict):
        return _ensure_rule(connector, index, **value)
    if isinstance(value, (list, tuple)):
        if not value:
            return None, False
        code, *flags = value
        send_code, changed = _ensure_rule(connector, index, code)
        for flag in flags:
            changed = _ensure(connector, f"Rule{index}", flag) or changed
        return send_code, changed
    return _ensure_rule(connector, index, value)


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
        changed = _ensure_any(connector, subject, value) or changed
    return changed


def _ensure_any(connector: TasmotaConnector, subject: str, value: Any) -> bool:
    """Ensure one command, whose value may be a list of values.

    A list ensures each value in order, skipping the ones that already match.
    """
    if isinstance(value, (list, tuple)):
        changed = False
        for element in value:
            changed = _ensure(connector, subject, element) or changed
        return changed
    return _ensure(connector, subject, value)


def _needs_change(connector: TasmotaConnector, subject: str, value: Any) -> bool:
    """Return whether a command must be sent, reading the device if needed."""
    adapter = adapter_for(subject)
    desired = str(adapter.prepare(value))
    described = adapter.describe(value)
    label = f"{subject} {described}" if described else subject

    if adapter.skip:
        if connector.executing:
            connector.log_at(
                2, "%s: %s is masked, setting unconditionally", connector.host.name, label
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
                label,
                redact(subject, adapter.observed(value, observed)),
            )
        return False

    if connector.executing:
        if observed is None:
            connector.log_at(
                2,
                "%s: %s is unknown, will set to %s",
                connector.host.name,
                label,
                redact(subject, desired),
            )
        else:
            connector.log_at(
                2,
                "%s: %s is %s, will set to %s",
                connector.host.name,
                label,
                redact(subject, adapter.observed(value, observed)),
                redact(subject, desired),
            )
    return True


def _ensure(connector: TasmotaConnector, subject: str, value: Any) -> bool:
    """Ensure one command, returning whether a change was recorded."""
    if _needs_change(connector, subject, value):
        connector.record(subject, str(adapter_for(subject).prepare(value)))
        return True
    return False


@operation()
def restart(code: int = 1) -> Generator[PyinfraCommand, None, None]:
    """Apply pending commands, send ``Restart <code>``, and wait for the device.

    The operation uses the same reboot detection, timeout, and cache refresh
    as other reboot-triggering commands.

    Args:
        code: The Tasmota restart code, default ``1``.

    Yields:
        One command that applies pending changes, restarts, and waits.
    """

    def send(state: Any, host: Any) -> bool:
        connector = host.connector
        assert isinstance(connector, TasmotaConnector)
        connector.apply()
        connector.send_reboot("Restart", str(code))
        return True

    yield FunctionCommand(send, (), {})


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
