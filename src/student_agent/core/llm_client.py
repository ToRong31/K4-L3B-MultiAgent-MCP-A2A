"""OpenAI-compatible LLM adapter shared by specialist agents."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx2

from .memory import AgentMemory, estimate_tokens

PROVIDER_DEFAULTS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
}


class LLMClient:
    @staticmethod
    def provider() -> str:
        provider = os.getenv("LLM_PROVIDER", "openrouter").strip().lower() or "openrouter"
        if provider not in PROVIDER_DEFAULTS:
            raise ValueError("LLM_PROVIDER must be 'openrouter' or 'openai'")
        return provider

    @classmethod
    def configured(cls) -> bool:
        prefix = cls.provider().upper()
        return bool(os.getenv(f"{prefix}_API_KEY") or os.getenv(f"{prefix}_MODEL"))

    def __init__(self, agent_id: str | None = None) -> None:
        self.provider_name = self.provider()
        prefix = self.provider_name.upper()
        self.api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
        role_model = os.getenv(f"{prefix}_{agent_id.upper()}_MODEL", "") if agent_id else ""
        self.model = (role_model or os.getenv(f"{prefix}_MODEL", "")).strip()
        self.context_length = int(os.getenv(f"{prefix}_CONTEXT_LENGTH", "32768"))
        if (
            not self.api_key
            or self.api_key == "replace_me"
            or not self.model
            or self.model == "replace_with_model_slug"
        ):
            raise ValueError(f"set {prefix}_API_KEY and {prefix}_MODEL in .env")
        if self.context_length <= 0:
            raise ValueError(f"{prefix}_CONTEXT_LENGTH must be positive")
        base_url = os.getenv(f"{prefix}_BASE_URL", PROVIDER_DEFAULTS[self.provider_name]).strip()
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"{prefix}_BASE_URL must be an absolute HTTP(S) URL")
        self._http = httpx2.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=90,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def check_context_length(self) -> int:
        # Use the configured limit for either provider; their model metadata APIs differ.
        return self.context_length

    async def complete(self, messages: list[dict[str, str]], *, max_tokens: int = 2048) -> str:
        response = await self._http.post(
            "chat/completions",
            json={"model": self.model, "messages": messages, "max_completion_tokens": max_tokens},
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError(f"{self.provider_name} returned non-text content")
        return content

    async def summarize(self, prior: str, events: list[dict[str, Any]]) -> str:
        return await self.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Summarize case history as concise facts, decisions, "
                        "conflicts and open tasks. "
                        "Keep exact identifiers and evidence_ref values. Do not invent evidence."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"prior_summary": prior, "events": events}, ensure_ascii=False
                    ),
                },
            ],
            max_tokens=1200,
        )

    async def complete_with_memory(
        self,
        memory: AgentMemory,
        *,
        case_id: str,
        agent_id: str,
        turn_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 2048,
    ) -> str:
        memory.append(case_id, agent_id, turn_id, "user_message", user_prompt)
        await self.check_context_length()
        await memory.compact_if_needed(
            case_id,
            agent_id,
            context_length=self.context_length,
            reserved_output_tokens=max_tokens,
            fixed_prompt_tokens=estimate_tokens(system_prompt),
            summarize=self.summarize,
        )
        history = memory.history(case_id, agent_id)
        # Serialize as one user block to preserve tool-call/result links and evidence metadata.
        response = await self.complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(history, ensure_ascii=False)},
            ],
            max_tokens=max_tokens,
        )
        memory.append(case_id, agent_id, turn_id, "assistant_message", response)
        return response
