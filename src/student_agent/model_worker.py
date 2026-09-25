from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx2


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "IFM/K2-Horizon-7B-GGUF:Q4_K_M"
    context_tokens: int = 8192
    temperature: float = 0.1
    request_timeout_seconds: float = 45.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("model endpoint must be local HTTP")
        if not 1024 <= self.context_tokens <= 8192:
            raise ValueError("model context must be between 1024 and 8192 tokens")


class LocalModelWorker:
    """One serial inference lane shared by all logical agents."""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self._lock = asyncio.Lock()
        self.invocations = 0

    @property
    def is_ollama(self) -> bool:
        parsed = urlparse(self.config.base_url)
        return parsed.port == 11434 or "/api" in parsed.path

    async def ready(self) -> bool:
        try:
            async with httpx2.AsyncClient(timeout=3.0) as client:
                url = (
                    f"{self.config.base_url.rstrip('/')}/models"
                    if "/v1" in self.config.base_url
                    else f"{self.config.base_url.rstrip('/')}/api/tags"
                )
                response = await client.get(url)
                response.raise_for_status()
                return True
        except (httpx2.HTTPError, ValueError):
            return False

    async def complete(
        self, *, system: str, payload: dict[str, Any], schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        async with self._lock:
            self.invocations += 1
            async with httpx2.AsyncClient(timeout=self.config.request_timeout_seconds) as client:
                if self.is_ollama:
                    base = self.config.base_url.replace("/v1", "").rstrip("/")
                    request = {
                        "model": self.config.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": json.dumps(payload, ensure_ascii=False, default=str),
                            },
                        ],
                        "format": schema,
                        "think": False,
                        "options": {
                            "temperature": self.config.temperature,
                            "num_predict": max_tokens,
                        },
                        "stream": False,
                    }
                    response = await client.post(f"{base}/api/chat", json=request)
                    response.raise_for_status()
                    content = response.json()["message"]["content"]
                else:
                    request = {
                        "model": self.config.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": json.dumps(payload, ensure_ascii=False, default=str),
                            },
                        ],
                        "response_format": {"type": "json_object", "schema": schema},
                        "temperature": self.config.temperature,
                        "max_tokens": max_tokens,
                        "stream": False,
                    }
                    response = await client.post(
                        f"{self.config.base_url.rstrip('/')}/chat/completions", json=request
                    )
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("model returned non-text content")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("model returned non-object JSON")
        return value
