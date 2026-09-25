from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from student_agent.cases import CaseSet
from student_agent.contracts import ContractError, Contracts
from student_agent.core.evidence import EvidenceCollector
from student_agent.core.memory import AgentMemory
from student_agent.submission import build_manifest


class FakeGateway:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[str, str]] = []

    async def list_tools(self) -> list[str]:
        return ["get_order", "get_order_payments"]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict:
        self.calls.append((tool_name, case_id))
        if self.failures:
            self.failures -= 1
            raise TimeoutError("temporary timeout")
        return {"evidence_ref": "ev_" + case_id.lower().replace("_", "x").ljust(24, "a")}


def test_public_contract_changes_are_rejected(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    copied = tmp_path / "schemas"
    shutil.copytree(root / "contracts" / "schemas", copied)
    Contracts(copied)
    path = copied / "trace-event-v1.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["properties"]["actor"]["maxLength"] = 81
    path.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(ContractError, match="public contract changed"):
        Contracts(copied)


def test_manifest_rejects_undeclared_field() -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    manifest = build_manifest(CaseSet("test-v1", "l3b", ("CASE_001",), {}))
    contracts.validate_manifest(manifest)
    manifest["internal_memory"] = "must not leave the process"
    with pytest.raises(ContractError, match="Additional properties are not allowed"):
        contracts.validate_manifest(manifest)


def test_evidence_permission_retry_and_case_cache(tmp_path: Path) -> None:
    async def run() -> None:
        memory = AgentMemory(tmp_path / "memory.sqlite3")
        gateway = FakeGateway(failures=1)
        collector = EvidenceCollector(gateway, memory)
        try:
            with pytest.raises(PermissionError):
                await collector.call("payment", "get_order", case_id="CASE_001", turn_id="t1")
            with pytest.raises(ValueError, match="not discovered"):
                await collector.call("order", "get_sellers", case_id="CASE_001", turn_id="t1")
            first = await collector.call(
                "order", "get_order", case_id="CASE_001", turn_id="t1", order_id="one"
            )
            second = await collector.call(
                "order", "get_order", case_id="CASE_001", turn_id="t1", order_id="one"
            )
            assert first == second
            await collector.call(
                "order", "get_order", case_id="CASE_001", turn_id="t3", order_id="one"
            )
            assert any(
                event["turn_id"] == "t3" and event["kind"] == "tool_result"
                for event in memory.raw_history("CASE_001", "order")
            )
            assert gateway.calls == [("get_order", "CASE_001"), ("get_order", "CASE_001")]
            assert len(collector.tool_uses("CASE_001", "order", "t1")) == 1
            await collector.call(
                "order", "get_order", case_id="CASE_002", turn_id="t2", order_id="one"
            )
            assert gateway.calls[-1] == ("get_order", "CASE_002")
            assert memory.raw_history("CASE_001", "orchestrator")
        finally:
            memory.close()

    asyncio.run(run())
