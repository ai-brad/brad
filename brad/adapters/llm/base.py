"""Abstract base class for LLM providers.

An :class:`LLMProvider` is a thin wrapper over a single model-serving API call
(Azure OpenAI, OpenAI direct, local vLLM, ...).  It deliberately does *not*
implement an agentic loop — that is the job of an
:class:`~brad.adapters.harness.base.AgentHarness`.  Providers are used today
only by :class:`~brad.adapters.harness.brad_harness.BradHarness`.

Legacy import compatibility: ``LLMAdapter``, ``LLMResult`` and ``LLMUsage`` are
re-exported from :mod:`brad.adapters.harness.base` with a deprecation warning
so older callers keep working for one release.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import warnings

# Re-export harness-level types for back-compat.
from brad.adapters.harness.base import LLMResult, LLMUsage  # noqa: F401

__all__ = ["LLMProvider", "ProviderResponse", "LLMResult", "LLMUsage"]


@dataclass
class ProviderResponse:
    """Single-call response from an :class:`LLMProvider`.

    Mirrors the shape of the Azure/OpenAI Responses API just enough for the
    harness to extract tool calls, text output, token usage and the session
    identifier (``response_id``) used for warm-starting subsequent turns.
    """
    output: List[Dict[str, Any]]            # raw items (function_call / message / ...)
    response_id: Optional[str] = None
    usage: Optional[LLMUsage] = None
    status: str = "completed"


class LLMProvider(ABC):
    """Abstract interface for LLM providers.

    A provider performs one request/response cycle against a chat/responses API
    with tool definitions.  It returns the raw output items so the harness can
    decide what to do next (execute tools, finish, retry).

    To add a new provider:
    1. Create a new file in ``brad/adapters/llm/``.
    2. Subclass :class:`LLMProvider` and implement :meth:`call`.
    3. Register it in :func:`brad.adapters.llm.build_provider`.
    """

    @abstractmethod
    def call(
        self,
        input_data: List[Any],
        tools: List[Dict[str, Any]],
        previous_response_id: Optional[str] = None,
    ) -> Optional[ProviderResponse]:
        """Make a single provider call.

        Args:
            input_data: Chat-style messages or tool outputs to append to the
                running conversation (as accepted by the underlying API).
            tools: OpenAI-style function/tool definitions.
            previous_response_id: Opaque session identifier for warm-starting.

        Returns:
            :class:`ProviderResponse` on success, ``None`` on unrecoverable
            failure (the harness will surface this as an ``ERROR:`` message).
        """
        ...


def __getattr__(name: str):
    """Deprecation shim: ``LLMAdapter`` used to live here as the top-level
    agentic-loop interface.  It has been renamed to
    :class:`~brad.adapters.harness.base.AgentHarness`.
    """
    if name == "LLMAdapter":
        warnings.warn(
            "LLMAdapter is deprecated; import AgentHarness from "
            "brad.adapters.harness instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        from brad.adapters.harness.base import AgentHarness
        return AgentHarness
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
