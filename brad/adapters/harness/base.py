"""Abstract base class for agent harnesses."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class LLMUsage:
    """Token usage statistics from an agent run."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class LLMResult:
    """Result from a single :meth:`AgentHarness.run` call."""
    text: str
    response_id: Optional[str] = None
    usage: Optional[LLMUsage] = None


class AgentHarness(ABC):
    """Abstract interface for agent harnesses.

    A harness drives an LLM through an agentic coding task: it provides tools
    (file I/O, shell, search, ...), runs the tool-calling loop, and returns the
    agent's final textual output.

    Implementations sit side-by-side:
      - :class:`~brad.adapters.harness.brad_harness.BradHarness` — Brad's own
        tool-calling loop, configured with a swappable :class:`LLMProvider`.
      - :class:`~brad.adapters.harness.codex_cli_harness.CodexCliHarness` —
        shells out to the ``codex`` CLI.

    To add a new harness:
    1. Create a new file in ``brad/adapters/harness/``.
    2. Subclass :class:`AgentHarness` and implement :meth:`run`.
    3. Register it in :func:`brad.adapters.harness.build_harness`.
    """

    @property
    def model_name(self) -> str:
        """Return the model name used by this harness for cost calculation."""
        raise NotImplementedError("Subclasses must implement model_name")

    @abstractmethod
    def run(
        self,
        task_prompt: str,
        repo_path: str,
        system_prompt: str = "",
    ) -> LLMResult:
        """Run an agentic coding task.

        Args:
            task_prompt: The task description / user-role prompt.
            repo_path: Absolute path to the repository the agent operates on.
            system_prompt: Optional system/developer prompt (e.g. codebase map).

        Returns:
            :class:`LLMResult` with the agent's final text, an opaque
            ``response_id`` (may be ``None``), and token usage if available.
        """
        ...
