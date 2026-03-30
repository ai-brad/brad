import pytest
from unittest.mock import Mock, patch
from ci_analyzer import CIAnalyzer, CIResult, DeploymentInfo
from config import Config
from test_helpers import make_test_config


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    return make_test_config(tmp_path)


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


# -------------------------
# Environment resolution tests
# -------------------------

def test_resolve_deployment_env_pr_0():
    """Test PR number 0 maps to int0."""
    info = CIAnalyzer.resolve_deployment_env(pr_number=0)
    assert info.environment == "int0"
    assert info.base_url == "https://int0.flaerobotics.ai"


def test_resolve_deployment_env_pr_modulo():
    """Test PR number modulo mapping (int0, int1, int2)."""
    assert CIAnalyzer.resolve_deployment_env(pr_number=3).environment == "int0"
    assert CIAnalyzer.resolve_deployment_env(pr_number=4).environment == "int1"
    assert CIAnalyzer.resolve_deployment_env(pr_number=5).environment == "int2"
    assert CIAnalyzer.resolve_deployment_env(pr_number=100).environment == "int1"


def test_resolve_deployment_env_main_branch():
    """Test main branch maps to dev."""
    info = CIAnalyzer.resolve_deployment_env(branch="main")
    assert info.environment == "dev"
    assert info.base_url == "https://dev.flaerobotics.ai"


def test_resolve_deployment_env_unknown_branch():
    """Test unknown branch returns None."""
    assert CIAnalyzer.resolve_deployment_env(branch="feature/foo") is None


def test_resolve_deployment_env_no_args():
    """Test no arguments returns None."""
    assert CIAnalyzer.resolve_deployment_env() is None


def test_resolve_deployment_env_pr_takes_precedence():
    """Test PR number takes precedence over branch."""
    info = CIAnalyzer.resolve_deployment_env(pr_number=42, branch="main")
    assert info.environment == "int0"  # 42 % 3 == 0


def test_resolve_deployment_env_urls():
    """Test that all URL fields are correct."""
    info = CIAnalyzer.resolve_deployment_env(pr_number=1)
    assert info.environment == "int1"
    assert info.base_url == "https://int1.flaerobotics.ai"
    assert info.api_version_url == "https://int1.flaerobotics.ai/api/version/v1/"
    assert info.api_config_url == "https://int1.flaerobotics.ai/api/config/v1/"


# -------------------------
# Analyze all runs tests
# -------------------------

def test_analyze_all_runs_all_pass(ci_analyzer):
    """Test analyzing multiple passing workflow runs."""
    runs = [
        {"id": 100, "name": "Build", "conclusion": "success"},
        {"id": 101, "name": "Deploy", "conclusion": "success"},
    ]
    
    mock_response = Mock()
    mock_response.json.return_value = {
        "jobs": [{"name": "test", "conclusion": "success"}]
    }
    mock_response.raise_for_status = Mock()
    
    with patch("ci_analyzer.requests.get", return_value=mock_response):
        result = ci_analyzer._analyze_all_runs(runs)
        assert result.success is True
        assert len(result.failed_jobs) == 0
        assert "Build" in result.logs
        assert "Deploy" in result.logs


def test_analyze_all_runs_mixed(ci_analyzer):
    """Test analyzing runs where one fails."""
    runs = [
        {"id": 100, "name": "Build", "conclusion": "success"},
        {"id": 101, "name": "Deploy", "conclusion": "failure"},
    ]
    
    def mock_get(*args, **kwargs):
        resp = Mock()
        resp.raise_for_status = Mock()
        if "100" in args[0]:
            resp.json.return_value = {"jobs": [{"name": "test", "conclusion": "success"}]}
        else:
            resp.json.return_value = {"jobs": [{"name": "deploy-int", "conclusion": "failure"}]}
        return resp
    
    with patch("ci_analyzer.requests.get", side_effect=mock_get):
        result = ci_analyzer._analyze_all_runs(runs)
        assert result.success is False
        assert "deploy-int" in result.failed_jobs


# -------------------------
# Wait for PR with multiple workflows
# -------------------------

def test_wait_for_pr_waits_for_all_workflows(ci_analyzer):
    """Test that wait_for_pr waits for ALL workflows, not just one."""
    mock_commits_response = Mock()
    mock_commits_response.json.return_value = [{"sha": "abc123"}]
    mock_commits_response.raise_for_status = Mock()
    
    # First poll: one completed, one in_progress
    mock_runs_partial = Mock()
    mock_runs_partial.json.return_value = {
        "workflow_runs": [
            {"id": 123, "status": "completed", "conclusion": "success", "name": "Build"},
            {"id": 124, "status": "in_progress", "conclusion": None, "name": "Deploy"},
        ]
    }
    mock_runs_partial.raise_for_status = Mock()
    
    # Second poll: all completed
    mock_runs_done = Mock()
    mock_runs_done.json.return_value = {
        "workflow_runs": [
            {"id": 123, "status": "completed", "conclusion": "success", "name": "Build"},
            {"id": 124, "status": "completed", "conclusion": "success", "name": "Deploy"},
        ]
    }
    mock_runs_done.raise_for_status = Mock()
    
    mock_jobs = Mock()
    mock_jobs.json.return_value = {"jobs": [{"name": "job1", "conclusion": "success"}]}
    mock_jobs.raise_for_status = Mock()
    
    call_count = [0]
    
    def mock_get(*args, **kwargs):
        if "commits" in args[0]:
            return mock_commits_response
        elif "actions/runs/" in args[0] and "jobs" in args[0]:
            return mock_jobs
        else:
            call_count[0] += 1
            if call_count[0] <= 1:
                return mock_runs_partial
            return mock_runs_done
    
    with patch("ci_analyzer.requests.get", side_effect=mock_get):
        with patch("ci_analyzer.time.sleep"):
            result = ci_analyzer.wait_for_pr(42, poll_interval=1, timeout=60)
            assert result.success is True


# -------------------------
# Get job logs tests
# -------------------------

def test_get_job_logs_failed_only(ci_analyzer):
    """Test fetching logs only for failed jobs."""
    mock_jobs_resp = Mock()
    mock_jobs_resp.json.return_value = {
        "jobs": [
            {"id": 10, "name": "test", "conclusion": "success"},
            {"id": 11, "name": "lint", "conclusion": "failure"},
        ]
    }
    mock_jobs_resp.raise_for_status = Mock()
    
    mock_log_resp = Mock()
    mock_log_resp.status_code = 200
    mock_log_resp.text = "Error: lint check failed on line 42"
    
    with patch("ci_analyzer.requests.get", side_effect=[mock_jobs_resp, mock_log_resp]):
        logs = ci_analyzer.get_job_logs(12345, failed_only=True)
        assert "lint" in logs
        assert "line 42" in logs


def test_get_job_logs_all(ci_analyzer):
    """Test fetching logs for all jobs."""
    mock_jobs_resp = Mock()
    mock_jobs_resp.json.return_value = {
        "jobs": [
            {"id": 10, "name": "test", "conclusion": "success"},
            {"id": 11, "name": "lint", "conclusion": "failure"},
        ]
    }
    mock_jobs_resp.raise_for_status = Mock()
    
    mock_log_resp_1 = Mock()
    mock_log_resp_1.status_code = 200
    mock_log_resp_1.text = "All tests passed"
    
    mock_log_resp_2 = Mock()
    mock_log_resp_2.status_code = 200
    mock_log_resp_2.text = "Lint failed"
    
    with patch("ci_analyzer.requests.get", side_effect=[mock_jobs_resp, mock_log_resp_1, mock_log_resp_2]):
        logs = ci_analyzer.get_job_logs(12345, failed_only=False)
        assert "test" in logs
        assert "lint" in logs


def test_get_job_logs_truncates_long_output(ci_analyzer):
    """Test that very long job logs are truncated."""
    mock_jobs_resp = Mock()
    mock_jobs_resp.json.return_value = {
        "jobs": [{"id": 10, "name": "test", "conclusion": "failure"}]
    }
    mock_jobs_resp.raise_for_status = Mock()
    
    mock_log_resp = Mock()
    mock_log_resp.status_code = 200
    mock_log_resp.text = "x" * 20000
    
    with patch("ci_analyzer.requests.get", side_effect=[mock_jobs_resp, mock_log_resp]):
        logs = ci_analyzer.get_job_logs(12345, failed_only=True)
        assert "truncated" in logs


# -------------------------
# Get failed run IDs tests
# -------------------------

def test_get_failed_run_ids(ci_analyzer):
    """Test getting IDs of failed runs."""
    mock_commits_resp = Mock()
    mock_commits_resp.json.return_value = [{"sha": "abc123"}]
    mock_commits_resp.raise_for_status = Mock()
    
    mock_runs_resp = Mock()
    mock_runs_resp.json.return_value = {
        "workflow_runs": [
            {"id": 100, "status": "completed", "conclusion": "success"},
            {"id": 101, "status": "completed", "conclusion": "failure"},
            {"id": 102, "status": "in_progress", "conclusion": None},
        ]
    }
    mock_runs_resp.raise_for_status = Mock()
    
    with patch("ci_analyzer.requests.get", side_effect=[mock_commits_resp, mock_runs_resp]):
        failed = ci_analyzer.get_failed_run_ids(42)
        assert failed == [101]


# -------------------------
# Deployment health check tests
# -------------------------

def test_check_deployment_health_success(ci_analyzer):
    """Test health check when deployment is healthy."""
    mock_version = Mock()
    mock_version.status_code = 200
    mock_version.json.return_value = {"version": "1.2.3"}
    
    mock_config_resp = Mock()
    mock_config_resp.status_code = 200
    
    with patch("ci_analyzer.requests.get", side_effect=[mock_version, mock_config_resp]):
        result = ci_analyzer.check_deployment_health(pr_number=1, max_attempts=1)
        assert result["healthy"] is True
        assert result["environment"] == "int1"


def test_check_deployment_health_failure(ci_analyzer):
    """Test health check when deployment is not healthy."""
    mock_resp = Mock()
    mock_resp.status_code = 503
    
    with patch("ci_analyzer.requests.get", return_value=mock_resp):
        with patch("ci_analyzer.time.sleep"):
            result = ci_analyzer.check_deployment_health(pr_number=1, max_attempts=2, wait_seconds=0)
            assert result["healthy"] is False
            assert "error" in result


def test_check_deployment_health_no_env(ci_analyzer):
    """Test health check when environment cannot be resolved."""
    result = ci_analyzer.check_deployment_health(branch="feature/foo")
    assert result["healthy"] is False
    assert "Could not resolve" in result["error"]
