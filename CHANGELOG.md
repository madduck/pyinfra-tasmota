# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-11-10

### Added

- A data-driven `tasmota()` deploy. It ensures every command under the `tasmota`
  host-data key and then applies, so a host inventory can describe its settings
  as a mapping of command name to value (or list of values).
- A `template` operation that stores a template and activates it by default.
  Activation selects `Module 0` and runs the template's `CMND` commands. Pass
  `activate=False` to store the template without selecting or running it.
- A `shutter` operation that enables shutter support, restarts the device, waits
  for it to return, then sets the interlock, mode, relay, button, and travel
  durations.
- A `rule` operation. Rule text can contain semicolons, so it is sent as its own
  request rather than in a backlog, and the flags (`enabled`, `once`,
  `stop_on_error`, `append`) are sent as ordinary commands.
- An `ensure_host_data` operation that reads the host-data key inside its
  generator, so a multi-host inventory registers one operation instead of one
  per host.
- A `send_passwords` option on `tasmota()`. It defaults to `True`; pass `False`
  to leave `Password1`, `Password2`, and `MqttPassword` out of a run.

### Changed

- `Rule<x>` and `Template` host-data entries are routed through the `rule` and
  `template` operations instead of being sent as ordinary commands.
- List values are ensured element-wise, so each value is diffed rather than sent
  unconditionally.
- The verbose logs mask password values as `***`.
- New comparison adapters reduce needless sends:
  - `AdcParam<x>` compares parameter lists and ignores one optional trailing
    zero in the device response.
  - `Template` compares as JSON, ignoring the device's `ARCH` field.
  - `Interlock` compares a boolean against the enabled state and the group list
    otherwise.
  - `Rule<x>` compares the text and the enable, once, and stop-on-error flags.
  - `ShutterButton<x>` matches on prefix, because the device answers with an
    expanded configuration.
  - Shutter durations are compared at tenths of a second, and interlock groups
    as sets of relay numbers.

### Fixed

- `Latitude` and `Longitude` now converge. The device stores them as
  micro-degrees using 32-bit float, which loses the last digit, so they are
  compared with a small tolerance.
- Shutter durations are rounded to one decimal place (half up) before they are
  sent, so values such as `32.26` no longer get floored by the device.
- Shutter commands whose index is optional (`ShutterCloseDuration`,
  `ShutterButton`, `Pulsetime`) are now matched with or without the index.
- Reboot-triggering commands may be read again. The old single set conflated
  "isolate and await on write" with "never read", so readable reboot settings
  such as `FullTopic` were not diffed.
- The rule diff log now names the compared field ("Rule1 text", "Rule1 State"),
  so a repair no longer reads as "already, skipping" before a send.

## [0.1.0] — 2026-11-10

Initial release.
