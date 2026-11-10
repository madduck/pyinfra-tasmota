"""Tests for the Tasmota deploy."""

from __future__ import annotations

from typing import Any

from pyinfra_tasmota import deploy as deploy_module


class TestTasmotaDeploy:
    def _patch(self, monkeypatch: Any) -> list[tuple[str, Any]]:
        calls: list[tuple[str, Any]] = []
        monkeypatch.setattr(
            deploy_module,
            "ensure_host_data",
            lambda **kwargs: calls.append(("ensure_host_data", kwargs)),
        )
        monkeypatch.setattr(deploy_module, "apply", lambda: calls.append(("apply", None)))
        return calls

    def test_ensures_host_data_then_applies(self, monkeypatch):
        calls = self._patch(monkeypatch)
        deploy_module.tasmota._inner()
        assert calls == [
            ("ensure_host_data", {"send_passwords": True}),
            ("apply", None),
        ]

    def test_send_passwords_is_forwarded(self, monkeypatch):
        calls = self._patch(monkeypatch)
        deploy_module.tasmota._inner(send_passwords=False)
        assert calls == [
            ("ensure_host_data", {"send_passwords": False}),
            ("apply", None),
        ]
