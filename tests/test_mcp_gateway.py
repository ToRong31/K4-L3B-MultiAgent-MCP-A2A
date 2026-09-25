from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.mcp_gateway import EvidenceGateway, MCPToolError


class FakeContracts:
    def validate_evidence(self, value: Any, label: str) -> None:
        assert value["schema_version"] == "day09-mcp-evidence-v1"
        assert label.startswith("MCP tool")


class FakeSession:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return self.result


def evidence() -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_abcdefghijklmnopqrstuvwxyz",
        "result_hash": f"sha256:{'a' * 64}",
        "domain": "order",
        "data": {"order_id": "order-1"},
    }


def test_gateway_accepts_snake_case_mcp_sdk_result() -> None:
    result = SimpleNamespace(is_error=False, structured_content=evidence(), content=[])
    gateway = EvidenceGateway(FakeSession(result), FakeContracts())  # type: ignore[arg-type]
    actual = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="order-1"))
    assert actual == evidence()


def test_gateway_accepts_camel_case_mcp_sdk_result() -> None:
    result = SimpleNamespace(isError=False, structuredContent=evidence(), content=[])
    gateway = EvidenceGateway(FakeSession(result), FakeContracts())  # type: ignore[arg-type]
    actual = asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="order-1"))
    assert actual == evidence()


def test_gateway_raises_tool_error_for_snake_case_result() -> None:
    result = SimpleNamespace(
        is_error=True,
        structured_content=None,
        content=[SimpleNamespace(text="not found")],
    )
    gateway = EvidenceGateway(FakeSession(result), FakeContracts())  # type: ignore[arg-type]
    with pytest.raises(MCPToolError, match="not found") as error:
        asyncio.run(gateway.call("get_order", case_id="CASE_001", order_id="missing"))
    assert error.value.retryable is False
