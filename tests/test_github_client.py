import pytest
from unittest.mock import Mock, patch
from github_client import GitHubClient
from config import Config
from test_helpers import make_test_config


@pytest.fixture
def mock_config(tmp_path):
    """Create a mock config for testing."""
    return make_test_config(tmp_path)


@pytest.fixture
def github_client(mock_config):
    """Create a GitHub client instance."""
    return GitHubClient(mock_config)


def test_github_client_initialization(github_client):
    """Test GitHub client initialization."""
    assert github_client.base_url == "https://api.github.com/repos/owner/repo"
    assert "token gh-token" in github_client.headers["Authorization"]
    assert github_client.repo == "owner/repo"


def test_open_pr_success(github_client):
    """Test opening a PR successfully."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "number": 123,
        "html_url": "https://github.com/owner/repo/pull/123",
        "title": "Test PR"
    }
    mock_response.raise_for_status = Mock()
    
    with patch("github_client.requests.post", return_value=mock_response) as mock_post:
        pr = github_client.open_pr(
            branch="feature-branch",
            title="Test PR",
            body="Test body"
        )
        
        assert pr["number"] == 123
        assert pr["html_url"] == "https://github.com/owner/repo/pull/123"
        
        # Verify API call
        call_args = mock_post.call_args
        payload = call_args.kwargs["json"]
        assert payload["head"] == "feature-branch"
        assert payload["base"] == "main"
        assert payload["title"] == "Test PR"


def test_open_pr_custom_base(github_client):
    """Test opening a PR with custom base branch."""
    mock_response = Mock()
    mock_response.json.return_value = {"number": 123, "html_url": "https://github.com/owner/repo/pull/123"}
    mock_response.raise_for_status = Mock()
    
    with patch("github_client.requests.post", return_value=mock_response) as mock_post:
        github_client.open_pr(
            branch="feature",
            title="Test",
            body="Body",
            base="develop"
        )
        
        payload = mock_post.call_args.kwargs["json"]
        assert payload["base"] == "develop"


def test_open_pr_failure(github_client):
    """Test handling PR creation failure."""
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = Exception("API Error")
    
    with patch("github_client.requests.post", return_value=mock_response):
        with pytest.raises(Exception):
            github_client.open_pr("branch", "title", "body")


def test_get_pr_success(github_client):
    """Test fetching PR information."""
    mock_response = Mock()
    mock_response.json.return_value = {
        "number": 123,
        "title": "Test PR",
        "state": "open"
    }
    mock_response.raise_for_status = Mock()
    
    with patch("github_client.requests.get", return_value=mock_response):
        pr = github_client.get_pr(123)
        
        assert pr["number"] == 123
        assert pr["state"] == "open"


def test_pr_exists_for_branch_true(github_client):
    """Test checking for existing PR when it exists."""
    mock_response = Mock()
    mock_response.json.return_value = [
        {"number": 123, "head": {"ref": "feature-branch"}}
    ]
    mock_response.raise_for_status = Mock()
    
    with patch("github_client.requests.get", return_value=mock_response):
        pr_number = github_client.pr_exists_for_branch("feature-branch")
        
        assert pr_number == 123


def test_pr_exists_for_branch_false(github_client):
    """Test checking for existing PR when it doesn't exist."""
    mock_response = Mock()
    mock_response.json.return_value = []
    mock_response.raise_for_status = Mock()
    
    with patch("github_client.requests.get", return_value=mock_response):
        pr_number = github_client.pr_exists_for_branch("feature-branch")
        
        assert pr_number is None


def test_pr_exists_for_branch_error(github_client):
    """Test checking for existing PR when API fails."""
    mock_response = Mock()
    mock_response.raise_for_status.side_effect = Exception("API Error")
    
    with patch("github_client.requests.get", return_value=mock_response):
        pr_number = github_client.pr_exists_for_branch("feature-branch")
        
        # Should return None on error, not raise
        assert pr_number is None


def test_fetch_review_comments(github_client):
    """Test fetching review comments for a PR."""
    mock_response = Mock()
    mock_response.json.return_value = [
        {"id": 1, "body": "Please fix this"},
        {"id": 2, "body": "Looks good"}
    ]
    mock_response.raise_for_status = Mock()
    
    with patch("github_client.requests.get", return_value=mock_response):
        comments = github_client.fetch_review_comments(123)
        
        assert len(comments) == 2
        assert comments[0]["body"] == "Please fix this"
