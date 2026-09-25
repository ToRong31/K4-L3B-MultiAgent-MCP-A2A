from __future__ import annotations

import asyncio
from pathlib import Path

from student_agent.core.entity_resolution import resolve_case_context
from student_agent.core.evidence import EvidenceCollector
from student_agent.core.memory import AgentMemory


class Gateway:
    def __init__(self, history: list[str], orders: dict[str, str], fail_missing=False) -> None:
        self.history = history
        self.orders = orders
        self.fail_missing = fail_missing
        self.calls: list[str] = []

    async def describe_tools(self):
        return {
            "get_order": {"type": "object", "properties": {"case_id": {}, "order_id": {}}},
            "get_customer_history": {
                "type": "object",
                "properties": {"case_id": {}, "customer_unique_id": {}},
            },
        }

    async def call(self, name, *, case_id, **arguments):
        self.calls.append(name)
        if name == "get_customer_history":
            data = {
                "customer_unique_id": "customer-1",
                "orders": [{"order_id": order, "customer_id": "row-1"} for order in self.history],
            }
        else:
            order = arguments["order_id"]
            if self.fail_missing and order not in self.orders:
                raise RuntimeError("MCP tool get_order failed: unavailable candidate")
            data = (
                {"order_id": order, "customer_id": self.orders[order]}
                if order in self.orders
                else {}
            )
        return {
            "evidence_ref": "ev_" + str(len(self.calls)).zfill(20),
            "domain": "customer" if name == "get_customer_history" else "order",
            "data": data,
        }


def _case():
    return {
        "case_id": "CASE_001",
        "candidate_order_ids": ["wrong", "right"],
        "customer_unique_id_hint": "customer-1",
        "customer_request": {},
    }


def test_resolves_and_rejects_with_real_linkage(tmp_path: Path):
    async def run():
        memory = AgentMemory(tmp_path / "m.db", run_id="run-1")
        try:
            result = await resolve_case_context(
                _case(),
                EvidenceCollector(Gateway(["right"], {"wrong": "row-2", "right": "row-1"}), memory),
                memory,
            )
            er = result["context"]["entity_resolution"]
            assert er["status"] == "resolved"
            assert er["resolved_order_ids"] == ["right"]
            assert er["rejected_candidates"] == ["wrong"]
            assert all(memory.get_evidence("CASE_001", ref) for ref in result["evidence_refs"])
        finally:
            memory.close()

    asyncio.run(run())


def test_ambiguous_candidates_are_not_arbitrarily_selected(tmp_path: Path):
    async def run():
        memory = AgentMemory(tmp_path / "m.db")
        try:
            result = await resolve_case_context(
                _case(),
                EvidenceCollector(
                    Gateway(["wrong", "right"], {"wrong": "row-1", "right": "row-1"}), memory
                ),
                memory,
            )
            er = result["context"]["entity_resolution"]
            assert er["status"] == "ambiguous"
            assert er["resolved_order_ids"] == []
            assert result["open_questions"]
        finally:
            memory.close()

    asyncio.run(run())


def test_failed_candidate_lookup_needs_customer_exclusion_to_reject(tmp_path: Path):
    async def run():
        memory = AgentMemory(tmp_path / "m.db")
        try:
            result = await resolve_case_context(
                _case(),
                EvidenceCollector(
                    Gateway(["right"], {"right": "row-1"}, fail_missing=True), memory
                ),
                memory,
            )
            assert result["context"]["entity_resolution"]["rejected_candidates"] == ["wrong"]
            assert result["context"]["entity_resolution"]["resolved_order_ids"] == ["right"]
        finally:
            memory.close()

    asyncio.run(run())
