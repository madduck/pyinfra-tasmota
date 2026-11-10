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

cmnd("SetOption114", 1)
cmnd("SetOption120", 0)
apply()
```

The connector pulls `Status 0` and `Status 13` at connect, which populates a
cache of the device settings. `cmnd` compares the desired value with the
cache. It reads the current value from the device if the setting is not
cached, and records a pending change only if the value differs. Every command
is sent by `apply`, or on disconnect as a safety net.

Pending commands are flushed as `Backlog0` batches, wrapped in `SaveData 0`
and `SaveData 1` to limit flash wear. A device left with `SaveData` stuck at
`0` is repaired at connect.

## Reboots

Some commands reboot the device, for example `Restart`, `Module`, and
`Template`. The connector sends these commands on their own, after all other
commands, and waits for the device to answer again before continuing. A
device is considered back once it was seen to drop and is answering, or once
its `BootCount` has advanced.

If the device does not return within the timeout, `apply` reports an error.

## Connector data

Set these on a host in the inventory:

- `tasmota_friendly_name`: an optional human-readable name, used in logs.
- `tasmota_reboot_timeout`: seconds to wait for a rebooted device to answer
  again (default `60`).

The connector registers itself with Pyinfra under the name `tasmota`, so use
`@tasmota/<host>` anywhere a connector is expected.
