"""Common specialist contract; domain investigation belongs in each agent folder."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace

from ..core.agent_messages import Finding, WorkOrder
from ..core.evidence import EvidenceCollector
from ..core.llm_client import LLMClient
from ..core.memory import AgentMemory


class Specialist(ABC):
    name: str
    description: str

    def __init__(
        self,
        memory: AgentMemory,
        llm: LLMClient | None = None,
        evidence: EvidenceCollector | None = None,
    ) -> None:
        self.memory = memory
        self.llm = llm
        self.evidence = evidence

    async def handle(self, work: WorkOrder) -> Finding:
        if work.target != self.name:
            raise ValueError(f"{self.name} cannot handle work for {work.target}")
        self.memory.append(work.case_id, self.name, work.task_id, "a2a_request", work.to_dict())
        finding = await self.investigate(work)
        if (finding.case_id, finding.agent, finding.task_id) != (
            work.case_id,
            self.name,
            work.task_id,
        ):
            raise ValueError("specialist returned a mismatched case or task")
        uses = (
            self.evidence.tool_uses(work.case_id, self.name, work.task_id) if self.evidence else []
        )
        allowed = set(work.evidence_refs) | {use["evidence_ref"] for use in uses}
        if not set(finding.evidence_refs).issubset(allowed):
            raise ValueError("specialist cited evidence outside this handoff")
        finding = replace(
            finding,
            evidence_refs=sorted(set(finding.evidence_refs) | {u["evidence_ref"] for u in uses}),
            tool_uses=uses,
        )
        if finding.status == "completed" and not finding.evidence_refs and self.name != "verifier":
            raise ValueError("completed specialist finding requires MCP evidence")
        self.memory.append(work.case_id, self.name, work.task_id, "a2a_response", finding.to_dict())
        return finding

    @abstractmethod
    async def investigate(self, work: WorkOrder) -> Finding:
        """Discover permitted MCP tools, collect evidence, and return grounded facts."""
