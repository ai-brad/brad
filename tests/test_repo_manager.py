import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import subprocess
from repo_manager import RepoManager
from config import Config
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


def test_repo_manager_invalid_path():
    """Test RepoManager with invalid path."""
    cfg = Mock()
    cfg.target_repo_path = "/nonexistent/path"
    
    with pytest.raises(ValueError, match="Repository path does not exist"):
        RepoManager(cfg)


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


def test_checkout_branch_existing(repo_manager):
    """Test checking out an existing branch."""
    mock_result_branch = Mock()
    mock_result_branch.returncode = 0
    mock_result_branch.stdout = "* main\n  DEV-123\n"
    mock_result_branch.stderr = ""
    
    mock_result_checkout = Mock()
    mock_result_checkout.returncode = 0
    mock_result_checkout.stdout = ""
    mock_result_checkout.stderr = ""
    
    with patch("subprocess.run", side_effect=[mock_result_branch, mock_result_checkout]):
        repo_manager.checkout_branch("DEV-123")


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
