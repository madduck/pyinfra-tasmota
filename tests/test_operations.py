"""Tests for the Tasmota operations."""

from __future__ import annotations

import logging

import pytest

from pyinfra_tasmota import operations
from pyinfra_tasmota.operations import apply, cmnd
from tests.conftest import CatchAll, make_state


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
        assert list(cmnd._inner(SetOption114=0)) == []

    def test_change_yields_one_command(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner(SetOption114=1))) == 1

    def test_no_commands_yields_nothing(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": []})
        assert list(cmnd._inner()) == []

    def test_multiple_commands_no_change(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": []})
        assert list(cmnd._inner(SetOption114=0, Module=1)) == []

    def test_multiple_commands_some_change(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner(SetOption114=1, Module=1))) == 1
        assert set(connector._pending) == {"setoption114"}

    def test_multiple_commands_all_change(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner(SetOption114=1, Power="on"))) == 1
        assert set(connector._pending) == {"setoption114", "power"}

    def test_unknown_reads_current_then_skips(self, use_connector, status0):
        connector = use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "SetOption120": {"SetOption120": 1},
            }
        )
        assert list(cmnd._inner(SetOption120=1)) == []
        assert connector.current("SetOption120") == "1"

    def test_unknown_reads_current_then_changes(self, use_connector, status0):
        connector = use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "SetOption120": {"SetOption120": 0},
            }
        )
        assert len(list(cmnd._inner(SetOption120=1))) == 1
        assert connector.current("SetOption120") == "0"

    def test_unknown_and_unreadable_yields_command(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner(SetOption120=1))) == 1
        assert connector.current("SetOption120") is None

    def test_case_insensitive_compare(self, use_connector, status0):
        status0["Status"]["Power"] = "ON"
        use_connector({"Status 0": status0, "Status 13": []})
        assert list(cmnd._inner(Power="on")) == []

    def test_reboot_command_is_not_read(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        before = len(connector.session.requests)
        assert len(list(cmnd._inner(Restart=1))) == 1
        assert len(connector.session.requests) == before
        assert connector._pending == {"restart": ("Restart", "1")}

    def test_records_desired_value_and_yields_marker(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        command = next(iter(cmnd._inner(SetOption114=1)))
        assert connector._pending == {"setoption114": ("SetOption114", "1")}
        assert command.execute(None, FakeHost(connector), {}) is True

    def test_read_current_rejects_multiple_unmatched_scalars(self, use_connector, status0):
        # Two scalar values and no canonical match: the read cannot decide, so
        # it reports nothing.
        connector = use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "Unknown": {"First": 1, "Second": 2},
            }
        )
        assert len(list(cmnd._inner(Unknown="x"))) == 1
        assert connector.current("Unknown") is None

    def test_read_current_handles_non_dict_reply(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": [], "SetOption120": "nope"})
        assert len(list(cmnd._inner(SetOption120=1))) == 1

    def test_read_current_rejects_command_error(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": [], "SetOption120": {"Command": "Error"}})
        assert len(list(cmnd._inner(SetOption120=1))) == 1

    def test_read_current_resolves_alias(self, use_connector, status0):
        # so114 is answered as SetOption114; the lone scalar resolves it.
        status0["Status"].pop("SetOption114", None)
        connector = use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "so114": {"SetOption114": "OFF"},
            }
        )
        assert list(cmnd._inner(so114=0)) == []
        assert connector.current("SetOption114") == "OFF"

    def test_read_current_accepts_lone_scalar(self, use_connector, status0):
        # A lone scalar under a name the connector does not know is accepted as
        # the value, which resolves aliases beyond SO.
        status0["Status"].pop("SetOption114", None)
        connector = use_connector(
            {"Status 0": status0, "Status 13": [], "Weird": {"SomethingElse": 1}}
        )
        assert list(cmnd._inner(Weird=1)) == []
        assert connector.current("SomethingElse") == "1"

    def test_value_folding_treats_off_as_zero(self, use_connector, status0):
        status0["Status"].pop("SetOption114", None)
        use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "SetOption114": {"SetOption114": "OFF"},
            }
        )
        assert list(cmnd._inner(SetOption114=0)) == []
        assert list(cmnd._inner(SetOption114="off")) == []

    def test_value_folding_treats_on_as_one(self, use_connector, status0):
        status0["Status"].pop("SetOption114", None)
        use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "SetOption114": {"SetOption114": "ON"},
            }
        )
        assert list(cmnd._inner(SetOption114=1)) == []
        assert list(cmnd._inner(SetOption114="on")) == []

    def test_alias_and_case_match_cached_value(self, use_connector, status0):
        # SetOption114 is already 0 in the cache; both spellings report no change
        # without reaching the device.
        connector = use_connector({"Status 0": status0, "Status 13": []})
        before = len(connector.session.requests)
        assert list(cmnd._inner(so114=0)) == []
        assert list(cmnd._inner(setoption114="off")) == []
        assert len(connector.session.requests) == before


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


class TestVerbosityLogging:
    def test_level_one_does_not_log_state(self, use_connector, status0, caplog):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.state = make_state(1)
        with caplog.at_level(logging.INFO):
            list(cmnd._inner(SetOption114=0))
        assert caplog.messages == []

    def test_level_two_logs_skip(self, use_connector, status0, caplog):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.state = make_state(2)
        with caplog.at_level(logging.INFO):
            list(cmnd._inner(SetOption114=0))
        assert "tasmota.example.org: SetOption114 is already 0, skipping" in caplog.messages

    def test_level_three_logs_read_and_change(self, use_connector, status0, caplog):
        status0["Status"].pop("SetOption114", None)
        connector = use_connector(
            {"Status 0": status0, "Status 13": [], "SetOption120": {"SetOption120": 0}}
        )
        connector.state = make_state(3)
        with caplog.at_level(logging.INFO):
            list(cmnd._inner(SetOption120=1))
        assert "tasmota.example.org: read SetOption120 as 0" in caplog.messages
        assert "tasmota.example.org: SetOption120 is 0, will set to 1" in caplog.messages

    def test_level_two_logs_unknown_state(self, use_connector, status0, caplog):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.state = make_state(2)
        with caplog.at_level(logging.INFO):
            list(cmnd._inner(Unknown="x"))
        assert "tasmota.example.org: Unknown state unknown, will set to x" in caplog.messages

    def test_not_executing_does_not_log_state(self, use_connector, status0, caplog):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.state = make_state(2, executing=False)
        with caplog.at_level(logging.INFO):
            list(cmnd._inner(SetOption114=0, Unknown="x"))
        assert caplog.messages == []
