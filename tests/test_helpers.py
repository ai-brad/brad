"""Shared test helpers for creating Config objects with all required fields."""

from brad.config import Config


def make_test_config(tmp_path, **overrides):
    """Create a Config with all required fields for testing. Override any field via kwargs."""
    defaults = dict(
        jira_url="https://test.atlassian.net",
        jira_token="test-token",
        jira_user="test@example.com",
        jira_project_key="DEV",
        github_token="gh-token",
        github_repo="owner/repo",
        target_repo_path=str(tmp_path),
        workspace_dir=str(tmp_path / "workspaces"),
        azure_openai_endpoint="https://test.openai.azure.com/openai/responses?api-version=2025-04-01-preview",
        azure_openai_api_key="test-key",
        azure_openai_model="gpt-4o",
        codex_model=None,
        codex_summarization_model=None,
        max_clarification_cycles=3,
        max_ci_fix_iterations=5,
        max_review_fix_iterations=3,
        max_flaky_retries=1,
        ci_poll_interval=60,
        azure_credentials_json="",
        azure_resource_group="TEST",
        azure_aks_cluster="test-cluster",
        github_app_id=None,
        github_app_installation_id=None,
        github_app_private_key=None,
        github_app_private_key_path=None,
        github_require_brad_author_identity=True,
        github_brad_author_logins=[],
        deployment_health_check=True,
        deployment_log_tail_lines=200,
        deployment_log_since="10m",
        cost_budget=150.0,
        llm_cost_per_1k_prompt_tokens=0.005,
        llm_cost_per_1k_completion_tokens=0.015,
        db_path=str(tmp_path / "test_brad.db"),
        log_level="INFO",
        attachments_dir=str(tmp_path / "attachments"),
        ticketing_adapter="jira",
        dummy_ticket_path=None,
        dummy_ticket_log_path=None,
        code_repo_adapter="github",
        ci_adapter="github_actions",
        dummy_code_repo_log_path=None,
        dummy_ci_log_path=None,
    )
    defaults.update(overrides)
    return Config(**defaults)
