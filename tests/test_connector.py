"""Tests for the Tasmota connector."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from pyinfra.api.exceptions import ConnectError, InventoryError, PyinfraError

from pyinfra_tasmota.connector import (
    BACKLOG_MAX_COMMANDS,
    BACKLOG_MAX_LENGTH,
    BACKLOG_PREFIX,
    TasmotaConnector,
    _backlog_batches,
    _canonical,
    _extract_bootcount,
    _normalize,
    _split_host_port,
)
from tests.conftest import CatchAll, FakeResponse, make_state


class TestMakeNamesData:
    def test_requires_a_name(self):
        with pytest.raises(InventoryError):
            list(TasmotaConnector.make_names_data(None))

    def test_plain_host(self):
        (name, data, groups) = next(iter(TasmotaConnector.make_names_data("tasmota.example.org")))
        assert name == "tasmota.example.org"
        assert data == {"tasmota_host": "tasmota.example.org", "tasmota_port": 80}
        assert groups == ["tasmota"]

    def test_host_with_port(self):
        (_name, data, _groups) = next(
            iter(TasmotaConnector.make_names_data("tasmota.example.org:8081"))
        )
        assert data["tasmota_host"] == "tasmota.example.org"
        assert data["tasmota_port"] == 8081


class TestSplitHostPort:
    def test_plain(self):
        assert _split_host_port("tasmota.example.org") == ("tasmota.example.org", 80)

    def test_http_scheme(self):
        assert _split_host_port("http://tasmota.example.org") == ("tasmota.example.org", 80)

    def test_https_scheme(self):
        assert _split_host_port("https://tasmota.example.org") == ("tasmota.example.org", 443)

    def test_explicit_port(self):
        assert _split_host_port("tasmota.example.org:81") == ("tasmota.example.org", 81)


class TestCanonical:
    def test_lowercases(self):
        assert _canonical("SetOption114") == "setoption114"

    def test_so_alias(self):
        assert _canonical("SO114") == "setoption114"
        assert _canonical("so") == "setoption"

    def test_so_prefix_without_digits_is_kept(self):
        assert _canonical("SoftAPMac") == "softapmac"

    def test_other_names(self):
        assert _canonical(" Module ") == "module"


class TestNormalize:
    def test_true_values(self):
        for value in ("1", "on", "ON", "true", "yes"):
            assert _normalize(value) == "1"

    def test_false_values(self):
        for value in ("0", "off", "OFF", "false", "no"):
            assert _normalize(value) == "0"

    def test_other_values(self):
        assert _normalize("3") == "3"
        assert _normalize("  Tokens ") == "tokens"


class TestBacklogBatches:
    def test_empty(self):
        assert list(_backlog_batches([])) == []

    def test_count_limit(self):
        commands = [(f"S{index}", "1") for index in range(BACKLOG_MAX_COMMANDS + 1)]
        batches = list(_backlog_batches(commands))
        assert [len(batch) for batch in batches] == [BACKLOG_MAX_COMMANDS, 1]

    def test_length_limit(self):
        commands = [("Long", "x" * 200), ("Long2", "y" * 200)]
        batches = list(_backlog_batches(commands))
        assert [len(batch) for batch in batches] == [1, 1]

    def test_single_oversized_command_makes_progress(self):
        batches = list(_backlog_batches([("Huge", "z" * 400)]))
        assert [len(batch) for batch in batches] == [1]

    def test_rendered_batches_fit(self):
        commands = [(f"SetOption{index}", str(index)) for index in range(100)]
        for batch in _backlog_batches(commands):
            rendered = ";".join(f"{subject} {value}" for subject, value in batch)
            assert len(BACKLOG_PREFIX + rendered) <= BACKLOG_MAX_LENGTH
            assert len(batch) <= BACKLOG_MAX_COMMANDS


class TestConnect:
    def test_populates_cache(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        assert connector.current("SetOption114") == "0"
        assert connector.current("Module") == "1"
        assert connector.current("Version") == "14.0.0"
        assert connector.current("Hostname") == "tasmota-abc"

    def test_case_insensitive_lookup(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        assert connector.current("setoption114") == "0"

    def test_flattens_shutter_status(self, make_connector, status0):
        connector = make_connector(
            {"Status 0": status0, "Status 13": {"StatusSHT": {"ShutterMode": 1}}}
        )
        connector.connect()
        assert connector.current("ShutterMode") == "1"

    def test_bad_payload(self, make_connector):
        connector = make_connector({"Status 0": {"Command": "Error"}})
        with pytest.raises(ConnectError):
            connector.connect()

    def test_request_error(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.session.responses.pop("Status 0")
        with pytest.raises(ConnectError):
            connector.connect()

    def test_missing_shutter_status_is_ignored(self, make_connector, status0):
        connector = make_connector({"Status 0": status0})
        connector.connect()
        assert connector.current("Module") == "1"

    def test_invalid_json_is_ignored_for_optional_status(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": ValueError("bad")})
        connector.connect()
        assert connector.current("Module") == "1"

    def test_invalid_json_fails_for_required_status(self, make_connector):
        connector = make_connector({"Status 0": ValueError("bad")})
        with pytest.raises(ConnectError):
            connector.connect()

    def test_skips_non_dict_sections(self, make_connector, status0):
        status0["StatusX"] = "not-a-section"
        connector = make_connector(
            {"Status 0": status0, "Status 13": {"StatusSHT": "not-a-section"}}
        )
        connector.connect()
        assert connector.current("Module") == "1"

    def test_http_error(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.session.get = lambda url, timeout=0: FakeResponse({"x": 1}, status_code=500)
        with pytest.raises(ConnectError):
            connector.connect()


class TestApply:
    def test_no_pending_is_a_noop(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        before = len(connector.session.requests)
        connector.apply()
        assert len(connector.session.requests) == before

    def test_flushes_a_batch(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        connector.record("SetOption114", 1)
        connector.session.responses["Backlog0 SetOption114 1"] = {}
        connector.apply()
        assert _last_cmnd(connector.session) == "Backlog0 SetOption114 1"
        assert connector.current("SetOption114") == "1"

    def test_apply_is_idempotent(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        connector.record("SetOption114", 1)
        connector.session.responses["Backlog0 SetOption114 1"] = {}
        connector.apply()
        count = len(connector.session.requests)
        connector.apply()
        assert len(connector.session.requests) == count

    def test_batches_respect_backlog_limits(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        for index in range(40):
            connector.record(f"SetOption{index}", index)
        connector.session.responses = CatchAll()
        connector.apply()
        batches = _backlog_payloads(connector.session)
        assert len(batches) > 1
        for commands in batches:
            assert len(commands) <= BACKLOG_MAX_COMMANDS
            assert len(BACKLOG_PREFIX + ";".join(commands)) <= BACKLOG_MAX_LENGTH

    def test_long_command_is_sent_alone(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        connector.record("Short", 1)
        connector.record("Long", "x" * 300)
        connector.session.responses = CatchAll()
        connector.apply()
        batches = _backlog_payloads(connector.session)
        assert len(batches) == 2
        assert batches[-1] == ["Long " + "x" * 300]

    def test_reboot_commands_are_sent_last_and_unwrapped(
        self, make_connector, status0, monkeypatch
    ):
        monkeypatch.setattr("pyinfra_tasmota.connector.time.sleep", lambda _seconds: None)
        pings = []

        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            if cmnd == "Status 1":
                pings.append(cmnd)
                if len(pings) <= 2:
                    return requests.ConnectionError("rebooting")
                return {"StatusPRM": {"BootCount": 6}}
            return {}

        connector = make_connector(handler)
        connector.connect()
        connector.record("SetOption114", 1)
        connector.record("Restart", 1)
        connector.apply()

        requests_seen = connector.session.requests
        reboot_at = next(i for i, u in enumerate(requests_seen) if "cmnd=Restart+1" in u)
        assert any("Backlog0" in u for u in requests_seen[:reboot_at])
        assert all("Restart" not in u for u in requests_seen if "Backlog0" in u)
        assert len(pings) >= 2
        assert connector.current("BootCount") == "5"  # re-ingest used the canned Status 0

    def test_fast_reboot_detected_by_bootcount(self, make_connector, status0, monkeypatch):
        monkeypatch.setattr("pyinfra_tasmota.connector.time.sleep", lambda _seconds: None)
        state = {"first": True}

        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            if cmnd == "Status 1" and state["first"]:
                state["first"] = False
                return {"StatusPRM": {"BootCount": 5}}
            if cmnd == "Status 1":
                return {"StatusPRM": {"BootCount": 6}}
            return {}

        connector = make_connector(handler)
        connector.connect()
        connector.record("Restart", 1)
        connector.apply()
        assert state["first"] is False

    def test_reboot_timeout_raises(self, make_connector, status0, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(
            "pyinfra_tasmota.connector.time.sleep",
            lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
        )
        monkeypatch.setattr("pyinfra_tasmota.connector.time.monotonic", lambda: clock["t"])

        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            return {"StatusPRM": {"BootCount": 5}}

        connector = make_connector(handler)
        connector.connect()
        connector.record("Restart", 1)
        with pytest.raises(PyinfraError):
            connector.apply()


def _last_cmnd(session):
    return parse_qs(urlparse(session.requests[-1]).query)["cmnd"][0]


def _backlog_requests(session):
    return [url for url in session.requests if "Backlog0" in url]


def _backlog_payloads(session):
    payloads = []
    for url in _backlog_requests(session):
        cmnd = parse_qs(urlparse(url).query)["cmnd"][0]
        payloads.append(cmnd.split(" ", 1)[1].split(";"))
    return payloads


class TestExtractBootcount:
    def test_valid(self):
        assert _extract_bootcount({"StatusPRM": {"BootCount": 7}}) == 7

    def test_missing(self):
        assert _extract_bootcount({"StatusFWR": {}}) is None

    def test_not_a_dict(self):
        assert _extract_bootcount("nope") is None

    def test_statusprm_not_a_dict(self):
        assert _extract_bootcount({"StatusPRM": "nope"}) is None


class TestDisconnect:
    def test_disconnect_flushes_and_closes(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        connector.record("SetOption114", 1)
        connector.session.responses["Backlog0 SetOption114 1"] = {}
        connector.disconnect()
        assert connector.session.closed is True

    def test_disconnect_logs_flush_error(self, make_connector, status0, monkeypatch):
        clock = {"t": 0.0}
        monkeypatch.setattr(
            "pyinfra_tasmota.connector.time.sleep",
            lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
        )
        monkeypatch.setattr("pyinfra_tasmota.connector.time.monotonic", lambda: clock["t"])

        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            return {"StatusPRM": {"BootCount": 5}}

        connector = make_connector(handler)
        connector.connect()
        connector.record("Restart", 1)
        connector.disconnect()
        assert connector.session.closed is True


class TestUnsupported:
    def test_shell(self, make_connector):
        connector = make_connector({})
        with pytest.raises(NotImplementedError):
            connector.run_shell_command(None, False, False)

    def test_put_file(self, make_connector):
        connector = make_connector({})
        with pytest.raises(NotImplementedError):
            connector.put_file(None, "remote", None, False, False)

    def test_get_file(self, make_connector):
        connector = make_connector({})
        with pytest.raises(NotImplementedError):
            connector.get_file("remote", None, None, False, False)


class TestVerbosity:
    def test_levels(self, make_connector):
        connector = make_connector({})
        assert connector.verbosity == 0
        connector.state = make_state(1)
        assert connector.verbosity == 1
        connector.state = make_state(2)
        assert connector.verbosity == 2
        connector.state = make_state(3)
        assert connector.verbosity == 3

    def test_silent_by_default(self, make_connector, status0, caplog):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        connector.record("SetOption114", 1)
        connector.session.responses = CatchAll()
        with caplog.at_level(logging.INFO):
            connector.apply()
        assert caplog.messages == []

    def test_level_one_reports_sent_commands(self, make_connector, status0, caplog):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.state = make_state(1)
        connector.connect()
        connector.record("SetOption114", 1)
        connector.session.responses = CatchAll()
        with caplog.at_level(logging.INFO):
            connector.apply()
        assert "tasmota.example.org: sent SetOption114 1" in caplog.messages
        assert not any("batch" in message for message in caplog.messages)

    def test_level_three_reports_internals(self, make_connector, status0, caplog):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.state = make_state(3)
        with caplog.at_level(logging.INFO):
            connector.connect()
            connector.record("SetOption114", 1)
            connector.session.responses = CatchAll()
            connector.apply()
        assert any("cached" in message for message in caplog.messages)
        assert any("flushing 1 commands" in message for message in caplog.messages)
        assert any("batch of 1 commands" in message for message in caplog.messages)
        assert "tasmota.example.org: sent SetOption114 1" in caplog.messages

    def test_level_three_reports_reboot(self, make_connector, status0, caplog, monkeypatch):
        monkeypatch.setattr("pyinfra_tasmota.connector.time.sleep", lambda _seconds: None)

        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            if cmnd == "Status 1":
                return {"StatusPRM": {"BootCount": 6}}
            return {}

        connector = make_connector(handler)
        connector.state = make_state(3)
        connector.connect()
        connector.record("Restart", 1)
        with caplog.at_level(logging.INFO):
            connector.apply()
        assert "tasmota.example.org: sent Restart 1" in caplog.messages
        assert any("after reboot" in message for message in caplog.messages)
