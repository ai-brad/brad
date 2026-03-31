"""Abstract base class for LLM adapters."""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass


@dataclass
class LLMUsage:
    """Token usage statistics from an LLM call."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class LLMResult:
    """Result from an LLM agent run."""
    text: str
    response_id: Optional[str] = None
    usage: Optional[LLMUsage] = None


class LLMAdapter(ABC):
    """
    Abstract interface for LLM providers (e.g. Azure OpenAI, OpenAI, local models).

    To add a new LLM adapter:
    1. Create a new file in brad/adapters/llm/
    2. Subclass LLMAdapter and implement all abstract methods
    3. Register it in brad/config.py so it can be selected via configuration
    See CONTRIBUTING.md for detailed instructions.
    """

    @abstractmethod
    def run(
        self,
        task_prompt: str,
        repo_path: str,
        system_prompt: str = "",
        previous_response_id: Optional[str] = None,
    ) -> LLMResult:
        """
        Run an agentic coding task.

        Args:
            task_prompt: The task description / prompt.
            repo_path: Path to the repository the agent operates on.
            system_prompt: Optional system/developer prompt (e.g. codebase map).
            previous_response_id: Optional ID for warm-starting from a prior session.

        Returns:
            LLMResult with the agent's final text, response ID, and token usage.
        """
        ...
