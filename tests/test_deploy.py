"""Tests for the Tasmota deploy."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyinfra_tasmota import deploy as deploy_module


class TestTasmotaDeploy:
    def _patch(self, monkeypatch: Any, data: dict[str, Any] | None = None) -> list[tuple[str, Any]]:
        calls: list[tuple[str, Any]] = []
        monkeypatch.setattr(deploy_module, "host", SimpleNamespace(data=data or {}))
        monkeypatch.setattr(
            deploy_module,
            "ensure_host_data",
            lambda **kwargs: calls.append(("ensure_host_data", kwargs)),
        )
        monkeypatch.setattr(
            deploy_module,
            "template",
            lambda *args, **kwargs: calls.append(("template", (args, kwargs))),
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

    def test_template_is_activated(self, monkeypatch):
        value = {"NAME": "x", "GPIO": [0, 1], "FLAG": 0, "BASE": 18}
        calls = self._patch(
            monkeypatch,
            data={"tasmota": {"Template": value, "Module": 0, "SetOption114": 1}},
        )
        deploy_module.tasmota._inner()
        assert calls == [
            ("ensure_host_data", {"send_passwords": True}),
            ("template", ((), {"value": value, "activate": True})),
            ("apply", None),
        ]

    def test_template_key_is_matched_canonically(self, monkeypatch):
        value = {"NAME": "x"}
        calls = self._patch(monkeypatch, data={"tasmota": {"template": value}})
        deploy_module.tasmota._inner()
        assert calls[1] == ("template", ((), {"value": value, "activate": True}))

    def test_no_template_skips_the_operation(self, monkeypatch):
        calls = self._patch(monkeypatch, data={"tasmota": {"Module": 1}})
        deploy_module.tasmota._inner()
        assert [name for name, _ in calls] == ["ensure_host_data", "apply"]

    def test_non_mapping_host_data_is_left_to_ensure_host_data(self, monkeypatch):
        calls = self._patch(monkeypatch, data={"tasmota": [1, 2]})
        deploy_module.tasmota._inner()
        assert [name for name, _ in calls] == ["ensure_host_data", "apply"]
