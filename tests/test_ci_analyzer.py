import pytest
from unittest.mock import Mock, patch
from brad.adapters.ci_cd.github_actions_adapter import GitHubActionsAdapter
from brad.adapters.ci_cd.base import CIResult
from brad.config import Config
from test_helpers import make_test_config


PATCH_PREFIX = "brad.adapters.ci_cd.github_actions_adapter"


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    return make_test_config(tmp_path)


@pytest.fixture
def ci_adapter(mock_config):
    """Create a GitHub Actions CI/CD adapter instance."""
    return GitHubActionsAdapter(mock_config)


def test_ci_adapter_initialization(ci_adapter):
    """Test CI adapter initialization."""
    assert ci_adapter.base_url == "https://api.github.com/repos/owner/repo"
    assert "token gh-token" in ci_adapter.headers["Authorization"]


def test_analyze_run_success(ci_adapter):
    """Test analyzing a successful workflow run."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jobs": [
            {"name": "test", "conclusion": "success"},
            {"name": "lint", "conclusion": "success"},
        ]
    }
    mock_response.raise_for_status = Mock()

    with patch(f"{PATCH_PREFIX}.requests.get", return_value=mock_response):
        result = ci_adapter._analyze_run(12345)

        assert result.success is True
        assert len(result.failed_jobs) == 0
        assert "test: success" in result.logs


def test_analyze_run_failure(ci_adapter):
    """Test analyzing a failed workflow run."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jobs": [
            {"name": "test", "conclusion": "failure"},
            {"name": "lint", "conclusion": "success"},
        ]
    }
    mock_response.raise_for_status = Mock()

    with patch(f"{PATCH_PREFIX}.requests.get", return_value=mock_response):
        result = ci_adapter._analyze_run(12345)

        assert result.success is False
        assert len(result.failed_jobs) == 1
        assert "test" in result.failed_jobs
        assert "test: failure" in result.logs


def test_analyze_run_with_skipped(ci_adapter):
    """Test that skipped jobs are not counted as failures."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "jobs": [
            {"name": "test", "conclusion": "success"},
            {"name": "deploy", "conclusion": "skipped"},
        ]
    }
    mock_response.raise_for_status = Mock()

    with patch(f"{PATCH_PREFIX}.requests.get", return_value=mock_response):
        result = ci_adapter._analyze_run(12345)

        assert result.success is True
        assert len(result.failed_jobs) == 0


def test_get_workflow_runs_success(ci_adapter):
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

    with patch(f"{PATCH_PREFIX}.requests.get", side_effect=[mock_commits_response, mock_runs_response]):
        runs = ci_adapter._get_workflow_runs(42)

        assert len(runs) == 1
        assert runs[0]["id"] == 123


def test_get_workflow_runs_no_commits(ci_adapter):
    """Test getting workflow runs when PR has no commits."""
    mock_response = Mock()
    mock_response.json.return_value = []
    mock_response.raise_for_status = Mock()

    with patch(f"{PATCH_PREFIX}.requests.get", return_value=mock_response):
        runs = ci_adapter._get_workflow_runs(42)
        assert len(runs) == 0


def test_get_workflow_runs_uses_head_sha(ci_adapter):
    """Test that workflow runs are queried by head_sha, not branch."""
    mock_commits_response = Mock()
    mock_commits_response.json.return_value = [{"sha": "abc123"}]
    mock_commits_response.raise_for_status = Mock()

    mock_runs_response = Mock()
    mock_runs_response.json.return_value = {"workflow_runs": []}
    mock_runs_response.raise_for_status = Mock()

    with patch(f"{PATCH_PREFIX}.requests.get", side_effect=[mock_commits_response, mock_runs_response]) as mock_get:
        ci_adapter._get_workflow_runs(42)

        second_call = mock_get.call_args_list[1]
        params = second_call.kwargs.get("params", {})
        assert "head_sha" in params
        assert params["head_sha"] == "abc123"


def test_wait_for_pr_immediate_success(ci_adapter):
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

    with patch(f"{PATCH_PREFIX}.requests.get", side_effect=[
        mock_commits_response,
        mock_runs_response,
        mock_jobs_response
    ]):
        result = ci_adapter.wait_for_pr(42, poll_interval=1, timeout=10)

        assert result.success is True


def test_wait_for_pr_timeout(ci_adapter):
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

    def mock_get_side_effect(*args, **kwargs):
        if "commits" in args[0]:
            return mock_commits_response
        else:
            return mock_runs_response

    with patch(f"{PATCH_PREFIX}.requests.get", side_effect=mock_get_side_effect):
        with patch(f"{PATCH_PREFIX}.time.sleep"):
            result = ci_adapter.wait_for_pr(42, poll_interval=1, timeout=2)

            assert result.success is False
            assert "timeout" in result.logs.lower()


# -------------------------
# Resolve deployment env (default returns None)
# -------------------------

def test_resolve_deployment_env_default_returns_none(ci_adapter):
    """Test that default resolve_deployment_env returns None."""
    assert ci_adapter.resolve_deployment_env(pr_number=1) is None
    assert ci_adapter.resolve_deployment_env(branch="main") is None


# -------------------------
# Analyze all runs tests
# -------------------------

def test_analyze_all_runs_all_pass(ci_adapter):
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

    with patch(f"{PATCH_PREFIX}.requests.get", return_value=mock_response):
        result = ci_adapter._analyze_all_runs(runs)
        assert result.success is True
        assert len(result.failed_jobs) == 0
        assert "Build" in result.logs
        assert "Deploy" in result.logs


def test_analyze_all_runs_mixed(ci_adapter):
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
            resp.json.return_value = {"jobs": [{"name": "deploy-step", "conclusion": "failure"}]}
        return resp

    with patch(f"{PATCH_PREFIX}.requests.get", side_effect=mock_get):
        result = ci_adapter._analyze_all_runs(runs)
        assert result.success is False
        assert "deploy-step" in result.failed_jobs


# -------------------------
# Get job logs tests
# -------------------------

def test_get_job_logs_failed_only(ci_adapter):
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

    with patch(f"{PATCH_PREFIX}.requests.get", side_effect=[mock_jobs_resp, mock_log_resp]):
        logs = ci_adapter.get_job_logs(12345, failed_only=True)
        assert "lint" in logs
        assert "line 42" in logs


def test_get_job_logs_all(ci_adapter):
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

    with patch(f"{PATCH_PREFIX}.requests.get", side_effect=[mock_jobs_resp, mock_log_resp_1, mock_log_resp_2]):
        logs = ci_adapter.get_job_logs(12345, failed_only=False)
        assert "test" in logs
        assert "lint" in logs


def test_get_job_logs_truncates_long_output(ci_adapter):
    """Test that very long job logs are truncated."""
    mock_jobs_resp = Mock()
    mock_jobs_resp.json.return_value = {
        "jobs": [{"id": 10, "name": "test", "conclusion": "failure"}]
    }
    mock_jobs_resp.raise_for_status = Mock()

    mock_log_resp = Mock()
    mock_log_resp.status_code = 200
    mock_log_resp.text = "x" * 20000

    with patch(f"{PATCH_PREFIX}.requests.get", side_effect=[mock_jobs_resp, mock_log_resp]):
        logs = ci_adapter.get_job_logs(12345, failed_only=True)
        assert "truncated" in logs


# -------------------------
# Get failed run IDs tests
# -------------------------

def test_get_failed_run_ids(ci_adapter):
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

    with patch(f"{PATCH_PREFIX}.requests.get", side_effect=[mock_commits_resp, mock_runs_resp]):
        failed = ci_adapter.get_failed_run_ids(42)
        assert failed == [101]


# -------------------------
# Deployment health check tests
# -------------------------

def test_check_deployment_health_no_env(ci_adapter):
    """Test health check when environment cannot be resolved (default adapter)."""
    result = ci_adapter.check_deployment_health(branch="feature/foo")
    assert result["healthy"] is False
    assert "Could not resolve" in result["error"]
