"""Tests for the command comparison adapters."""

from __future__ import annotations

from pyinfra_tasmota.compare import adapter_for

ARCH_TEMPLATE = {"NAME": "x", "ARCH": "ESP8266", "GPIO": [0, 1], "FLAG": 0, "BASE": 18}
INTERLOCK_OBSERVED = {"Interlock": "ON", "Groups": "1,2"}
RULE_OBSERVED = {
    "State": "ON",
    "Once": "OFF",
    "StopOnError": "OFF",
    "Rules": "on x do y endon",
}


class TestDefaultAdapter:
    def test_unknown_command_is_not_skipped(self):
        assert adapter_for("SetOption114").skip is False

    def test_folds_booleans(self):
        adapter = adapter_for("SetOption114")
        assert adapter.matches(1, 1)
        assert adapter.matches("on", "ON")
        assert adapter.matches("off", 0)
        assert not adapter.matches("on", "off")

    def test_matches_canonically(self):
        assert adapter_for("setoption114") is adapter_for("SetOption114")
        assert adapter_for("so114") is adapter_for("SetOption114")


class TestStructAdapter:
    def test_time_commands(self):
        observed = {"Hemisphere": 0, "Week": 0, "Month": 3, "Day": 1, "Hour": 1, "Offset": 120}
        for name in ("TimeSTD", "TimeDST", "timestd", "timedst"):
            assert adapter_for(name).matches("0, 0, 3, 1, 1, 120", observed)

    def test_differs(self):
        observed = {"Hemisphere": 0, "Week": 0, "Month": 3, "Day": 1, "Hour": 1, "Offset": 60}
        assert not adapter_for("TimeDST").matches("0,0,3,1,1,120", observed)

    def test_scalar_fallback(self):
        assert adapter_for("TimeDST").matches("1,2,3,4,5,6", "1,2,3,4,5,6")


class TestFieldAdapter:
    def test_pulsetime_compares_set(self):
        assert adapter_for("Pulsetime3").matches(10, {"Set": 10, "Remaining": 5})

    def test_differs(self):
        assert not adapter_for("Pulsetime3").matches(11, {"Set": 10, "Remaining": 5})

    def test_scalar_fallback(self):
        assert adapter_for("Pulsetime3").matches(10, 10)


class TestApproximateAdapter:
    def test_within_tolerance(self):
        # The device stores micro-degrees with 32-bit float, so the value can
        # come back a few units off.
        assert adapter_for("Latitude").matches("65.389124", 65.389123)
        assert adapter_for("Longitude").matches("-169.105553", -169.105576)

    def test_outside_tolerance(self):
        assert not adapter_for("Longitude").matches("51.507350", 51.507500)

    def test_non_numeric_falls_back(self):
        assert adapter_for("Latitude").matches("nope", "nope")
        assert not adapter_for("Latitude").matches("nope", "other")


class TestShutterDurationAdapter:
    def test_rounds_to_one_decimal(self):
        # The device stores tenths of a second, so 32.26 rounds to 32.3.
        assert adapter_for("ShutterOpenDuration1").matches(32.26, 32.3)
        assert adapter_for("ShutterCloseDuration1").matches("12", "12.0")

    def test_differs(self):
        assert not adapter_for("ShutterOpenDuration1").matches(12.3, 13.3)

    def test_non_numeric_falls_back(self):
        assert adapter_for("ShutterOpenDuration1").matches("nope", "nope")

    def test_prepare_rounds_to_one_decimal(self):
        assert adapter_for("ShutterCloseDuration1").prepare("32.26") == "32.3"


class TestShutterButtonAdapter:
    def test_prefix_matches(self):
        assert adapter_for("ShutterButton1").matches("1 up", "1 up 100/0 50/0")

    def test_not_a_prefix(self):
        assert not adapter_for("ShutterButton1").matches("1 down", "1 up 100/0")

    def test_dict_falls_back(self):
        assert not adapter_for("ShutterButton1").matches("x", {"x": "x"})


class TestAliasAdapter:
    def test_fulltopic_default(self):
        assert adapter_for("FullTopic").matches(1, "%prefix%/%topic%/")

    def test_fulltopic_user_value(self):
        assert adapter_for("FullTopic").matches("tasmota/%topic%/", "tasmota/%topic%/")

    def test_matches_canonically(self):
        assert adapter_for("fulltopic") is adapter_for("FullTopic")


class TestJsonAdapter:
    def test_ignores_arch(self):
        desired = {"NAME": "x", "GPIO": [0, 1], "FLAG": 0, "BASE": 18}
        assert adapter_for("Template").matches(desired, ARCH_TEMPLATE)

    def test_accepts_json_string(self):
        desired = '{"NAME":"x","GPIO":[0,1],"FLAG":0,"BASE":18}'
        assert adapter_for("Template").matches(desired, ARCH_TEMPLATE)

    def test_key_order_does_not_matter(self):
        desired = {"BASE": 18, "FLAG": 0, "GPIO": [0, 1], "NAME": "x"}
        assert adapter_for("Template").matches(desired, ARCH_TEMPLATE)

    def test_differs(self):
        desired = {"NAME": "y", "GPIO": [0, 1], "FLAG": 0, "BASE": 18}
        assert not adapter_for("Template").matches(desired, ARCH_TEMPLATE)

    def test_list_value(self):
        assert adapter_for("Template").matches("[1, 2]", [1, 2])

    def test_invalid_json_falls_back(self):
        assert adapter_for("Template").matches("not json", "not json")

    def test_template_uses_whole_response(self):
        assert adapter_for("Template").whole is True

    def test_other_commands_use_the_command_key(self):
        assert adapter_for("SetOption114").whole is False


class TestInterlockAdapter:
    def test_groups(self):
        assert adapter_for("Interlock").matches("1,2", INTERLOCK_OBSERVED)

    def test_group_order_and_spacing(self):
        assert adapter_for("Interlock").matches("2, 1", INTERLOCK_OBSERVED)

    def test_multiple_groups_differ(self):
        observed = {"Interlock": "ON", "Groups": "1,2 3,4"}
        assert not adapter_for("Interlock").matches("1,2", observed)

    def test_enabled(self):
        assert adapter_for("Interlock").matches("ON", INTERLOCK_OBSERVED)

    def test_differs(self):
        assert not adapter_for("Interlock").matches("OFF", INTERLOCK_OBSERVED)
        assert not adapter_for("Interlock").matches("3,4", INTERLOCK_OBSERVED)

    def test_scalar_fallback(self):
        assert adapter_for("Interlock").matches("1,2", "1,2")

    def test_uses_whole_response(self):
        assert adapter_for("Interlock").whole is True


class TestRuleAdapter:
    def test_text(self):
        assert adapter_for("Rule1").matches("ON x DO y ENDON", RULE_OBSERVED)
        assert not adapter_for("Rule1").matches("ON z DO y ENDON", RULE_OBSERVED)

    def test_enabled_flag(self):
        assert adapter_for("Rule1").matches("1", RULE_OBSERVED)
        assert not adapter_for("Rule1").matches("0", RULE_OBSERVED)

    def test_once_flag(self):
        assert not adapter_for("Rule1").matches("5", RULE_OBSERVED)
        assert adapter_for("Rule1").matches("4", RULE_OBSERVED)

    def test_stop_on_error_flag(self):
        assert not adapter_for("Rule1").matches("9", RULE_OBSERVED)
        assert adapter_for("Rule1").matches("8", RULE_OBSERVED)

    def test_scalar_fallback(self):
        assert adapter_for("Rule1").matches("text", "text")

    def test_uses_command_key(self):
        assert adapter_for("Rule1").whole is False

    def test_describe_names_field(self):
        adapter = adapter_for("Rule1")
        assert adapter.describe("1") == "State"
        assert adapter.describe("5") == "Once"
        assert adapter.describe("9") == "StopOnError"
        assert adapter.describe("ON x DO y ENDON") == "text"

    def test_observed_narrows_to_field(self):
        adapter = adapter_for("Rule1")
        assert adapter.observed("1", RULE_OBSERVED) == "ON"
        assert adapter.observed("ON x DO y ENDON", RULE_OBSERVED) == RULE_OBSERVED["Rules"]
        assert adapter.observed("text", "text") == "text"

    def test_default_adapter_ignores_field(self):
        adapter = adapter_for("Power")
        assert adapter.describe("ON") == ""
        assert adapter.observed("ON", "OFF") == "OFF"
        assert adapter.prepare("ON") == "ON"


class TestSkipAdapter:
    def test_passwords(self):
        for name in ("Password1", "Password2", "Password3", "mqttpassword", "MqttPassword"):
            assert adapter_for(name).skip is True

    def test_other_commands_are_not_skipped(self):
        assert adapter_for("Module").skip is False
