"""Deterministic L3B verification using case-scoped stored MCP responses."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ...contracts import ContractError, Contracts
from ...core.agent_messages import Finding, WorkOrder
from ..base import Specialist


def nested_refs(value: Any) -> set[str]:
    if isinstance(value, list):
        return set().union(*(nested_refs(item) for item in value)) if value else set()
    if not isinstance(value, dict):
        return set()
    refs = set()
    for key, child in value.items():
        if key == "evidence_refs" and isinstance(child, list):
            refs.update(ref for ref in child if isinstance(ref, str))
        else:
            refs.update(nested_refs(child))
    return refs


def money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


class VerifierAgent(Specialist):
    name = "verifier"
    description = "Check schema, evidence provenance, scope and consistency."

    def __init__(self, memory, llm=None, evidence=None, contracts=None):
        super().__init__(memory, llm, evidence)
        self.contracts = contracts or Contracts(
            Path(__file__).resolve().parents[4] / "contracts" / "schemas"
        )

    async def investigate(self, work: WorkOrder) -> Finding:
        draft = work.input.get("draft_output")
        case = work.input.get("case", {})
        context = work.input.get("context", {})
        findings = work.input.get("findings", [])
        errors: list[dict[str, str]] = []

        def fail(code: str, target: str, message: str) -> None:
            errors.append({"code": code, "message": message, "target": target})

        if not isinstance(draft, dict):
            fail("SCHEMA", "coordinator", "draft_output must be an object")
            draft = {}
        try:
            self.contracts.validate_output(draft, "draft")
        except ContractError as exc:
            fail("SCHEMA", "coordinator", str(exc))
        if (
            draft.get("case_id") != work.case_id
            or not isinstance(case, dict)
            or case.get("case_id") != work.case_id
        ):
            fail("CASE_SCOPE", "coordinator", "case_id differs from WorkOrder")
        if isinstance(context, dict):
            for name in ("entity_resolution", "customer_context"):
                if name in context and draft.get(name) != context[name]:
                    fail("CONTEXT_MISMATCH", "coordinator", name)

        # A regex or a Finding alone never establishes provenance.
        stored: dict[str, set[str]] = {}
        for event in self.memory.raw_history(work.case_id, "orchestrator"):
            if event["kind"] != "tool_result":
                continue
            payload = event["payload"]
            envelope = payload.get("evidence", {})
            ref = envelope.get("evidence_ref") if isinstance(envelope, dict) else None
            if isinstance(ref, str):
                stored.setdefault(ref, set()).add(str(payload.get("agent", "orchestrator")))
        cited = nested_refs(draft)
        for ref in sorted(cited | set(work.evidence_refs)):
            if ref not in stored:
                fail("UNRECORDED_EVIDENCE", "coordinator", ref)
        if cited - set(draft.get("evidence_refs", [])):
            fail("EVIDENCE_LINKAGE", "coordinator", "nested refs missing from top-level list")
        if isinstance(findings, list):
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                agent = str(finding.get("agent", "coordinator"))
                if finding.get("case_id") != work.case_id:
                    fail("FINDING_SCOPE", "coordinator", agent)
                for ref in nested_refs(finding.get("facts", [])):
                    if ref not in finding.get("evidence_refs", []):
                        fail("FINDING_LINKAGE", agent, ref)
                    if ref in stored and agent not in stored[ref] and ref not in work.evidence_refs:
                        fail("EVIDENCE_OWNER", agent, ref)

        entity = draft.get("entity_resolution", {})
        affected = draft.get("affected_entities", {})
        if isinstance(entity, dict) and isinstance(affected, dict):
            resolved = set(entity.get("resolved_order_ids", []))
            rejected = set(entity.get("rejected_candidates", []))
            if resolved & rejected or rejected & set(affected.get("order_ids", [])):
                fail("ENTITY_CONFLICT", "coordinator", "rejected order is resolved or affected")
            if entity.get("status") != "resolved" and resolved:
                fail("ENTITY_STATUS", "coordinator", "unresolved entity has resolved IDs")
            if not set(affected.get("order_ids", [])).issubset(resolved):
                fail("ENTITY_SCOPE", "order", "affected order is outside resolved scope")

        payment = draft.get("payment_analysis", {})
        financial = draft.get("financial_resolution", {})
        if not isinstance(financial, dict):
            financial = {}
        if isinstance(payment, dict) and isinstance(financial, dict):
            recommended = money(financial.get("recommended_refund_brl"))
            lines = financial.get("refund_lines", [])
            amounts = (
                [money(line.get("amount_brl")) for line in lines if isinstance(line, dict)]
                if isinstance(lines, list)
                else []
            )
            if (
                recommended is not None
                and amounts
                and all(x is not None for x in amounts)
                and recommended != sum(amounts, Decimal(0))
            ):
                fail("REFUND_SUM", "policy", "recommended refund differs from refund lines")
            refundable = money(payment.get("refundable_total_brl"))
            if recommended is not None and refundable is not None and recommended > refundable:
                fail("REFUND_LIMIT", "policy", "recommended exceeds refundable total")
            captured = money(payment.get("captured_total_brl"))
            refunded = money(payment.get("refunded_total_brl"))
            if captured is not None and refunded is not None and refunded > captured:
                fail("PAYMENT_TOTAL", "payment", "refunded exceeds captured")

        assessment = draft.get("assessment", {})
        shipment = draft.get("shipment_analysis", {})
        causes = draft.get("root_cause_analysis", {})
        if isinstance(assessment, dict):
            issue = assessment.get("primary_issue")
            status = assessment.get("case_status")
            if status == "action_required" and not draft.get("evidence_refs"):
                fail("UNSUPPORTED_ACTION", "policy", "action requires recorded evidence")
            no_action_steps = draft.get("resolution_actions", [])
            if status == "no_action" and (
                any(step != "document_no_action" for step in no_action_steps)
                or financial.get("recommended_refund_brl", 0)
            ):
                fail("ACTION_STATUS", "policy", "no_action conflicts with actions or refund")
            if issue == "insufficient_evidence" and status != "needs_investigation":
                fail("ISSUE_STATUS", "policy", "insufficient evidence requires investigation")
            expected_shipment = {
                "late_delivery_seller": "seller_delay",
                "late_delivery_logistics": "logistics_delay",
            }.get(issue)
            if (
                expected_shipment
                and isinstance(shipment, dict)
                and shipment.get("verdict") != expected_shipment
            ):
                fail("SHIPMENT_ISSUE", "shipment", "verdict conflicts with issue")
            if expected_shipment and isinstance(causes, dict):
                expected_party = (
                    "seller" if expected_shipment == "seller_delay" else "logistics_provider"
                )
                parties = causes.get("responsible_parties", [])
                if parties and not any(
                    isinstance(party, dict) and party.get("party_type") == expected_party
                    for party in parties
                ):
                    fail("RESPONSIBLE_PARTY", "policy", "party conflicts with shipment cause")
            expected_payment = {
                "duplicate_charge": "duplicate_capture",
                "payment_mismatch": "capture_mismatch",
                "refund_pending": "refund_pending",
                "refund_failed": "refund_failed",
            }.get(issue)
            if (
                expected_payment
                and isinstance(payment, dict)
                and payment.get("verdict") != expected_payment
            ):
                fail("PAYMENT_ISSUE", "payment", "verdict conflicts with issue")
            if (
                status == "needs_investigation"
                and isinstance(assessment.get("confidence"), (int, float))
                and assessment["confidence"] > 0.8
            ):
                fail("CONFIDENCE", "policy", "investigation status has excessive confidence")
        if (
            isinstance(shipment, dict)
            and shipment.get("verdict") == "insufficient_evidence"
            and shipment.get("timeline_complete")
        ):
            fail("TIMELINE", "shipment", "insufficient timeline marked complete")

        return Finding(
            work.case_id,
            self.name,
            work.task_id,
            "completed",
            facts=[
                {
                    "kind": "verification",
                    "data": {"approved": not errors, "errors": errors},
                    "evidence_refs": [],
                }
            ],
        )
