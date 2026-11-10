"""Tests for the Tasmota connector."""

from __future__ import annotations

import pytest
from pyinfra.api.exceptions import ConnectError, InventoryError

from pyinfra_tasmota.connector import (
    BACKLOG_MAX_COMMANDS,
    TasmotaConnector,
    _chunk,
    _split_host_port,
)
from tests.conftest import CatchAll, FakeResponse


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


class TestChunk:
    def test_chunks(self):
        assert list(_chunk([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]

    def test_empty(self):
        assert list(_chunk([], 2)) == []


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


class TestSaveDataRepair:
    def test_repairs_stuck_zero(self, make_connector, status0):
        status0["Status"]["SaveData"] = 0
        connector = make_connector(
            {"Status 0": status0, "Status 13": [], "SaveData 1": {"SaveData": "Done"}}
        )
        connector.connect()
        assert any("cmnd=SaveData+1" in url for url in connector.session.requests)

    def test_leaves_healthy_value(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        assert not any("cmnd=SaveData+1" in url for url in connector.session.requests)

    def test_missing_savedata_is_ignored(self, make_connector, status0):
        del status0["Status"]["SaveData"]
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        assert connector.current("Module") == "1"


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
        connector.session.responses["Backlog0 SaveData 0;SetOption114 1;SaveData 1"] = {}
        connector.apply()
        assert (
            "Backlog0 SaveData 0;SetOption114 1;SaveData 1" in connector.session.requests[-1]
            or "Backlog0+SaveData+0" in connector.session.requests[-1]
        )
        assert connector.current("SetOption114") == "1"

    def test_apply_is_idempotent(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        connector.record("SetOption114", 1)
        connector.session.responses["Backlog0 SaveData 0;SetOption114 1;SaveData 1"] = {}
        connector.apply()
        count = len(connector.session.requests)
        connector.apply()
        assert len(connector.session.requests) == count

    def test_chunks_large_batches(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        for index in range(BACKLOG_MAX_COMMANDS):
            connector.record(f"SetOption{index}", 1)
        connector.session.responses = CatchAll()
        connector.apply()
        assert len(_backlog_requests(connector.session)) == 2

    def test_reboot_commands_are_isolated(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        connector.record("SetOption114", 1)
        connector.record("Restart", 1)
        connector.session.responses = CatchAll()
        with pytest.raises(NotImplementedError):
            connector.apply()
        assert len(_backlog_requests(connector.session)) == 2


def _backlog_requests(session):
    return [url for url in session.requests if "Backlog0" in url]


class TestDisconnect:
    def test_disconnect_flushes_and_closes(self, make_connector, status0):
        connector = make_connector({"Status 0": status0, "Status 13": []})
        connector.connect()
        connector.record("SetOption114", 1)
        connector.session.responses["Backlog0 SaveData 0;SetOption114 1;SaveData 1"] = {}
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
