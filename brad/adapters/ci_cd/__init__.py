"""CI/CD pipeline adapter (e.g. GitHub Actions)."""
from brad.adapters.ci_cd.base import CICDAdapter


def build_ci_adapter(cfg) -> CICDAdapter:
    """Construct the configured CI/CD adapter."""
    name = (getattr(cfg, "ci_adapter", "") or "github_actions").strip().lower()
    if name in ("github_actions", "github"):
        from brad.adapters.ci_cd.github_actions_adapter import GitHubActionsAdapter
        return GitHubActionsAdapter(cfg)
    if name in ("dummy", "local", "null"):
        from brad.adapters.ci_cd.dummy_adapter import DummyCICDAdapter
        return DummyCICDAdapter(cfg)
    raise ValueError(
        f"Unknown BRAD_CI={name!r}. Known adapters: 'github_actions', 'dummy'."
    )


__all__ = ["CICDAdapter", "build_ci_adapter"]
