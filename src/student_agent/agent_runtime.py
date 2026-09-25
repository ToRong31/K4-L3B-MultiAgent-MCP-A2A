from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from .agents import (
    AdjudicatorAgent,
    CriticAgent,
    CustomerContextAgent,
    EntityAgent,
    ModelWorker,
    PaymentRefundAgent,
    PolicyAgent,
    ShipmentAgent,
    SupervisorAgent,
    make_task,
)
from .evidence import EvidenceLedger
from .trace import TraceWriter

ALL_ROLES = frozenset({"entity", "shipment", "payment_refund", "customer_context", "policy"})
PAYMENT_TOPICS = frozenset(
    {
        "payment_mismatch",
        "duplicate_charge",
        "valid_split_payment",
        "refund_pending",
        "refund_failed",
        "canceled_order_paid",
        "unavailable_order_paid",
    }
)
SHIPMENT_TOPICS = frozenset({"late_delivery_seller", "late_delivery_logistics"})


def deterministic_route(case: dict[str, Any]) -> frozenset[str]:
    topics = {
        str(claim.get("topic", "")) for claim in case.get("customer_request", {}).get("claims", ())
    }
    roles = {"entity", "policy"}
    if topics & SHIPMENT_TOPICS:
        roles.add("shipment")
    if topics & PAYMENT_TOPICS or "requested_full_refund" in topics:
        roles.add("payment_refund")
    if case.get("investigation_scope", {}).get("include_customer_history"):
        roles.add("customer_context")
    return frozenset(roles)


@dataclass(frozen=True)
class AgentReview:
    candidate: dict[str, Any] | None
    warnings: tuple[str, ...]
    fallback_roles: tuple[str, ...]


class AgentRuntime:
    """Logical hierarchy; every role shares one serial model worker."""

    def __init__(self, worker: ModelWorker, trace: TraceWriter) -> None:
        self.worker = worker
        self.trace = trace
        self.agents = {
            "supervisor": SupervisorAgent(worker, trace),
            "entity": EntityAgent(worker, trace),
            "shipment": ShipmentAgent(worker, trace),
            "payment_refund": PaymentRefundAgent(worker, trace),
            "customer_context": CustomerContextAgent(worker, trace),
            "policy": PolicyAgent(worker, trace),
            "adjudicator": AdjudicatorAgent(worker, trace),
            "critic": CriticAgent(worker, trace),
        }

    async def plan(self, case: dict[str, Any]) -> frozenset[str]:
        baseline = deterministic_route(case)
        topics = tuple(
            (str(c["claim_id"]), str(c["topic"]))
            for c in case.get("customer_request", {}).get("claims", ())
        )
        if len(topics) <= 2 and all(topic for _, topic in topics):
            return baseline
        correlation = f"corr_{secrets.token_urlsafe(10)}"
        task = make_task(
            case["case_id"],
            "supervisor",
            topics,
            (),
            (),
            {"allowed_specialists": sorted(ALL_ROLES)},
            correlation,
        )
        result = await self.agents["supervisor"].run(task)
        if result.value is None:
            return baseline
        selected = result.value["specialists"]
        if not set(selected).issubset(ALL_ROLES):
            return baseline
        # The model may add relevant work but cannot remove mandatory deterministic routes.
        return baseline | frozenset(selected)

    async def review(
        self,
        case: dict[str, Any],
        ledger: EvidenceLedger,
        roles: frozenset[str],
        deterministic: dict[str, Any],
        domain_context: dict[str, dict[str, Any]],
    ) -> AgentReview:
        correlation = f"corr_{secrets.token_urlsafe(10)}"
        claims = tuple(
            (str(c["claim_id"]), str(c["topic"]))
            for c in case.get("customer_request", {}).get("claims", ())
        )
        refs = tuple(ledger.consumed_refs)
        specialist_results: dict[str, dict[str, Any]] = {}
        fallbacks: list[str] = []
        for role in sorted(roles):
            context = domain_context.get(role, {})
            if not context:
                continue
            task = make_task(
                case["case_id"], role, claims, ledger.facts, refs, context, correlation
            )
            result = await self.agents[role].run(task, ledger)
            if result.value is None:
                fallbacks.append(role)
            else:
                specialist_results[role] = result.value

        allowed_issues = sorted(
            {c[1] for c in claims if c[1] != "requested_full_refund"}
            | {deterministic["assessment"]["primary_issue"]}
        )
        task = make_task(
            case["case_id"],
            "adjudicator",
            claims,
            ledger.facts,
            refs,
            {
                "specialists": specialist_results,
                "deterministic_summary": {
                    "primary_issue": deterministic["assessment"]["primary_issue"],
                    "shipment_verdict": deterministic["shipment_analysis"]["verdict"],
                    "payment_verdict": deterministic["payment_analysis"]["verdict"],
                    "policy_status": deterministic["assessment"]["case_status"],
                },
                "allowed_issues": allowed_issues,
            },
            correlation,
        )
        adjudication = await self.agents["adjudicator"].run(task, ledger)
        candidate = adjudication.value
        if candidate is None:
            fallbacks.append("adjudicator")
        elif candidate["primary_issue"] not in allowed_issues:
            candidate = None
            fallbacks.append("adjudicator_invalid_issue")
        elif not set(candidate["secondary_issues"]).issubset(allowed_issues) or not set(
            candidate["claim_verdicts"]
        ).issubset({claim_id for claim_id, _ in claims}):
            candidate = None
            fallbacks.append("adjudicator_invalid_labels")
        critic_task = make_task(
            case["case_id"],
            "critic",
            claims,
            ledger.facts,
            refs,
            {
                "candidate": candidate,
                "deterministic_primary": deterministic["assessment"]["primary_issue"],
            },
            correlation,
        )
        critique = await self.agents["critic"].run(critic_task, ledger)
        if critique.value is None:
            fallbacks.append("critic")
            warnings = ("critic_unavailable",)
        else:
            warnings = tuple(critique.value["warnings"])
            if critique.value["request_reverification"]:
                warnings += ("deterministic_reverification_requested",)
        return AgentReview(candidate, warnings, tuple(fallbacks))
