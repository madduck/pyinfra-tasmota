"""Deploys for the Tasmota connector."""

from __future__ import annotations

from pyinfra.api.deploy import deploy

from .operations import apply, ensure_host_data


@deploy("Ensure Tasmota settings")
def tasmota(send_passwords: bool = True) -> None:
    """Ensure every command under the ``tasmota`` host-data key.

    The key holds a mapping of command name to a value or a list of values.
    A list sends the command once per value, in order, which suits commands
    that must be sent more than once, such as ``Rule1``. For example::

        {"tasmota": {"SetOption114": 1, "Rule1": ["ON ... ENDON", "1"]}}

    List values are sent unconditionally, so they always apply.

    Args:
        send_passwords: When False, drop password commands (``Password1``,
            ``Password2``, ``MqttPassword``) before applying, so credentials
            can be left out of a routine run.
    """
    ensure_host_data(send_passwords=send_passwords)
    apply()
