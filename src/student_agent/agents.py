"""Specialist agents for the L3B multi-agent dispute investigation pipeline."""

from __future__ import annotations

import logging
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)


def _unique(items: list[str], limit: int = 20) -> list[str]:
    """Return unique items preserving order, capped at limit."""
    return list(dict.fromkeys(items))[:limit]


class EvidenceCache:
    """Per-case cache to avoid duplicate MCP calls and optimize efficiency score."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self._refs: list[str] = []

    def _key(self, tool_name: str, **kwargs: str) -> str:
        parts = [tool_name] + [f"{k}={v}" for k, v in sorted(kwargs.items())]
        return "|".join(parts)

    async def call(
        self,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        tool_name: str,
        case_id: str,
        actor: str,
        **kwargs: str,
    ) -> dict[str, Any]:
        cache_key = self._key(tool_name, case_id=case_id, **kwargs)
        if cache_key in self._store:
            return self._store[cache_key]

        evidence = await gateway.call(tool_name, case_id=case_id, **kwargs)
        self._store[cache_key] = evidence
        ref = evidence.get("evidence_ref", "")
        if ref:
            self._refs.append(ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[ref],
            )
        return evidence

    @property
    def all_refs(self) -> list[str]:
        return list(dict.fromkeys(self._refs))


async def run_entity_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    cache: EvidenceCache,
) -> dict[str, Any]:
    """Resolve candidate order IDs and establish customer context."""
    case_id = case["case_id"]
    actor = "entity-agent"

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
    )

    candidates = case.get("candidate_order_ids", [])
    claimed_id = case.get("customer_request", {}).get("claimed_order_id", "")
    customer_hint = case.get("customer_unique_id_hint", "")

    resolved_ids: list[str] = []
    rejected_ids: list[str] = []
    customer_unique_id: str | None = None
    related_order_ids: list[str] = []

    # Try each candidate
    for cid in candidates:
        if cid.startswith("candidate-"):
            rejected_ids.append(cid)
            continue
        try:
            ev = await cache.call(gateway, trace, "get_order", case_id=case_id, actor=actor, order_id=cid)
            if ev.get("data"):
                resolved_ids.append(cid)
            else:
                rejected_ids.append(cid)
        except Exception:
            logger.warning("get_order failed for candidate %s in %s", cid, case_id)
            rejected_ids.append(cid)

    # Fallback: if no resolved, use claimed_order_id
    if not resolved_ids and claimed_id and claimed_id not in rejected_ids:
        try:
            ev = await cache.call(gateway, trace, "get_order", case_id=case_id, actor=actor, order_id=claimed_id)
            if ev.get("data"):
                resolved_ids.append(claimed_id)
        except Exception:
            pass

    # Get customer history
    if customer_hint:
        try:
            ev = await cache.call(
                gateway, trace, "get_customer_history",
                case_id=case_id, actor=actor,
                customer_unique_id=customer_hint,
            )
            data = ev.get("data", {})
            customer_unique_id = customer_hint
            if isinstance(data, dict):
                related_order_ids = data.get("order_ids", [])
                if isinstance(related_order_ids, list):
                    related_order_ids = [oid for oid in related_order_ids if isinstance(oid, str)]
                else:
                    related_order_ids = []
            elif isinstance(data, list):
                related_order_ids = [
                    item.get("order_id", "") for item in data
                    if isinstance(item, dict) and item.get("order_id")
                ]
        except Exception:
            logger.warning("get_customer_history failed for %s", case_id)

    entity_status = "resolved" if resolved_ids else ("ambiguous" if candidates else "not_found")
    er_confidence = 1.0 if len(resolved_ids) == 1 else (0.7 if resolved_ids else 0.3)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        attributes={"resolved_count": len(resolved_ids), "rejected_count": len(rejected_ids)},
    )

    return {
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": _unique(resolved_ids),
            "rejected_candidates": _unique(rejected_ids),
            "confidence": er_confidence,
        },
        "customer_context": {
            "customer_unique_id": customer_unique_id,
            "related_order_ids": _unique(related_order_ids),
        },
        "resolved_order_ids": _unique(resolved_ids),
    }


async def run_order_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    cache: EvidenceCache,
    resolved_order_ids: list[str],
) -> dict[str, Any]:
    """Collect order details, items, and sellers."""
    case_id = case["case_id"]
    actor = "order-agent"

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
    )

    order_ids: list[str] = []
    item_ids: list[str] = []
    seller_ids: list[str] = []
    payment_refs: list[str] = []
    shipment_ids: list[str] = []
    order_data_list: list[dict[str, Any]] = []

    for order_id in resolved_order_ids:
        # Get order items
        try:
            ev = await cache.call(
                gateway, trace, "get_order_items",
                case_id=case_id, actor=actor, order_id=order_id,
            )
            data = ev.get("data", {})
            items = data if isinstance(data, list) else (data.get("items", []) if isinstance(data, dict) else [])
            for item in items:
                if isinstance(item, dict):
                    if item.get("order_item_id"):
                        item_ids.append(str(item["order_item_id"]))
                    if item.get("seller_id") and str(item["seller_id"]) not in seller_ids:
                        seller_ids.append(str(item["seller_id"]))
                    if item.get("product_id"):
                        order_data_list.append(item)
        except Exception:
            logger.warning("get_order_items failed for %s", order_id)

        order_ids.append(order_id)

        # Get sellers info
        try:
            ev = await cache.call(
                gateway, trace, "get_sellers",
                case_id=case_id, actor=actor, order_id=order_id,
            )
            data = ev.get("data", {})
            sellers = data if isinstance(data, list) else (data.get("sellers", []) if isinstance(data, dict) else [])
            for s in sellers:
                if isinstance(s, dict) and s.get("seller_id") and str(s["seller_id"]) not in seller_ids:
                    seller_ids.append(str(s["seller_id"]))
        except Exception:
            pass

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
    )

    return {
        "affected_entities": {
            "order_ids": _unique(order_ids),
            "item_ids": _unique(item_ids),
            "seller_ids": _unique(seller_ids),
            "payment_references": _unique(payment_refs),
            "shipment_ids": _unique(shipment_ids),
        },
        "order_data": order_data_list,
    }


async def run_shipment_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    cache: EvidenceCache,
    resolved_order_ids: list[str],
    entities: dict[str, Any],
) -> dict[str, Any]:
    """Analyze shipment timeline and determine delivery verdict."""
    case_id = case["case_id"]
    actor = "shipment-agent"

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
    )

    verdict = "insufficient_evidence"
    late_seller_ids: list[str] = []
    timeline_complete = False
    shipment_ids: list[str] = []

    for order_id in resolved_order_ids:
        try:
            ev = await cache.call(
                gateway, trace, "get_shipment_summary",
                case_id=case_id, actor=actor, order_id=order_id,
            )
            data = ev.get("data", {})
            if isinstance(data, dict):
                status = data.get("delivery_status", data.get("status", ""))
                shipped_at = data.get("shipped_at", data.get("shipping_limit_date", ""))
                delivered_at = data.get("delivered_at", "")
                estimated_at = data.get("estimated_delivery_date", "")
                carrier_date = data.get("carrier_delivery_date", "")
                seller_ship_limit = data.get("shipping_limit_date", "")

                if data.get("shipment_id"):
                    shipment_ids.append(str(data["shipment_id"]))

                timeline_complete = bool(shipped_at and (delivered_at or estimated_at))

                if status in ("delivered", "shipped"):
                    if delivered_at and estimated_at and delivered_at > estimated_at:
                        # Late delivery - determine whose fault
                        if shipped_at and seller_ship_limit and shipped_at > seller_ship_limit:
                            verdict = "seller_delay"
                            late_seller_ids = entities.get("seller_ids", [])[:20]
                        else:
                            verdict = "logistics_delay"
                    else:
                        verdict = "on_time"
                elif status == "canceled":
                    verdict = "returned"
                elif status in ("lost", "unavailable"):
                    verdict = "lost"
                else:
                    # Try to infer from dates
                    if delivered_at and estimated_at:
                        verdict = "logistics_delay" if delivered_at > estimated_at else "on_time"
                    elif shipped_at:
                        verdict = "on_time"
            elif isinstance(data, list):
                for shipment in data:
                    if isinstance(shipment, dict):
                        if shipment.get("shipment_id"):
                            shipment_ids.append(str(shipment["shipment_id"]))
                        timeline_complete = True
        except Exception:
            logger.warning("get_shipment_summary failed for %s", order_id)

    # Update entities with shipment_ids
    if shipment_ids:
        entities["shipment_ids"] = _unique(shipment_ids)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
    )

    return {
        "shipment_analysis": {
            "verdict": verdict,
            "late_seller_ids": late_seller_ids,
            "timeline_complete": timeline_complete,
        },
    }


async def run_payment_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    cache: EvidenceCache,
    resolved_order_ids: list[str],
    entities: dict[str, Any],
) -> dict[str, Any]:
    """Analyze payments, refunds, and reconcile financial data."""
    case_id = case["case_id"]
    actor = "payment-agent"

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
    )

    captured_total: float = 0.0
    refunded_total: float = 0.0
    refundable_total: float = 0.0
    verdict = "insufficient_evidence"
    payment_refs: list[str] = []

    for order_id in resolved_order_ids:
        # Get payments
        try:
            ev = await cache.call(
                gateway, trace, "get_order_payments",
                case_id=case_id, actor=actor, order_id=order_id,
            )
            data = ev.get("data", {})
            payments = data if isinstance(data, list) else (data.get("payments", []) if isinstance(data, dict) else [])
            for p in payments:
                if isinstance(p, dict):
                    val = p.get("payment_value", p.get("amount", 0))
                    if isinstance(val, (int, float)):
                        captured_total += val
                    ref_id = p.get("payment_id", p.get("payment_sequential", ""))
                    if ref_id:
                        payment_refs.append(str(ref_id))
        except Exception:
            logger.warning("get_order_payments failed for %s", order_id)

        # Get payment timeline
        try:
            ev = await cache.call(
                gateway, trace, "get_payment_timeline",
                case_id=case_id, actor=actor, order_id=order_id,
            )
        except Exception:
            pass

        # Get refund timeline
        try:
            ev = await cache.call(
                gateway, trace, "get_refund_timeline",
                case_id=case_id, actor=actor, order_id=order_id,
            )
            data = ev.get("data", {})
            refunds = data if isinstance(data, list) else (data.get("refunds", []) if isinstance(data, dict) else [])
            for r in refunds:
                if isinstance(r, dict):
                    val = r.get("refund_amount", r.get("amount", 0))
                    if isinstance(val, (int, float)):
                        refunded_total += val
        except Exception:
            pass

    # Determine refundable
    refundable_total = max(0.0, captured_total - refunded_total)

    # Determine verdict
    if captured_total > 0:
        if refunded_total > 0 and refunded_total >= captured_total:
            verdict = "refunded"
        elif refunded_total > 0:
            verdict = "refund_pending"
        else:
            verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"

    # Update entities
    if payment_refs:
        entities["payment_references"] = _unique(payment_refs)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
    )

    return {
        "payment_analysis": {
            "verdict": verdict,
            "captured_total_brl": round(captured_total, 2),
            "refunded_total_brl": round(refunded_total, 2),
            "refundable_total_brl": round(refundable_total, 2),
        },
    }


async def run_policy_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    cache: EvidenceCache,
    llm: Any,
    entity_result: dict[str, Any],
    shipment_result: dict[str, Any],
    payment_result: dict[str, Any],
    entities: dict[str, Any],
) -> dict[str, Any]:
    """Apply policy rules to determine primary issue, responsible parties, and financial resolution."""
    case_id = case["case_id"]
    actor = "policy-agent"

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
    )

    # Get policy
    policy_data = {}
    try:
        ev = await cache.call(
            gateway, trace, "get_policy",
            case_id=case_id, actor=actor,
            policy_version=case.get("policy_version", "EC_POLICY_V2"),
        )
        policy_data = ev.get("data", {})
    except Exception:
        logger.warning("get_policy failed for %s", case_id)

    # Build analysis context for LLM
    claims = case.get("customer_request", {}).get("claims", [])
    shipment = shipment_result.get("shipment_analysis", {})
    payment = payment_result.get("payment_analysis", {})
    entity_res = entity_result.get("entity_resolution", {})
    customer_msg = case.get("customer_request", {}).get("message", "")

    prompt = _build_policy_prompt(
        case_id=case_id,
        claims=claims,
        customer_message=customer_msg,
        shipment=shipment,
        payment=payment,
        entity_resolution=entity_res,
        policy_data=policy_data,
        entities=entities,
    )

    try:
        result = await llm.chat_json(
            messages=[
                {"role": "system", "content": POLICY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
    except Exception as exc:
        logger.warning("LLM call failed for policy agent: %s. Using rule-based fallback.", exc)
        result = _rule_based_policy(claims, shipment, payment, entity_res)

    # Normalize and validate the result
    assessment = _normalize_assessment(result, shipment, payment, entity_res)
    root_cause = _normalize_root_cause(result, assessment)
    financial = _normalize_financial(result, payment)
    actions = _normalize_actions(result, assessment)
    data_conflicts = _normalize_conflicts(result)
    claim_assessments = _normalize_claim_assessments(result, claims, cache)

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor=actor,
        decision_code=assessment.get("primary_issue", "unknown"),
        attributes={"confidence": assessment.get("confidence", 0.5)},
    )

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="verifier-agent",
    )

    return {
        "assessment": assessment,
        "root_cause_analysis": root_cause,
        "financial_resolution": financial,
        "resolution_actions": actions,
        "data_conflicts": data_conflicts,
        "claim_assessments": claim_assessments,
    }


POLICY_SYSTEM_PROMPT = """You are a dispute investigation policy engine for an e-commerce platform.
Given evidence about an order dispute, you must determine:
1. primary_issue: one of [canceled_order_paid, unavailable_order_paid, late_delivery_seller, late_delivery_logistics, valid_split_payment, payment_mismatch, duplicate_charge, refund_pending, refund_failed, unsupported_claim, insufficient_evidence]
2. case_status: one of [action_required, no_action, needs_investigation]
3. confidence: float 0.0-1.0
4. responsible_parties: array of {party_type, party_id}. party_type one of [seller, platform, logistics_provider, payment_provider, customer, unknown]
5. ranked_causes: array of {cause_code (UPPER_SNAKE_CASE), rank (1-5)}
6. recommended_refund_brl: number >= 0
7. refund_lines: array of {reason_code, amount_brl, entity_id}
8. resolution_actions: array of action strings (max 8, unique)
9. data_conflicts: array of {field, sources, selected_source, resolution_code} if any conflicts found
10. claim_verdicts: for each claim, {claim_id, verdict (supported/unsupported/partially_supported/insufficient_evidence), confidence}

Respond in JSON format. Be precise with financial amounts. Use evidence-based reasoning.
If data is insufficient, set confidence low and use insufficient_evidence."""


def _build_policy_prompt(
    case_id: str,
    claims: list[dict],
    customer_message: str,
    shipment: dict,
    payment: dict,
    entity_resolution: dict,
    policy_data: dict,
    entities: dict,
) -> str:
    return f"""Case ID: {case_id}

Customer message: {customer_message}

Claims: {_safe_json(claims)}

Entity Resolution: {_safe_json(entity_resolution)}

Shipment Analysis: {_safe_json(shipment)}

Payment Analysis: {_safe_json(payment)}

Affected Entities: {_safe_json(entities)}

Policy Rules: {_safe_json(policy_data)}

Based on the evidence above, provide your dispute resolution decision as JSON with these fields:
- primary_issue (string)
- case_status (string)
- confidence (float 0-1)
- responsible_parties (array of objects with party_type and party_id)
- ranked_causes (array of objects with cause_code in UPPER_SNAKE_CASE and rank 1-5)
- recommended_refund_brl (number)
- refund_lines (array of objects with reason_code, amount_brl, entity_id)
- resolution_actions (array of strings, max 8)
- data_conflicts (array of conflict objects if any, can be empty)
- claim_verdicts (array with claim_id, verdict, confidence for each claim)"""


def _safe_json(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(obj)


VALID_PRIMARY_ISSUES = {
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed",
    "unsupported_claim", "insufficient_evidence",
}

VALID_CASE_STATUSES = {"action_required", "no_action", "needs_investigation"}

VALID_PARTY_TYPES = {"seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"}

VALID_VERDICTS = {"supported", "unsupported", "partially_supported", "insufficient_evidence"}


def _rule_based_policy(
    claims: list[dict],
    shipment: dict,
    payment: dict,
    entity_res: dict,
) -> dict[str, Any]:
    """Fallback rule-based policy when LLM is unavailable."""
    primary_topic = claims[0].get("topic", "") if claims else ""
    shipment_verdict = shipment.get("verdict", "insufficient_evidence")
    payment_verdict = payment.get("verdict", "insufficient_evidence")
    captured = payment.get("captured_total_brl", 0)
    refunded = payment.get("refunded_total_brl", 0)
    refundable = payment.get("refundable_total_brl", 0)

    # Map claim topic to primary_issue
    topic_map = {
        "late_delivery_logistics": "late_delivery_logistics",
        "late_delivery_seller": "late_delivery_seller",
        "canceled_order": "canceled_order_paid",
        "unavailable_order": "unavailable_order_paid",
        "payment_mismatch": "payment_mismatch",
        "duplicate_charge": "duplicate_charge",
        "valid_split_payment": "valid_split_payment",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }

    # Cross-reference with shipment/payment evidence
    if shipment_verdict == "seller_delay":
        primary_issue = "late_delivery_seller"
    elif shipment_verdict == "logistics_delay":
        primary_issue = "late_delivery_logistics"
    elif shipment_verdict in ("lost", "returned"):
        primary_issue = "canceled_order_paid" if captured > 0 else "insufficient_evidence"
    elif payment_verdict == "refund_pending":
        primary_issue = "refund_pending"
    elif payment_verdict == "refunded":
        primary_issue = "refund_pending"
    elif primary_topic in topic_map:
        primary_issue = topic_map[primary_topic]
    else:
        primary_issue = "insufficient_evidence"

    # Determine responsible party
    if primary_issue in ("late_delivery_seller",):
        party_type = "seller"
    elif primary_issue in ("late_delivery_logistics",):
        party_type = "logistics_provider"
    elif primary_issue in ("payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed"):
        party_type = "payment_provider"
    elif primary_issue in ("canceled_order_paid", "unavailable_order_paid"):
        party_type = "platform"
    else:
        party_type = "unknown"

    case_status = "no_action" if primary_issue in ("valid_split_payment", "insufficient_evidence") else "action_required"
    confidence = 0.6 if entity_res.get("status") == "resolved" else 0.4

    recommended_refund = refundable if case_status == "action_required" else 0.0

    return {
        "primary_issue": primary_issue,
        "case_status": case_status,
        "confidence": confidence,
        "responsible_parties": [{"party_type": party_type, "party_id": None}],
        "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
        "recommended_refund_brl": round(recommended_refund, 2),
        "refund_lines": [{"reason_code": primary_issue, "amount_brl": round(recommended_refund, 2), "entity_id": None}] if recommended_refund > 0 else [],
        "resolution_actions": _default_actions(primary_issue),
        "data_conflicts": [],
        "claim_verdicts": [],
    }


def _default_actions(primary_issue: str) -> list[str]:
    base_actions = {
        "late_delivery_seller": ["Notify seller about shipping SLA violation", "Issue customer refund", "Review seller performance metrics"],
        "late_delivery_logistics": ["File claim with logistics provider", "Issue customer refund", "Monitor carrier performance"],
        "canceled_order_paid": ["Process full refund to customer", "Update order status in system"],
        "unavailable_order_paid": ["Process full refund to customer", "Remove product listing"],
        "payment_mismatch": ["Reconcile payment records", "Issue correction to customer"],
        "duplicate_charge": ["Reverse duplicate transaction", "Notify payment provider"],
        "refund_pending": ["Expedite pending refund", "Notify customer of refund status"],
        "refund_failed": ["Retry refund processing", "Escalate to payment team"],
        "valid_split_payment": ["Confirm split payment is valid", "Close investigation"],
        "unsupported_claim": ["Notify customer claim is unsupported", "Close case"],
        "insufficient_evidence": ["Request additional documentation", "Escalate for manual review"],
    }
    return base_actions.get(primary_issue, ["Escalate for manual review"])


def _normalize_assessment(
    result: dict, shipment: dict, payment: dict, entity_res: dict
) -> dict[str, Any]:
    primary = result.get("primary_issue", "insufficient_evidence")
    if primary not in VALID_PRIMARY_ISSUES:
        primary = "insufficient_evidence"

    status = result.get("case_status", "needs_investigation")
    if status not in VALID_CASE_STATUSES:
        status = "needs_investigation"

    confidence = result.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)):
        confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))

    secondary_issues: list[str] = result.get("secondary_issues", [])
    if not isinstance(secondary_issues, list):
        secondary_issues = []
    secondary_issues = [s for s in secondary_issues if isinstance(s, str) and 0 < len(s) <= 80][:10]

    return {
        "primary_issue": primary,
        "secondary_issues": secondary_issues,
        "case_status": status,
        "confidence": confidence,
    }


def _normalize_root_cause(result: dict, assessment: dict) -> dict[str, Any]:
    ranked_causes = result.get("ranked_causes", [])
    if not isinstance(ranked_causes, list) or not ranked_causes:
        cause_code = assessment.get("primary_issue", "INSUFFICIENT_EVIDENCE").upper()
        # Ensure cause_code matches pattern ^[A-Z][A-Z0-9_]{2,79}$
        cause_code = cause_code.replace("-", "_")
        if not cause_code or not cause_code[0].isalpha():
            cause_code = "UNKNOWN_CAUSE"
        ranked_causes = [{"cause_code": cause_code, "rank": 1}]
    else:
        normalized = []
        for rc in ranked_causes[:5]:
            if isinstance(rc, dict):
                cc = str(rc.get("cause_code", "UNKNOWN")).upper().replace("-", "_")
                if not cc or not cc[0].isalpha() or len(cc) < 3:
                    cc = "UNKNOWN_CAUSE"
                rank = rc.get("rank", len(normalized) + 1)
                if not isinstance(rank, int) or rank < 1 or rank > 5:
                    rank = len(normalized) + 1
                normalized.append({"cause_code": cc[:80], "rank": rank})
        ranked_causes = normalized if normalized else [{"cause_code": "UNKNOWN_CAUSE", "rank": 1}]

    parties = result.get("responsible_parties", [])
    if not isinstance(parties, list) or not parties:
        parties = [{"party_type": "unknown", "party_id": None}]
    else:
        normalized_p = []
        for p in parties[:5]:
            if isinstance(p, dict):
                pt = p.get("party_type", "unknown")
                if pt not in VALID_PARTY_TYPES:
                    pt = "unknown"
                pid = p.get("party_id")
                if pid is not None:
                    pid = str(pid)[:128]
                normalized_p.append({"party_type": pt, "party_id": pid})
        parties = normalized_p if normalized_p else [{"party_type": "unknown", "party_id": None}]

    return {
        "ranked_causes": ranked_causes,
        "responsible_parties": parties,
    }


def _normalize_financial(result: dict, payment: dict) -> dict[str, Any]:
    refund = result.get("recommended_refund_brl", 0)
    if not isinstance(refund, (int, float)):
        refund = 0
    refund = max(0.0, float(refund))

    lines = result.get("refund_lines", [])
    if not isinstance(lines, list):
        lines = []
    normalized_lines = []
    for line in lines[:10]:
        if isinstance(line, dict):
            reason = str(line.get("reason_code", "refund"))[:80]
            if not reason:
                reason = "refund"
            amount = line.get("amount_brl", 0)
            if not isinstance(amount, (int, float)):
                amount = 0
            amount = max(0.0, float(amount))
            eid = line.get("entity_id")
            if eid is not None:
                eid = str(eid)[:128]
            normalized_lines.append({
                "reason_code": reason,
                "amount_brl": round(amount, 2),
                "entity_id": eid,
            })

    # If refund > 0 but no lines, create one
    if refund > 0 and not normalized_lines:
        normalized_lines.append({
            "reason_code": "customer_refund",
            "amount_brl": round(refund, 2),
            "entity_id": None,
        })

    # Ensure refund matches sum of lines
    if normalized_lines:
        total_lines = sum(l["amount_brl"] for l in normalized_lines)
        if total_lines > 0 and abs(total_lines - refund) > 0.01:
            refund = round(total_lines, 2)

    return {
        "currency": "BRL",
        "recommended_refund_brl": round(refund, 2),
        "refund_lines": normalized_lines,
    }


def _normalize_actions(result: dict, assessment: dict) -> list[str]:
    actions = result.get("resolution_actions", [])
    if not isinstance(actions, list) or not actions:
        actions = _default_actions(assessment.get("primary_issue", "insufficient_evidence"))
    normalized = []
    seen: set[str] = set()
    for a in actions:
        if isinstance(a, str) and 0 < len(a) <= 80 and a not in seen:
            normalized.append(a)
            seen.add(a)
    return normalized[:8] if normalized else ["Escalate for manual review"]


def _normalize_conflicts(result: dict) -> list[dict[str, Any]]:
    conflicts = result.get("data_conflicts", [])
    if not isinstance(conflicts, list):
        return []
    normalized = []
    for c in conflicts[:5]:
        if isinstance(c, dict):
            field = str(c.get("field", "unknown"))[:100]
            if not field:
                field = "unknown"
            sources = c.get("sources", [])
            if not isinstance(sources, list) or len(sources) < 2:
                sources = ["source_a", "source_b"]
            sources = [str(s)[:80] for s in sources if s][:5]
            if len(sources) < 2:
                continue
            selected = c.get("selected_source")
            if selected is not None:
                selected = str(selected)[:80]
            code = str(c.get("resolution_code", "manual_review"))[:80]
            if not code:
                code = "manual_review"
            normalized.append({
                "field": field,
                "sources": sources,
                "selected_source": selected,
                "resolution_code": code,
            })
    return normalized


def _normalize_claim_assessments(
    result: dict, claims: list[dict], cache: EvidenceCache
) -> list[dict[str, Any]]:
    verdicts_raw = result.get("claim_verdicts", result.get("claim_assessments", []))
    if not isinstance(verdicts_raw, list):
        verdicts_raw = []

    verdict_map: dict[str, dict] = {}
    for v in verdicts_raw:
        if isinstance(v, dict) and v.get("claim_id"):
            verdict_map[v["claim_id"]] = v

    assessments = []
    for claim in claims[:5]:
        cid = claim.get("claim_id", "")
        if not cid:
            continue
        v = verdict_map.get(cid, {})
        verdict = v.get("verdict", "insufficient_evidence")
        if verdict not in VALID_VERDICTS:
            verdict = "insufficient_evidence"
        conf = v.get("confidence", 0.5)
        if not isinstance(conf, (int, float)):
            conf = 0.5
        conf = max(0.0, min(1.0, float(conf)))
        assessments.append({
            "claim_id": cid[:64],
            "verdict": verdict,
            "confidence": conf,
            "evidence_refs": cache.all_refs[:30],
        })
    return assessments


async def run_verifier_agent(
    case: dict[str, Any],
    trace: TraceWriter,
    output: dict[str, Any],
    cache: EvidenceCache,
) -> dict[str, Any]:
    """Cross-field consistency checks and confidence calibration."""
    case_id = case["case_id"]
    actor = "verifier-agent"

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
    )

    issues: list[str] = []
    assessment = output.get("assessment", {})
    financial = output.get("financial_resolution", {})
    shipment = output.get("shipment_analysis", {})
    payment = output.get("payment_analysis", {})

    # Check: no_action should have 0 refund
    if assessment.get("case_status") == "no_action":
        if financial.get("recommended_refund_brl", 0) > 0:
            financial["recommended_refund_brl"] = 0.0
            financial["refund_lines"] = []
            issues.append("fixed: no_action status with non-zero refund")

    # Check: refund should not exceed captured
    captured = payment.get("captured_total_brl", 0) or 0
    refund = financial.get("recommended_refund_brl", 0) or 0
    if captured > 0 and refund > captured:
        financial["recommended_refund_brl"] = round(captured, 2)
        if financial.get("refund_lines"):
            # Scale down refund lines
            total_lines = sum(l.get("amount_brl", 0) for l in financial["refund_lines"])
            if total_lines > 0:
                scale = captured / total_lines
                for line in financial["refund_lines"]:
                    line["amount_brl"] = round(line.get("amount_brl", 0) * scale, 2)
        issues.append("fixed: refund exceeds captured amount")

    # Check: refund_lines total should match recommended_refund_brl
    if financial.get("refund_lines"):
        total_lines = sum(l.get("amount_brl", 0) for l in financial["refund_lines"])
        if abs(total_lines - financial.get("recommended_refund_brl", 0)) > 0.01:
            financial["recommended_refund_brl"] = round(total_lines, 2)

    # Check: entity_resolution.resolved_order_ids should be in candidate_order_ids
    candidates = set(case.get("candidate_order_ids", []))
    er = output.get("entity_resolution", {})
    resolved = er.get("resolved_order_ids", [])
    if candidates and resolved:
        er["resolved_order_ids"] = [oid for oid in resolved if oid in candidates]

    # Check: rejected should not overlap resolved
    rejected = set(er.get("rejected_candidates", []))
    resolved_set = set(er.get("resolved_order_ids", []))
    if rejected & resolved_set:
        er["rejected_candidates"] = [r for r in er.get("rejected_candidates", []) if r not in resolved_set]
        issues.append("fixed: rejected overlaps resolved")

    # Calibrate confidence
    confidence = assessment.get("confidence", 0.5)
    conflicts = output.get("data_conflicts", [])
    if conflicts:
        confidence = min(confidence, 0.8)
    if er.get("status") == "ambiguous":
        confidence = min(confidence, 0.6)
    if shipment.get("verdict") == "insufficient_evidence":
        confidence = min(confidence, 0.7)
    if payment.get("verdict") == "insufficient_evidence":
        confidence = min(confidence, 0.7)
    assessment["confidence"] = round(confidence, 2)

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=actor,
        attributes={"issues_fixed": len(issues), "final_confidence": assessment["confidence"]},
    )

    return output
