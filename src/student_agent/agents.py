from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from .evidence import DerivedFact, EvidenceLedger
from .trace import TraceWriter


class ModelWorker(Protocol):
    async def complete(
        self, *, system: str, payload: dict[str, Any], schema: dict[str, Any], max_tokens: int
    ) -> dict[str, Any]: ...


HYPOTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hypotheses", "evidence_refs", "uncertainties", "follow_up_tools"],
    "properties": {
        "hypotheses": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 160},
        },
        "evidence_refs": {
            "type": "array",
            "uniqueItems": True,
            "maxItems": 12,
            "items": {"type": "string"},
        },
        "uncertainties": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 160},
        },
        "follow_up_tools": {
            "type": "array",
            "uniqueItems": True,
            "maxItems": 2,
            "items": {"type": "string"},
        },
    },
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["specialists", "unresolved_questions"],
    "properties": {
        "specialists": {
            "type": "array",
            "uniqueItems": True,
            "maxItems": 5,
            "items": {"type": "string"},
        },
        "unresolved_questions": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string", "maxLength": 160},
        },
    },
}

ADJUDICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "primary_issue",
        "secondary_issues",
        "responsible_parties",
        "claim_verdicts",
        "evidence_refs",
        "qualitative_confidence",
        "contradictions",
    ],
    "properties": {
        "primary_issue": {"type": "string"},
        "secondary_issues": {
            "type": "array",
            "uniqueItems": True,
            "maxItems": 5,
            "items": {"type": "string"},
        },
        "responsible_parties": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
        "claim_verdicts": {
            "type": "object",
            "additionalProperties": {
                "enum": ["supported", "unsupported", "partially_supported", "insufficient_evidence"]
            },
        },
        "evidence_refs": {
            "type": "array",
            "uniqueItems": True,
            "maxItems": 20,
            "items": {"type": "string"},
        },
        "qualitative_confidence": {"enum": ["high", "medium", "low"]},
        "contradictions": {
            "type": "array",
            "maxItems": 5,
            "items": {"type": "string", "maxLength": 160},
        },
    },
}

CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["warnings", "request_reverification", "evidence_refs"],
    "properties": {
        "warnings": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 160}},
        "request_reverification": {"type": "boolean"},
        "evidence_refs": {
            "type": "array",
            "uniqueItems": True,
            "maxItems": 20,
            "items": {"type": "string"},
        },
    },
}


@dataclass(frozen=True)
class AgentTask:
    case_id: str
    task_id: str
    correlation_id: str
    role: str
    claims: tuple[tuple[str, str], ...]
    facts: tuple[DerivedFact, ...]
    evidence_refs: tuple[str, ...]
    context: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.case_id or not self.task_id or not self.correlation_id:
            raise ValueError("task identity is required")


@dataclass(frozen=True)
class AgentResult:
    case_id: str
    task_id: str
    correlation_id: str
    role: str
    value: dict[str, Any] | None
    fallback_reason: str | None = None


class BoundedAgent:
    role = "base"
    instructions = ""
    tool_allowlist: frozenset[str] = frozenset()
    output_schema: dict[str, Any] = HYPOTHESIS_SCHEMA
    max_tokens = 256
    timeout_seconds = 20.0
    retry_limit = 1

    def __init__(self, worker: ModelWorker, trace: TraceWriter) -> None:
        self.worker = worker
        self.trace = trace

    async def run(self, task: AgentTask, ledger: EvidenceLedger | None = None) -> AgentResult:
        if task.role != self.role:
            raise ValueError("task role mismatch")
        valid_refs = set(ledger.consumed_refs) if ledger is not None else set(task.evidence_refs)
        if not set(task.evidence_refs).issubset(valid_refs):
            raise ValueError("task contains evidence outside the case ledger")
        self.trace.emit(
            case_id=task.case_id,
            event_type="task_assigned",
            actor="supervisor",
            target=self.role,
            decision_code="AGENT_STARTED",
            attributes={
                "task_id": task.task_id,
                "correlation_id": task.correlation_id,
                "role": self.role,
            },
        )
        payload = {
            "case_id": task.case_id,
            "claims": [{"claim_id": claim_id, "topic": topic} for claim_id, topic in task.claims],
            "facts": [
                {"code": fact.code, "value": fact.value, "evidence_refs": fact.evidence_refs}
                for fact in task.facts
            ],
            "evidence_refs": task.evidence_refs,
            "context": task.context,
            "allowed_tools": sorted(self.tool_allowlist),
        }
        error = "unknown"
        for attempt in range(self.retry_limit + 1):
            try:
                value = await asyncio.wait_for(
                    self.worker.complete(
                        system=self.instructions,
                        payload=payload,
                        schema=self.output_schema,
                        max_tokens=self.max_tokens,
                    ),
                    timeout=self.timeout_seconds,
                )
                Draft202012Validator(self.output_schema).validate(value)
                if not set(value.get("evidence_refs", ())).issubset(valid_refs):
                    raise ValueError("agent invented evidence_ref")
                if not set(value.get("follow_up_tools", ())).issubset(self.tool_allowlist):
                    raise ValueError("agent requested forbidden tool")
                self.trace.emit(
                    case_id=task.case_id,
                    event_type="handoff",
                    actor=self.role,
                    target="supervisor",
                    decision_code="AGENT_COMPLETED",
                    attributes={
                        "task_id": task.task_id,
                        "correlation_id": task.correlation_id,
                        "model_invocations": attempt + 1,
                    },
                )
                return AgentResult(
                    task.case_id, task.task_id, task.correlation_id, self.role, value
                )
            except Exception as exc:  # A malformed or unavailable model is a per-agent fallback.
                error = type(exc).__name__
        self.trace.emit(
            case_id=task.case_id,
            event_type="handoff",
            actor=self.role,
            target="supervisor",
            decision_code="AGENT_FALLBACK",
            attributes={
                "task_id": task.task_id,
                "correlation_id": task.correlation_id,
                "fallback_reason": error,
            },
        )
        return AgentResult(task.case_id, task.task_id, task.correlation_id, self.role, None, error)


class SupervisorAgent(BoundedAgent):
    role = "supervisor"
    instructions = (
        "You route a commerce complaint. Return only JSON. Pick relevant specialists "
        "from context.allowed_specialists. Never calculate money, invent evidence, "
        "or decide verdicts."
    )
    output_schema = PLAN_SCHEMA
    max_tokens = 384


class EntityAgent(BoundedAgent):
    role = "entity"
    instructions = (
        "Inspect verified candidate links only. Return JSON hypotheses; "
        "never invent IDs or evidence."
    )
    tool_allowlist = frozenset({"get_order", "get_customer_history"})


class ShipmentAgent(BoundedAgent):
    role = "shipment"
    instructions = (
        "Inspect shipment facts and actor attribution. Return JSON hypotheses, refs, uncertainties."
    )
    tool_allowlist = frozenset({"get_shipment_summary", "get_order_items"})


class PaymentRefundAgent(BoundedAgent):
    role = "payment_refund"
    instructions = (
        "Inspect payment and refund events. Do not calculate money. "
        "Return JSON hypotheses and refs."
    )
    tool_allowlist = frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    )


class CustomerContextAgent(BoundedAgent):
    role = "customer_context"
    instructions = "Inspect customer history links. Return JSON hypotheses and uncertainties only."
    tool_allowlist = frozenset({"get_customer_history"})


class PolicyAgent(BoundedAgent):
    role = "policy"
    instructions = (
        "Identify relevant policy clauses. Do not calculate refunds or choose final status. "
        "Return JSON."
    )
    tool_allowlist = frozenset({"get_policy"})


class AdjudicatorAgent(BoundedAgent):
    role = "adjudicator"
    instructions = (
        "Propose an evidence-linked candidate verdict using only allowed labels and refs. "
        "Do not calculate money or build final output. Return JSON only."
    )
    output_schema = ADJUDICATION_SCHEMA
    max_tokens = 768
    timeout_seconds = 30.0


class CriticAgent(BoundedAgent):
    role = "critic"
    instructions = (
        "Independently find unsupported claims, contradictions, wrong responsibility, "
        "policy and evidence gaps. Return warnings only; do not edit facts. JSON only."
    )
    output_schema = CRITIC_SCHEMA
    max_tokens = 512


def make_task(
    case_id: str,
    role: str,
    claims: tuple[tuple[str, str], ...],
    facts: tuple[DerivedFact, ...],
    refs: tuple[str, ...],
    context: dict[str, Any],
    correlation_id: str,
) -> AgentTask:
    return AgentTask(
        case_id,
        f"task_{secrets.token_urlsafe(12)}",
        correlation_id,
        role,
        claims,
        facts,
        refs,
        context,
    )
