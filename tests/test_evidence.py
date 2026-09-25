from __future__ import annotations

import asyncio
from typing import Any

import pytest

from student_agent.evidence import EvidenceLedger


class FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_abcdefghijklmnopqrstuvwxyz",
            "result_hash": f"sha256:{'a' * 64}",
            "domain": "order",
            "data": {"order_id": arguments["order_id"]},
        }


class FlakyGateway(FakeGateway):
    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        if self.calls == 0:
            self.calls += 1
            raise RuntimeError("transient timeout")
        return await super().call(tool_name, case_id=case_id, **arguments)


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> None:
        self.events.append(event)


def test_ledger_caches_calls_and_emits_consumption_once() -> None:
    gateway = FakeGateway()
    trace = FakeTrace()
    ledger = EvidenceLedger("CASE_001", gateway, trace)  # type: ignore[arg-type]

    first = asyncio.run(ledger.fetch("get_order", actor="entity", order_id="order-1"))
    second = asyncio.run(ledger.fetch("get_order", actor="entity", order_id="order-1"))
    ledger.consume(first, actor="entity")
    ledger.consume(second, actor="entity")

    assert first is second
    assert gateway.calls == 1
    assert ledger.call_count == 1
    assert ledger.consumed_refs == [first.evidence_ref]
    assert len(trace.events) == 1


def test_ledger_enforces_call_budget() -> None:
    ledger = EvidenceLedger(
        "CASE_001", FakeGateway(), FakeTrace(), max_calls=1  # type: ignore[arg-type]
    )
    asyncio.run(ledger.fetch("get_order", actor="entity", order_id="order-1"))
    with pytest.raises(RuntimeError, match="budget exhausted"):
        asyncio.run(ledger.fetch("get_order", actor="entity", order_id="order-2"))


def test_ledger_retries_and_reports_metrics() -> None:
    ledger = EvidenceLedger("CASE_001", FlakyGateway(), FakeTrace())  # type: ignore[arg-type]
    asyncio.run(ledger.fetch("get_order", actor="entity", order_id="order-1"))
    assert ledger.metrics()["mcp_calls"] == 2
    assert ledger.metrics()["retries"] == 1


def test_cache_is_isolated_between_cases() -> None:
    gateway = FakeGateway()
    first = EvidenceLedger("CASE_001", gateway, FakeTrace())  # type: ignore[arg-type]
    second = EvidenceLedger("CASE_002", gateway, FakeTrace())  # type: ignore[arg-type]
    asyncio.run(first.fetch("get_order", actor="entity", order_id="order-1"))
    asyncio.run(second.fetch("get_order", actor="entity", order_id="order-1"))
    assert gateway.calls == 2
