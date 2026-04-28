import pytest
from brad.config import load_config, validate_config
from test_helpers import make_test_config


def test_load_config_with_required_vars(monkeypatch):
    """Test loading config with all required environment variables."""
    monkeypatch.setenv("JIRA_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_TOKEN", "test-token")
    monkeypatch.setenv("JIRA_USER", "test@example.com")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("TARGET_REPO_PATH", "/fake/repo")

    cfg = load_config()

    assert cfg.jira_url == "https://test.atlassian.net"
    assert cfg.jira_token == "test-token"
    assert cfg.jira_user == "test@example.com"
    assert cfg.github_token == "gh-token"
    assert cfg.github_repo == "owner/repo"
    assert cfg.target_repo_path == "/fake/repo"
    assert cfg.azure_openai_model == "gpt-4o"  # default


def test_load_config_with_defaults(monkeypatch):
    """Test that default values are applied correctly."""
    monkeypatch.setenv("JIRA_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_TOKEN", "test-token")
    monkeypatch.setenv("JIRA_USER", "test@example.com")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("TARGET_REPO_PATH", "/fake/repo")

    cfg = load_config()

    assert cfg.jira_project_key == "DEV"
    assert cfg.max_clarification_cycles == 3
    assert cfg.max_ci_fix_iterations == 5
    assert cfg.max_flaky_retries == 1
    assert cfg.ci_poll_interval == 60
    assert cfg.log_level == "INFO"
    # Azure defaults
    assert cfg.azure_credentials_json == ""
    assert cfg.azure_resource_group == ""
    assert cfg.azure_aks_cluster == ""
    assert cfg.deployment_health_check is True
    assert cfg.deployment_log_tail_lines == 200
    assert cfg.deployment_log_since == "10m"


def test_load_config_with_custom_values(monkeypatch):
    """Test that custom values override defaults."""
    monkeypatch.setenv("JIRA_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_TOKEN", "test-token")
    monkeypatch.setenv("JIRA_USER", "test@example.com")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "CUSTOM")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("TARGET_REPO_PATH", "/fake/repo")
    monkeypatch.setenv("MAX_CLARIFICATION_CYCLES", "5")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    cfg = load_config()

    assert cfg.jira_project_key == "CUSTOM"
    assert cfg.max_clarification_cycles == 5
    assert cfg.log_level == "DEBUG"


def test_validate_config_missing_repo_path_is_ok(tmp_path):
    """Missing target_repo_path is now fine: RepoManager self-bootstraps via clone."""
    # Parent must be writable; point at tmp_path/not-yet-cloned which doesn't exist
    missing = tmp_path / "not-yet-cloned"
    cfg = make_test_config(tmp_path, target_repo_path=str(missing))

    # Should NOT raise (path is allowed to not-yet-exist)
    validate_config(cfg)


def test_validate_config_uncreatable_parent(tmp_path):
    """Validation still fails if we can't even create the parent dir."""
    cfg = make_test_config(tmp_path, target_repo_path="/proc/cannot-create/here")

    with pytest.raises(ValueError, match="Cannot create parent dir"):
        validate_config(cfg)


def test_validate_config_exists_but_not_git_repo(tmp_path):
    """If the path exists but isn't a git repo, validation still fails."""
    cfg = make_test_config(tmp_path)  # tmp_path exists but has no .git

    with pytest.raises(ValueError, match="not a git repository"):
        validate_config(cfg)


def test_validate_config_valid(tmp_path):
    """Test that validation passes with valid config."""
    # Create a fake .git directory
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    cfg = make_test_config(tmp_path)

    # Should not raise
    validate_config(cfg)

    # Attachments dir should be created
    assert (tmp_path / "attachments").exists()
