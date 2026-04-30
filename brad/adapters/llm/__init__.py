"""LLM provider layer — single-call wrappers around model-serving APIs.

Providers are consumed by :class:`~brad.adapters.harness.brad_harness.BradHarness`.
Other harnesses (e.g. Codex CLI) bring their own model auth and ignore this layer.
"""
from brad.adapters.llm.base import LLMProvider, ProviderResponse


def build_provider(cfg) -> LLMProvider:
    """Construct the configured :class:`LLMProvider`.

    Selection is driven by ``Config.llm_provider`` (env: ``BRAD_LLM_PROVIDER``).
    Defaults to ``azure_openai`` for back-compat.
    """
    name = (getattr(cfg, "llm_provider", "") or "azure_openai").strip().lower()

    if name in ("azure_openai", "azure-openai", "azure"):
        from brad.adapters.llm.azure_openai_provider import AzureOpenAIProvider
        return AzureOpenAIProvider(cfg)

    raise ValueError(
        f"Unknown BRAD_LLM_PROVIDER={name!r}. Known providers: 'azure_openai'."
    )


__all__ = ["LLMProvider", "ProviderResponse", "build_provider"]
