"""Tests for the command comparison adapters."""

from __future__ import annotations

from pyinfra_tasmota.compare import adapter_for


class TestDefaultAdapter:
    def test_unknown_command_is_not_skipped(self):
        assert adapter_for("SetOption114").skip is False

    def test_folds_booleans(self):
        adapter = adapter_for("SetOption114")
        assert adapter.desired("1") == "1"
        assert adapter.observed(1) == "1"
        assert adapter.observed("ON") == "1"
        assert adapter.observed("off") == "0"

    def test_matches_canonically(self):
        assert adapter_for("setoption114") is adapter_for("SetOption114")
        assert adapter_for("so114") is adapter_for("SetOption114")


class TestStructAdapter:
    def test_time_commands(self):
        for name in ("TimeSTD", "TimeDST", "timestd", "timedst"):
            adapter = adapter_for(name)
            assert adapter.desired("0, 0, 3, 1, 1, 120") == "0,0,3,1,1,120"
            observed = {
                "Hemisphere": 0,
                "Week": 0,
                "Month": 3,
                "Day": 1,
                "Hour": 1,
                "Offset": 120,
            }
            assert adapter.observed(observed) == "0,0,3,1,1,120"

    def test_scalar_fallback(self):
        assert adapter_for("TimeDST").observed("1,2,3,4,5,6") == "1,2,3,4,5,6"


class TestFieldAdapter:
    def test_pulsetime_compares_set(self):
        adapter = adapter_for("Pulsetime3")
        assert adapter.observed({"Set": 10, "Remaining": 5}) == "10"

    def test_scalar_fallback(self):
        assert adapter_for("Pulsetime3").observed(10) == "10"


class TestDecimalAdapter:
    def test_truncates_desired(self):
        assert adapter_for("Latitude").desired("51.5073509") == "51.507350"

    def test_truncates_observed(self):
        assert adapter_for("Longitude").observed(51.5073509) == "51.507350"

    def test_negative_truncates_toward_zero(self):
        assert adapter_for("Longitude").observed(-0.1234567) == "-0.123456"

    def test_invalid_falls_back(self):
        assert adapter_for("Latitude").observed("nope") == "nope"


class TestAliasAdapter:
    def test_fulltopic_default(self):
        assert adapter_for("FullTopic").desired("1") == "%prefix%/%topic%/"

    def test_fulltopic_user_value(self):
        assert adapter_for("FullTopic").desired("tasmota/%topic%/") == "tasmota/%topic%/"

    def test_fulltopic_matches_canonically(self):
        assert adapter_for("fulltopic") is adapter_for("FullTopic")


class TestSkipAdapter:
    def test_passwords(self):
        for name in ("Password1", "Password2", "Password3", "mqttpassword", "MqttPassword"):
            assert adapter_for(name).skip is True

    def test_other_commands_are_not_skipped(self):
        assert adapter_for("Module").skip is False
