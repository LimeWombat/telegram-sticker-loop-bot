import pytest
from bot import clamp, normalize_hex, parse_int_list, format_duration, prune_window
from bot import env_int, env_float, env_bool, env_str
from collections import deque
import os


class TestClamp:
    def test_clamp_below(self):
        assert clamp(-5, 0, 10) == 0

    def test_clamp_above(self):
        assert clamp(20, 0, 10) == 10

    def test_clamp_inside(self):
        assert clamp(5, 0, 10) == 5

    def test_clamp_at_bounds(self):
        assert clamp(0, 0, 10) == 0
        assert clamp(10, 0, 10) == 10


class TestNormalizeHex:
    def test_with_hash(self):
        assert normalize_hex("#ff0000") == "#ff0000"

    def test_without_hash(self):
        assert normalize_hex("ff0000") == "#ff0000"

    def test_short_hex(self):
        assert normalize_hex("#f00") == "#ff0000"

    def test_short_hex_no_hash(self):
        assert normalize_hex("f00") == "#ff0000"

    def test_uppercase(self):
        assert normalize_hex("#FF0000") == "#ff0000"

    def test_mixed_case(self):
        assert normalize_hex("#AbC123") == "#abc123"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            normalize_hex("not-a-color")
        with pytest.raises(ValueError):
            normalize_hex("")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            normalize_hex("")

    def test_three_digit_without_hash(self):
        assert normalize_hex("abc") == "#aabbcc"

    def test_six_digit_without_hash(self):
        assert normalize_hex("abcdef") == "#abcdef"


class TestParseIntList:
    def test_empty(self):
        assert parse_int_list(None) == set()
        assert parse_int_list("") == set()

    def test_single(self):
        assert parse_int_list("42") == {42}

    def test_multiple(self):
        assert parse_int_list("1,2,3") == {1, 2, 3}

    def test_with_spaces(self):
        assert parse_int_list(" 1 , 2 , 3 ") == {1, 2, 3}

    def test_invalid_skipped(self):
        assert parse_int_list("1,abc,3") == {1, 3}

    def test_whitespace_only(self):
        assert parse_int_list("   ") == set()


class TestFormatDuration:
    def test_seconds_only(self):
        assert "сек" in format_duration(5.0)

    def test_minutes(self):
        assert "мин" in format_duration(90.0)

    def test_hours(self):
        assert "ч" in format_duration(4000.0)

    def test_zero(self):
        result = format_duration(0.0)
        assert isinstance(result, str)
        assert len(result) > 0


class TestPruneWindow:
    def test_removes_old_events(self):
        items = deque([10.0, 20.0, 30.0, 40.0, 50.0])
        prune_window(items, now=60.0, window_seconds=15)
        assert list(items) == [50.0]

    def test_keeps_all_recent(self):
        items = deque([45.0, 50.0, 55.0])
        prune_window(items, now=60.0, window_seconds=30)
        assert len(items) == 3

    def test_empty_queue(self):
        items = deque()
        prune_window(items, now=60.0, window_seconds=10)
        assert len(items) == 0


class TestEnvParsers:
    def test_env_int(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "42")
        assert env_int("TEST_INT", 0) == 42

    def test_env_int_default(self, monkeypatch):
        monkeypatch.delenv("TEST_INT", raising=False)
        assert env_int("TEST_INT", 10) == 10

    def test_env_float(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT", "2.5")
        assert env_float("TEST_FLOAT", 1.0) == 2.5

    def test_env_bool_true(self, monkeypatch):
        monkeypatch.setenv("TEST_BOOL", "true")
        assert env_bool("TEST_BOOL", False) is True

    def test_env_bool_false(self, monkeypatch):
        monkeypatch.setenv("TEST_BOOL", "false")
        assert env_bool("TEST_BOOL", True) is False

    def test_env_str(self, monkeypatch):
        monkeypatch.setenv("TEST_STR", "hello")
        assert env_str("TEST_STR", "") == "hello"

    def test_env_str_default(self, monkeypatch):
        monkeypatch.delenv("TEST_STR", raising=False)
        assert env_str("TEST_STR", "fallback") == "fallback"
