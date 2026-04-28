"""Azure OpenAI provider — single-call wrapper around the Responses API.

This module is intentionally tiny.  All agentic-loop logic lives in
:class:`~brad.adapters.harness.brad_harness.BradHarness`; everything Azure-
specific (endpoint URL, ``api-key`` header, response shape) is contained here.
Swap this file for an ``OpenAIProvider`` or ``VLLMProvider`` to point Brad's
harness at a different model server.
"""
import time
from typing import Any, Dict, List, Optional

import requests as http_requests

from brad.adapters.llm.base import LLMProvider, ProviderResponse
from brad.adapters.harness.base import LLMUsage
from brad.logging_config import get_logger


class AzureOpenAIProvider(LLMProvider):
    """Calls the Azure OpenAI Responses API once per :meth:`call`.

    Handles HTTP retries (rate limits, timeouts, connection errors) but performs
    no tool-calling — that is the harness's responsibility.
    """

    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.endpoint = cfg.azure_openai_endpoint
        self.api_key = cfg.azure_openai_api_key
        self.model = cfg.azure_openai_model
        self.api_timeout = 180  # seconds per API call
        self.logger.info(f"AzureOpenAIProvider initialized: model={self.model}")

    def call(
        self,
        input_data: List[Any],
        tools: List[Dict[str, Any]],
        previous_response_id: Optional[str] = None,
    ) -> Optional[ProviderResponse]:
        body: Dict[str, Any] = {
            "model": self.model,
            "input": input_data,
            "tools": tools,
            "parallel_tool_calls": True,
            "max_output_tokens": 16384,
        }
        if previous_response_id:
            body["previous_response_id"] = previous_response_id

        headers = {
            "Content-Type": "application/json",
            "api-key": self.api_key,
        }

        max_retries = 10
        for attempt in range(max_retries):
            try:
                resp = http_requests.post(
                    self.endpoint, headers=headers, json=body, timeout=self.api_timeout
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", str(min(10 * (2 ** attempt), 120))))
                    self.logger.warning(f"Rate limited, waiting {wait}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    self.logger.error(f"API {resp.status_code}: {resp.text[:500]}")
                    return None
                return self._parse(resp.json())
            except (http_requests.exceptions.Timeout, http_requests.exceptions.ConnectionError) as e:
                self.logger.warning(f"API {type(e).__name__} (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    wait = min(10 * (2 ** attempt), 120)
                    self.logger.warning(f"Retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                self.logger.error(f"API call failed after {max_retries} attempts: {e}")
                return None
            except Exception as e:
                self.logger.error(f"API call error: {e}")
                return None
        self.logger.error(f"API call failed after {max_retries} attempts")
        return None

    @staticmethod
    def _parse(payload: Dict[str, Any]) -> ProviderResponse:
        usage_data = payload.get("usage") or {}
        usage: Optional[LLMUsage] = None
        if usage_data:
            prompt_tokens = usage_data.get("input_tokens", 0)
            completion_tokens = usage_data.get("output_tokens", 0)
            cached_tokens = (usage_data.get("input_tokens_details") or {}).get("cached_tokens", 0)
            usage = LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cached_tokens=cached_tokens,
            )
        return ProviderResponse(
            output=payload.get("output", []) or [],
            response_id=payload.get("id"),
            usage=usage,
            status=payload.get("status", "unknown"),
        )
