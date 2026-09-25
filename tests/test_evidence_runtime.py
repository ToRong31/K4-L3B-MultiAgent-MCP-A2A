from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from jsonschema import ValidationError

from student_agent.core.evidence import EvidenceCollector
from student_agent.core.memory import AgentMemory


class Gateway:
    def __init__(self, failures=0):
        self.calls = 0
        self.failures = failures

    async def describe_tools(self):
        return {
            "get_order": {
                "type": "object",
                "properties": {"case_id": {}, "order_id": {}},
                "required": ["case_id", "order_id"],
                "additionalProperties": False,
            }
        }

    async def call(self, name, *, case_id, **arguments):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise TimeoutError("temporary")
        return {
            "evidence_ref": "ev_" + str(self.calls).zfill(20),
            "domain": "order",
            "data": {"order_id": arguments["order_id"]},
        }


def test_cache_hit_recorded_for_each_task_and_retrievable_across_connections(tmp_path: Path):
    async def run():
        path = tmp_path / "memory.db"
        m1 = AgentMemory(path, run_id="run-1")
        m2 = AgentMemory(path, run_id="run-1")
        gateway = Gateway()
        try:
            first = await EvidenceCollector(gateway, m1).call(
                "order", "get_order", case_id="CASE_001", turn_id="task-1", order_id="one"
            )
            second_collector = EvidenceCollector(gateway, m2)
            second = await second_collector.call(
                "order", "get_order", case_id="CASE_001", turn_id="task-2", order_id="one"
            )
            assert first == second and gateway.calls == 1
            assert second_collector.tool_uses("CASE_001", "order", "task-2")
            assert any(
                e["turn_id"] == "task-2" and e["kind"] == "tool_result"
                for e in m1.raw_history("CASE_001", "order")
            )
            assert m2.get_evidence("CASE_001", first["evidence_ref"]) == first
            with pytest.raises(KeyError):
                m2.get_evidence("CASE_002", first["evidence_ref"])
            m3 = AgentMemory(path, run_id="run-2")
            try:
                with pytest.raises(KeyError):
                    m3.get_evidence("CASE_001", first["evidence_ref"])
                await EvidenceCollector(gateway, m3).call(
                    "order", "get_order", case_id="CASE_001", turn_id="task-3", order_id="one"
                )
                assert gateway.calls == 2
            finally:
                m3.close()
        finally:
            m1.close()
            m2.close()

    asyncio.run(run())


def test_retry_budget_and_schema_guard(tmp_path: Path, monkeypatch):
    async def run():
        memory = AgentMemory(tmp_path / "m.db")
        gateway = Gateway(failures=1)
        collector = EvidenceCollector(gateway, memory)
        monkeypatch.setattr("student_agent.core.evidence.QUERY_BUDGET", {"order": 2})
        try:
            with pytest.raises(ValidationError):
                await collector.call(
                    "order", "get_order", case_id="CASE_001", turn_id="bad", guessed_argument="x"
                )
            assert gateway.calls == 0
            await collector.call(
                "order", "get_order", case_id="CASE_001", turn_id="task-1", order_id="one"
            )
            assert gateway.calls == 2
            with pytest.raises(RuntimeError, match="budget exhausted"):
                await collector.call(
                    "order", "get_order", case_id="CASE_001", turn_id="task-2", order_id="two"
                )
        finally:
            memory.close()

    asyncio.run(run())
