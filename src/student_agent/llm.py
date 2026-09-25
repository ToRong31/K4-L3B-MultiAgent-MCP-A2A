from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from .config import Settings

logger = logging.getLogger(__name__)


class LLMClient:
    """Flexible LLM Client supporting both OpenRouter and official OpenAI APIs."""

    def __init__(
        self,
        settings: Settings | None = None,
        provider: str | None = None,
    ) -> None:
        if settings is None:
            settings = Settings.load()
        self.settings = settings
        self.provider = (provider or settings.llm_provider).lower()

        self._clients: dict[str, AsyncOpenAI] = {}
        self._init_clients()

    def _init_clients(self) -> None:
        if self.settings.openrouter_api_key:
            self._clients["openrouter"] = AsyncOpenAI(
                api_key=self.settings.openrouter_api_key,
                base_url=self.settings.openrouter_base_url,
                default_headers={
                    "HTTP-Referer": "https://day09.vinaction.local",
                    "X-Title": "Day09 Multi-Agent Dispute Investigation",
                },
            )
        if self.settings.openai_api_key:
            self._clients["openai"] = AsyncOpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
            )

    @property
    def active_client(self) -> AsyncOpenAI | None:
        return self._clients.get(self.provider)

    @property
    def is_available(self) -> bool:
        return self.active_client is not None

    def get_default_model(self, provider: str | None = None) -> str:
        p = (provider or self.provider).lower()
        if p == "openai":
            return self.settings.openai_model
        return self.settings.openrouter_model

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.1,
        response_format: dict[str, Any] | None = None,
        provider: str | None = None,
    ) -> str:
        """Call LLM via chosen provider ('openrouter' or 'openai') and return text."""
        target_provider = (provider or self.provider).lower()
        client = self._clients.get(target_provider)

        if client is None:
            # Check if alternative provider is available
            alt_provider = "openai" if target_provider == "openrouter" else "openrouter"
            if alt_provider in self._clients:
                logger.warning(
                    "Provider '%s' has no API key, falling back to '%s'",
                    target_provider,
                    alt_provider,
                )
                target_provider = alt_provider
                client = self._clients[target_provider]
            else:
                raise RuntimeError(
                    f"No API key configured for provider '{target_provider}'. "
                    f"Please set OPENROUTER_API_KEY or OPENAI_API_KEY in .env."
                )

        target_model = model or self.get_default_model(target_provider)
        kwargs: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 2048,
            "timeout": 25.0,
        }
        if target_provider == "openrouter":
            kwargs["extra_body"] = {"reasoning": {"effort": "none"}}
        elif response_format is not None:
            kwargs["response_format"] = response_format

        try:
            response = await client.chat.completions.create(**kwargs)
            msg = response.choices[0].message
            content = msg.content or ""
            if not content and hasattr(msg, "reasoning") and msg.reasoning:
                content = msg.reasoning
            return content
        except Exception as exc:
            # If current provider failed and alternate is configured, attempt fallback
            alt_provider = "openai" if target_provider == "openrouter" else "openrouter"
            if alt_provider in self._clients and provider is None:
                logger.warning(
                    "Call to %s (%s) failed with %s. Attempting fallback to %s...",
                    target_provider,
                    target_model,
                    exc,
                    alt_provider,
                )
                fallback_client = self._clients[alt_provider]
                fallback_model = self.get_default_model(alt_provider)
                kwargs["model"] = fallback_model
                if alt_provider == "openai" and "extra_body" in kwargs:
                    del kwargs["extra_body"]
                response = await fallback_client.chat.completions.create(**kwargs)
                return response.choices[0].message.content or ""
            raise

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.1,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Call LLM and parse JSON response robustly."""
        content = await self.chat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            provider=provider,
        )
        content = content.strip()
        first_brace = content.find("{")
        last_brace = content.rfind("}")
        if first_brace != -1 and last_brace != -1:
            json_str = content[first_brace : last_brace + 1]
            return json.loads(json_str)
        return json.loads(content)

