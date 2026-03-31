import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
from brad.adapters.ticketing.jira_adapter import JiraAdapter
from brad.config import Config
from test_helpers import make_test_config


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    return make_test_config(tmp_path)


@pytest.fixture
def jira_client(mock_config):
    """Create a JIRA adapter instance."""
    return JiraAdapter(mock_config)


def test_jira_client_initialization(jira_client):
    """Test JIRA client initialization."""
    assert jira_client.base_url == "https://test.atlassian.net"
    assert jira_client.auth == ("test@example.com", "test-token")
    assert "application/json" in jira_client.headers["Accept"]


def test_fetch_issues_with_label(jira_client):
    """Test fetching issues with a specific label."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "issues": [
            {
                "key": "DEV-123",
                "fields": {
                    "summary": "Test issue"
                }
            }
        ]
    }
    mock_response.raise_for_status = Mock()
    
    with patch("brad.adapters.ticketing.jira_adapter.requests.post", return_value=mock_response) as mock_post:
        issues = jira_client.fetch_issues_with_label("BradReview")
        
        assert len(issues) == 1
        assert issues[0]["key"] == "DEV-123"


def test_fetch_issues_empty_result(jira_client):
    """Test fetching issues when no issues are found."""
    mock_response = Mock()
    mock_response.json.return_value = {"issues": []}
    mock_response.raise_for_status = Mock()
    
    with patch("brad.adapters.ticketing.jira_adapter.requests.post", return_value=mock_response):
        issues = jira_client.fetch_issues_with_label("NonExistentLabel")
        assert len(issues) == 0


def test_remove_label(jira_client):
    """Test removing a label from an issue."""
    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    
    with patch("brad.adapters.ticketing.jira_adapter.requests.put", return_value=mock_response) as mock_put:
        jira_client.remove_label("DEV-123", "BradReview")
        
        mock_put.assert_called_once()
        call_args = mock_put.call_args
        payload = call_args.kwargs["json"]
        assert payload["update"]["labels"][0]["remove"] == "BradReview"


def test_add_label(jira_client):
    """Test adding a label to an issue."""
    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    
    with patch("brad.adapters.ticketing.jira_adapter.requests.put", return_value=mock_response) as mock_put:
        jira_client.add_label("DEV-123", "NeedsReview")
        
        mock_put.assert_called_once()
        call_args = mock_put.call_args
        payload = call_args.kwargs["json"]
        assert payload["update"]["labels"][0]["add"] == "NeedsReview"


def test_comment(jira_client):
    """Test adding a comment to an issue."""
    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    
    with patch("brad.adapters.ticketing.jira_adapter.requests.post", return_value=mock_response) as mock_post:
        jira_client.comment("DEV-123", "This is a test comment")
        
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        payload = call_args.kwargs["json"]
        
        # Verify comment structure
        assert payload["body"]["type"] == "doc"
        content = payload["body"]["content"][0]["content"][0]
        assert content["text"] == "This is a test comment"


def test_set_status_success(jira_client):
    """Test setting issue status when transition exists."""
    # Mock get transitions
    mock_get_response = Mock()
    mock_get_response.json.return_value = {
        "transitions": [
            {"id": "11", "to": {"name": "In Progress"}},
            {"id": "21", "to": {"name": "Done"}},
        ]
    }
    mock_get_response.raise_for_status = Mock()
    
    # Mock post transition
    mock_post_response = Mock()
    mock_post_response.raise_for_status = Mock()
    
    with patch("brad.adapters.ticketing.jira_adapter.requests.get", return_value=mock_get_response):
        with patch("brad.adapters.ticketing.jira_adapter.requests.post", return_value=mock_post_response) as mock_post:
            jira_client.set_status("DEV-123", "In Progress")
            
            # Verify transition was called with correct ID
            call_args = mock_post.call_args
            payload = call_args.kwargs["json"]
            assert payload["transition"]["id"] == "11"


def test_set_status_no_transition(jira_client):
    """Test setting status when transition doesn't exist."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "transitions": [
            {"id": "11", "to": {"name": "In Progress"}},
        ]
    }
    mock_response.raise_for_status = Mock()
    
    with patch("brad.adapters.ticketing.jira_adapter.requests.get", return_value=mock_response):
        with pytest.raises(RuntimeError, match="No transition to status"):
            jira_client.set_status("DEV-123", "NonExistentStatus")


def test_download_attachments(jira_client, tmp_path):
    """Test downloading attachments from an issue."""
    attachments = [
        {
            "filename": "test.txt",
            "content": "https://example.com/test.txt"
        },
        {
            "filename": "image.png",
            "content": "https://example.com/image.png"
        }
    ]
    
    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    mock_response.iter_content = Mock(return_value=[b"test content"])
    
    with patch("brad.adapters.ticketing.jira_adapter.requests.get", return_value=mock_response):
        paths = jira_client.download_attachments("DEV-123", attachments)
        
        assert len(paths) == 2
        assert all(Path(p).exists() for p in paths)
        assert "test.txt" in paths[0]
        assert "image.png" in paths[1]


def test_download_attachments_empty(jira_client):
    """Test downloading when no attachments exist."""
    paths = jira_client.download_attachments("DEV-123", [])
    assert len(paths) == 0
