"""Shared fixtures for the pyinfra-tasmota tests."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from pyinfra_tasmota.connector import TasmotaConnector


class CatchAll(dict[str, Any]):
    """A mapping that reports every key as present."""


class FakeResponse:
    """A minimal stand-in for a ``requests.Response``."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
    """A ``requests.Session`` that answers from a canned mapping."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requests: list[str] = []
        self.closed = False

    def get(self, url: str, timeout: int = 0) -> FakeResponse:
        from urllib.parse import parse_qs, urlparse

        self.requests.append(url)
        cmnd = parse_qs(urlparse(url).query).get("cmnd", [""])[0]
        if cmnd in self.responses:
            return FakeResponse(self.responses[cmnd])
        if isinstance(self.responses, CatchAll):
            return FakeResponse({})
        raise requests.ConnectionError(f"no canned reply for {cmnd!r}")

    def close(self) -> None:
        self.closed = True


class FakeHost:
    def __init__(self, name: str = "tasmota.example.org", **data: Any) -> None:
        self.name = name
        self.data = dict(data)
        self.connector: TasmotaConnector | None = None


@pytest.fixture
def status0():
    """A representative ``Status 0`` reply."""
    return {
        "Status": {
            "Module": 1,
            "DeviceName": "Example Sonoff",
            "FriendlyName": ["Kitchen"],
            "Topic": "tasmota_abc",
            "Power": "OFF",
            "SaveData": 1,
            "SetOption114": 0,
        },
        "StatusPRM": {"Baudrate": 115200, "RestartReason": "Power On"},
        "StatusFWR": {"Version": "14.0.0"},
        "StatusNET": {"Hostname": "tasmota-abc"},
    }


@pytest.fixture
def make_connector():
    """Build a connector wired to a :class:`FakeSession`."""

    def factory(responses: dict[str, Any], **host_data: Any) -> TasmotaConnector:
        host = FakeHost(**host_data)
        connector = TasmotaConnector(state=None, host=host)
        connector.session = FakeSession(responses)  # type: ignore[assignment]
        host.connector = connector
        return connector

    return factory
