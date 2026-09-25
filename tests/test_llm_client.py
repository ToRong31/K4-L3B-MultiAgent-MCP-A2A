from __future__ import annotations

import asyncio

import pytest

from student_agent.core.llm_client import LLMClient


@pytest.mark.parametrize(
    ("provider", "base_url", "model"),
    [
        ("openrouter", "https://openrouter.ai/api/v1", "qwen/qwen3.5-9b"),
        ("openai", "https://api.openai.com/v1", "gpt-4o-mini"),
    ],
)
def test_selected_provider_routes_completion(monkeypatch, provider, base_url, model):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class HTTPClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def post(self, path, *, json):
            calls.append((path, json))
            return Response()

        async def aclose(self):
            pass

    monkeypatch.setattr("student_agent.core.llm_client.httpx2.AsyncClient", HTTPClient)
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-secret")
    monkeypatch.setenv("OPENROUTER_MODEL", "router-default")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENAI_MODEL", "openai-default")
    prefix = provider.upper()
    monkeypatch.setenv(f"{prefix}_ORDER_MODEL", model)
    monkeypatch.setenv(f"{prefix}_BASE_URL", base_url)
    client = LLMClient("order")
    try:
        assert asyncio.run(client.complete([{"role": "user", "content": "hello"}])) == "ok"
    finally:
        asyncio.run(client.close())
    assert calls[0]["base_url"] == base_url + "/"
    expected_key = "openai-secret" if provider == "openai" else "router-secret"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {expected_key}"
    assert calls[1] == (
        "chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 2048,
        },
    )


def test_invalid_or_partial_provider_configuration(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "other")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        LLMClient.configured()
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert LLMClient.configured()
    with pytest.raises(ValueError, match="OPENAI_API_KEY and OPENAI_MODEL"):
        LLMClient()
