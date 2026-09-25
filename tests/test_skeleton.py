from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
import uvicorn

from student_agent.agents.order_item import OrderItemAgent
from student_agent.core.a2a_transport import send_work, specialist_app
from student_agent.core.agent_messages import WorkOrder
from student_agent.core.memory import AgentMemory


def test_a2a_app_exposes_card_and_rpc(tmp_path: Path) -> None:
    memory = AgentMemory(tmp_path / "memory.sqlite3")
    try:
        app = specialist_app(OrderItemAgent(memory), "http://127.0.0.1:9001")
        assert {route.path for route in app.routes} == {
            "/.well-known/agent-card.json",
            "/a2a/jsonrpc/",
        }
    finally:
        memory.close()


def test_work_order_rejects_wrong_target() -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        WorkOrder("CASE_001", "other", "task-1", {})


def test_a2a_round_trip(tmp_path: Path) -> None:
    async def run() -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        memory = AgentMemory(tmp_path / "memory.sqlite3")
        url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(
            uvicorn.Config(
                specialist_app(OrderItemAgent(memory), url),
                host="127.0.0.1",
                port=port,
                log_level="error",
            )
        )
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            assert server.started
            finding = await send_work(url, WorkOrder("CASE_001", "order", "task-1", {}))
            assert finding.status == "needs_evidence"
            assert len(memory.raw_history("CASE_001", "order")) == 2
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            memory.close()

    asyncio.run(run())


def test_memory_compacts_at_80_percent_and_keeps_raw_history(tmp_path: Path) -> None:
    async def run() -> None:
        memory = AgentMemory(tmp_path / "memory.sqlite3")
        try:
            memory.append("CASE_001", "payment", "turn-1", "tool_call", {"tool": "payment"})
            memory.append(
                "CASE_001",
                "payment",
                "turn-1",
                "tool_result",
                {
                    "evidence_ref": "ev_" + "a" * 24,
                    "data": "x" * 1700,
                },
            )
            memory.append("CASE_001", "payment", "turn-2", "user_message", "recent")
            memory.append("CASE_001", "payment", "turn-3", "user_message", "new")

            async def summarize(prior: str, events: list[dict]) -> str:
                assert prior == ""
                assert [item["kind"] for item in events] == ["tool_call", "tool_result"]
                return "Payment evidence collected."

            changed = await memory.compact_if_needed(
                "CASE_001",
                "payment",
                context_length=1000,
                reserved_output_tokens=100,
                fixed_prompt_tokens=20,
                summarize=summarize,
            )
            assert changed
            assert len(memory.raw_history("CASE_001", "payment")) == 4
            history = memory.history("CASE_001", "payment")
            assert len(history["events"]) == 2
            assert history["evidence_refs"] == ["ev_" + "a" * 24]
            assert memory.history("CASE_002", "payment")["events"] == []
        finally:
            memory.close()

    asyncio.run(run())
