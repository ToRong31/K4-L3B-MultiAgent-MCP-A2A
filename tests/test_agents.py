from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from student_agent.agent_runtime import AgentRuntime, deterministic_route
from student_agent.agents import (
    HYPOTHESIS_SCHEMA,
    AgentTask,
    EntityAgent,
    ShipmentAgent,
    make_task,
)
from student_agent.contracts import Contracts
from student_agent.evidence import EvidenceLedger, ToolRequest
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case
from test_workflow import FakeGateway


class FakeWorker:
    def __init__(self, *, bad_refs: bool = False, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bad_refs = bad_refs
        self.fail = fail

    async def complete(
        self, *, system: str, payload: dict[str, Any], schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]:
        self.calls.append((system, payload))
        if self.fail:
            raise ValueError("invalid model JSON")
        if schema is HYPOTHESIS_SCHEMA:
            return {
                "hypotheses": ["check structured evidence"],
                "evidence_refs": ["ev_invented"]
                if self.bad_refs
                else list(payload["evidence_refs"][:1]),
                "uncertainties": [],
                "follow_up_tools": [],
            }
        if "qualitative_confidence" in schema["properties"]:
            return {
                "primary_issue": payload["context"]["allowed_issues"][0],
                "secondary_issues": [],
                "responsible_parties": [],
                "claim_verdicts": {},
                "evidence_refs": list(payload["evidence_refs"][:1]),
                "qualitative_confidence": "low",
                "contradictions": [],
            }
        return {
            "warnings": ["disagreement"],
            "request_reverification": True,
            "evidence_refs": list(payload["evidence_refs"][:1]),
        }


def fixtures(tmp_path: Path) -> tuple[Contracts, FakeGateway, TraceWriter]:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    return contracts, FakeGateway(contracts), TraceWriter(tmp_path / "trace.jsonl", contracts)


def sample_case() -> dict[str, Any]:
    return {
        "case_id": "CASE_001",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-1", "topic": "late_delivery_logistics"}],
        },
        "candidate_order_ids": ["order-1"],
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {},
    }


def test_typed_envelope_and_tool_allowlists(tmp_path: Path) -> None:
    _, _, trace = fixtures(tmp_path)
    worker = FakeWorker()
    entity = EntityAgent(worker, trace)
    shipment = ShipmentAgent(worker, trace)
    assert entity.worker is shipment.worker
    assert "get_order" in entity.tool_allowlist
    assert "get_order" not in shipment.tool_allowlist
    task = make_task("CASE_001", "entity", (), (), (), {}, "corr_1")
    with pytest.raises(FrozenInstanceError):
        task.role = "shipment"  # type: ignore[misc]
    with pytest.raises(ValueError, match="identity"):
        AgentTask("", "task", "corr", "entity", (), (), (), {})


def test_invented_refs_retry_then_fallback(tmp_path: Path) -> None:
    _, _, trace = fixtures(tmp_path)
    worker = FakeWorker(bad_refs=True)
    task = make_task("CASE_001", "entity", (), (), (), {}, "corr_1")
    result = asyncio.run(EntityAgent(worker, trace).run(task))
    assert result.value is None
    assert result.fallback_reason == "ValueError"
    assert len(worker.calls) == 2


def test_invalid_json_and_timeout_fallback(tmp_path: Path) -> None:
    _, _, trace = fixtures(tmp_path)
    worker = FakeWorker(fail=True)
    task = make_task("CASE_001", "entity", (), (), (), {}, "corr_1")
    assert asyncio.run(EntityAgent(worker, trace).run(task)).value is None
    assert len(worker.calls) == 2

    class SlowWorker(FakeWorker):
        async def complete(self, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(0.02)
            return await super().complete(**kwargs)

    slow = SlowWorker()
    agent = EntityAgent(slow, trace)
    agent.timeout_seconds = 0.001
    assert asyncio.run(agent.run(task)).fallback_reason == "TimeoutError"


def test_tool_request_ledger_immutability_budget_and_case_isolation(tmp_path: Path) -> None:
    _, gateway, trace = fixtures(tmp_path)
    first = EvidenceLedger("CASE_001", gateway, trace, max_calls=1)
    request = ToolRequest("CASE_001", "entity", "get_order", {"order_id": "order-1"})
    record = asyncio.run(first.request(request))
    assert asyncio.run(first.request(request)) is record
    assert first.metrics()["cache_hits"] == 1
    with pytest.raises(TypeError):
        record.data["order_id"] = "changed"  # type: ignore[index]
    with pytest.raises(PermissionError):
        asyncio.run(first.request(ToolRequest("CASE_002", "entity", "get_order", {})))
    with pytest.raises(PermissionError):
        asyncio.run(first.request(ToolRequest("CASE_001", "shipment", "get_order", {})))
    with pytest.raises(RuntimeError, match="budget"):
        asyncio.run(first.fetch("get_customer_history", actor="entity", customer_unique_id="x"))
    second = EvidenceLedger("CASE_002", gateway, trace)
    asyncio.run(
        second.request(ToolRequest("CASE_002", "entity", "get_order", {"order_id": "order-1"}))
    )
    assert len(gateway.calls) == 2


def test_shared_worker_critic_disagreement_and_deterministic_authority(tmp_path: Path) -> None:
    contracts, gateway, trace = fixtures(tmp_path)
    worker = FakeWorker()
    runtime = AgentRuntime(worker, trace)
    assert {id(agent.worker) for agent in runtime.agents.values()} == {id(worker)}
    case = sample_case()
    output = asyncio.run(solve_case(case, gateway, trace, agent_runtime=runtime))  # type: ignore[arg-type]
    contracts.validate_output(output, "offline")
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert output["assessment"]["confidence"] <= 0.55
    assert len(worker.calls) == 5  # Entity, Shipment, Policy, Adjudicator, Critic.
    assert all("customer history" not in system.lower() for system, _ in worker.calls)
    assert deterministic_route(case) == frozenset({"entity", "shipment", "policy"})


def test_model_failure_does_not_crash_case(tmp_path: Path) -> None:
    _, gateway, trace = fixtures(tmp_path)
    worker = FakeWorker(fail=True)
    output = asyncio.run(
        solve_case(sample_case(), gateway, trace, agent_runtime=AgentRuntime(worker, trace))
    )  # type: ignore[arg-type]
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert output["assessment"]["confidence"] <= 0.7
