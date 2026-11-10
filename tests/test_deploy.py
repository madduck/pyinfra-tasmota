"""Tests for the Tasmota deploy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyinfra_tasmota import deploy as deploy_module


class TestTasmotaDeploy:
    def _patch(self, monkeypatch: Any, data: dict[str, Any]) -> list[tuple[str, Any]]:
        calls: list[tuple[str, Any]] = []
        monkeypatch.setattr(deploy_module, "host", SimpleNamespace(data=data))
        monkeypatch.setattr(deploy_module, "cmnd", lambda **kwargs: calls.append(("cmnd", kwargs)))
        monkeypatch.setattr(deploy_module, "apply", lambda: calls.append(("apply", None)))
        return calls

    def test_ensures_declared_commands(self, monkeypatch):
        calls = self._patch(monkeypatch, {"tasmota": {"SetOption114": 1}})
        deploy_module.tasmota._inner()
        assert calls == [("cmnd", {"SetOption114": 1}), ("apply", None)]

    def test_missing_key_is_empty(self, monkeypatch):
        calls = self._patch(monkeypatch, {})
        deploy_module.tasmota._inner()
        assert calls == [("cmnd", {}), ("apply", None)]

    def test_rejects_non_mapping(self, monkeypatch):
        self._patch(monkeypatch, {"tasmota": [1, 2]})
        with pytest.raises(TypeError):
            deploy_module.tasmota._inner()
