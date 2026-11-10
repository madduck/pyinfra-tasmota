"""Tests for the Tasmota operations."""

from __future__ import annotations

import pytest

from pyinfra_tasmota import operations
from pyinfra_tasmota.operations import apply, cmnd
from tests.conftest import CatchAll


class FakeHost:
    def __init__(self, connector):
        self.connector = connector


@pytest.fixture
def use_connector(monkeypatch, make_connector):
    def factory(responses, **host_data):
        connector = make_connector(responses, **host_data)
        connector.connect()
        monkeypatch.setattr(operations, "host", FakeHost(connector))
        return connector

    return factory


class TestCmnd:
    def test_no_change_yields_nothing(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": []})
        assert list(cmnd._inner("SetOption114", 0)) == []

    def test_change_yields_one_command(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner("SetOption114", 1))) == 1

    def test_unknown_reads_current_then_skips(self, use_connector, status0):
        connector = use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "SetOption120": {"SetOption120": 1},
            }
        )
        assert list(cmnd._inner("SetOption120", 1)) == []
        assert connector.current("SetOption120") == "1"

    def test_unknown_reads_current_then_changes(self, use_connector, status0):
        connector = use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "SetOption120": {"SetOption120": 0},
            }
        )
        assert len(list(cmnd._inner("SetOption120", 1))) == 1
        assert connector.current("SetOption120") == "0"

    def test_unknown_and_unreadable_yields_command(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner("SetOption120", 1))) == 1
        assert connector.current("SetOption120") is None

    def test_case_insensitive_compare(self, use_connector, status0):
        status0["Status"]["Power"] = "ON"
        use_connector({"Status 0": status0, "Status 13": []})
        assert list(cmnd._inner("Power", "on")) == []

    def test_reboot_command_is_not_read(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        before = len(connector.session.requests)
        assert len(list(cmnd._inner("Restart", 1))) == 1
        assert len(connector.session.requests) == before
        assert connector._pending == {"Restart": "1"}

    def test_records_desired_value_and_yields_marker(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        command = next(iter(cmnd._inner("SetOption114", 1)))
        assert connector._pending == {"SetOption114": "1"}
        assert command.execute(None, FakeHost(connector), {}) is True

    def test_read_current_ignores_nested_dicts(self, use_connector, status0):
        # The reply has a non-matching key, then a matching key whose value is
        # itself a dict. Neither yields a scalar, so the read reports nothing.
        connector = use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "Unknown": {"Unrelated": 1, "Unknown": {"Nested": 2}},
            }
        )
        assert len(list(cmnd._inner("Unknown", "x"))) == 1
        assert connector.current("Unknown") is None

    def test_read_current_handles_non_dict_reply(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": [], "SetOption120": "nope"})
        assert len(list(cmnd._inner("SetOption120", 1))) == 1


class TestApply:
    def test_no_pending_yields_nothing(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": []})
        assert list(apply._inner()) == []

    def test_pending_yields_flush_command(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.record("SetOption114", 1)
        assert len(list(apply._inner())) == 1

    def test_flush_command_succeeds(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.record("SetOption114", 1)
        command = next(iter(apply._inner()))
        connector.session.responses = CatchAll()
        assert command.execute(None, FakeHost(connector), {}) is True
        assert connector.has_pending() is False

    def test_flush_command_failure_returns_false(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.record("SetOption114", 1)
        command = next(iter(apply._inner()))
        assert command.execute(None, FakeHost(connector), {}) is False
        assert connector.current("SetOption120") is None
