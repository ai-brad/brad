"""Configuration management for Brad."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Config:
    jira_url: str
    jira_token: str
    jira_user: str
    jira_project_key: str

    github_token: str
    github_repo: str

    # Absolute path to the local working clone of github_repo.
    # If not set in the environment, load_config() derives it from workspace_dir
    # and github_repo, and RepoManager will self-bootstrap the clone via
    # HTTPS+token on startup.
    target_repo_path: str

    # Root under which self-bootstrapped target repo clones live
    workspace_dir: str

    # Azure OpenAI configuration (used by AzureOpenAIProvider)
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
    github_app_id: Optional[str] = None
    github_app_installation_id: Optional[str] = None
    github_app_private_key: Optional[str] = None
    github_app_private_key_path: Optional[str] = None
    github_require_brad_author_identity: bool = True
    github_brad_author_logins: List[str] = field(default_factory=list)
    azure_deployments: List[str] = field(default_factory=list)
    deployment_health_check: bool = True
    deployment_log_tail_lines: int = 200
    deployment_log_since: str = "10m"

    # Cost tracking
    llm_cost_per_1k_prompt_tokens: float = 0.0
    llm_cost_per_1k_completion_tokens: float = 0.0
    cost_budget: float = 150.0

    # Database
    db_path: str = "brad_data.db"

    log_level: str = "INFO"
    attachments_dir: str = "attachments"

    # Agent harness selection — which agentic loop drives the model.
    # See ``brad/adapters/harness`` for available harnesses.
    harness: str = "brad"  # "brad" or "codex"

    # Inner LLM provider — only consulted when ``harness == "brad"``.
    # See ``brad/adapters/llm`` for available providers.
    llm_provider: str = "azure_openai"

    # Codex CLI configuration (used by CodexCliHarness).
    # ``codex_model`` is intentionally optional: when unset, the CodexCliHarness
    # omits ``--model`` so codex falls back to whatever is in ~/.codex/config.toml
    # (model + model_provider + auth). Set CODEX_MODEL only to override.
    codex_bin: str = "codex"
    codex_model: Optional[str] = None
    codex_sandbox: str = "workspace-write"
    # ``danger`` => --dangerously-bypass-approvals-and-sandbox.  Required because
    # the implementation prompt asks the agent to push branches and open PRs,
    # both of which need network + .git writes that --full-auto blocks.
    codex_approval: str = "danger"
    codex_timeout: int = 3600

    # Optional path to a shell script that Brad runs once after cloning the
    # target repo (and again whenever the clone is re-created from scratch).
    # The script receives the absolute clone path as its first argument ($1).
    # Use it to drop the target repo's own .env, start companion services, etc.
    # The script must be executable and must NOT be committed to version control.
    # Example: /home/you/.brad/hooks/provision-bea.sh
    target_repo_provision_hook: Optional[str] = None


def load_config() -> Config:
    deployments_raw = os.environ.get("AZURE_DEPLOYMENTS", "")
    deployments = [d.strip() for d in deployments_raw.split(",") if d.strip()] if deployments_raw else []

    github_repo = os.environ["GITHUB_REPO"]
    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_app_id = os.environ.get("GITHUB_APP_ID") or None
    github_app_installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID") or None
    github_app_private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY") or None
    github_app_private_key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH") or None
    github_require_brad_author_identity = (
        os.environ.get("GITHUB_REQUIRE_BRAD_AUTHOR_IDENTITY", "true").lower() == "true"
    )
    github_brad_author_logins_raw = os.environ.get("GITHUB_BRAD_AUTHOR_LOGINS", "")
    github_brad_author_logins = [
        login.strip()
        for login in github_brad_author_logins_raw.split(",")
        if login.strip()
    ]

    has_github_app_auth = bool(
        github_app_id and github_app_installation_id and (github_app_private_key or github_app_private_key_path)
    )
    if not github_token and not has_github_app_auth:
        raise KeyError(
            "Set GITHUB_TOKEN or GITHUB_APP_ID + GITHUB_APP_INSTALLATION_ID + "
            "GITHUB_APP_PRIVATE_KEY[_PATH]."
        )

    workspace_dir = os.environ.get(
        "BRAD_WORKSPACE_DIR",
        str(Path.home() / ".brad" / "workspaces"),
    )

    # target_repo_path is optional. If not provided, derive a default under
    # workspace_dir. RepoManager handles clone-if-missing.
    target_repo_path = os.environ.get("TARGET_REPO_PATH") or str(
        Path(workspace_dir) / github_repo.replace("/", "-")
    )

    return Config(
        jira_url=os.environ["JIRA_URL"],
        jira_token=os.environ["JIRA_TOKEN"],
        jira_user=os.environ["JIRA_USER"],
        jira_project_key=os.environ.get("JIRA_PROJECT_KEY", "DEV"),

        github_token=github_token,
        github_repo=github_repo,

        target_repo_path=target_repo_path,
        workspace_dir=workspace_dir,
        target_repo_provision_hook=os.environ.get("TARGET_REPO_PROVISION_HOOK") or None,

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
        github_app_id=github_app_id,
        github_app_installation_id=github_app_installation_id,
        github_app_private_key=github_app_private_key,
        github_app_private_key_path=github_app_private_key_path,
        github_require_brad_author_identity=github_require_brad_author_identity,
        github_brad_author_logins=github_brad_author_logins,
        azure_deployments=deployments,
        deployment_health_check=os.environ.get("DEPLOYMENT_HEALTH_CHECK", "true").lower() == "true",
        deployment_log_tail_lines=int(os.environ.get("DEPLOYMENT_LOG_TAIL_LINES", "200")),
        deployment_log_since=os.environ.get("DEPLOYMENT_LOG_SINCE", "10m"),

        llm_cost_per_1k_prompt_tokens=float(os.environ.get("LLM_COST_PER_1K_PROMPT_TOKENS", "0.0")),
        llm_cost_per_1k_completion_tokens=float(os.environ.get("LLM_COST_PER_1K_COMPLETION_TOKENS", "0.0")),
        cost_budget=float(os.environ.get("COST_BUDGET", "150.0")),

        db_path=os.environ.get("BRAD_DB_PATH", "brad_data.db"),

        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        attachments_dir=os.environ.get("ATTACHMENTS_DIR", str(Path.cwd() / "attachments")),

        harness=os.environ.get("BRAD_HARNESS", "brad"),
        llm_provider=os.environ.get("BRAD_LLM_PROVIDER", "azure_openai"),

        codex_bin=os.environ.get("CODEX_BIN", "codex"),
        codex_model=os.environ.get("CODEX_MODEL") or None,
        codex_sandbox=os.environ.get("CODEX_SANDBOX", "workspace-write"),
        codex_approval=os.environ.get("CODEX_APPROVAL", "danger"),
        codex_timeout=int(os.environ.get("CODEX_TIMEOUT", "3600")),
    )


def validate_config(cfg: Config) -> None:
    """Validate that all required paths and credentials exist.

    target_repo_path is NOT required to exist here: RepoManager will clone it
    on demand via HTTPS+github_token. We only validate that the parent directory
    can be created.
    """
    errors = []

    repo_path = Path(cfg.target_repo_path)

    # If the path exists, it must be a git repo. If it doesn't exist,
    # RepoManager will bootstrap a clone there.
    if repo_path.exists() and not (repo_path / ".git").exists():
        errors.append(f"Target repo path exists but is not a git repository: {cfg.target_repo_path}")

    # Ensure parent is creatable
    try:
        repo_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        errors.append(f"Cannot create parent dir for target_repo_path {repo_path.parent}: {e}")

    Path(cfg.attachments_dir).mkdir(parents=True, exist_ok=True)

    has_github_app_auth = bool(
        cfg.github_app_id
        and cfg.github_app_installation_id
        and (cfg.github_app_private_key or cfg.github_app_private_key_path)
    )
    if not cfg.github_token and not has_github_app_auth:
        errors.append(
            "GitHub auth is missing. Set GITHUB_TOKEN or "
            "GITHUB_APP_ID + GITHUB_APP_INSTALLATION_ID + GITHUB_APP_PRIVATE_KEY[_PATH]."
        )

    if errors:
        raise ValueError("Configuration validation failed:\n" + "\n".join(errors))
