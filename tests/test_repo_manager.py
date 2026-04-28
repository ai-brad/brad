import pytest
from unittest.mock import Mock, patch
from pathlib import Path
from brad.repo_manager import RepoManager
from test_helpers import make_test_config


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config with a temporary git repo."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    return make_test_config(tmp_path)


@pytest.fixture
def repo_manager(mock_config):
    """Create a RepoManager instance."""
    return RepoManager(mock_config)


def test_repo_manager_initialization(repo_manager, tmp_path):
    """Test RepoManager initialization."""
    assert repo_manager.repo_path == Path(tmp_path)


def test_repo_manager_invalid_path(tmp_path):
    """If the target path is missing and no github creds are set,
    RepoManager cannot self-bootstrap and must raise.
    """
    missing = tmp_path / "never-cloned"
    cfg = make_test_config(
        tmp_path, target_repo_path=str(missing), github_repo="", github_token=""
    )

    with pytest.raises(ValueError, match="Not a git repository"):
        RepoManager(cfg)


def test_repo_manager_self_bootstraps_clone(tmp_path):
    """If target path is missing and github creds are set, RepoManager clones."""
    missing = tmp_path / "bea"
    cfg = make_test_config(
        tmp_path,
        target_repo_path=str(missing),
        github_repo="flaerobotics/bea",
        github_token="fake-token",
    )

    clone_calls = []

    def fake_run(cmd, capture_output, text):
        clone_calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            target = cmd[-1]
            (Path(target) / ".git").mkdir(parents=True, exist_ok=True)
            return Mock(returncode=0, stdout="", stderr="")
        return Mock(returncode=1, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        RepoManager(cfg)

    assert any(c[:2] == ["git", "clone"] for c in clone_calls)
    clone_cmd = next(c for c in clone_calls if c[:2] == ["git", "clone"])
    assert (
        clone_cmd[2]
        == "https://x-access-token:fake-token@github.com/flaerobotics/bea.git"
    )


def test_run_git_success(repo_manager):
    """Test running a successful git command."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "success"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = repo_manager._run_git("status")
        assert result.returncode == 0


def test_run_git_failure(repo_manager):
    """Test running a failed git command."""
    mock_result = Mock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "fatal: not a git repository"

    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="Git command failed"):
            repo_manager._run_git("status")


def test_prepare_branch(repo_manager):
    """Test preparing a new feature branch."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        repo_manager.prepare_branch("DEV-123", base_branch="main")

        # Should have called fetch, checkout, reset, and checkout -b
        # We can't easily verify the exact calls without more complex mocking


def _git_result(stdout="", returncode=0):
    m = Mock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


def test_checkout_branch_existing_syncs_with_remote(repo_manager):
    """Existing local branch should be hard-reset to origin/<branch> to avoid stale tips."""
    branch_list = _git_result("* main\n  DEV-123\n")
    checkout = _git_result()
    ls_remote = _git_result("abc123\trefs/heads/DEV-123\n")  # remote exists
    fetch = _git_result()
    reset = _git_result()

    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        # Order: branch, checkout -f, ls-remote (branch_exists_remote),
        # fetch, reset --hard
        return [branch_list, checkout, ls_remote, fetch, reset][len(calls) - 1]

    with patch("subprocess.run", side_effect=fake_run):
        repo_manager.checkout_branch("DEV-123")

    # Verify the sync happened
    flat = [" ".join(c) for c in calls]
    assert any("checkout -f DEV-123" in c for c in flat)
    assert any("ls-remote --heads origin DEV-123" in c for c in flat)
    assert any("fetch origin DEV-123" in c for c in flat)
    assert any("reset --hard origin/DEV-123" in c for c in flat)


def test_checkout_branch_existing_skips_sync_when_no_remote(repo_manager):
    """Local-only branch should not attempt to fetch/reset against a missing remote ref."""
    branch_list = _git_result("* main\n  local-only\n")
    checkout = _git_result()
    ls_remote = _git_result("")  # remote does not exist

    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return [branch_list, checkout, ls_remote][len(calls) - 1]

    with patch("subprocess.run", side_effect=fake_run):
        repo_manager.checkout_branch("local-only")

    flat = [" ".join(c) for c in calls]
    assert not any("fetch origin local-only" in c for c in flat)
    assert not any("reset --hard origin/local-only" in c for c in flat)


def test_checkout_branch_not_exists(repo_manager):
    """Test checking out a branch that doesn't exist."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "* main\n"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="does not exist"):
            repo_manager.checkout_branch("NonExistent", create_if_missing=False)


def test_branch_exists_remote_true(repo_manager):
    """Test checking if branch exists on remote (exists)."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "refs/heads/DEV-123"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        exists = repo_manager.branch_exists_remote("DEV-123")
        assert exists is True


def test_branch_exists_remote_false(repo_manager):
    """Test checking if branch exists on remote (doesn't exist)."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        exists = repo_manager.branch_exists_remote("NonExistent")
        assert exists is False


def test_commit_all_with_changes(repo_manager):
    """Test committing when changes exist."""
    mock_add = Mock(returncode=0, stdout="", stderr="")
    mock_status = Mock(returncode=0, stdout="M file.txt\n", stderr="")
    mock_commit = Mock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=[mock_add, mock_status, mock_commit]):
        repo_manager.commit_all("Test commit message")


def test_commit_all_no_changes(repo_manager):
    """Test committing when no changes exist."""
    mock_add = Mock(returncode=0, stdout="", stderr="")
    mock_status = Mock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=[mock_add, mock_status]):
        repo_manager.commit_all("Test commit message")
        # Should not raise, just log warning


def test_push(repo_manager):
    """Test pushing a branch."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "To origin"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        repo_manager.push("DEV-123")


def test_push_force(repo_manager):
    """Test force pushing a branch."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        repo_manager.push("DEV-123", force=True)


def test_is_clean_working_tree_true(repo_manager):
    """Test checking clean working tree (clean)."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        is_clean = repo_manager.is_clean_working_tree()
        assert is_clean is True


def test_is_clean_working_tree_false(repo_manager):
    """Test checking clean working tree (has changes)."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "M file.txt\n"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        is_clean = repo_manager.is_clean_working_tree()
        assert is_clean is False


def test_get_current_branch(repo_manager):
    """Test getting current branch name."""
    mock_result = Mock()
    mock_result.returncode = 0
    mock_result.stdout = "main\n"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        branch = repo_manager.get_current_branch()
        assert branch == "main"
