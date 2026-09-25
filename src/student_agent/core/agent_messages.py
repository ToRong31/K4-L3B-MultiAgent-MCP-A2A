"""Small, versioned payloads carried inside A2A text messages."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

AGENTS = ("order", "payment", "shipment", "policy", "verifier")
EVIDENCE_REF = re.compile(r"^ev_[A-Za-z0-9_-]{20,96}$")
CASE_ID = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,63}$")


@dataclass(frozen=True)
class WorkOrder:
    case_id: str
    target: str
    task_id: str
    input: dict[str, Any]
    evidence_refs: list[str] = field(default_factory=list)
    version: int = 1

    def __post_init__(self) -> None:
        if self.target not in AGENTS:
            raise ValueError(f"unknown agent: {self.target}")
        if not CASE_ID.fullmatch(self.case_id) or not self.task_id:
            raise ValueError("case_id and task_id are required")
        if any(
            not isinstance(ref, str) or not EVIDENCE_REF.fullmatch(ref)
            for ref in self.evidence_refs
        ):
            raise ValueError("invalid evidence_ref in WorkOrder")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkOrder:
        if data.get("version") != 1:
            raise ValueError("unsupported WorkOrder version")
        return cls(**data)


@dataclass(frozen=True)
class Finding:
    case_id: str
    agent: str
    task_id: str
    status: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    tool_uses: list[dict[str, str]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    version: int = 1

    def __post_init__(self) -> None:
        if self.agent not in AGENTS or self.status not in {"completed", "needs_evidence", "failed"}:
            raise ValueError("invalid Finding agent or status")
        if not CASE_ID.fullmatch(self.case_id) or not self.task_id:
            raise ValueError("invalid Finding case or task")
        if any(
            not isinstance(ref, str) or not EVIDENCE_REF.fullmatch(ref)
            for ref in self.evidence_refs
        ):
            raise ValueError("invalid evidence_ref in Finding")
        if any(
            not isinstance(use, dict)
            or set(use) != {"tool_name", "evidence_ref"}
            or not isinstance(use["tool_name"], str)
            or not isinstance(use["evidence_ref"], str)
            or not EVIDENCE_REF.fullmatch(use["evidence_ref"])
            for use in self.tool_uses
        ):
            raise ValueError("invalid tool use in Finding")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        if data.get("version") != 1:
            raise ValueError("unsupported Finding version")
        return cls(**data)
