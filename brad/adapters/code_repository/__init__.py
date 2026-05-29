"""Code repository adapter (e.g. GitHub)."""
from brad.adapters.code_repository.base import CodeRepositoryAdapter


def build_code_repo_adapter(cfg) -> CodeRepositoryAdapter:
    """Construct the configured code repository adapter."""
    name = (getattr(cfg, "code_repo_adapter", "") or "github").strip().lower()
    if name in ("github",):
        from brad.adapters.code_repository.github_adapter import GitHubAdapter
        return GitHubAdapter(cfg)
    if name in ("dummy", "local", "null"):
        from brad.adapters.code_repository.dummy_adapter import DummyCodeRepositoryAdapter
        return DummyCodeRepositoryAdapter(cfg)
    raise ValueError(
        f"Unknown BRAD_CODE_REPO={name!r}. Known adapters: 'github', 'dummy'."
    )


__all__ = ["CodeRepositoryAdapter", "build_code_repo_adapter"]
