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
    
    # Azure OpenAI configuration (the AI brain)
    azure_openai_endpoint: str   # Full URL including api-version
    azure_openai_api_key: str
    azure_openai_model: str      # e.g. "gpt-5.2-codex"
    
    max_clarification_cycles: int
    max_ci_fix_iterations: int
    max_review_fix_iterations: int
    max_flaky_retries: int
    ci_poll_interval: int
    
    # Azure / AKS configuration for deployment log monitoring
    azure_credentials_json: str  # JSON string with clientId, clientSecret, tenantId, subscriptionId
    azure_resource_group: str
    azure_aks_cluster: str
    deployment_health_check: bool  # Whether to check deployment health after CI passes
    deployment_log_tail_lines: int
    deployment_log_since: str  # e.g. "10m", "1h"
    
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
        
        azure_openai_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
        azure_openai_api_key=os.environ.get("AZURE_OPENAI_API_KEY", ""),
        azure_openai_model=os.environ.get("AZURE_OPENAI_MODEL", "gpt-5.2-codex"),
        
        max_clarification_cycles=int(os.environ.get("MAX_CLARIFICATION_CYCLES", "3")),
        max_ci_fix_iterations=int(os.environ.get("MAX_CI_FIX_ITERATIONS", "5")),
        max_review_fix_iterations=int(os.environ.get("MAX_REVIEW_FIX_ITERATIONS", "3")),
        max_flaky_retries=int(os.environ.get("MAX_FLAKY_RETRIES", "1")),
        ci_poll_interval=int(os.environ.get("CI_POLL_INTERVAL", "60")),
        
        azure_credentials_json=os.environ.get("AZURE_CREDENTIALS_JSON", ""),
        azure_resource_group=os.environ.get("AZURE_RESOURCE_GROUP", "TEST"),
        azure_aks_cluster=os.environ.get("AZURE_AKS_CLUSTER", "bea-test2"),
        deployment_health_check=os.environ.get("DEPLOYMENT_HEALTH_CHECK", "true").lower() == "true",
        deployment_log_tail_lines=int(os.environ.get("DEPLOYMENT_LOG_TAIL_LINES", "200")),
        deployment_log_since=os.environ.get("DEPLOYMENT_LOG_SINCE", "10m"),
        
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
