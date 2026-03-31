"""Tests for phase_cache module."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from brad.phase_cache import _make_key, get_cached_phase, set_cached_phase, CACHE_DIR


@pytest.fixture(autouse=True)
def clean_cache(tmp_path, monkeypatch):
    """Redirect CACHE_DIR to a temp directory for each test."""
    monkeypatch.setattr("brad.phase_cache.CACHE_DIR", tmp_path / ".brad_cache")
    yield


class TestMakeKey:
    def test_deterministic(self):
        k1 = _make_key("abc123", "2026-03-30T10:00:00", "endpoint|model")
        k2 = _make_key("abc123", "2026-03-30T10:00:00", "endpoint|model")
        assert k1 == k2

    def test_differs_on_commit(self):
        k1 = _make_key("abc123", "2026-03-30T10:00:00", "endpoint|model")
        k2 = _make_key("def456", "2026-03-30T10:00:00", "endpoint|model")
        assert k1 != k2

    def test_differs_on_jira_updated(self):
        k1 = _make_key("abc123", "2026-03-30T10:00:00", "endpoint|model")
        k2 = _make_key("abc123", "2026-03-30T11:00:00", "endpoint|model")
        assert k1 != k2

    def test_differs_on_model(self):
        k1 = _make_key("abc123", "2026-03-30T10:00:00", "endpoint|model-a")
        k2 = _make_key("abc123", "2026-03-30T10:00:00", "endpoint|model-b")
        assert k1 != k2

    def test_key_is_24_chars(self):
        k = _make_key("x", "y", "z")
        assert len(k) == 24


class TestCacheRoundTrip:
    def test_miss_returns_none(self):
        result = get_cached_phase("DEV-100", "requirements", "a", "b", "c")
        assert result is None

    def test_set_then_get(self):
        data = {"action": "ready", "message": "looks good"}
        set_cached_phase("DEV-100", "requirements", "a", "b", "c", data)
        got = get_cached_phase("DEV-100", "requirements", "a", "b", "c")
        assert got == data

    def test_stale_key_invalidated(self):
        data1 = {"action": "ready", "message": "v1"}
        data2 = {"action": "clarify", "message": "v2"}

        set_cached_phase("DEV-100", "requirements", "commit1", "t1", "m", data1)
        assert get_cached_phase("DEV-100", "requirements", "commit1", "t1", "m") == data1

        # New commit → new key → old entry should be cleaned
        set_cached_phase("DEV-100", "requirements", "commit2", "t1", "m", data2)
        assert get_cached_phase("DEV-100", "requirements", "commit2", "t1", "m") == data2
        assert get_cached_phase("DEV-100", "requirements", "commit1", "t1", "m") is None

    def test_different_issues_independent(self):
        d1 = {"action": "ready"}
        d2 = {"action": "clarify"}
        set_cached_phase("DEV-1", "requirements", "a", "b", "c", d1)
        set_cached_phase("DEV-2", "requirements", "a", "b", "c", d2)
        assert get_cached_phase("DEV-1", "requirements", "a", "b", "c") == d1
        assert get_cached_phase("DEV-2", "requirements", "a", "b", "c") == d2
