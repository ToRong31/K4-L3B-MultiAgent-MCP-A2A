"""Run one specialist A2A server: python -m student_agent.agents.serve NAME PORT."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from ..config import Settings
from ..contracts import Contracts
from ..core.a2a_transport import specialist_app
from ..core.evidence import EvidenceCollector
from ..core.llm_client import LLMClient
from ..core.memory import AgentMemory
from ..mcp_gateway import connect_gateway
from .order_item import OrderItemAgent
from .payment import PaymentAgent
from .policy import PolicyAgent
from .shipment import ShipmentAgent
from .verifier import VerifierAgent

AGENT_CLASSES = {
    "order": OrderItemAgent,
    "payment": PaymentAgent,
    "shipment": ShipmentAgent,
    "policy": PolicyAgent,
    "verifier": VerifierAgent,
}


async def _serve(name: str, port: int, root: Path) -> None:
    if not os.getenv("L3B_RUN_ID", "").strip():
        raise RuntimeError("L3B_RUN_ID must be shared by coordinator and all A2A servers")
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    db_path = root / os.getenv("AGENT_MEMORY_DB", "runtime/agent-memory.sqlite3")
    memory = AgentMemory(db_path)
    llm = LLMClient(name) if LLMClient.configured() else None
    try:
        if name == "verifier":
            specialist = VerifierAgent(memory, llm, contracts=contracts)
            server = uvicorn.Server(
                uvicorn.Config(
                    specialist_app(specialist, f"http://127.0.0.1:{port}"),
                    host="127.0.0.1",
                    port=port,
                )
            )
            await server.serve()
        else:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                evidence = EvidenceCollector(gateway, memory)
                await evidence.discover()
                specialist = AGENT_CLASSES[name](memory, llm, evidence)
                server = uvicorn.Server(
                    uvicorn.Config(
                        specialist_app(specialist, f"http://127.0.0.1:{port}"),
                        host="127.0.0.1",
                        port=port,
                    )
                )
                await server.serve()
    finally:
        if llm is not None:
            await llm.close()
        memory.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", choices=AGENT_CLASSES)
    parser.add_argument("port", type=int)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    load_dotenv(root / ".env")
    asyncio.run(_serve(args.name, args.port, root))


if __name__ == "__main__":
    main()
