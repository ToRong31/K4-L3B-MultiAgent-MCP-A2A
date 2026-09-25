import asyncio
from pathlib import Path

from student_agent.agents.verifier import VerifierAgent
from student_agent.core.agent_messages import WorkOrder
from student_agent.core.memory import AgentMemory
from student_agent.orchestrator.coordinator import DEFAULTS


def draft(case_id="CASE_001"):
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        **{key: value.copy() for key, value in DEFAULTS.items()},
        "evidence_refs": [],
    }


def verify(memory, output, refs=None):
    work = WorkOrder(
        "CASE_001",
        "verifier",
        "verify-1",
        {"case": {"case_id": "CASE_001"}, "draft_output": output, "context": {}, "findings": []},
        refs or [],
    )
    return asyncio.run(VerifierAgent(memory).handle(work)).facts[0]["data"]


def test_valid_investigation_is_approved(tmp_path: Path):
    memory = AgentMemory(tmp_path / "memory.db")
    try:
        assert verify(memory, draft()) == {"approved": True, "errors": []}
    finally:
        memory.close()


def test_regex_ref_without_recorded_tool_result_is_rejected(tmp_path: Path):
    memory = AgentMemory(tmp_path / "memory.db")
    try:
        output = draft()
        output["evidence_refs"] = ["ev_" + "x" * 24]
        result = verify(memory, output, output["evidence_refs"])
        assert not result["approved"]
        assert "UNRECORDED_EVIDENCE" in {e["code"] for e in result["errors"]}
    finally:
        memory.close()


def test_stored_case_scoped_tool_result_supports_ref(tmp_path: Path):
    memory = AgentMemory(tmp_path / "memory.db")
    try:
        ref = "ev_" + "y" * 24
        memory.append("CASE_001", "orchestrator", "task-1", "tool_result", {
            "agent": "order", "tool_name": "get_order",
            "evidence": {"evidence_ref": ref, "data": {"order_id": "one"}}
        })
        output = draft()
        output["evidence_refs"] = [ref]
        assert verify(memory, output, [ref])["approved"]
        other = "ev_" + "z" * 24
        memory.append("CASE_002", "orchestrator", "task-2", "tool_result", {
            "agent": "order", "tool_name": "get_order",
            "evidence": {"evidence_ref": other}
        })
        output["evidence_refs"] = [other]
        assert not verify(memory, output, [other])["approved"]
    finally:
        memory.close()


def test_refund_and_entity_conflicts_are_rejected(tmp_path: Path):
    memory = AgentMemory(tmp_path / "memory.db")
    try:
        output = draft()
        output["entity_resolution"] = {
            "status": "resolved",
            "resolved_order_ids": ["order-1"],
            "rejected_candidates": ["order-1"],
            "confidence": 0.9,
        }
        output["financial_resolution"] = {
            "currency": "BRL",
            "recommended_refund_brl": 10,
            "refund_lines": [{"reason_code": "REFUND", "amount_brl": 5, "entity_id": None}],
        }
        result = verify(memory, output)
        codes = {e["code"] for e in result["errors"]}
        assert {"ENTITY_CONFLICT", "REFUND_SUM"}.issubset(codes)
    finally:
        memory.close()


def test_aggregate_refund_without_allocation_lines_is_allowed(tmp_path: Path):
    memory = AgentMemory(tmp_path / "memory.db")
    try:
        output = draft()
        output["financial_resolution"] = {
            "currency": "BRL",
            "recommended_refund_brl": 10,
            "refund_lines": [],
        }
        assert verify(memory, output)["approved"]
    finally:
        memory.close()
