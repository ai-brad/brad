import pytest
from unittest.mock import Mock, patch
from ci_analyzer import CIAnalyzer, CIResult
from config import Config


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    return Config(
        jira_url="https://test.atlassian.net",
        jira_token="test-token",
        jira_user="test@example.com",
        jira_project_key="DEV",
        github_token="gh-token",
        github_repo="owner/repo",
        target_repo_path=str(tmp_path),
        claude_cli_path="claude",
        max_clarification_cycles=3,
        max_ci_fix_iterations=5,
        max_flaky_retries=1,
        ci_poll_interval=60,
        log_level="INFO",
        attachments_dir=str(tmp_path / "attachments")
    )


@pytest.fixture
def ci_analyzer(mock_config):
    """Create a CI analyzer instance."""
    return CIAnalyzer(mock_config)


def test_ci_analyzer_initialization(ci_analyzer):
    """Test CI analyzer initialization."""
    assert ci_analyzer.base_url == "https://api.github.com/repos/owner/repo"
    assert "token gh-token" in ci_analyzer.headers["Authorization"]


def test_analyze_run_success(ci_analyzer):
    """Test analyzing a successful workflow run."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jobs": [
            {"name": "test", "conclusion": "success"},
            {"name": "lint", "conclusion": "success"},
        ]
    }
    mock_response.raise_for_status = Mock()
    
    with patch("ci_analyzer.requests.get", return_value=mock_response):
        result = ci_analyzer._analyze_run(12345)
        
        assert result.success is True
        assert len(result.failed_jobs) == 0
        assert "test: success" in result.logs


def test_analyze_run_failure(ci_analyzer):
    """Test analyzing a failed workflow run."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jobs": [
            {"name": "test", "conclusion": "failure"},
            {"name": "lint", "conclusion": "success"},
        ]
    }
    mock_response.raise_for_status = Mock()
    
    with patch("ci_analyzer.requests.get", return_value=mock_response):
        result = ci_analyzer._analyze_run(12345)
        
        assert result.success is False
        assert len(result.failed_jobs) == 1
        assert "test" in result.failed_jobs
        assert "test: failure" in result.logs


def test_analyze_run_with_skipped(ci_analyzer):
    """Test that skipped jobs are not counted as failures."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jobs": [
            {"name": "test", "conclusion": "success"},
            {"name": "deploy", "conclusion": "skipped"},
        ]
    }
    mock_response.raise_for_status = Mock()
    
    with patch("ci_analyzer.requests.get", return_value=mock_response):
        result = ci_analyzer._analyze_run(12345)
        
        assert result.success is True
        assert len(result.failed_jobs) == 0


def test_get_workflow_runs_success(ci_analyzer):
    """Test getting workflow runs for a PR."""
    mock_commits_response = Mock()
    mock_commits_response.json.return_value = [
        {"sha": "abc123"},
        {"sha": "def456"},
    ]
    mock_commits_response.raise_for_status = Mock()
    
    mock_runs_response = Mock()
    mock_runs_response.json.return_value = {
        "workflow_runs": [
            {"id": 123, "status": "completed", "conclusion": "success"}
        ]
    }
    mock_runs_response.raise_for_status = Mock()
    
    with patch("ci_analyzer.requests.get", side_effect=[mock_commits_response, mock_runs_response]):
        runs = ci_analyzer._get_workflow_runs(42)
        
        assert len(runs) == 1
        assert runs[0]["id"] == 123


def test_get_workflow_runs_no_commits(ci_analyzer):
    """Test getting workflow runs when PR has no commits."""
    mock_response = Mock()
    mock_response.json.return_value = []
    mock_response.raise_for_status = Mock()
    
    with patch("ci_analyzer.requests.get", return_value=mock_response):
        runs = ci_analyzer._get_workflow_runs(42)
        assert len(runs) == 0


def test_get_workflow_runs_uses_head_sha(ci_analyzer):
    """Test that workflow runs are queried by head_sha, not branch."""
    mock_commits_response = Mock()
    mock_commits_response.json.return_value = [{"sha": "abc123"}]
    mock_commits_response.raise_for_status = Mock()
    
    mock_runs_response = Mock()
    mock_runs_response.json.return_value = {"workflow_runs": []}
    mock_runs_response.raise_for_status = Mock()
    
    with patch("ci_analyzer.requests.get", side_effect=[mock_commits_response, mock_runs_response]) as mock_get:
        ci_analyzer._get_workflow_runs(42)
        
        # Check that the second call used head_sha parameter
        second_call = mock_get.call_args_list[1]
        params = second_call.kwargs.get("params", {})
        assert "head_sha" in params
        assert params["head_sha"] == "abc123"


def test_wait_for_pr_immediate_success(ci_analyzer):
    """Test waiting for PR when CI completes immediately."""
    mock_commits_response = Mock()
    mock_commits_response.json.return_value = [{"sha": "abc123"}]
    mock_commits_response.raise_for_status = Mock()
    
    mock_runs_response = Mock()
    mock_runs_response.json.return_value = {
        "workflow_runs": [
            {"id": 123, "status": "completed", "conclusion": "success", "name": "CI"}
        ]
    }
    mock_runs_response.raise_for_status = Mock()
    
    mock_jobs_response = Mock()
    mock_jobs_response.json.return_value = {
        "jobs": [{"name": "test", "conclusion": "success"}]
    }
    mock_jobs_response.raise_for_status = Mock()
    
    with patch("ci_analyzer.requests.get", side_effect=[
        mock_commits_response,
        mock_runs_response,
        mock_jobs_response
    ]):
        result = ci_analyzer.wait_for_pr(42, poll_interval=1, timeout=10)
        
        assert result.success is True


def test_wait_for_pr_timeout(ci_analyzer):
    """Test that wait_for_pr times out appropriately."""
    mock_commits_response = Mock()
    mock_commits_response.json.return_value = [{"sha": "abc123"}]
    mock_commits_response.raise_for_status = Mock()
    
    mock_runs_response = Mock()
    mock_runs_response.json.return_value = {
        "workflow_runs": [
            {"id": 123, "status": "in_progress", "conclusion": None, "name": "CI"}
        ]
    }
    mock_runs_response.raise_for_status = Mock()
    
    # Create an infinite iterator
    def mock_get_side_effect(*args, **kwargs):
        if "commits" in args[0]:
            return mock_commits_response
        else:
            return mock_runs_response
    
    with patch("ci_analyzer.requests.get", side_effect=mock_get_side_effect):
        with patch("ci_analyzer.time.sleep"):
            result = ci_analyzer.wait_for_pr(42, poll_interval=1, timeout=2)
            
            assert result.success is False
            assert "timeout" in result.logs.lower()
