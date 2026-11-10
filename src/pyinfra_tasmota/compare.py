"""Per-command value comparison for the diff.

Most commands echo their value as a scalar, so the desired value can be
compared with the response directly. Some do not: they answer with a struct or
JSON, mask secrets, hold a value with limited precision, or accept a shortcut
that expands to a different stored value. An :class:`Adapter` decides whether
the observed value already reaches the desired one for those commands.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .connector import _canonical, _normalize, is_secret

# Fields of the TimeSTD/TimeDST response, in the order the command accepts.
_TIME_FIELDS = ("Hemisphere", "Week", "Month", "Day", "Hour", "Offset")
# The firmware default FullTopic (MQTT_FULLTOPIC).
_MQTT_FULLTOPIC = "%prefix%/%topic%/"

# Latitude and Longitude are stored as ``int(value * 10**6)`` with 32-bit
# float arithmetic, so the stored value can differ from the requested one by
# several micro-degrees. Comparison tolerates that difference.
_COORDINATE_TOLERANCE = 5e-5


class Adapter:
    """Decides whether an observed value already reaches a desired one."""

    skip: bool = False
    # True when a command answers with the value itself rather than an object
    # keyed by the command name, as Template does.
    unwrapped: bool = False

    def matches(self, desired: Any, observed: Any) -> bool:
        """Return whether ``observed`` already reaches ``desired``."""
        return _normalize(observed) == _normalize(desired)


class _Skip(Adapter):
    """A command whose observed value is masked, so it cannot be diffed."""

    skip = True


class _Struct(Adapter):
    """Compare a comma-separated value with a nested response struct."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        self._fields = fields

    def matches(self, desired: Any, observed: Any) -> bool:
        if isinstance(observed, dict):
            observed = ",".join(_normalize(observed.get(field)) for field in self._fields)
        wanted = ",".join(part.strip() for part in str(desired).split(","))
        return _normalize(observed) == _normalize(wanted)


class _Field(Adapter):
    """Compare only one field of a nested response struct."""

    def __init__(self, field: str) -> None:
        self._field = field

    def matches(self, desired: Any, observed: Any) -> bool:
        if isinstance(observed, dict):
            observed = observed.get(self._field)
        return _normalize(observed) == _normalize(desired)


class _Approximate(Adapter):
    """Compare numbers within a tolerance."""

    def __init__(self, tolerance: float) -> None:
        self._tolerance = tolerance

    def matches(self, desired: Any, observed: Any) -> bool:
        try:
            return abs(float(desired) - float(observed)) <= self._tolerance
        except (TypeError, ValueError):
            return _normalize(observed) == _normalize(desired)


class _Alias(Adapter):
    """Expand a shortcut desired value to the value the device stores."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self._aliases = aliases

    def matches(self, desired: Any, observed: Any) -> bool:
        wanted = self._aliases.get(_normalize(desired), desired)
        return _normalize(observed) == _normalize(wanted)


class _Json(Adapter):
    """Compare JSON values, ignoring listed keys."""

    def __init__(self, ignore: tuple[str, ...] = (), unwrapped: bool = False) -> None:
        self._ignore = frozenset(ignore)
        self.unwrapped = unwrapped

    def _canonical(self, value: Any) -> str:
        data = value if isinstance(value, (dict, list)) else json.loads(str(value))
        if isinstance(data, dict):
            data = {key: item for key, item in data.items() if key not in self._ignore}
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def matches(self, desired: Any, observed: Any) -> bool:
        try:
            return self._canonical(desired) == self._canonical(observed)
        except (TypeError, ValueError):
            return _normalize(observed) == _normalize(desired)


_DEFAULT = Adapter()
_PULSETIME = _Field("Set")
_MASKED = _Skip()

_ADAPTERS: dict[str, Adapter] = {
    "timestd": _Struct(_TIME_FIELDS),
    "timedst": _Struct(_TIME_FIELDS),
    "latitude": _Approximate(_COORDINATE_TOLERANCE),
    "longitude": _Approximate(_COORDINATE_TOLERANCE),
    "mqttpassword": _MASKED,
    "fulltopic": _Alias({"1": _MQTT_FULLTOPIC}),
    "template": _Json(ignore=("ARCH",), unwrapped=True),
}

# Shutter travel durations are stored as tenths of a second and read back as
# a float (12.0 for 12), so compare them numerically.
_SHUTTER_DURATION = _Approximate(0.05)
_SHUTTER_DURATION_PATTERN = re.compile(r"shutter(?:open|close)duration\d+$")

_PULSETIME_PATTERN = re.compile(r"pulsetime\d+$")


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
    if _SHUTTER_DURATION_PATTERN.fullmatch(canonical):
        return _SHUTTER_DURATION
    if is_secret(command):
        return _MASKED
    return _DEFAULT
