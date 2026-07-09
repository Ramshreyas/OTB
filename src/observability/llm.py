"""LLM abstraction — LiteLLM proxy client with Langfuse tracing."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LLMClient:
    """OpenAI-compatible client pointed at the LiteLLM proxy.

    All calls are automatically traced by Langfuse when a langfuse_prompt
    is provided.

    Usage:
        client = LLMClient.from_env()
        prompt = client.get_prompt("weather-spec-extraction")
        compiled = prompt.compile(title="...", ancillary_data="...")
        response = client.complete(compiled, temperature=0.0)
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._openai = None  # lazily created

    @classmethod
    def from_env(cls) -> "LLMClient":
        """Create from environment variables.

        Required env vars:
            LITELLM_BASE_URL (default: http://localhost:4000/v1)
            LITELLM_API_KEY (default: sk-litellm-otb-master-key)
            LITELLM_MODEL (default: gemini-2.5-flash)
        """
        return cls(
            base_url=os.getenv("LITELLM_BASE_URL", "http://localhost:4000/v1"),
            api_key=os.getenv("LITELLM_API_KEY", "sk-litellm-otb-master-key"),
            model=os.getenv("LITELLM_MODEL", "gemini-2.5-flash"),
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def _client(self):
        """Lazy-init the OpenAI client with Langfuse tracing.

        Uses langfuse.openai.OpenAI (Langfuse v3 drop-in wrapper) instead of
        plain openai.OpenAI. This automatically creates Generation observations
        nested under the current Span, capturing model, tokens, latency, and I/O.

        If Langfuse is not configured, falls back to plain OpenAI client.
        """
        if self._openai is None:
            try:
                from langfuse.openai import OpenAI as LangfuseOpenAI
                self._openai = LangfuseOpenAI(
                    base_url=self._base_url,
                    api_key=self._api_key,
                )
                logger.debug("LLM client using langfuse.openai.OpenAI for auto-tracing")
            except (ImportError, Exception):
                from openai import OpenAI
                self._openai = OpenAI(
                    base_url=self._base_url,
                    api_key=self._api_key,
                )
                logger.debug("LLM client using plain openai.OpenAI (no Langfuse)")
        return self._openai

    def get_prompt(self, name: str, label: str = "production") -> Any:
        """Fetch a prompt from the Langfuse Prompt Registry.

        Args:
            name: Prompt name in Langfuse (e.g., "weather-spec-extraction").
            label: Prompt label (default: "production").

        Returns:
            A Langfuse prompt object with .compile(**vars) method.
        """
        from src.observability.tracing import get_langfuse_client
        client = get_langfuse_client()
        if client is None:
            raise RuntimeError(
                "Langfuse client not available. Set LANGFUSE_PUBLIC_KEY, "
                "LANGFUSE_SECRET_KEY, and LANGFUSE_HOST."
            )
        return client.get_prompt(name, label=label)

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        langfuse_prompt: Any = None,
        generation_name: str = "llm-completion",
    ) -> dict[str, Any]:
        """Send a chat completion request via LiteLLM proxy.

        When langfuse.openai.OpenAI is used, the call is automatically traced
        as a Generation observation nested in the current span. The
        langfuse_prompt parameter links the generation to a Langfuse prompt
        version for prompt-version tracking in the UI.

        Args:
            messages: List of {"role": "...", "content": "..."} dicts.
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Maximum tokens in response.
            langfuse_prompt: Optional Langfuse prompt object for linking.
            generation_name: Name for the generation observation in Langfuse.

        Returns:
            Dict with keys: content, model, usage, latency_ms.
        """
        start = time.monotonic()

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # langfuse.openai.OpenAI accepts a 'name' parameter for the generation
        # observation name
        kwargs["name"] = generation_name

        # If we have a Langfuse prompt, use the langfuse_prompt parameter
        # which langfuse.openai.OpenAI understands for prompt linking
        if langfuse_prompt is not None:
            kwargs["langfuse_prompt"] = langfuse_prompt

        resp = self._client.chat.completions.create(**kwargs)

        latency_ms = int((time.monotonic() - start) * 1000)
        choice = resp.choices[0]
        usage = resp.usage

        return {
            "content": choice.message.content or "",
            "model": resp.model,
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
            },
            "latency_ms": latency_ms,
        }


# Module-level singleton — initialized once, reused everywhere
_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """Get the module-level LLMClient singleton."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient.from_env()
    return _llm_client
