import pytest
import os
from pathlib import Path
from config import load_config, validate_config, Config


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
    assert cfg.claude_cli_path == "claude"  # default


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


def test_validate_config_invalid_repo_path(tmp_path):
    """Test that validation fails with invalid repo path."""
    cfg = Config(
        jira_url="https://test.atlassian.net",
        jira_token="token",
        jira_user="user",
        jira_project_key="DEV",
        github_token="token",
        github_repo="owner/repo",
        target_repo_path="/nonexistent/path",
        claude_cli_path="claude",
        max_clarification_cycles=3,
        max_ci_fix_iterations=5,
        max_flaky_retries=1,
        ci_poll_interval=60,
        log_level="INFO",
        attachments_dir=str(tmp_path / "attachments")
    )
    
    with pytest.raises(ValueError, match="Target repo path does not exist"):
        validate_config(cfg)


def test_validate_config_not_git_repo(tmp_path):
    """Test that validation fails when path is not a git repo."""
    cfg = Config(
        jira_url="https://test.atlassian.net",
        jira_token="token",
        jira_user="user",
        jira_project_key="DEV",
        github_token="token",
        github_repo="owner/repo",
        target_repo_path=str(tmp_path),  # exists but not a git repo
        claude_cli_path="claude",
        max_clarification_cycles=3,
        max_ci_fix_iterations=5,
        max_flaky_retries=1,
        ci_poll_interval=60,
        log_level="INFO",
        attachments_dir=str(tmp_path / "attachments")
    )
    
    with pytest.raises(ValueError, match="not a git repository"):
        validate_config(cfg)


def test_validate_config_valid(tmp_path):
    """Test that validation passes with valid config."""
    # Create a fake .git directory
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    
    cfg = Config(
        jira_url="https://test.atlassian.net",
        jira_token="token",
        jira_user="user",
        jira_project_key="DEV",
        github_token="token",
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
    
    # Should not raise
    validate_config(cfg)
    
    # Attachments dir should be created
    assert (tmp_path / "attachments").exists()
