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

`cmnd` takes any number of commands as keyword arguments. It compares each
desired value with the cache. It reads the current value from the device if
the setting is not cached, and records a pending change only if the value
differs. Every command is sent by `apply`, or on disconnect as a safety net.

Command names are matched case-insensitively and resolve the `SO` alias of
`SetOption`, so `SetOption114`, `setoption114`, and `so114` are the same
command. Boolean values are folded, so a desired `0` matches a device value
of `OFF`, and `1` matches `ON`.

Non-rebooting commands are flushed as `Backlog0` batches, split to stay
within Tasmota's limit of 30 commands and decode-config's limit of 320
characters per command line. Reboot-triggering commands are sent on their own.

## Reboots

Some commands reboot the device, for example `Restart`, `Module`, and
`Template`. The connector sends these commands on their own, after all other
commands, and waits for the device to answer again before continuing. A
device is considered back once it was seen to drop and is answering, or once
its `BootCount` has advanced.

If the device does not return within the timeout, `apply` reports an error.

## Verbosity

The connector follows pyinfra's `-v` flags:

- `-v` reports every command sent to the device.
- `-vv` also reports the observed state of each command, and any command
  skipped because the device already matches the desired value.
- `-vvv` also reports internal detail, such as the size of the status cache
  and how commands are grouped into `Backlog0` batches.

## Connector data

Set these on a host in the inventory:

- `tasmota_friendly_name`: an optional human-readable name, used in logs.
- `tasmota_reboot_timeout`: seconds to wait for a rebooted device to answer
  again (default `60`).

The connector registers itself with Pyinfra under the name `tasmota`, so use
`@tasmota/<host>` anywhere a connector is expected.
