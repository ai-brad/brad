import pytest
from unittest.mock import Mock, patch, MagicMock
from azure_log_client import AzureLogClient
from test_helpers import make_test_config


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    return make_test_config(tmp_path)


@pytest.fixture
def azure_client(mock_config):
    """Create an Azure log client instance."""
    return AzureLogClient(mock_config)


def test_azure_client_initialization(azure_client):
    """Test Azure log client initialization."""
    assert azure_client.resource_group == "TEST"
    assert azure_client.cluster_name == "bea-test2"
    assert azure_client._kubeconfig_ready is False


def test_azure_client_custom_config(tmp_path):
    """Test Azure log client with custom config."""
    cfg = make_test_config(
        tmp_path,
        azure_resource_group="PROD",
        azure_aks_cluster="bea-prod",
    )
    client = AzureLogClient(cfg)
    assert client.resource_group == "PROD"
    assert client.cluster_name == "bea-prod"


@patch("azure_log_client.subprocess.run")
def test_ensure_azure_login_already_logged_in(mock_run, azure_client):
    """Test that login check succeeds when already logged in."""
    mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
    assert azure_client._ensure_azure_login() is True


@patch("azure_log_client.subprocess.run")
def test_ensure_azure_login_not_logged_in_no_creds(mock_run, azure_client):
    """Test that login fails when not logged in and no credentials."""
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="not logged in")
    assert azure_client._ensure_azure_login() is False


@patch("azure_log_client.subprocess.run")
def test_ensure_azure_login_az_not_found(mock_run, azure_client):
    """Test handling when az CLI is not installed."""
    mock_run.side_effect = FileNotFoundError("az not found")
    assert azure_client._ensure_azure_login() is False


@patch("azure_log_client.subprocess.run")
def test_ensure_kubeconfig_success(mock_run, azure_client):
    """Test successful kubeconfig setup."""
    mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
    assert azure_client._ensure_kubeconfig() is True
    assert azure_client._kubeconfig_ready is True


@patch("azure_log_client.subprocess.run")
def test_ensure_kubeconfig_already_ready(mock_run, azure_client):
    """Test that kubeconfig skips setup if already ready."""
    azure_client._kubeconfig_ready = True
    assert azure_client._ensure_kubeconfig() is True
    mock_run.assert_not_called()


@patch("azure_log_client.subprocess.run")
def test_get_pod_logs_success(mock_run, azure_client):
    """Test successful pod log retrieval."""
    # First call: az account show (login check)
    # Second call: az aks get-credentials
    # Third call: kubectl logs
    mock_run.side_effect = [
        Mock(returncode=0, stdout="", stderr=""),  # az account show
        Mock(returncode=0, stdout="", stderr=""),  # az aks get-credentials
        Mock(returncode=0, stdout="2024-01-01 INFO Starting server...\n2024-01-01 INFO Server ready", stderr=""),
    ]
    
    logs = azure_client.get_pod_logs("int0", "bea-backend", tail_lines=100)
    assert "Starting server" in logs
    assert "Server ready" in logs


@patch("azure_log_client.subprocess.run")
def test_get_pod_logs_kubectl_failure(mock_run, azure_client):
    """Test handling when kubectl logs fails."""
    mock_run.side_effect = [
        Mock(returncode=0, stdout="", stderr=""),  # az account show
        Mock(returncode=0, stdout="", stderr=""),  # az aks get-credentials
        Mock(returncode=1, stdout="", stderr="error: deployment not found"),
    ]
    
    logs = azure_client.get_pod_logs("int0", "bea-backend")
    assert "[ERROR]" in logs
    assert "deployment not found" in logs


@patch("azure_log_client.subprocess.run")
def test_get_pod_events(mock_run, azure_client):
    """Test Kubernetes events retrieval."""
    mock_run.side_effect = [
        Mock(returncode=0, stdout="", stderr=""),  # az account show
        Mock(returncode=0, stdout="", stderr=""),  # az aks get-credentials
        Mock(returncode=0, stdout="LAST SEEN   TYPE    REASON   OBJECT   MESSAGE\n5m          Normal  Pulled   pod/bea  Container pulled", stderr=""),
    ]
    
    events = azure_client.get_pod_events("int0")
    assert "LAST SEEN" in events
    assert "Container pulled" in events


@patch("azure_log_client.subprocess.run")
def test_get_pod_status(mock_run, azure_client):
    """Test pod status retrieval."""
    mock_run.side_effect = [
        Mock(returncode=0, stdout="", stderr=""),  # az account show
        Mock(returncode=0, stdout="", stderr=""),  # az aks get-credentials
        Mock(returncode=0, stdout="NAME          READY   STATUS    RESTARTS   AGE\nbea-backend   1/1     Running   0          5m", stderr=""),
    ]
    
    status = azure_client.get_pod_status("int0")
    assert "Running" in status
    assert "bea-backend" in status


@patch("azure_log_client.subprocess.run")
def test_get_environment_diagnostics(mock_run, azure_client):
    """Test comprehensive environment diagnostics."""
    mock_run.side_effect = [
        Mock(returncode=0, stdout="", stderr=""),  # az account show
        Mock(returncode=0, stdout="", stderr=""),  # az aks get-credentials
        Mock(returncode=0, stdout="pod-status-output", stderr=""),
        Mock(returncode=0, stdout="deployment-status-output", stderr=""),
        Mock(returncode=0, stdout="events-output", stderr=""),
        Mock(returncode=0, stdout="backend-logs", stderr=""),
        Mock(returncode=0, stdout="admin-ui-logs", stderr=""),
    ]
    
    diagnostics = azure_client.get_environment_diagnostics("int0", tail_lines=50, since="5m")
    
    assert "pod_status" in diagnostics
    assert "deployment_status" in diagnostics
    assert "events" in diagnostics
    assert "bea-backend_logs" in diagnostics
    assert "bea-admin-ui_logs" in diagnostics


def test_format_diagnostics_summary(azure_client):
    """Test diagnostics formatting."""
    diagnostics = {
        "pod_status": "NAME READY STATUS\nbea 1/1 Running",
        "events": "No events",
    }
    
    summary = azure_client.format_diagnostics_summary(diagnostics)
    assert "=== Pod Status ===" in summary
    assert "=== Events ===" in summary
    assert "Running" in summary


def test_format_diagnostics_truncation(azure_client):
    """Test that very long diagnostics are truncated."""
    diagnostics = {
        "long_logs": "x" * 5000,
    }
    
    summary = azure_client.format_diagnostics_summary(diagnostics)
    assert "... [truncated]" in summary
    assert len(summary) < 5000
