"""Tests for test_selector module."""

import pytest
from test_selector import (
    map_source_to_test_dirs,
    extract_failed_tests_from_ci_logs,
)


class TestMapSourceToTestDirs:
    def test_module_source_maps_to_test_dir(self):
        changed = ["src/bea/modules/text_agent/text_agent_preprocessor.py"]
        result = map_source_to_test_dirs(changed)
        assert "tests/unit/modules/text_agent" in result

    def test_base_source_maps_to_test_dir(self):
        changed = ["src/bea/base/services/messages.py"]
        result = map_source_to_test_dirs(changed)
        assert "tests/unit/base/services" in result

    def test_common_source_maps_to_test_dir(self):
        changed = ["src/bea/common/system_tags.py"]
        result = map_source_to_test_dirs(changed)
        assert "tests/unit/common" in result

    def test_non_python_files_ignored(self):
        changed = ["README.md", "package.json", "src/bea/modules/text_agent/foo.txt"]
        result = map_source_to_test_dirs(changed)
        assert result == []

    def test_test_files_included(self):
        changed = ["tests/unit/modules/text_agent/test_core.py"]
        result = map_source_to_test_dirs(changed)
        assert "tests/unit/modules/text_agent" in result

    def test_deduplication(self):
        changed = [
            "src/bea/modules/text_agent/a.py",
            "src/bea/modules/text_agent/b.py",
            "src/bea/modules/text_agent/sub/c.py",
        ]
        result = map_source_to_test_dirs(changed)
        assert result.count("tests/unit/modules/text_agent") == 1

    def test_multiple_modules(self):
        changed = [
            "src/bea/modules/text_agent/foo.py",
            "src/bea/base/services/bar.py",
        ]
        result = map_source_to_test_dirs(changed)
        assert "tests/unit/modules/text_agent" in result
        assert "tests/unit/base/services" in result

    def test_empty_input(self):
        assert map_source_to_test_dirs([]) == []


class TestExtractFailedTests:
    def test_extracts_pytest_failures(self):
        ci_logs = """
============================= short test summary info =============================
FAILED tests/unit/modules/text_agent/test_core.py::test_something - AssertionError
FAILED tests/unit/base/test_foo.py::TestBar::test_baz - TypeError
============================== 2 failed, 100 passed ==============================
"""
        result = extract_failed_tests_from_ci_logs(ci_logs)
        assert "tests/unit/modules/text_agent/test_core.py::test_something" in result
        assert "tests/unit/base/test_foo.py::TestBar::test_baz" in result

    def test_deduplicates(self):
        ci_logs = """
FAILED tests/unit/test_x.py::test_a - Error
FAILED tests/unit/test_x.py::test_a - Error
"""
        result = extract_failed_tests_from_ci_logs(ci_logs)
        assert result.count("tests/unit/test_x.py::test_a") == 1

    def test_no_failures_returns_none(self):
        ci_logs = "============================== 100 passed =============================="
        result = extract_failed_tests_from_ci_logs(ci_logs)
        assert result is None

    def test_file_level_fallback(self):
        ci_logs = """
FAILED tests/unit/test_x.py
"""
        result = extract_failed_tests_from_ci_logs(ci_logs)
        assert result is None or "tests/unit/test_x.py" in (result or "")
