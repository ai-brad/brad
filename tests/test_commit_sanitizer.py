"""Tests for the AI-driven commit sanitizer."""

import subprocess
from unittest.mock import MagicMock

import pytest

from brad.adapters.llm.base import LLMResult, LLMUsage
from brad.commit_sanitizer import CommitSanitizer
from brad.repo_manager import RepoManager
from test_helpers import make_test_config


# ----------------------------------------------------------------------
# Pure unit tests (no git required) for the heuristic filter
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "FEATURE_PLAN.md",
        "IMPLEMENTATION_SUMMARY.md",
        "docs/progress_notes.md",
        "playwright-report/index.html",
        "test-results/retries/trace.zip",
        "log.html",
        "trace.zip",
        "build/output.js",
        "dist/app.bundle.js",
        "debug_repro.py",
        "scratch_check.py",
        "tmp_script.sh",
        "fix_bug.ps1",
        "coverage/lcov.info",
        "prd_draft.md",
        "something.log",
    ],
)
def test_suspicious_paths_detected(path):
    assert CommitSanitizer._is_suspicious(path), f"expected {path!r} to be suspicious"


@pytest.mark.parametrize(
    "path",
    [
        "src/module/foo.py",
        "tests/unit/test_foo.py",
        "frontend/src/components/Button.tsx",
        "migrations/0042_add_column.py",
        "app/templates/index.html",
        "public/index.html",
        "src/api/handler.go",
        "lib/util.rs",
    ],
)
def test_legitimate_paths_not_suspicious(path):
    assert not CommitSanitizer._is_suspicious(path), (
        f"{path!r} should not be suspicious"
    )


def test_parse_json_array_plain():
    out = CommitSanitizer._parse_json_array(
        '[{"path": "x.md", "action": "remove", "reason": "plan"}]'
    )
    assert out == [{"path": "x.md", "action": "remove", "reason": "plan"}]


def test_parse_json_array_with_fence_and_prose():
    text = 'Here is the answer:\n```json\n[{"path": "a", "action": "keep"}]\n```\n'
    assert CommitSanitizer._parse_json_array(text) == [{"path": "a", "action": "keep"}]


def test_parse_json_array_invalid_returns_none():
    assert CommitSanitizer._parse_json_array("not json") is None


# ----------------------------------------------------------------------
# Integration-ish tests with a real temp git repo
# ----------------------------------------------------------------------


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    """Create a tiny git repo with an 'origin/main' and a feature branch that
    added: one real source file, one plan markdown, one playwright log."""
    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    repo.mkdir()
    # Bare origin
    subprocess.run(
        ["git", "init", "--bare", str(origin)], check=True, capture_output=True
    )
    # Working repo
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@t"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    (repo / "README.md").write_text("# base\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    _run(["git", "push", "-u", "origin", "main"], repo)

    _run(["git", "checkout", "-b", "DEV-1"], repo)
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("def f():\n    return 1\n")
    (repo / "FEATURE_PLAN.md").write_text("# plan\n- step 1\n- step 2\n")
    (repo / "playwright-report").mkdir()
    (repo / "playwright-report" / "index.html").write_text("<html>log</html>")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "DEV-1: add feature + junk"], repo)
    return repo


def _make_sanitizer(repo_path, llm_text):
    cfg = make_test_config(repo_path)
    rm = RepoManager(cfg)
    llm = MagicMock()
    llm.run.return_value = LLMResult(text=llm_text, response_id=None, usage=LLMUsage())
    return CommitSanitizer(rm, llm), rm, llm


def test_run_removes_flagged_files_and_keeps_others(git_repo):
    llm_text = (
        "[\n"
        '  {"path": "FEATURE_PLAN.md", "action": "remove", "reason": "plan file"},\n'
        '  {"path": "playwright-report/index.html", "action": "remove", "reason": "test log"}\n'
        "]"
    )
    sanitizer, rm, llm = _make_sanitizer(git_repo, llm_text)

    summary = sanitizer.run(
        issue_key="DEV-1",
        issue_description="Implement feature.",
        branch_name="DEV-1",
    )

    assert set(summary["removed"]) == {
        "FEATURE_PLAN.md",
        "playwright-report/index.html",
    }
    assert not (git_repo / "FEATURE_PLAN.md").exists()
    assert not (git_repo / "playwright-report" / "index.html").exists()
    # Real source file must still be present
    assert (git_repo / "src" / "feature.py").exists()
    # A follow-up commit should exist
    log = subprocess.run(
        ["git", "log", "--oneline", "origin/main..HEAD"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "remove unintended artifacts" in log


def test_run_keeps_all_when_llm_says_keep(git_repo):
    llm_text = (
        '[{"path": "FEATURE_PLAN.md", "action": "keep", "reason": "user asked for docs"},'
        ' {"path": "playwright-report/index.html", "action": "keep", "reason": "ok"}]'
    )
    sanitizer, _, _ = _make_sanitizer(git_repo, llm_text)

    summary = sanitizer.run(
        issue_key="DEV-1",
        issue_description="",
        branch_name="DEV-1",
    )
    assert summary["removed"] == []
    assert (git_repo / "FEATURE_PLAN.md").exists()


def test_run_fails_open_on_invalid_llm_output(git_repo):
    sanitizer, _, _ = _make_sanitizer(git_repo, "not JSON at all")
    summary = sanitizer.run(
        issue_key="DEV-1",
        issue_description="",
        branch_name="DEV-1",
    )
    assert summary["removed"] == []
    assert summary["error"] == "classification_failed"
    assert (git_repo / "FEATURE_PLAN.md").exists()


def test_run_no_added_files_skips_llm(tmp_path):
    # Repo where branch == main: no added files
    repo = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "--bare", str(origin)], check=True, capture_output=True
    )
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@t"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    _run(["git", "remote", "add", "origin", str(origin)], repo)
    (repo / "README.md").write_text("# base\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "init"], repo)
    _run(["git", "push", "-u", "origin", "main"], repo)

    sanitizer, _, llm = _make_sanitizer(repo, "[]")
    summary = sanitizer.run(issue_key="DEV-1", issue_description="", branch_name="main")
    assert summary == {"candidates": 0, "removed": [], "kept": [], "error": None}
    llm.run.assert_not_called()
