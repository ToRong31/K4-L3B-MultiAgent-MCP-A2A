from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .contracts import Contracts
from .core.evidence import EvidenceCollector
from .core.memory import AgentMemory
from .mcp_gateway import EvidenceGateway
from .orchestrator.coordinator import Coordinator
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """A2A coordinator entry point with a shared case/run evidence scope."""
    if not os.getenv("L3B_RUN_ID", "").strip():
        raise RuntimeError("L3B_RUN_ID must be shared by coordinator and all A2A servers")
    root = Path(trace.path).resolve().parent.parent
    memory_path = root / os.getenv("AGENT_MEMORY_DB", "runtime/agent-memory.sqlite3")
    memory = AgentMemory(memory_path)
    try:
        contracts = Contracts(root / "contracts" / "schemas")
        evidence = EvidenceCollector(gateway, memory)
        return await Coordinator(memory, trace, contracts, evidence).solve(case)
    finally:
        memory.close()
