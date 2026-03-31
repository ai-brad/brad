"""Tests for codebase_map module."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from brad.codebase_map import (
    _extract_python_symbols,
    _build_tree,
    _format_map,
    get_codebase_map,
    MAX_MAP_CHARS,
)


@pytest.fixture
def sample_repo(tmp_path):
    """Create a minimal repo structure for testing."""
    # src/app/modules/text_agent/core.py
    mod_dir = tmp_path / "src" / "app" / "modules" / "text_agent"
    mod_dir.mkdir(parents=True)
    (mod_dir / "core.py").write_text(
        "class TextAgent:\n"
        "    def process(self): pass\n"
        "    def _internal(self): pass\n"
        "\n"
        "def create_agent(): pass\n"
    )
    (mod_dir / "__init__.py").write_text("")

    # src/app/base/services/messages.py
    svc_dir = tmp_path / "src" / "app" / "base" / "services"
    svc_dir.mkdir(parents=True)
    (svc_dir / "messages.py").write_text(
        "class MessageService:\n"
        "    def send(self): pass\n"
    )

    # tests/unit/modules/text_agent/test_core.py
    test_dir = tmp_path / "tests" / "unit" / "modules" / "text_agent"
    test_dir.mkdir(parents=True)
    (test_dir / "test_core.py").write_text(
        "def test_process(): pass\n"
    )

    # A non-python file that should be ignored
    (tmp_path / "README.md").write_text("# Hello")

    return tmp_path


class TestExtractPythonSymbols:
    def test_extracts_classes_and_functions(self, sample_repo):
        fp = sample_repo / "src" / "app" / "modules" / "text_agent" / "core.py"
        symbols = _extract_python_symbols(fp)
        assert any("class TextAgent" in s for s in symbols)
        assert any("def create_agent" in s for s in symbols)

    def test_skips_private_methods(self, sample_repo):
        fp = sample_repo / "src" / "app" / "modules" / "text_agent" / "core.py"
        symbols = _extract_python_symbols(fp)
        joined = " ".join(symbols)
        assert "_internal" not in joined

    def test_handles_syntax_error(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text("def broken(:\n")
        symbols = _extract_python_symbols(bad)
        assert symbols == []


class TestBuildTree:
    def test_finds_directories(self, sample_repo):
        data = _build_tree(str(sample_repo))
        tree_str = "\n".join(data["tree"])
        assert "src/app/modules/text_agent" in tree_str
        assert "src/app/base/services" in tree_str

    def test_finds_symbols(self, sample_repo):
        data = _build_tree(str(sample_repo))
        assert any("core.py" in k for k in data["symbols"])

    def test_skips_venv(self, sample_repo):
        venv_dir = sample_repo / ".venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "some.py").write_text("x = 1")
        data = _build_tree(str(sample_repo))
        tree_str = "\n".join(data["tree"])
        assert ".venv" not in tree_str


class TestFormatMap:
    def test_output_under_limit(self, sample_repo):
        data = _build_tree(str(sample_repo))
        text = _format_map(data)
        assert len(text) <= MAX_MAP_CHARS

    def test_contains_header(self, sample_repo):
        data = _build_tree(str(sample_repo))
        text = _format_map(data)
        assert "CODEBASE MAP" in text


class TestGetCodebaseMap:
    def test_generates_and_caches(self, sample_repo, tmp_path, monkeypatch):
        monkeypatch.setattr("brad.codebase_map.CACHE_DIR", tmp_path / ".cache")
        text = get_codebase_map(str(sample_repo), main_commit="abc123")
        assert "CODEBASE MAP" in text
        # Second call should hit cache
        text2 = get_codebase_map(str(sample_repo), main_commit="abc123")
        assert text2 == text

    def test_new_commit_regenerates(self, sample_repo, tmp_path, monkeypatch):
        monkeypatch.setattr("brad.codebase_map.CACHE_DIR", tmp_path / ".cache")
        text1 = get_codebase_map(str(sample_repo), main_commit="aaa")
        text2 = get_codebase_map(str(sample_repo), main_commit="bbb")
        # Both should be valid maps
        assert "CODEBASE MAP" in text1
        assert "CODEBASE MAP" in text2
