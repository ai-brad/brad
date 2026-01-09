import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    jira_url: str
    jira_token: str
    jira_user: str
    jira_project_key: str

    github_token: str
    github_repo: str

    target_repo_path: str
    ai_agent: str  # "claude" or "opencode"
    claude_cli_path: str
    opencode_cli_path: str
    
    max_clarification_cycles: int
    max_ci_fix_iterations: int
    max_flaky_retries: int
    ci_poll_interval: int
    
    log_level: str
    attachments_dir: str


def load_config() -> Config:
    return Config(
        jira_url=os.environ["JIRA_URL"],
        jira_token=os.environ["JIRA_TOKEN"],
        jira_user=os.environ["JIRA_USER"],
        jira_project_key=os.environ.get("JIRA_PROJECT_KEY", "DEV"),

        github_token=os.environ["GITHUB_TOKEN"],
        github_repo=os.environ["GITHUB_REPO"],

        target_repo_path=os.environ["TARGET_REPO_PATH"],
        ai_agent=os.environ.get("AI_AGENT", "claude"),
        claude_cli_path=os.environ.get("CLAUDE_CLI_PATH", "claude"),
        opencode_cli_path=os.environ.get("OPENCODE_CLI_PATH", "opencode"),
        
        max_clarification_cycles=int(os.environ.get("MAX_CLARIFICATION_CYCLES", "3")),
        max_ci_fix_iterations=int(os.environ.get("MAX_CI_FIX_ITERATIONS", "5")),
        max_flaky_retries=int(os.environ.get("MAX_FLAKY_RETRIES", "1")),
        ci_poll_interval=int(os.environ.get("CI_POLL_INTERVAL", "60")),
        
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        attachments_dir=os.environ.get("ATTACHMENTS_DIR", str(Path.cwd() / "attachments")),
    )


def validate_config(cfg: Config) -> None:
    """Validate that all required paths and credentials exist."""
    errors = []
    
    repo_path = Path(cfg.target_repo_path)
    
    if not repo_path.exists():
        errors.append(f"Target repo path does not exist: {cfg.target_repo_path}")
    
    if not (repo_path / ".git").exists():
        errors.append(f"Target repo path is not a git repository: {cfg.target_repo_path}")
    
    Path(cfg.attachments_dir).mkdir(parents=True, exist_ok=True)
    
    if errors:
        raise ValueError("Configuration validation failed:\n" + "\n".join(errors))
