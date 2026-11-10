"""A Pyinfra connector to manage Tasmota devices via their HTTP API."""

from .connector import TasmotaConnector
from .deploy import tasmota

__all__ = ["TasmotaConnector", "tasmota"]
