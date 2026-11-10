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
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
    # True when the comparison needs the whole response object rather than the
    # value under the command's key, as Template and Interlock do.
    whole: bool = False

    def matches(self, desired: Any, observed: Any) -> bool:
        """Return whether ``observed`` already reaches ``desired``."""
        return _normalize(observed) == _normalize(desired)

    def describe(self, desired: Any) -> str:
        """Return a short label for the part of the value being compared.

        Most commands compare the whole value, so the default is empty. The
        rule adapter overrides this to name the field, which keeps its text
        and flag comparisons distinguishable in the log.
        """
        return ""

    def observed(self, desired: Any, observed: Any) -> Any:
        """Return the part of ``observed`` that the comparison uses.

        Most commands compare the whole value, so the default is the value
        itself. The rule adapter narrows this to the field it compares, which
        keeps the log readable.
        """
        return observed

    def prepare(self, desired: Any) -> Any:
        """Return the value to record and send for ``desired``.

        Most commands send the value as given. Commands whose device keeps
        less precision override this, so the sent value matches the one the
        comparison uses.
        """
        return desired


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


class _AdcParam(Adapter):
    """Compare comma-separated ADC parameters with a response list.

    The device can include a trailing zero that the desired value omits.
    Only that extra zero is ignored, not a nonzero parameter.
    """

    def matches(self, desired: Any, observed: Any) -> bool:
        if not isinstance(observed, list):
            return super().matches(desired, observed)
        wanted = [_normalize(part) for part in str(desired).split(",")]
        actual = [_normalize(part) for part in observed]
        return actual in (wanted, [*wanted, "0"])


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


class _Decimal(Adapter):
    """Compare numbers rounded to a number of decimal places.

    Tasmota stores the shutter durations as tenths of a second, so it keeps
    only one decimal place. The device floors when it stores a value, so the
    desired value is rounded here; the sent value follows the rounded form.
    """

    def __init__(self, decimals: int) -> None:
        self._quantum = Decimal(1).scaleb(-decimals)

    def _round(self, value: Any) -> str:
        try:
            return str(Decimal(str(value)).quantize(self._quantum, rounding=ROUND_HALF_UP))
        except InvalidOperation:
            return _normalize(value)

    def matches(self, desired: Any, observed: Any) -> bool:
        return self._round(desired) == self._round(observed)

    def prepare(self, desired: Any) -> str:
        return self._round(desired)


class _Prefix(Adapter):
    """Compare when the device's value starts with the desired value.

    ``ShutterButton`` answers with the expanded button configuration, of which
    the desired value is a prefix.
    """

    def matches(self, desired: Any, observed: Any) -> bool:
        if isinstance(observed, dict):
            return _normalize(observed) == _normalize(desired)
        return _normalize(observed).startswith(_normalize(desired))


class _Alias(Adapter):
    """Expand a shortcut desired value to the value the device stores."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self._aliases = aliases

    def matches(self, desired: Any, observed: Any) -> bool:
        wanted = self._aliases.get(_normalize(desired), desired)
        return _normalize(observed) == _normalize(wanted)


class _Json(Adapter):
    """Compare JSON values, ignoring listed keys."""

    def __init__(self, ignore: tuple[str, ...] = (), whole: bool = False) -> None:
        self._ignore = frozenset(ignore)
        self.whole = whole

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


class _Interlock(Adapter):
    """Compare an interlock group list or its enabled state.

    ``Interlock`` answers with both at the top level, e.g.
    ``{"Interlock":"ON","Groups":"1,2"}``. A boolean desired value compares
    with the enabled state, anything else with the group list.
    """

    whole = True

    def matches(self, desired: Any, observed: Any) -> bool:
        if not isinstance(observed, dict):
            return _normalize(observed) == _normalize(desired)
        wanted = _normalize(desired)
        if wanted in ("0", "1"):
            return _normalize(observed.get("Interlock")) == wanted
        return _groups(desired) == _groups(observed.get("Groups"))


def _groups(value: Any) -> list[frozenset[int]]:
    """Return interlock groups as sets of relay numbers."""
    normalized = re.sub(r"\s*,\s*", ",", str(value).strip())
    return [
        frozenset(int(part) for part in group.split(",") if part.isdigit())
        for group in normalized.split()
    ]


# Rule flags map ``Rule<x>`` payloads to the response field they change.
_RULE_FLAGS = {
    "0": ("State", "0"),
    "1": ("State", "1"),
    "4": ("Once", "0"),
    "5": ("Once", "1"),
    "8": ("StopOnError", "0"),
    "9": ("StopOnError", "1"),
}


class _Rule(Adapter):
    """Compare a rule set's text or one of its flags.

    ``Rule<x>`` answers with ``State`` (enabled), ``Once``, ``StopOnError`` and
    ``Rules`` (the text). The desired value is a flag (``0``/``1`` enable,
    ``4``/``5`` once, ``8``/``9`` stop-on-error) or the rule text.
    """

    def matches(self, desired: Any, observed: Any) -> bool:
        if not isinstance(observed, dict):
            return _normalize(observed) == _normalize(desired)
        wanted = _normalize(desired)
        flag = _RULE_FLAGS.get(wanted)
        if flag is not None:
            field, expected = flag
            return _normalize(observed.get(field)) == expected
        return _normalize(observed.get("Rules")) == wanted

    def describe(self, desired: Any) -> str:
        flag = _RULE_FLAGS.get(_normalize(desired))
        return flag[0] if flag is not None else "text"

    def observed(self, desired: Any, observed: Any) -> Any:
        if not isinstance(observed, dict):
            return observed
        flag = _RULE_FLAGS.get(_normalize(desired))
        return observed.get(flag[0] if flag is not None else "Rules")


_DEFAULT = Adapter()
_PULSETIME = _Field("Set")
_MASKED = _Skip()
_RULE = _Rule()

_ADAPTERS: dict[str, Adapter] = {
    "timestd": _Struct(_TIME_FIELDS),
    "timedst": _Struct(_TIME_FIELDS),
    "latitude": _Approximate(_COORDINATE_TOLERANCE),
    "longitude": _Approximate(_COORDINATE_TOLERANCE),
    "mqttpassword": _MASKED,
    "fulltopic": _Alias({"1": _MQTT_FULLTOPIC}),
    "template": _Json(ignore=("ARCH", "CMND"), whole=True),
    "interlock": _Interlock(),
}

# Shutter travel durations are stored as tenths of a second, so the device
# keeps one decimal place. ShutterButtons answer with an expanded config of
# which the desired value is a prefix.
_SHUTTER_DURATION = _Decimal(1)
_SHUTTER_DURATION_PATTERN = re.compile(r"shutter(?:open|close)duration\d*$")
_SHUTTER_BUTTON = _Prefix()
_SHUTTER_BUTTON_PATTERN = re.compile(r"shutterbutton\d*$")

_ADC_PARAM = _AdcParam()
_ADC_PARAM_PATTERN = re.compile(r"adcparam\d+$")

_PULSETIME_PATTERN = re.compile(r"pulsetime\d*$")
_RULE_PATTERN = re.compile(r"rule\d+$")


# Indexed commands, matched by pattern rather than exact name.
_PATTERNS = (
    (_ADC_PARAM_PATTERN, _ADC_PARAM),
    (_PULSETIME_PATTERN, _PULSETIME),
    (_SHUTTER_DURATION_PATTERN, _SHUTTER_DURATION),
    (_SHUTTER_BUTTON_PATTERN, _SHUTTER_BUTTON),
    (_RULE_PATTERN, _RULE),
)


def adapter_for(command: str) -> Adapter:
    """Return the comparison adapter for a command name.

    Names are matched canonically, so case and the ``SO`` alias do not matter.
    Commands without a specific adapter fold booleans through the default.
    """
    canonical = _canonical(command)
    adapter = _ADAPTERS.get(canonical)
    if adapter is not None:
        return adapter
    for pattern, candidate in _PATTERNS:
        if pattern.fullmatch(canonical):
            return candidate
    if is_secret(command):
        return _MASKED
    return _DEFAULT


def is_rule(command: str) -> bool:
    """Return whether a command names a rule set (``Rule1``, ``Rule2``, ...)."""
    return _RULE_PATTERN.fullmatch(_canonical(command)) is not None
