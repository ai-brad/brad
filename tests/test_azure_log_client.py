import pytest
from unittest.mock import Mock, patch, MagicMock
from brad.adapters.observability.azure_adapter import AzureObservabilityAdapter
from test_helpers import make_test_config


PATCH_PREFIX = "brad.adapters.observability.azure_adapter"


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    return make_test_config(tmp_path)


@pytest.fixture
def azure_client(mock_config):
    """Create an Azure observability adapter instance."""
    return AzureObservabilityAdapter(mock_config)


def test_azure_client_initialization(azure_client):
    """Test Azure adapter initialization."""
    assert azure_client.resource_group == "TEST"
    assert azure_client.cluster_name == "test-cluster"
    assert azure_client._kubeconfig_ready is False


def test_azure_client_custom_config(tmp_path):
    """Test Azure adapter with custom config."""
    cfg = make_test_config(
        tmp_path,
        azure_resource_group="PROD",
        azure_aks_cluster="prod-cluster",
    )
    client = AzureObservabilityAdapter(cfg)
    assert client.resource_group == "PROD"
    assert client.cluster_name == "prod-cluster"


@patch(f"{PATCH_PREFIX}.subprocess.run")
def test_ensure_azure_login_already_logged_in(mock_run, azure_client):
    """Test that login check succeeds when already logged in."""
    mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
    assert azure_client._ensure_azure_login() is True


@patch(f"{PATCH_PREFIX}.subprocess.run")
def test_ensure_azure_login_not_logged_in_no_creds(mock_run, azure_client):
    """Test that login fails when not logged in and no credentials."""
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="not logged in")
    assert azure_client._ensure_azure_login() is False


@patch(f"{PATCH_PREFIX}.subprocess.run")
def test_ensure_azure_login_az_not_found(mock_run, azure_client):
    """Test handling when az CLI is not installed."""
    mock_run.side_effect = FileNotFoundError("az not found")
    assert azure_client._ensure_azure_login() is False


@patch(f"{PATCH_PREFIX}.subprocess.run")
def test_ensure_kubeconfig_success(mock_run, azure_client):
    """Test successful kubeconfig setup."""
    mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
    assert azure_client._ensure_kubeconfig() is True
    assert azure_client._kubeconfig_ready is True


@patch(f"{PATCH_PREFIX}.subprocess.run")
def test_ensure_kubeconfig_already_ready(mock_run, azure_client):
    """Test that kubeconfig skips setup if already ready."""
    azure_client._kubeconfig_ready = True
    assert azure_client._ensure_kubeconfig() is True
    mock_run.assert_not_called()


@patch(f"{PATCH_PREFIX}.subprocess.run")
def test_get_environment_diagnostics_success(mock_run, azure_client):
    """Test successful environment diagnostics."""
    pods_json = '{"items": [{"metadata": {"name": "app-backend-abc"}, "status": {"phase": "Running"}}]}'
    mock_run.side_effect = [
        Mock(returncode=0, stdout="", stderr=""),  # az account show
        Mock(returncode=0, stdout="", stderr=""),  # az aks get-credentials
        Mock(returncode=0, stdout=pods_json, stderr=""),  # kubectl get pods
    ]

    diagnostics = azure_client.get_environment_diagnostics("int0", tail_lines=100)
    assert "environment" in diagnostics
    assert diagnostics["environment"] == "int0"
    assert "app-backend-abc" in diagnostics["pods"]
    assert diagnostics["pods"]["app-backend-abc"] == "Running"


@patch(f"{PATCH_PREFIX}.subprocess.run")
def test_get_environment_diagnostics_no_kubeconfig(mock_run, azure_client):
    """Test diagnostics when kubeconfig cannot be set up."""
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="not logged in")

    diagnostics = azure_client.get_environment_diagnostics("int0")
    assert "error" in diagnostics


def test_format_diagnostics_summary(azure_client):
    """Test diagnostics formatting."""
    diagnostics = {
        "environment": "int0",
        "timestamp": "2024-01-01T00:00:00Z",
        "pods": {"app-backend-abc": "Running"},
        "logs": {},
    }

    summary = azure_client.format_diagnostics_summary(diagnostics)
    assert "int0" in summary
    assert "Running" in summary


def test_format_diagnostics_summary_with_logs(azure_client):
    """Test diagnostics formatting with log data."""
    diagnostics = {
        "environment": "int0",
        "timestamp": "2024-01-01T00:00:00Z",
        "pods": {},
        "logs": {"app-backend": "line1\nline2\nline3"},
    }

    summary = azure_client.format_diagnostics_summary(diagnostics)
    assert "app-backend" in summary
    assert "line3" in summary
