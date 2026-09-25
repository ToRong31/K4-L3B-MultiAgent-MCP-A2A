"""Case-scoped routing, fact assembly and bounded independent verification."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from ..contracts import Contracts
from ..core.a2a_transport import send_work
from ..core.agent_messages import Finding, WorkOrder
from ..core.evidence import EvidenceCollector
from ..core.investigation_contract import validate_fact, validate_work_input
from ..core.memory import AgentMemory
from ..trace import TraceWriter

Send = Callable[[str, WorkOrder], Awaitable[Finding]]

ARRAY_SECTIONS = {"claim_assessments", "data_conflicts", "resolution_actions"}
PAYMENT_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
}
REFUND_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "refund_pending",
    "refund_failed",
}
SHIPMENT_ISSUES = {"late_delivery_seller", "late_delivery_logistics"}
SECTIONS = {
    "assessment",
    "affected_entities",
    "claim_assessments",
    "entity_resolution",
    "customer_context",
    "shipment_analysis",
    "payment_analysis",
    "root_cause_analysis",
    "data_conflicts",
    "financial_resolution",
    "resolution_actions",
}
DEFAULTS: dict[str, Any] = {
    "assessment": {
        "primary_issue": "insufficient_evidence",
        "secondary_issues": [],
        "case_status": "needs_investigation",
        "confidence": 0.0,
    },
    "affected_entities": {
        "order_ids": [],
        "item_ids": [],
        "seller_ids": [],
        "payment_references": [],
        "shipment_ids": [],
    },
    "entity_resolution": {
        "status": "not_found",
        "resolved_order_ids": [],
        "rejected_candidates": [],
        "confidence": 0.0,
    },
    "customer_context": {"customer_unique_id": None, "related_order_ids": []},
    "shipment_analysis": {
        "verdict": "insufficient_evidence",
        "late_seller_ids": [],
        "timeline_complete": False,
    },
    "payment_analysis": {
        "verdict": "insufficient_evidence",
        "captured_total_brl": None,
        "refunded_total_brl": None,
        "refundable_total_brl": None,
    },
    "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
    "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []},
    "claim_assessments": [],
    "data_conflicts": [],
    "resolution_actions": [],
}


def assemble_output(
    case: dict[str, Any],
    context: dict[str, Any],
    findings: list[Finding],
    context_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Merge facts without treating absent optional arrays as missing evidence."""
    result = {"schema_version": "day09-l3b-output-v2", "case_id": case["case_id"]}
    provenance: dict[str, tuple[str, Any]] = {}
    sources: list[tuple[str, dict[str, Any], list[str]]] = [("entity", context, context_refs or [])]
    for finding in findings:
        for fact in finding.facts:
            if not isinstance(fact, dict) or fact.get("kind") not in SECTIONS:
                continue
            data = fact.get("data")
            if not isinstance(data, dict):
                continue
            value = data.get("items") if fact["kind"] in ARRAY_SECTIONS else data
            sources.append((finding.agent, {fact["kind"]: value}, fact.get("evidence_refs", [])))
    refs: set[str] = set(context_refs or [])
    for finding in findings:
        refs.update(finding.evidence_refs)
    conflicts: list[dict[str, Any]] = []
    conflicted_fields: set[str] = set()

    def merge_unique(section: str, existing: list[Any], incoming: list[Any]) -> list[Any]:
        def identity(item: Any) -> str:
            if section == "data_conflicts" and isinstance(item, dict):
                item = {**item, "sources": sorted(item.get("sources", []))}
            return json.dumps(item, sort_keys=True, ensure_ascii=False)

        values = list(existing)
        seen = {identity(item) for item in values}
        for item in incoming:
            key = identity(item)
            if key not in seen:
                values.append(item)
                seen.add(key)
        return values

    for actor, sections, section_refs in sources:
        refs.update(section_refs)
        for key, value in sections.items():
            if key not in SECTIONS:
                continue
            if key in conflicted_fields:
                continue
            if key in result and key in ARRAY_SECTIONS and isinstance(value, list):
                result[key] = merge_unique(key, result[key], value)
                continue
            if (
                key in result
                and key == "affected_entities"
                and isinstance(value, dict)
                and all(
                    isinstance(result[key].get(field), list) and isinstance(items, list)
                    for field, items in value.items()
                )
            ):
                result[key] = {
                    field: merge_unique(field, result[key].get(field, []), value.get(field, []))
                    for field in DEFAULTS["affected_entities"]
                }
                continue
            if key in provenance and provenance[key][1] != value:
                prior = provenance[key][0]
                conflicts.append(
                    {
                        "field": key,
                        "sources": [prior, actor],
                        "selected_source": None,
                        "resolution_code": "UNRESOLVED_SOURCE_CONFLICT",
                    }
                )
                result.pop(key, None)
                conflicted_fields.add(key)
                continue
            if key not in result:
                result[key] = value
                provenance[key] = (actor, value)

    provided = set(result)
    for key, value in DEFAULTS.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
    if conflicts:
        result["data_conflicts"] = merge_unique(
            "data_conflicts", result["data_conflicts"], conflicts
        )

    assessment = result["assessment"]
    issue = assessment["primary_issue"]
    missing_agents = {f.agent for f in findings if f.status != "completed"}
    dependent_missing = (
        ("policy" in missing_agents)
        or (
            issue in PAYMENT_ISSUES
            and ("payment" in missing_agents or "payment_analysis" not in provided)
        )
        or (
            issue in SHIPMENT_ISSUES
            and ("shipment" in missing_agents or "shipment_analysis" not in provided)
        )
        or (
            issue in {"canceled_order_paid", "unavailable_order_paid"}
            and ("order" in missing_agents or "affected_entities" not in provided)
        )
        or (
            context.get("entity_resolution", {}).get("status") != "resolved"
            and issue != "insufficient_evidence"
        )
        or ("assessment" in conflicted_fields)
    )
    if "financial_resolution" not in provided:
        secondary = list(assessment["secondary_issues"])
        if "refund_amount_unverified" not in secondary:
            secondary.append("refund_amount_unverified")
        assessment = {**assessment, "secondary_issues": secondary[:10]}
        result["assessment"] = assessment
        if issue in REFUND_ISSUES or assessment["case_status"] == "no_action":
            dependent_missing = True
    if assessment["case_status"] == "action_required" and "resolution_actions" not in provided:
        dependent_missing = True
    if (
        (issue in PAYMENT_ISSUES and "payment_analysis" in conflicted_fields)
        or (issue in SHIPMENT_ISSUES and "shipment_analysis" in conflicted_fields)
        or (issue in REFUND_ISSUES and "financial_resolution" in conflicted_fields)
    ):
        dependent_missing = True
    if dependent_missing and assessment["case_status"] != "needs_investigation":
        result["assessment"] = {
            **assessment,
            "case_status": "needs_investigation",
            "confidence": min(assessment["confidence"], 0.8),
        }
    result["evidence_refs"] = sorted(refs)
    return result


class Coordinator:
    def __init__(
        self,
        memory: AgentMemory,
        trace: TraceWriter,
        contracts: Contracts,
        evidence: EvidenceCollector,
        sender: Send | None = None,
        resolver: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        max_repair_rounds: int = 1,
    ) -> None:
        self.memory, self.trace, self.contracts, self.evidence = memory, trace, contracts, evidence
        self.sender, self.resolver = sender or send_work, resolver
        self.max_repair_rounds = max_repair_rounds

    async def _handoff(
        self, case_id: str, target: str, payload: dict[str, Any], refs: list[str]
    ) -> Finding:
        url = os.getenv(f"A2A_{target.upper()}_URL", "").strip()
        if not url and self.sender is send_work:
            raise ValueError(f"A2A_{target.upper()}_URL is required")
        work = WorkOrder(case_id, target, uuid4().hex, payload, sorted(set(refs)))
        validate_work_input(work.input)
        self.memory.append(case_id, "orchestrator", work.task_id, "a2a_request", work.to_dict())
        self.trace.emit(
            case_id=case_id, event_type="task_assigned", actor="orchestrator", target=target
        )
        result = await self.sender(url, work)
        if (result.case_id, result.agent, result.task_id) != (case_id, target, work.task_id):
            raise ValueError("A2A Finding has mismatched case, agent or task")
        recorded = {
            (
                event["payload"].get("agent"),
                event["payload"].get("tool_name"),
                event["payload"].get("evidence", {}).get("evidence_ref"),
            )
            for event in self.memory.raw_history(case_id, "orchestrator")
            if event["kind"] == "tool_result"
        }
        new_refs = set()
        for use in result.tool_uses:
            if (target, use["tool_name"], use["evidence_ref"]) not in recorded:
                raise ValueError("A2A Finding cites an unrecorded MCP tool result")
            new_refs.add(use["evidence_ref"])
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=target,
                tool_name=use["tool_name"],
                evidence_refs=[use["evidence_ref"]],
            )
        if not set(result.evidence_refs).issubset(set(refs) | new_refs):
            raise ValueError("A2A Finding cites evidence outside this handoff")
        for fact in result.facts:
            validate_fact(fact, set(result.evidence_refs))
        self.memory.append(case_id, "orchestrator", work.task_id, "a2a_response", result.to_dict())
        self.trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=target,
            target="orchestrator",
            evidence_refs=result.evidence_refs,
        )
        return result

    async def solve(self, case: dict[str, Any]) -> dict[str, Any]:
        case_id = case["case_id"]
        self.memory.append(case_id, "orchestrator", uuid4().hex, "case_input", case)
        resolver = self.resolver
        if resolver is None:
            from ..core.entity_resolution import resolve_case_context

            resolver = resolve_case_context
        resolution = await resolver(case, self.evidence, self.memory)
        context = resolution.get("context", {})
        snapshot = resolution.get("snapshot")
        if not isinstance(context, dict) or set(context) != {
            "entity_resolution",
            "customer_context",
        }:
            raise ValueError("entity resolver returned invalid context")
        self.contracts.validate_output(assemble_output(case, context, [], []), f"context/{case_id}")
        refs = list(resolution.get("evidence_refs", []))
        entity_results = {
            event["payload"].get("evidence", {}).get("evidence_ref"): event["payload"]
            for event in self.memory.raw_history(case_id, "orchestrator")
            if event["kind"] == "tool_result"
        }
        for ref in refs:
            if ref not in entity_results:
                raise ValueError("entity resolver cited unrecorded evidence")
            self.trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor="orchestrator",
                tool_name=entity_results[ref]["tool_name"],
                evidence_refs=[ref],
            )
        resolved_ids = context["entity_resolution"]["resolved_order_ids"]
        if context["entity_resolution"]["status"] == "resolved" and resolved_ids:
            first = await asyncio.gather(
                *(
                    self._handoff(
                        case_id, target,
                        {"case": case, "context": context, "snapshot": snapshot}, refs,
                    )
                    for target in ("order", "payment", "shipment")
                )
            )
        else:
            first = [
                Finding(
                    case_id,
                    target,
                    uuid4().hex,
                    "needs_evidence",
                    open_questions=["Entity resolution is not unique"],
                )
                for target in ("order", "payment", "shipment")
            ]
        for finding in first:
            for fact in finding.facts:
                if (
                    fact.get("kind")
                    in {"order_state", "payment_reconciliation", "shipment_timeline"}
                    and fact.get("data", {}).get("order_id") not in resolved_ids
                ):
                    raise ValueError("specialist fact is outside resolved order scope")
        findings = list(first)
        all_refs = sorted(set(refs).union(*(f.evidence_refs for f in findings)))
        policy = await self._handoff(
            case_id,
            "policy",
            {
                "case": case, "context": context, "snapshot": snapshot,
                "findings": [f.to_dict() for f in findings],
            },
            all_refs,
        )
        findings.append(policy)
        if policy.status == "completed" and any(
            fact.get("kind") in {"assessment", "financial_resolution", "resolution_actions"}
            for fact in policy.facts
        ):
            self.trace.emit(
                case_id=case_id,
                event_type="policy_decided",
                actor="policy",
                decision_code="POLICY_APPLIED",
                evidence_refs=policy.evidence_refs,
            )
        for round_no in range(self.max_repair_rounds + 1):
            draft = assemble_output(case, context, findings, refs)
            verification = await self._handoff(
                case_id,
                "verifier",
                {
                    "case": case,
                    "context": context,
                    "snapshot": snapshot,
                    "draft_output": draft,
                    "findings": [f.to_dict() for f in findings],
                },
                draft["evidence_refs"],
            )
            fact = next((f for f in verification.facts if f.get("kind") == "verification"), {})
            verdict = fact.get("data", {})
            approved = verification.status == "completed" and verdict.get("approved") is True
            self.trace.emit(
                case_id=case_id,
                event_type="verification_completed",
                actor="verifier",
                decision_code="APPROVED" if approved else "REJECTED",
                evidence_refs=verification.evidence_refs,
            )
            if approved:
                self.contracts.validate_output(draft, f"final/{case_id}")
                return draft
            if round_no == self.max_repair_rounds:
                raise RuntimeError(
                    f"{case_id}: verifier rejected draft: {verdict.get('errors', [])}"
                )
            targets = {e.get("target") for e in verdict.get("errors", []) if isinstance(e, dict)}
            repairable = targets & {"order", "payment", "shipment", "policy"}
            if not repairable:
                raise RuntimeError(
                    f"{case_id}: verifier rejected coordinator draft: {verdict.get('errors', [])}"
                )
            for target in sorted(repairable):
                replacement = await self._handoff(
                    case_id,
                    target,
                    {
                        "case": case,
                        "context": context,
                        "snapshot": snapshot,
                        "findings": [f.to_dict() for f in findings],
                        "draft_output": draft,
                    },
                    draft["evidence_refs"],
                )
                findings = [replacement if f.agent == target else f for f in findings]
        raise AssertionError("unreachable")
