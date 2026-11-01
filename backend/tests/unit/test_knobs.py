"""Unit tests for app.knobs — the settings module (docs/conventions.md §1–§3)."""

import pytest

from app.knobs import knob, knob_flag, knob_set, required_knob


class TestKnob:
    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("IS_TEST_KNOB", "value")
        assert knob("IS_TEST_KNOB", "default") == "value"

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("IS_TEST_KNOB", raising=False)
        assert knob("IS_TEST_KNOB", "default") == "default"

    def test_returns_none_without_default(self, monkeypatch):
        monkeypatch.delenv("IS_TEST_KNOB", raising=False)
        assert knob("IS_TEST_KNOB") is None

    def test_empty_string_is_returned_not_defaulted(self, monkeypatch):
        """Same as os.getenv: set-but-empty is a value, not a request for the default."""
        monkeypatch.setenv("IS_TEST_KNOB", "")
        assert knob("IS_TEST_KNOB", "default") == ""

    def test_value_is_not_stripped(self, monkeypatch):
        monkeypatch.setenv("IS_TEST_KNOB", "  spaced  ")
        assert knob("IS_TEST_KNOB") == "  spaced  "

    def test_reads_at_call_time(self, monkeypatch):
        monkeypatch.setenv("IS_TEST_KNOB", "first")
        assert knob("IS_TEST_KNOB") == "first"
        monkeypatch.setenv("IS_TEST_KNOB", "second")
        assert knob("IS_TEST_KNOB") == "second"


class TestRequiredKnob:
    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("IS_TEST_KNOB", "value")
        assert required_knob("IS_TEST_KNOB") == "value"

    def test_empty_value_counts_as_set(self, monkeypatch):
        monkeypatch.setenv("IS_TEST_KNOB", "")
        assert required_knob("IS_TEST_KNOB") == ""

    def test_raises_key_error_naming_the_variable(self, monkeypatch):
        """main.lifespan builds its startup error from str(KeyError) — keep it the variable name."""
        monkeypatch.delenv("IS_TEST_KNOB", raising=False)
        with pytest.raises(KeyError) as exc_info:
            required_knob("IS_TEST_KNOB")
        assert str(exc_info.value) == "'IS_TEST_KNOB'"


class TestKnobFlag:
    @pytest.mark.parametrize("value", ["false", "FALSE", " False ", "0", "no", "NO"])
    def test_off_spellings(self, monkeypatch, value):
        monkeypatch.setenv("IS_TEST_FLAG", value)
        assert knob_flag("IS_TEST_FLAG", "true") is False

    @pytest.mark.parametrize("value", ["true", "1", "yes", "off", "disabled", "", "flase"])
    def test_everything_else_is_on(self, monkeypatch, value):
        """Fail closed: only the three off spellings disable a switch."""
        monkeypatch.setenv("IS_TEST_FLAG", value)
        assert knob_flag("IS_TEST_FLAG", "true") is True

    def test_default_applies_when_unset(self, monkeypatch):
        monkeypatch.delenv("IS_TEST_FLAG", raising=False)
        assert knob_flag("IS_TEST_FLAG", "true") is True
        assert knob_flag("IS_TEST_FLAG", "false") is False


class TestKnobSet:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("IS_TEST_SET", raising=False)
        assert knob_set("IS_TEST_SET", "ory") == {"ory"}

    def test_strips_lowercases_and_drops_empty_entries(self, monkeypatch):
        monkeypatch.setenv("IS_TEST_SET", "Ory, ,DESCOPE ,")
        assert knob_set("IS_TEST_SET", "ory") == {"ory", "descope"}

    def test_empty_value_is_the_empty_set(self, monkeypatch):
        monkeypatch.setenv("IS_TEST_SET", "")
        assert knob_set("IS_TEST_SET", "ory") == set()
