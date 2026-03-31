"""Configuration management for Brad."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


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
    azure_openai_model: str      # e.g. "gpt-4o"

    max_clarification_cycles: int
    max_ci_fix_iterations: int
    max_review_fix_iterations: int
    max_flaky_retries: int
    ci_poll_interval: int

    # Azure / AKS configuration for deployment log monitoring
    azure_credentials_json: str
    azure_resource_group: str
    azure_aks_cluster: str
    azure_deployments: List[str] = field(default_factory=list)
    deployment_health_check: bool = True
    deployment_log_tail_lines: int = 200
    deployment_log_since: str = "10m"

    # Cost tracking
    llm_cost_per_1k_prompt_tokens: float = 0.0
    llm_cost_per_1k_completion_tokens: float = 0.0

    # Database
    db_path: str = "brad_data.db"

    log_level: str = "INFO"
    attachments_dir: str = "attachments"


def load_config() -> Config:
    deployments_raw = os.environ.get("AZURE_DEPLOYMENTS", "")
    deployments = [d.strip() for d in deployments_raw.split(",") if d.strip()] if deployments_raw else []

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
        azure_openai_model=os.environ.get("AZURE_OPENAI_MODEL", "gpt-4o"),

        max_clarification_cycles=int(os.environ.get("MAX_CLARIFICATION_CYCLES", "3")),
        max_ci_fix_iterations=int(os.environ.get("MAX_CI_FIX_ITERATIONS", "5")),
        max_review_fix_iterations=int(os.environ.get("MAX_REVIEW_FIX_ITERATIONS", "3")),
        max_flaky_retries=int(os.environ.get("MAX_FLAKY_RETRIES", "1")),
        ci_poll_interval=int(os.environ.get("CI_POLL_INTERVAL", "60")),

        azure_credentials_json=os.environ.get("AZURE_CREDENTIALS_JSON", ""),
        azure_resource_group=os.environ.get("AZURE_RESOURCE_GROUP", ""),
        azure_aks_cluster=os.environ.get("AZURE_AKS_CLUSTER", ""),
        azure_deployments=deployments,
        deployment_health_check=os.environ.get("DEPLOYMENT_HEALTH_CHECK", "true").lower() == "true",
        deployment_log_tail_lines=int(os.environ.get("DEPLOYMENT_LOG_TAIL_LINES", "200")),
        deployment_log_since=os.environ.get("DEPLOYMENT_LOG_SINCE", "10m"),

        llm_cost_per_1k_prompt_tokens=float(os.environ.get("LLM_COST_PER_1K_PROMPT_TOKENS", "0.0")),
        llm_cost_per_1k_completion_tokens=float(os.environ.get("LLM_COST_PER_1K_COMPLETION_TOKENS", "0.0")),

        db_path=os.environ.get("BRAD_DB_PATH", "brad_data.db"),

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
