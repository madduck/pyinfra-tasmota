"""Deploys for the Tasmota connector."""

from __future__ import annotations

from pyinfra import host
from pyinfra.api.deploy import deploy

from .operations import apply, cmnd


@deploy("Ensure Tasmota settings")
def tasmota() -> None:
    """Ensure every command under the ``tasmota`` host-data key.

    The key holds a mapping of command name to a value or a list of values.
    A list sends the command once per value, in order, which suits commands
    that must be sent more than once, such as ``Rule1``. For example::

        {"tasmota": {"SetOption114": 1, "Rule1": ["ON ... ENDON", "1"]}}

    List values are sent unconditionally, so they always apply.
    """
    commands = host.data.get("tasmota", {})
    if not isinstance(commands, dict):
        raise TypeError("host data 'tasmota' must be a mapping of command to value")
    cmnd(**commands)
    apply()
