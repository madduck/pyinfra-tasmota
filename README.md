# pyinfra-tasmota

A [Pyinfra](https://pyinfra.com) connector for Tasmota devices.

The connector talks to a Tasmota device over its HTTP API. It reads the
device settings once at connect time, compares operation intent against
that cache, and sends only the commands that are needed as batched
`Backlog0` requests.

## Install

```sh
uv add pyinfra-tasmota
```

## Usage

Inventory:

```py
hosts = ["@tasmota/tasmota.example.org"]
```

Deploy:

```py
from pyinfra_tasmota.operations import apply, cmnd

cmnd(SetOption114=1, SetOption120=0, Power="toggle")
apply()
```

`cmnd` takes any number of commands as keyword arguments. A value is either a
single value or a list of values. A list ensures each value in order, which
suits commands that carry more than one value, such as `Interlock 1,2` and
`Interlock ON`:

```py
cmnd(Interlock=["1,2", "ON"], SetOption114=1)
```

`cmnd` compares each desired value with the cache. It reads the current value
from the device if the setting is not cached, and records a pending change
only if the value differs. Every command is sent by `apply`, or on disconnect
as a safety net.

Command names are matched case-insensitively and resolve the `SO` alias of
`SetOption`, so `SetOption114`, `setoption114`, and `so114` are the same
command. Boolean values are folded, so a desired `0` matches a device value
of `OFF`, and `1` matches `ON`.

Non-rebooting commands are flushed as `Backlog0` batches, split to stay
within Tasmota's limit of 30 commands and decode-config's limit of 320
characters per command line. Reboot-triggering commands are sent on their own.

## Comparison

`cmnd` compares the desired value with the device response. A few commands
answer with something other than the desired value, so they are compared
through an adapter:

- `TimeSTD` and `TimeDST` take a comma-separated list but answer with a
  struct, which is flattened for the comparison.
- `Pulsetime<x>` compares only its `Set` field.
- `Latitude` and `Longitude` are compared with a small tolerance, because the
  device stores them as micro-degrees using 32-bit float, which loses the last
  digit.
- `Template` is compared as JSON, ignoring `ARCH` (device architecture) and
  `CMND` (commands to run when activating the template).
- `Password1`, `Password2`, and `MqttPassword` answer with `***`, so their
  diff is skipped and they are always sent.
- `FullTopic 1` selects the firmware default, `%prefix%/%topic%/`, which is
  what the desired value is expanded to before comparison.

## Deploy

The package ships a deploy that ensures everything under the `tasmota`
host-data key:

```py
# deploy.py
from pyinfra_tasmota import tasmota

tasmota()
```

```py
# inventory.py
hosts = (
    ["@tasmota/tasmota.example.org"],
    {
        "tasmota": {
            "SetOption114": 1,
            "Rule1": {"code": "ON clock DO power ENDON", "enabled": True},
        },
    },
)
```

The key holds a mapping of command name to a value or a list of values. The
deploy ensures each command and then calls `apply`.

`Rule<x>` keys are handled like the `rule` operation rather than as ordinary
commands. The value is the rule text, a list of the text followed by flag
payloads (`Rule1: [code, "1"]`), or a mapping with `code` and the flags
(`enabled`, `once`, `stop_on_error`, `append`). Their text is sent directly,
because it can contain semicolons.

Password commands are sent by default. Call `tasmota(send_passwords=False)` to
leave `Password1`, `Password2`, and `MqttPassword` out of the run.

## Reboots

Some commands reboot the device, for example `Restart`, `Module`, and
`Template`. The connector sends these commands on their own, after all other
commands, and waits for the device to answer again before continuing. A
device is considered back once it was seen to drop and is answering, or once
its `BootCount` has advanced.

Reboot-triggering settings are still read and diffed like any other command.
Only commands whose bare form acts on the device or reports no value, such as
`Backlog` or `Restart`, are sent without a read.

If the device does not return within the timeout, `apply` reports an error.

## Templates

The `template` operation stores a template and activates it by default:

```py
from pyinfra_tasmota.operations import apply, template

template(
    {
        "NAME": "Custom relay",
        "GPIO": [0, 0, 0, 0, 224, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        "FLAG": 0,
        "BASE": 18,
        "CMND": "SetOption114 1|Power ON",
    }
)
apply()
```

The value can be a dictionary or a JSON object string. The operation compares
and queues `Template`, then ensures `Module 0` to select the template. It also
ensures each pipe-separated (`|`) command in `CMND`. An optional `Backlog`
prefix is accepted. Commands keep their arguments, including spaces.

`CMND` is removed from the template sent to the device. Its commands and
`Module 0` are compared even when the stored template already matches.
Commands whose values already match are skipped.

With `template(value, activate=False)`, only the template is stored. The
operation neither selects `Module 0` nor runs the `CMND` commands.

All changes use the existing queue and need `apply()` or disconnect to send
them. The connector sends ordinary commands before reboot-triggering commands.
Thus ordinary `CMND` commands run before `Template` and `Module`, not after
activation. Although `CMND` uses pipes as separators, the queue uses semicolons
for `Backlog0` batches. Do not put arguments that contain semicolons in this
queue, such as complex rule text. Use `rule` for those rules.

## Shutters

The `shutter` operation configures a shutter. Shutter support has to be
enabled first, and the device restarted for that to take effect, so the
operation enables `SetOption80`, restarts the device, waits for it to return,
and only then sends the remaining settings:

```py
from pyinfra_tasmota.operations import apply, shutter

shutter(open_duration=12, close_duration=12, relay=1, button=4)
apply()
```

It sets `Interlock <pair>` and `Interlock ON` first (the shutter mode needs
the interlock), then `ShutterMode`, `ShutterRelay<x>`, `ShutterButton<x>`
(when given), `ShutterOpenDuration<x>`, and `ShutterCloseDuration<x>`.
`index` selects the shutter and defaults to `1`. Settings that already match
are skipped, and the device is only restarted when shutter support has to be
enabled. Tasmota stores the travel durations as tenths of a second, so the
durations are rounded to one decimal place before they are sent.

## Rules

Rule text contains semicolons, which separate `Backlog` commands, so it
cannot be sent in a batch. The `rule` operation sends the text as its own
request and the flags as ordinary commands:

```py
from pyinfra_tasmota.operations import apply, rule

rule("ON clock#Timer=1 DO Power1 ON ENDON", enabled=True, once=False)
apply()
```

`index` selects the rule set and defaults to `1`. `enabled`, `once`, and
`stop_on_error` set the matching flag when given, and `append` adds to the set
instead of replacing it. The text already has semicolons, so the operation
never puts it in a backlog.

## Verbosity

The connector follows pyinfra's `-v` flags:

- `-v` reports every command sent to the device.
- `-vv` also reports the observed state of each command, and any command
  skipped because the device already matches the desired value.
- `-vvv` also reports internal detail, such as the size of the status cache
  and how commands are grouped into `Backlog0` batches.

Secret values (passwords) are masked as `***` in these logs.

## Versioning

The package version comes from Git via `uv-dynamic-versioning`. Tag a release
as `vX.Y.Z`; commits after a tag get a `.postN.dev0+<hash>` suffix. A checkout
with no tags reports `0.0.0`.

## Connector data

Set these on a host in the inventory:

- `tasmota_friendly_name`: an optional human-readable name, used in logs.
- `tasmota_reboot_timeout`: seconds to wait for a rebooted device to answer
  again (default `60`).

The connector registers itself with Pyinfra under the name `tasmota`, so use
`@tasmota/<host>` anywhere a connector is expected.
