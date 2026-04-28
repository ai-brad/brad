"""Agent harness layer — swappable agentic backends (Brad's loop, Codex CLI, ...).

An ``AgentHarness`` wraps an LLM with tools, a tool-calling loop, and a prompt
protocol.  It is the peer-level abstraction to third-party coding agents such as
``codex`` or ``claude`` CLIs — i.e. the thing that actually *drives* the model
through an agentic task.

Selection is driven by ``Config.harness`` (see ``brad.config``).  To add a new
harness, subclass :class:`AgentHarness` and register it in :func:`build_harness`.
"""
from brad.adapters.harness.base import AgentHarness, LLMResult, LLMUsage


def build_harness(cfg) -> AgentHarness:
    """Construct the configured :class:`AgentHarness`.

    The inner LLM provider (used only by :class:`BradHarness`) is resolved via
    :func:`brad.adapters.llm.build_provider`.
    """
    name = (getattr(cfg, "harness", "") or "brad").strip().lower()

    if name == "brad":
        from brad.adapters.harness.brad_harness import BradHarness
        from brad.adapters.llm import build_provider
        return BradHarness(cfg, provider=build_provider(cfg))

    if name in ("codex", "codex_cli", "codex-cli"):
        from brad.adapters.harness.codex_cli_harness import CodexCliHarness
        return CodexCliHarness(cfg)

    raise ValueError(
        f"Unknown BRAD_HARNESS={name!r}. Known harnesses: 'brad', 'codex'."
    )


__all__ = ["AgentHarness", "LLMResult", "LLMUsage", "build_harness"]
