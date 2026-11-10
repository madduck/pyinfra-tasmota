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
single value or a list of values. A list sends the command once per value, in
order, for commands that must be sent more than once, such as `Rule1`:

```py
cmnd(Rule1=["ON clock DO power ENDON", "1"], SetOption114=1)
```

`cmnd` compares each desired value with the cache. It reads the current value
from the device if the setting is not cached, and records a pending change
only if the value differs. List values are never diffed, so they always
apply. Every command is sent by `apply`, or on disconnect as a safety net.

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
- `Template` is compared as JSON, ignoring its `ARCH` field, which the device
  adds but the desired value does not carry.
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
        "tasmota": {"SetOption114": 1, "Rule1": ["ON clock DO power ENDON", "1"]},
    },
)
```

The key holds a mapping of command name to a value or a list of values. The
deploy ensures each command and then calls `apply`.

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
