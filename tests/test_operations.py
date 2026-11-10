"""Tests for the Tasmota operations."""

from __future__ import annotations

import logging

import pytest

from pyinfra_tasmota import operations
from pyinfra_tasmota.operations import apply, cmnd, ensure_host_data, rule, shutter
from tests.conftest import CatchAll, make_state


class FakeHost:
    def __init__(self, connector, data=None):
        self.connector = connector
        self.data = {} if data is None else data


@pytest.fixture
def use_connector(monkeypatch, make_connector):
    def factory(responses, data=None, **host_data):
        connector = make_connector(responses, **host_data)
        connector.connect()
        monkeypatch.setattr(operations, "host", FakeHost(connector, data))
        return connector

    return factory


class TestEnsureHostData:
    def test_ensures_declared_commands(self, use_connector, status0):
        connector = use_connector(
            {"Status 0": status0, "Status 13": []},
            data={"tasmota": {"SetOption114": 1, "Module": 1}},
        )
        assert len(list(ensure_host_data._inner())) == 1
        assert list(connector._pending) == [("setoption114", "1")]

    def test_no_data_yields_nothing(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": []})
        assert list(ensure_host_data._inner()) == []

    def test_custom_key(self, use_connector, status0):
        connector = use_connector(
            {"Status 0": status0, "Status 13": []},
            data={"other": {"SetOption114": 1}},
        )
        assert len(list(ensure_host_data._inner(key="other"))) == 1
        assert list(connector._pending) == [("setoption114", "1")]

    def test_keeps_passwords_by_default(self, use_connector, status0):
        connector = use_connector(
            {"Status 0": status0, "Status 13": []},
            data={"tasmota": {"Password1": "secret"}},
        )
        assert len(list(ensure_host_data._inner())) == 1
        assert list(connector._pending) == [("password1", "secret")]

    def test_drops_passwords_when_disabled(self, use_connector, status0):
        connector = use_connector(
            {"Status 0": status0, "Status 13": []},
            data={"tasmota": {"SetOption114": 1, "Password1": "secret", "MqttPassword": "x"}},
        )
        assert len(list(ensure_host_data._inner(send_passwords=False))) == 1
        assert list(connector._pending) == [("setoption114", "1")]

    def test_rejects_non_mapping(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": []}, data={"tasmota": [1, 2]})
        with pytest.raises(TypeError):
            list(ensure_host_data._inner())

    def test_list_value_is_diffed(self, use_connector, status0):
        # SetOption114 is cached as 0, so only the 1 needs sending.
        connector = use_connector(
            {"Status 0": status0, "Status 13": []},
            data={"tasmota": {"SetOption114": [0, 1]}},
        )
        assert len(list(ensure_host_data._inner())) == 1
        assert list(connector._pending) == [("setoption114", "1")]

    def test_rule_code_is_sent_directly(self, use_connector, status0):
        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            if cmnd == "Rule1":
                return {"Rule1": {"State": "OFF", "Rules": ""}}
            return {}

        connector = use_connector(handler, data={"tasmota": {"Rule1": "ON x DO y ENDON"}})
        commands = list(ensure_host_data._inner())
        assert len(commands) == 1
        assert commands[0].execute(None, FakeHost(connector), {}) is True
        assert any("Rule1+ON+x+DO+y+ENDON" in url for url in connector.session.requests)

    def test_rule_list_is_code_and_flags(self, use_connector, status0):
        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            if cmnd == "Rule1":
                return {"Rule1": {"State": "OFF", "Rules": ""}}
            return {}

        connector = use_connector(handler, data={"tasmota": {"Rule1": ["ON x DO y ENDON", "1"]}})
        commands = list(ensure_host_data._inner())
        assert len(commands) == 1
        assert commands[0].execute(None, FakeHost(connector), {}) is True
        assert any("Rule1+ON+x+DO+y+ENDON" in url for url in connector.session.requests)
        assert ("rule1", "1") in connector._pending

    def test_rule_empty_list_yields_nothing(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": []}, data={"tasmota": {"Rule1": []}})
        assert list(ensure_host_data._inner()) == []

    def test_rule_mapping_sets_flags(self, use_connector, status0):
        responses = {
            "Status 0": status0,
            "Status 13": [],
            "Rule1": {"Rule1": {"State": "OFF", "Once": "OFF", "Rules": "on x do y endon"}},
        }
        connector = use_connector(
            responses,
            data={"tasmota": {"Rule1": {"code": "ON x DO y ENDON", "enabled": True}}},
        )
        commands = list(ensure_host_data._inner())
        assert len(commands) == 1
        assert ("rule1", "1") in connector._pending


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
        assert set(connector._pending) == {("setoption114", "1")}

    def test_multiple_commands_all_change(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner(SetOption114=1, Power="on"))) == 1
        assert set(connector._pending) == {("setoption114", "1"), ("power", "on")}

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
        assert connector._pending == {("restart", "1"): "Restart"}

    def test_action_command_is_not_read(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        before = len(connector.session.requests)
        assert len(list(cmnd._inner(Backlog="Power1 on"))) == 1
        assert len(connector.session.requests) == before
        assert connector._pending == {("backlog", "Power1 on"): "Backlog"}

    def test_records_desired_value_and_yields_marker(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        command = next(iter(cmnd._inner(SetOption114=1)))
        assert connector._pending == {("setoption114", "1"): "SetOption114"}
        assert command.execute(None, FakeHost(connector), {}) is True

    def test_list_value_queues_each_in_order(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner(Rule1=["ON", "1"]))) == 1
        assert list(connector._pending) == [("rule1", "ON"), ("rule1", "1")]

    def test_list_value_is_diffed(self, use_connector, status0):
        # Power is cached as OFF, so only the "on" needs sending.
        connector = use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner(Power=["off", "on"]))) == 1
        assert list(connector._pending) == [("power", "on")]

    def test_tuple_value_is_treated_as_a_list(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner(Rule1=("ON", "1")))) == 1
        assert list(connector._pending) == [("rule1", "ON"), ("rule1", "1")]

    def test_repeated_identical_command_collapses(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        list(cmnd._inner(SetOption114=1))
        list(cmnd._inner(SetOption114=1))
        assert list(connector._pending) == [("setoption114", "1")]

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


class TestAdapters:
    def test_time_struct_matches(self, use_connector, status0):
        use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "TimeDST": {
                    "TimeDst": {
                        "Hemisphere": 0,
                        "Week": 0,
                        "Month": 3,
                        "Day": 1,
                        "Hour": 1,
                        "Offset": 120,
                    }
                },
            }
        )
        assert list(cmnd._inner(TimeDST="0,0,3,1,1,120")) == []

    def test_time_struct_differs(self, use_connector, status0):
        connector = use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "TimeDST": {
                    "TimeDst": {
                        "Hemisphere": 0,
                        "Week": 0,
                        "Month": 3,
                        "Day": 1,
                        "Hour": 1,
                        "Offset": 60,
                    }
                },
            }
        )
        assert len(list(cmnd._inner(TimeDST="0,0,3,1,1,120"))) == 1
        assert list(connector._pending) == [("timedst", "0,0,3,1,1,120")]

    def test_pulsetime_compares_set_field(self, use_connector, status0):
        use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "Pulsetime3": {"Pulsetime3": {"Set": 10, "Remaining": 4}},
            }
        )
        assert list(cmnd._inner(Pulsetime3=10)) == []

    def test_latitude_within_tolerance(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": [], "Latitude": {"Latitude": 65.389123}})
        assert list(cmnd._inner(Latitude="65.389124")) == []

    def test_latitude_outside_tolerance_changes(self, use_connector, status0):
        connector = use_connector(
            {"Status 0": status0, "Status 13": [], "Latitude": {"Latitude": 51.507300}}
        )
        assert len(list(cmnd._inner(Latitude="51.507350"))) == 1
        assert list(connector._pending) == [("latitude", "51.507350")]

    def test_template_ignores_arch(self, use_connector, status0):
        # Template answers with the template object directly, including ARCH.
        template = {"NAME": "x", "ARCH": "ESP8266", "GPIO": [0, 1], "FLAG": 0, "BASE": 18}
        use_connector({"Status 0": status0, "Status 13": [], "Template": template})
        desired = {"NAME": "x", "GPIO": [0, 1], "FLAG": 0, "BASE": 18}
        assert list(cmnd._inner(Template=desired)) == []

    def test_template_differs(self, use_connector, status0):
        template = {"NAME": "x", "ARCH": "ESP8266", "GPIO": [0, 1], "FLAG": 0, "BASE": 18}
        use_connector({"Status 0": status0, "Status 13": [], "Template": template})
        desired = {"NAME": "y", "GPIO": [0, 1], "FLAG": 0, "BASE": 18}
        assert len(list(cmnd._inner(Template=desired))) == 1

    def test_fulltopic_default_matches(self, use_connector, status0):
        use_connector(
            {
                "Status 0": status0,
                "Status 13": [],
                "FullTopic": {"FullTopic": "%prefix%/%topic%/"},
            }
        )
        assert list(cmnd._inner(FullTopic=1)) == []

    def test_masked_command_is_always_sent(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        assert len(list(cmnd._inner(Password1="secret"))) == 1
        assert list(connector._pending) == [("password1", "secret")]


SHUTTER_RESPONSES = {
    "SetOption80": {"SetOption80": "ON"},
    "Interlock": {"Interlock": "ON", "Groups": "1,2"},
    "ShutterMode": {"ShutterMode": 1},
    "ShutterRelay1": {"ShutterRelay1": 1},
    "ShutterOpenDuration1": {"ShutterOpenDuration1": 12.0},
    "ShutterCloseDuration1": {"ShutterCloseDuration1": 12.0},
}


class TestShutterOperation:
    def test_no_change_yields_nothing(self, use_connector, status0):
        use_connector({"Status 0": status0, "Status 13": [], **SHUTTER_RESPONSES})
        assert list(shutter._inner(open_duration=12, close_duration=12)) == []

    def test_change_records_interlock_first(self, use_connector, status0):
        responses = {
            **SHUTTER_RESPONSES,
            "Interlock": {"Interlock": "ON", "Groups": "3,4"},
        }
        connector = use_connector({"Status 0": status0, "Status 13": [], **responses})
        commands = list(shutter._inner(open_duration=12, close_duration=12, mode=2))
        assert len(commands) == 1
        assert list(connector._pending) == [
            ("interlock", "1,2"),
            ("shuttermode", "2"),
        ]

    def test_button_is_included_when_given(self, use_connector, status0):
        connector = use_connector({"Status 0": status0, "Status 13": [], **SHUTTER_RESPONSES})
        list(shutter._inner(open_duration=12, close_duration=12, button=1))
        assert ("shutterbutton1", "1") in connector._pending

    def test_button_prefix_matches(self, use_connector, status0):
        responses = {**SHUTTER_RESPONSES, "ShutterButton1": {"ShutterButton1": "1 up 100/0"}}
        connector = use_connector({"Status 0": status0, "Status 13": [], **responses})
        list(shutter._inner(open_duration=12, close_duration=12, button="1 up"))
        assert ("shutterbutton1", "1 up") not in connector._pending

    def test_disabled_yields_enable_command(self, use_connector, status0):
        responses = {**SHUTTER_RESPONSES, "SetOption80": {"SetOption80": "OFF"}}
        use_connector({"Status 0": status0, "Status 13": [], **responses})
        commands = list(shutter._inner(open_duration=12, close_duration=12))
        assert len(commands) == 1

    def test_enable_command_runs_at_execution(self, use_connector, status0, monkeypatch):
        monkeypatch.setattr("pyinfra_tasmota.connector.time.sleep", lambda _seconds: None)

        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            if cmnd == "SetOption80":
                return {"SetOption80": "OFF"}
            if cmnd == "Status 1":
                return {"StatusPRM": {"BootCount": 6}}
            return SHUTTER_RESPONSES.get(cmnd, {})

        connector = use_connector(handler)
        command = next(iter(shutter._inner(open_duration=12, close_duration=12)))
        assert command.execute(None, FakeHost(connector), {}) is True
        assert connector.shutter_enabled() is True


RULE_RESPONSE = {"Rule1": {"State": "OFF", "Once": "OFF", "StopOnError": "OFF", "Rules": ""}}


class TestRuleOperation:
    def test_sends_code_directly(self, use_connector, status0):
        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            if cmnd == "Rule1":
                return RULE_RESPONSE
            return {}

        connector = use_connector(handler)
        command = next(iter(rule._inner("ON x DO y ENDON")))
        assert command.execute(None, FakeHost(connector), {}) is True
        assert any("Rule1+ON+x+DO+y+ENDON" in url for url in connector.session.requests)

    def test_no_change_yields_nothing(self, use_connector, status0):
        responses = {
            "Status 0": status0,
            "Status 13": [],
            "Rule1": {"Rule1": {"State": "ON", "Rules": "on x do y endon"}},
        }
        use_connector(responses)
        assert list(rule._inner("ON x DO y ENDON")) == []

    def test_flag_matches_when_state_on(self, use_connector, status0):
        responses = {
            "Status 0": status0,
            "Status 13": [],
            "Rule1": {"Rule1": {"State": "ON", "Once": "OFF", "Rules": "on x do y endon"}},
        }
        connector = use_connector(responses)
        assert list(rule._inner("ON x DO y ENDON", enabled=True)) == []
        assert connector._pending == {}

    def test_flags_are_recorded(self, use_connector, status0):
        responses = {
            "Status 0": status0,
            "Status 13": [],
            "Rule1": {"Rule1": {"State": "OFF", "Once": "OFF", "Rules": "on x do y endon"}},
        }
        connector = use_connector(responses)
        commands = list(rule._inner("ON x DO y ENDON", enabled=True, once=True))
        assert len(commands) == 1
        assert list(connector._pending) == [("rule1", "1"), ("rule1", "5")]

    def test_append_always_sends(self, use_connector, status0):
        def handler(cmnd):
            if cmnd == "Status 0":
                return status0
            if cmnd == "Status 13":
                return {}
            if cmnd == "Rule1":
                return {"Rule1": {"State": "ON", "Rules": "on x do y endon"}}
            return {}

        connector = use_connector(handler)
        command = next(iter(rule._inner("ON z DO y ENDON", append=True)))
        assert command.execute(None, FakeHost(connector), {}) is True
        assert any("Rule1+%2BON+z+DO+y+ENDON" in url for url in connector.session.requests)

    def test_stop_on_error_flag(self, use_connector, status0):
        responses = {
            "Status 0": status0,
            "Status 13": [],
            "Rule1": {"Rule1": {"State": "OFF", "StopOnError": "OFF", "Rules": ""}},
        }
        connector = use_connector(responses)
        list(rule._inner("ON x DO y ENDON", stop_on_error=True))
        assert ("rule1", "9") in connector._pending


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

    def test_level_two_logs_masked(self, use_connector, status0, caplog):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.state = make_state(2)
        with caplog.at_level(logging.INFO):
            list(cmnd._inner(Password1="secret"))
        assert (
            "tasmota.example.org: Password1 is masked, setting unconditionally" in caplog.messages
        )

    def test_level_two_logs_list_values(self, use_connector, status0, caplog):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.state = make_state(2)
        with caplog.at_level(logging.INFO):
            list(cmnd._inner(SetOption114=[0, 1]))
        assert "tasmota.example.org: SetOption114 is already 0, skipping" in caplog.messages
        assert "tasmota.example.org: SetOption114 is 0, will set to 1" in caplog.messages

    def test_level_two_masks_secret(self, use_connector, status0, caplog):
        connector = use_connector({"Status 0": status0, "Status 13": []})
        connector.state = make_state(2)
        with caplog.at_level(logging.INFO):
            list(cmnd._inner(Password1=["hunter2"]))
        assert (
            "tasmota.example.org: Password1 is masked, setting unconditionally" in caplog.messages
        )
        assert not any("hunter2" in message for message in caplog.messages)

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
