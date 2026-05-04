"""End-to-end-ish tests for brad.conflict_context using a real git repo.

These tests exercise the gathering pipeline against an actual on-disk git
repo with a real merge conflict, so they cover the bits that mocking would
hide (stage blobs, blame ranges, conflict-marker parsing).
"""

import subprocess
from unittest.mock import Mock

import pytest

from brad.conflict_context import (
    ConflictContext,
    extract_jira_key,
    find_conflict_line_ranges,
    gather_context,
)
from brad.repo_manager import RepoManager
from test_helpers import make_test_config


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def conflicted_repo(tmp_path):
    """Create a real git repo with an in-progress rebase conflict.

    Layout:
        main:    foo.py with a function ``greet`` returning "hello main"
        DEV-42:  forks before main's change, modifies the same line to "hi feature"

    After ``git rebase main`` on DEV-42 there is a conflict in foo.py.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    foo = repo / "foo.py"
    foo.write_text("def greet():\n    return 'hello'\n")
    _git(repo, "add", "foo.py")
    _git(repo, "commit", "-m", "DEV-1: initial greet")

    # Create the feature branch FIRST, then diverge on main.
    _git(repo, "checkout", "-b", "DEV-42")
    foo.write_text("def greet():\n    return 'hi feature'\n")
    _git(repo, "commit", "-am", "DEV-42: feature greet")

    _git(repo, "checkout", "main")
    foo.write_text("def greet():\n    return 'hello main'\n")
    _git(repo, "commit", "-am", "DEV-100: main-side greet")

    # Set up a fake "origin/main" that conflict_context can blame against.
    _git(repo, "branch", "-f", "origin/main", "main")
    # Same trick for the feature branch's upstream-equivalent ref.
    _git(repo, "checkout", "DEV-42")

    # Trigger the conflict by rebasing onto main (which we'll reference as
    # ``origin/main`` in the gather call — the fake ref above makes that work
    # without needing a real remote).
    result = subprocess.run(
        ["git", "-C", str(repo), "rebase", "main"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "expected conflict but rebase succeeded"
    return repo


@pytest.fixture
def repo_manager(conflicted_repo):
    cfg = make_test_config(
        conflicted_repo,
        target_repo_path=str(conflicted_repo),
        github_repo="",
        github_token="",
    )
    return RepoManager(cfg)


# ---------------------------------------------------------------------------
# unit-ish helpers
# ---------------------------------------------------------------------------


def test_extract_jira_key():
    assert extract_jira_key("DEV-42") == "DEV-42"
    assert extract_jira_key("brad/DEV-3205-fix") == "DEV-3205"
    assert extract_jira_key("Brad: DEV-100 do thing") == "DEV-100"
    assert extract_jira_key("nothing here") is None
    assert extract_jira_key("") is None


def test_find_conflict_line_ranges_simple():
    content = (
        "line1\n"
        "<<<<<<< HEAD\n"
        "ours-a\n"
        "ours-b\n"
        "=======\n"
        "theirs-a\n"
        ">>>>>>> branch\n"
        "trailing\n"
    )
    ranges = find_conflict_line_ranges(content)
    assert len(ranges) == 1
    r = ranges[0]
    # ours: lines 3..4 ; theirs: line 6
    assert r["ours_start"] == 3
    assert r["ours_end"] == 4
    assert r["theirs_start"] == 6
    assert r["theirs_end"] == 6


def test_find_conflict_line_ranges_no_conflict():
    assert find_conflict_line_ranges("just plain text\n") == []


# ---------------------------------------------------------------------------
# RepoManager helpers
# ---------------------------------------------------------------------------


def test_list_unmerged_files_returns_conflicts(repo_manager):
    unmerged = repo_manager.list_unmerged_files()
    assert "foo.py" in unmerged


def test_is_rebase_in_progress(repo_manager):
    assert repo_manager.is_rebase_in_progress() is True


def test_show_stage_blob_returns_each_side(repo_manager):
    base = repo_manager.show_stage_blob(1, "foo.py")
    ours = repo_manager.show_stage_blob(2, "foo.py")
    theirs = repo_manager.show_stage_blob(3, "foo.py")
    # During `git rebase main` from DEV-42, "ours" (stage 2) is the rebased
    # tip (main) and "theirs" (stage 3) is the cherry-picked feature commit.
    # Either way both stages exist and the base is the common ancestor.
    assert "hello" in base
    assert ours.strip() != ""
    assert theirs.strip() != ""
    assert ours != theirs


def test_force_push_with_lease_refuses_main(repo_manager):
    with pytest.raises(ValueError):
        repo_manager.force_push_with_lease("main")
    with pytest.raises(ValueError):
        repo_manager.force_push_with_lease("master")


def test_abort_rebase_clears_state(repo_manager):
    repo_manager.abort_rebase()
    assert repo_manager.is_rebase_in_progress() is False


# ---------------------------------------------------------------------------
# gather_context
# ---------------------------------------------------------------------------


def test_gather_context_collects_blobs_and_jira(repo_manager, conflicted_repo):
    fake_jira = Mock()
    fake_jira.fetch_issue.return_value = {
        "fields": {
            "summary": "Add feature greeting",
            "description": "We want greet() to say 'hi feature'.",
        }
    }
    fake_code_repo = Mock()
    fake_code_repo.get_prs_for_commit.return_value = []  # no PRs reachable

    ctx = gather_context(
        repo=repo_manager,
        branch_name="DEV-42",
        base_branch="main",
        conflicted_files=["foo.py"],
        code_repo=fake_code_repo,
        ticketing=fake_jira,
        logger=None,
    )

    assert isinstance(ctx, ConflictContext)
    assert ctx.feature_jira is not None
    assert ctx.feature_jira.key == "DEV-42"
    assert "Add feature greeting" in ctx.feature_jira.summary

    assert len(ctx.files) == 1
    f = ctx.files[0]
    assert f.path == "foo.py"
    assert "<<<<<<<" in f.working_copy
    assert "hello" in f.base_blob
    assert f.ours_blob.strip() != ""
    assert f.theirs_blob.strip() != ""

    rendered = ctx.to_prompt_section()
    assert "DEV-42" in rendered
    assert "foo.py" in rendered
    assert "<<<<<<<" in rendered  # the working copy is included


def test_gather_context_survives_missing_jira(repo_manager):
    # ticketing returns None for everything — must not raise.
    fake_jira = Mock()
    fake_jira.fetch_issue.return_value = None

    ctx = gather_context(
        repo=repo_manager,
        branch_name="DEV-42",
        base_branch="main",
        conflicted_files=["foo.py"],
        code_repo=None,
        ticketing=fake_jira,
        logger=None,
    )
    # No Jira data, but file context still collected.
    assert ctx.feature_jira is None
    assert len(ctx.files) == 1
    assert ctx.files[0].path == "foo.py"


def test_gather_context_handles_no_jira_key_in_branch(repo_manager):
    fake_jira = Mock()
    ctx = gather_context(
        repo=repo_manager,
        branch_name="random-branch-name",
        base_branch="main",
        conflicted_files=["foo.py"],
        ticketing=fake_jira,
        logger=None,
    )
    # No key extracted → no Jira lookup → no fetch_issue call.
    assert ctx.feature_jira is None
    fake_jira.fetch_issue.assert_not_called()
