from __future__ import annotations

import json
from typing import Any

from ...core.agent_messages import EVIDENCE_REF, Finding, WorkOrder
from ..base import Specialist
from ..domain_helpers import fact, fetch, field, finding, money, records

ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}


def _parts(findings: Any) -> dict[str, list[tuple[dict, list[str]]]]:
    output: dict[str, list[tuple[dict, list[str]]]] = {}
    if not isinstance(findings, list):
        return output
    for finding_data in findings:
        if not isinstance(finding_data, dict) or finding_data.get("status") != "completed":
            continue
        for item in finding_data.get("facts", []):
            if isinstance(item, dict) and isinstance(item.get("data"), dict):
                output.setdefault(str(item.get("kind")), []).append(
                    (item["data"], item.get("evidence_refs", []))
                )
    return output


def _public_conflict(item: dict[str, Any], policy: Any) -> dict[str, Any] | None:
    """Only map explicit, source-backed conflict metadata into the public schema."""
    field_name = item.get("field")
    sources = item.get("sources")
    if not isinstance(field_name, str) or not 1 <= len(field_name) <= 100:
        return None
    if (
        not isinstance(sources, list)
        or not 2 <= len(sources) <= 5
        or any(not isinstance(source, str) or not 1 <= len(source) <= 80 for source in sources)
        or len(set(sources)) != len(sources)
    ):
        return None
    rule = None
    if isinstance(policy, dict):
        resolutions = policy.get("conflict_resolutions")
        if isinstance(resolutions, dict):
            rule = resolutions.get(field_name)
    if not isinstance(rule, dict):
        rule = {}
    code = rule.get("resolution_code", item.get("resolution_code"))
    selected = rule.get("selected_source", item.get("selected_source"))
    if not isinstance(code, str) or not 1 <= len(code) <= 80:
        return None
    if selected is not None and selected not in sources:
        return None
    return {
        "field": field_name,
        "sources": sources,
        "selected_source": selected,
        "resolution_code": code,
    }


def analyze_policy(
    policy: Any, findings: Any, policy_ref: str, case: dict[str, Any] | None = None
) -> tuple[list[dict], list[str]]:
    parts = _parts(findings)
    issues: set[str] = set()
    for data, _ in parts.get("payment_analysis", []):
        issues.update({"duplicate_charge"} if data.get("verdict") == "duplicate_capture" else set())
        issues.update({"payment_mismatch"} if data.get("verdict") == "capture_mismatch" else set())
        if data.get("verdict") in {"refund_pending", "refund_failed"}:
            issues.add(data["verdict"])
        if data.get("verdict") == "reconciled" and any(
            len(detail.get("payment_references", [])) > 1
            for detail, _ in parts.get("payment_reconciliation", [])
        ):
            issues.add("valid_split_payment")
    for data, _ in parts.get("shipment_analysis", []):
        if data.get("verdict") in {"seller_delay", "logistics_delay"}:
            issues.add("late_delivery_" + data["verdict"].split("_")[0])
    for data, _ in parts.get("order_state", []):
        if data.get("order_status") in {"canceled", "unavailable"} and any(
            p.get("captured_total_brl") for p, _ in parts.get("payment_analysis", [])
        ):
            issues.add(data["order_status"] + "_order_paid")
    if not issues and isinstance(findings, list):
        specialist_findings = {
            item.get("agent"): item for item in findings if isinstance(item, dict)
        }
        if all(
            specialist_findings.get(agent, {}).get("status") == "completed"
            for agent in ("order", "payment", "shipment")
        ) and any(
            data.get("verdict") == "reconciled" for data, _ in parts.get("payment_analysis", [])
        ):
            issues.add("unsupported_claim")
    raw_rules = policy.get("rules") if isinstance(policy, dict) else None
    if isinstance(raw_rules, dict):
        rules = [
            {"issue": key, **value}
            for key, value in raw_rules.items()
            if key in ISSUES and isinstance(value, dict)
        ]
    else:
        rules = [
            r
            for r in records(policy, "rules", "policies")
            if field(r, "issue", "primary_issue") in ISSUES
        ]
    applicable = [r for r in rules if field(r, "issue", "primary_issue") in issues]
    facts: list[dict] = []
    questions = []
    conflicts = []
    conflict_refs = []
    public_conflicts = []
    for kind in ("source_conflicts", "data_conflicts"):
        for data, refs in parts.get(kind, []):
            for item in data.get("items", []):
                if not isinstance(item, dict):
                    continue
                conflicts.append(item)
                conflict_refs.extend(refs)
                public = _public_conflict(item, policy)
                if (
                    public is not None
                    and public not in public_conflicts
                    and any(isinstance(ref, str) and EVIDENCE_REF.fullmatch(ref) for ref in refs)
                ):
                    public_conflicts.append(public)
    for data, refs in parts.get("order_state", []):
        if data.get("conflicting_item_ids") or data.get("conflicting_order_ids"):
            conflicts.append(
                {
                    "field": "order_items",
                    "order_id": data.get("order_id"),
                    "item_ids": data.get("conflicting_item_ids", []),
                }
            )
            conflict_refs.extend(refs)
    for data, refs in parts.get("shipment_analysis", []):
        if data.get("verdict") == "conflicting":
            conflicts.append({"field": "shipment_timeline"})
            conflict_refs.extend(refs)
    if conflicts:
        facts.append(fact("source_conflicts", {"items": conflicts}, sorted(set(conflict_refs))))
        if public_conflicts:
            facts.append(
                fact(
                    "data_conflicts",
                    {"items": public_conflicts[:5]},
                    sorted(set([policy_ref, *conflict_refs])),
                )
            )
        facts.append(
            fact(
                "assessment",
                {
                    "primary_issue": "insufficient_evidence",
                    "secondary_issues": sorted(issues)[:10],
                    "case_status": "needs_investigation",
                    "confidence": 0.2,
                },
                sorted(set([policy_ref, *conflict_refs])),
            )
        )
        questions.append("Conflict requires issue recomputation or lacks complete source metadata")
        # Do not emit a second, contradictory assessment from a matching rule.
        return facts, questions
    if not applicable:
        assessment = {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.2,
        }
        facts.append(fact("assessment", assessment, [policy_ref]))
        questions.append("No explicit policy rule matches supported findings")
        return facts, questions
    # Only explicit policy priority can break competing issue ties.
    claim_topics = {
        claim.get("topic")
        for claim in (case or {}).get("customer_request", {}).get("claims", [])
        if isinstance(claim, dict)
    }
    claimed_matches = [
        rule for rule in applicable if field(rule, "issue", "primary_issue") in claim_topics
    ]
    if len(claimed_matches) == 1:
        applicable = claimed_matches
    priorities = [r.get("priority") for r in applicable]
    if len(applicable) > 1 and (
        any(not isinstance(p, int) for p in priorities) or len(set(priorities)) != len(priorities)
    ):
        facts.append(
            fact(
                "assessment",
                {
                    "primary_issue": "insufficient_evidence",
                    "secondary_issues": sorted(issues)[:10],
                    "case_status": "needs_investigation",
                    "confidence": 0.2,
                },
                [policy_ref],
            )
        )
        questions.append("Multiple supported issues have no unambiguous policy priority")
        return facts, questions
    rule = min(applicable, key=lambda r: r.get("priority", 0))
    issue = field(rule, "issue", "primary_issue")
    supporting = (
        parts.get("payment_analysis", [])
        + parts.get("shipment_analysis", [])
        + parts.get("order_state", [])
    )
    source_refs = [ref for _, refs in supporting for ref in refs]
    refs = sorted(set([policy_ref, *source_refs]))
    case_status = rule.get("case_status")
    if case_status not in {"action_required", "no_action", "needs_investigation"}:
        questions.append("Policy rule has no valid case_status")
    else:
        confidence = 0.8 if case_status == "needs_investigation" else 0.9
        facts.append(
            fact(
                "assessment",
                {
                    "primary_issue": issue,
                    "secondary_issues": sorted(issues - {issue})[:10],
                    "case_status": case_status,
                    "confidence": confidence,
                },
                refs,
            )
        )
    cause = rule.get("cause_code")
    parties = rule.get("responsible_parties")
    if isinstance(parties, list):
        parties = [dict(party) for party in parties if isinstance(party, dict)]
        if issue == "late_delivery_seller":
            grounded = [
                seller_id
                for data, _ in parts.get("shipment_analysis", [])
                for seller_id in data.get("late_seller_ids", [])
                if isinstance(seller_id, str)
            ]
            if grounded:
                for party in parties:
                    if party.get("party_type") == "seller":
                        party["party_id"] = grounded[0]
        if issue == "unavailable_order_paid":
            grounded = [
                seller_id
                for data, _ in parts.get("affected_entities", [])
                for seller_id in data.get("seller_ids", [])
                if isinstance(seller_id, str)
            ]
            if grounded:
                for party in parties:
                    if party.get("party_type") == "seller":
                        party["party_id"] = grounded[0]
        facts.append(
            fact(
                "root_cause_analysis",
                {
                    "ranked_causes": (
                        [{"cause_code": cause, "rank": 1}] if isinstance(cause, str) else []
                    ),
                    "responsible_parties": parties,
                },
                refs,
            )
        )
        if not isinstance(cause, str):
            questions.append("Policy rule lacks cause code")
    else:
        questions.append("Policy rule lacks responsible parties")
    actions = rule.get("actions")
    if actions is None and isinstance(rule.get("recommended_action"), str):
        actions = [rule["recommended_action"]]
    if isinstance(actions, list) and all(isinstance(a, str) for a in actions):
        facts.append(fact("resolution_actions", {"items": actions[:8]}, refs))
    else:
        questions.append("Policy rule lacks action codes")
    refund = field(rule, "recommended_refund_brl", "refund_brl")
    amount = money(refund)
    if amount is not None:
        lines = rule.get("refund_lines", [])
        if isinstance(lines, list):
            facts.append(
                fact(
                    "financial_resolution",
                    {
                        "currency": "BRL",
                        "recommended_refund_brl": float(amount),
                        "refund_lines": lines[:10],
                    },
                    refs,
                )
            )
    else:
        questions.append("Policy rule lacks an explicit refund amount or rule")
    claim_items = []
    payment_total = next(
        (
            money(data.get("captured_total_brl"))
            for data, _ in parts.get("payment_analysis", [])
            if money(data.get("captured_total_brl")) is not None
        ),
        None,
    )
    for claim in (case or {}).get("customer_request", {}).get("claims", []):
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            if case_status == "needs_investigation" or amount is None or payment_total is None:
                verdict = "insufficient_evidence"
            elif amount == 0:
                verdict = "unsupported"
            elif amount >= payment_total:
                verdict = "supported"
            else:
                verdict = "partially_supported"
        elif issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        else:
            verdict = "supported" if topic == issue else "unsupported"
        claim_items.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": 0.8 if verdict == "insufficient_evidence" else 0.9,
                "evidence_refs": refs,
            }
        )
    if claim_items:
        facts.append(fact("claim_assessments", {"items": claim_items}, refs))
    return facts, questions


class PolicyAgent(Specialist):
    name = "policy"
    description = "Apply the requested policy version to grounded specialist findings."

    async def investigate(self, work: WorkOrder) -> Finding:
        version = (work.input.get("case") or {}).get("policy_version")
        if not isinstance(version, str) or not version:
            return finding(work, self.name, "needs_evidence", [], ["Policy version is missing"])
        try:
            response = await fetch(self, work, "get_policy", policy_version=version)
            facts, questions = analyze_policy(
                response.get("data"), work.input.get("findings"), response["evidence_ref"],
                work.input.get("case"),
            )
            assessment = next((item for item in facts if item.get("kind") == "assessment"), None)
            if self.llm is not None and assessment is not None:
                model_response = await self.llm.complete_with_memory(
                    self.memory,
                    case_id=work.case_id,
                    agent_id=self.name,
                    turn_id=work.task_id,
                    system_prompt=(
                        'Review the evidence-backed assessment. Return only JSON in the form '
                        '{"confidence": 0.0}. Confidence must be between 0 and 1. '
                        'Assess the existing primary_issue and case_status; do not invent evidence.'
                    ),
                    user_prompt=json.dumps(
                        {
                            "assessment": assessment["data"],
                            "customer_request": (work.input.get("case") or {}).get("customer_request"),
                            "findings": work.input.get("findings"),
                        },
                        ensure_ascii=False,
                    ),
                    max_tokens=80,
                )
                start, end = model_response.find("{"), model_response.rfind("}")
                if start < 0 or end < start:
                    raise ValueError("model response did not contain a JSON object")
                model_confidence = json.loads(model_response[start : end + 1])["confidence"]
                if not isinstance(model_confidence, (int, float)) or not 0 <= model_confidence <= 1:
                    raise ValueError("model confidence must be between 0 and 1")
                original = assessment["data"]["confidence"]
                revised = (original + model_confidence) / 2
                if assessment["data"]["case_status"] == "needs_investigation":
                    revised = min(revised, 0.8)
                assessment["data"]["confidence"] = round(revised, 2)
                self.memory.append(
                    work.case_id, self.name, work.task_id, "llm_review",
                    {"provider": self.llm.provider_name, "model": self.llm.model},
                )
            return finding(work, self.name, "completed", facts, questions)
        except Exception as exc:
            return finding(work, self.name, "failed", [], [f"Policy MCP error: {exc}"])
