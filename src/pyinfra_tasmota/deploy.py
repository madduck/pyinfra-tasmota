"""Deploys for the Tasmota connector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyinfra.api.deploy import deploy
from pyinfra.context import host

from .connector import _canonical
from .operations import apply, ensure_host_data, template

if TYPE_CHECKING:
    from collections.abc import Mapping


@deploy("Ensure Tasmota settings")
def tasmota(send_passwords: bool = True) -> None:
    """Ensure every command under the ``tasmota`` host-data key.

    The key holds a mapping of command name to a value or a list of values.
    A list sends the command once per value, in order, which suits commands
    that must be sent more than once, such as ``Rule1``. For example::

        {"tasmota": {"SetOption114": 1, "Rule1": ["ON ... ENDON", "1"]}}

    List values are sent unconditionally, so they always apply.

    A ``Template`` entry is handled by the ``template`` operation with
    ``activate=True`` instead of being sent as an ordinary command, and the
    ``Module 0`` that selects it is dropped, because the operation sets it.

    Args:
        send_passwords: When False, drop password commands (``Password1``,
            ``Password2``, ``MqttPassword``) before applying, so credentials
            can be left out of a routine run.
    """
    commands = host.data.get("tasmota", {})
    value = _template_value(commands) if isinstance(commands, dict) else None
    ensure_host_data(send_passwords=send_passwords)
    if value is not None:
        template(value=value, activate=True)
    apply()


def _template_value(commands: Mapping[str, Any]) -> Any | None:
    """Return the ``Template`` value from a host-data mapping, or ``None``."""
    for subject, value in commands.items():
        if _canonical(subject) == "template":
            return value
    return None
