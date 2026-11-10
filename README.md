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
from pyinfra_tasmota import TasmotaConnector

hosts = ["@tasmota/tasmota.example.org"]
```

Deploy:

```py
from pyinfra import host
from pyinfra_tasmota.operations import cmnd

cmnd("SetOption114", 1)
```

The connector pulls `Status 0` (and `Status 13`) at connect. `cmnd` compares
the desired value with the cache. It reads the current value from the device
if the setting is not cached, and records a pending change only if the value
differs. Pending changes are flushed in `Backlog0` batches on disconnect,
wrapped in `SaveData 0` and `SaveData 1` to limit flash wear.

## Connector data

Set these on a host in the inventory:

- `tasmota_friendly_name`: an optional human-readable name, used in logs.

The connector registers itself with Pyinfra under the name `tasmota`, so use
`@tasmota/<host>` anywhere a connector is expected.
