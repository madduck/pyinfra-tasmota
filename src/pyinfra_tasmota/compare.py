"""Per-command value comparison for the diff.

Most commands echo their value as a scalar, so the desired value can be
compared with the response directly. Some do not: they answer with a struct,
mask secrets, or accept a shortcut that expands to a different stored value.
An :class:`Adapter` maps both sides to a comparable form for those commands.
"""

from __future__ import annotations

import re
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from .connector import _canonical, _normalize

# Fields of the TimeSTD/TimeDST response, in the order the command accepts.
_TIME_FIELDS = ("Hemisphere", "Week", "Month", "Day", "Hour", "Offset")
# The firmware default FullTopic (MQTT_FULLTOPIC).
_MQTT_FULLTOPIC = "%prefix%/%topic%/"


class Adapter:
    """Maps a command's desired and observed values to a comparable form."""

    skip: bool = False

    def desired(self, value: str) -> str:
        """Return the comparable form of a desired value."""
        return _normalize(value)

    def observed(self, value: Any) -> str:
        """Return the comparable form of an observed value."""
        return _normalize(value)


class _Skip(Adapter):
    """A command whose observed value is masked, so it cannot be diffed."""

    skip = True


class _Struct(Adapter):
    """Flatten a nested response struct to the command's comma-separated form."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        self._fields = fields

    def desired(self, value: str) -> str:
        return ",".join(part.strip() for part in str(value).split(","))

    def observed(self, value: Any) -> str:
        if isinstance(value, dict):
            return ",".join(_normalize(value.get(field)) for field in self._fields)
        return _normalize(value)


class _Field(Adapter):
    """Compare only one field of a nested response struct."""

    def __init__(self, field: str) -> None:
        self._field = field

    def observed(self, value: Any) -> str:
        if isinstance(value, dict):
            return _normalize(value.get(self._field))
        return _normalize(value)


class _Decimal(Adapter):
    """Compare numbers truncated to a number of decimal places.

    Tasmota stores Latitude and Longitude as ``(int)(value * 10**6)``, which
    truncates rather than rounds, so this truncates too, to converge.
    """

    def __init__(self, digits: int) -> None:
        self._quantum = Decimal(1).scaleb(-digits)

    def _truncate(self, value: Any) -> str:
        try:
            return str(Decimal(str(value)).quantize(self._quantum, rounding=ROUND_DOWN))
        except InvalidOperation:
            return _normalize(value)

    def desired(self, value: str) -> str:
        return self._truncate(value)

    def observed(self, value: Any) -> str:
        return self._truncate(value)


class _Alias(Adapter):
    """Expand a shortcut desired value to the value the device stores."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self._aliases = aliases

    def desired(self, value: str) -> str:
        return _normalize(self._aliases.get(_normalize(value), value))


_DEFAULT = Adapter()
_PULSETIME = _Field("Set")
_MASKED = _Skip()

_ADAPTERS: dict[str, Adapter] = {
    "timestd": _Struct(_TIME_FIELDS),
    "timedst": _Struct(_TIME_FIELDS),
    "latitude": _Decimal(6),
    "longitude": _Decimal(6),
    "mqttpassword": _Skip(),
    "fulltopic": _Alias({"1": _MQTT_FULLTOPIC}),
}

_PULSETIME_PATTERN = re.compile(r"pulsetime\d+$")
_PASSWORD_PATTERN = re.compile(r"password\d+$")


def adapter_for(command: str) -> Adapter:
    """Return the comparison adapter for a command name.

    Names are matched canonically, so case and the ``SO`` alias do not matter.
    Commands without a specific adapter fold booleans through the default.
    """
    canonical = _canonical(command)
    adapter = _ADAPTERS.get(canonical)
    if adapter is not None:
        return adapter
    if _PULSETIME_PATTERN.fullmatch(canonical):
        return _PULSETIME
    if _PASSWORD_PATTERN.fullmatch(canonical):
        return _MASKED
    return _DEFAULT
