from __future__ import annotations

from decimal import Decimal
from typing import Any

from .domain import collect, first, json_money, money, objects, parse_datetime, unique_strings
from .evidence import EvidenceLedger, EvidenceRecord
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter
from .verification import verify_output


def _has_record(data: Any, order_id: str) -> bool:
    found = first(data, "order_id")
    return found == order_id


def _sum_money(data: Any, *keys: str) -> Decimal:
    values = (money(value) for value in collect(data, *keys))
    return sum((value for value in values if value is not None), start=Decimal("0.00"))


def _shipment(data: Any, seller_ids: list[str]) -> tuple[str, list[str], bool]:
    status_text = " ".join(str(value).lower() for value in collect(data, "status", "event_type"))
    event_types = [str(value).lower() for value in collect(data, "event_type")]
    actors = [str(value).lower() for value in collect(data, "actor")]
    carrier = parse_datetime(
        first(data, "order_delivered_carrier_date", "delivered_carrier_at", "carrier_handoff_at")
    )
    limit = parse_datetime(
        first(data, "shipping_limit_date", "shipping_limit_at", "seller_handoff_deadline")
    )
    delivered = parse_datetime(
        first(data, "order_delivered_customer_date", "delivered_customer_at", "delivered_at")
    )
    estimated = parse_datetime(
        first(data, "order_estimated_delivery_date", "estimated_delivery_at")
    )

    if "lost" in status_text:
        return "lost", [], bool(carrier or delivered or estimated)
    if "return" in status_text:
        return "returned", [], bool(carrier or delivered or estimated)
    if any("late" in value for value in event_types) and "logistics_provider" in actors:
        return "logistics_delay", [], delivered is not None and estimated is not None
    if any("late" in value for value in event_types) and "seller" in actors:
        return "seller_delay", seller_ids, delivered is not None and estimated is not None
    if carrier and limit and carrier > limit:
        return "seller_delay", seller_ids, delivered is not None and estimated is not None
    if delivered and estimated and delivered > estimated:
        return "logistics_delay", [], True
    if delivered and estimated:
        return "on_time", [], True
    return "insufficient_evidence", [], False


def _payment(
    payments: Any, timeline: Any, refunds: Any
) -> tuple[str, Decimal, Decimal, Decimal, int]:
    captured = _sum_money(payments, "payment_value", "captured_amount_brl", "captured_amount")
    refunded = _sum_money(
        refunds, "refund_amount_brl", "refunded_amount_brl", "refund_amount", "amount_brl"
    )
    event_text = " ".join(
        str(value).lower()
        for value in [
            *collect(timeline, "status", "event_type", "type"),
            *collect(refunds, "status", "event_type", "type"),
        ]
    )
    raw_payment_refs = collect(payments, "payment_reference", "transaction_id", "payment_id")
    payment_rows = sum(1 for item in objects(payments) if "payment_value" in item)
    captured_events = sum(
        (
            money(item.get("amount_brl")) or Decimal("0.00")
            for item in objects(timeline)
            if str(item.get("event_type", "")).lower() == "captured"
        ),
        start=Decimal("0.00"),
    )
    refundable = max(Decimal("0.00"), captured - refunded)
    has_duplicate_ref = len(raw_payment_refs) != len(set(raw_payment_refs))
    if "duplicate" in event_text or has_duplicate_ref:
        verdict = "duplicate_capture"
    elif "refund_failed" in event_text or ("refund" in event_text and "failed" in event_text):
        verdict = "refund_failed"
    elif "refund_pending" in event_text or ("refund" in event_text and "pending" in event_text):
        verdict = "refund_pending"
    elif refunded > 0 and refundable == 0:
        verdict = "refunded"
    elif captured_events > 0 and captured != captured_events:
        verdict = "capture_mismatch"
    elif captured > 0:
        verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"
    return verdict, captured, refunded, refundable, payment_rows


def _data_conflicts(item_data: Any, shipment_data: Any) -> list[dict[str, Any]]:
    item_limits = unique_strings(collect(item_data, "shipping_limit_date"))
    shipment_limits = unique_strings(collect(shipment_data, "shipping_limit_at"))
    distinct_limits = set([*item_limits, *shipment_limits])
    if len(distinct_limits) <= 1:
        return []
    return [
        {
            "field": "shipping_limit_at",
            "sources": ["order_items", "shipment_summary", "shipment_event"],
            "selected_source": "shipment_event",
            "resolution_code": "AUTHORITATIVE_EVENT_PRECEDENCE",
        }
    ]


def _primary_issue(
    *,
    order_status: str,
    shipment_verdict: str,
    payment_verdict: str,
    payment_rows: int,
    claimed_topics: set[str],
) -> str:
    status = order_status.lower()
    if "cancel" in status and payment_verdict not in {"refunded", "insufficient_evidence"}:
        return "canceled_order_paid"
    if "unavailable" in status and payment_verdict not in {"refunded", "insufficient_evidence"}:
        return "unavailable_order_paid"
    mapping = {
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
        "duplicate_capture": "duplicate_charge",
        "capture_mismatch": "payment_mismatch",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    detected = mapping.get(shipment_verdict) or mapping.get(payment_verdict)
    if detected:
        return detected
    for topic in claimed_topics:
        if topic == "valid_split_payment" and payment_verdict == "reconciled" and payment_rows > 1:
            return topic
        if topic == "unsupported_claim":
            return topic
    return "insufficient_evidence"


async def _fetch_optional(
    ledger: EvidenceLedger, tool: str, actor: str, **arguments: Any
) -> EvidenceRecord | None:
    try:
        return await ledger.fetch(tool, actor=actor, **arguments)
    except (RuntimeError, ValueError):
        return None


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the deterministic evidence graph for one isolated case."""
    case_id = case["case_id"]
    contracts = gateway.contracts
    ledger = EvidenceLedger(case_id, gateway, trace)
    candidates = unique_strings(case.get("candidate_order_ids", ()))
    claimed = case.get("customer_request", {}).get("claimed_order_id")
    ordered_candidates = unique_strings([claimed, *candidates])

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-customer",
        decision_code="RESOLVE_CANDIDATES",
    )
    order_record: EvidenceRecord | None = None
    resolved_order: str | None = None
    rejected: list[str] = []
    for candidate in ordered_candidates:
        candidate_record = await _fetch_optional(
            ledger, "get_order", "entity-customer", order_id=candidate
        )
        if candidate_record and _has_record(candidate_record.data, candidate):
            order_record = candidate_record
            resolved_order = candidate
            ledger.consume(candidate_record, actor="entity-customer")
            break
        rejected.append(candidate)
    if resolved_order:
        rejected.extend(value for value in candidates if value != resolved_order)
    rejected = unique_strings(rejected)
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-customer",
        target="coordinator",
        decision_code="ENTITY_RESOLVED" if resolved_order else "ENTITY_NOT_FOUND",
        attributes={"candidate_count": len(candidates), "resolved": resolved_order is not None},
    )

    records: dict[str, EvidenceRecord | None] = {}
    if resolved_order:
        assignments = {
            "items": ("get_order_items", "order-product", {"order_id": resolved_order}),
            "product": ("get_product_context", "order-product", {"order_id": resolved_order}),
            "shipment": ("get_shipment_summary", "shipment", {"order_id": resolved_order}),
            "payments": ("get_order_payments", "payment-refund", {"order_id": resolved_order}),
            "payment_timeline": (
                "get_payment_timeline",
                "payment-refund",
                {"order_id": resolved_order},
            ),
            "refunds": ("get_refund_timeline", "payment-refund", {"order_id": resolved_order}),
            "policy": ("get_policy", "policy", {"policy_version": case["policy_version"]}),
        }
        customer_hint = case.get("customer_unique_id_hint")
        if customer_hint and case.get("investigation_scope", {}).get("include_customer_history"):
            assignments["customer"] = (
                "get_customer_history",
                "entity-customer",
                {"customer_unique_id": customer_hint},
            )
        for _, actor, _ in assignments.values():
            trace.emit(
                case_id=case_id,
                event_type="task_assigned",
                actor="coordinator",
                target=actor,
                decision_code="DOMAIN_INVESTIGATION",
            )
        results = []
        for tool, actor, arguments in assignments.values():
            results.append(await _fetch_optional(ledger, tool, actor, **arguments))
        records = dict(zip(assignments, results, strict=True))
        for name, record in records.items():
            actor = assignments[name][1]
            if record:
                ledger.consume(record, actor=actor)
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="SPECIALIST_COMPLETE" if record else "SPECIALIST_INSUFFICIENT",
            )

    order_data = order_record.data if order_record else {}
    item_data = records["items"].data if records.get("items") else {}
    shipment_data = records["shipment"].data if records.get("shipment") else {}
    payment_data = records["payments"].data if records.get("payments") else {}
    payment_timeline = (
        records["payment_timeline"].data if records.get("payment_timeline") else {}
    )
    refund_data = records["refunds"].data if records.get("refunds") else {}
    customer_data = records["customer"].data if records.get("customer") else {}

    seller_ids = unique_strings(collect(item_data, "seller_id"))
    item_ids = unique_strings(collect(item_data, "order_item_id", "item_id"))
    shipment_ids = unique_strings(collect(shipment_data, "shipment_id", "tracking_code"))
    payment_refs = unique_strings(
        collect(payment_data, "payment_reference", "transaction_id", "payment_id")
    )
    shipment_verdict, late_sellers, timeline_complete = _shipment(shipment_data, seller_ids)
    payment_verdict, captured, refunded, refundable, payment_rows = _payment(
        payment_data, payment_timeline, refund_data
    )
    order_status = str(first(order_data, "order_status", "status") or "")
    claims = case.get("customer_request", {}).get("claims", ())
    claimed_topics = {claim["topic"] for claim in claims if isinstance(claim, dict)}
    primary_issue = _primary_issue(
        order_status=order_status,
        shipment_verdict=shipment_verdict,
        payment_verdict=payment_verdict,
        payment_rows=payment_rows,
        claimed_topics=claimed_topics,
    )
    supported = primary_issue != "insufficient_evidence"
    policy_data = records["policy"].data if records.get("policy") else {}
    policy_rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
    policy_rule = policy_rules.get(primary_issue, {}) if isinstance(policy_rules, dict) else {}
    default_action_required = (
        primary_issue not in {"valid_split_payment", "unsupported_claim"} and supported
    )
    case_status = policy_rule.get(
        "case_status",
        "action_required"
        if default_action_required
        else ("no_action" if supported else "needs_investigation"),
    )
    action_required = case_status == "action_required"
    policy_refund = money(policy_rule.get("refund_brl"))
    recommended = (
        policy_refund
        if policy_refund is not None
        else (refundable if action_required else Decimal("0.00"))
    )
    confidence = 0.9 if supported and resolved_order else (0.45 if resolved_order else 0.1)
    policy_parties = policy_rule.get("responsible_parties", [])
    responsible: list[dict[str, str | None]] = (
        list(policy_parties) if isinstance(policy_parties, list) else []
    )
    if shipment_verdict == "seller_delay" and late_sellers:
        responsible = [
            {"party_type": "seller", "party_id": value} for value in late_sellers
        ]
    elif not responsible and shipment_verdict == "seller_delay":
        responsible.extend({"party_type": "seller", "party_id": value} for value in late_sellers)
    elif not responsible and shipment_verdict in {"logistics_delay", "lost", "returned"}:
        responsible.append({"party_type": "logistics_provider", "party_id": None})
    elif not responsible and payment_verdict in {
        "capture_mismatch",
        "duplicate_capture",
        "refund_failed",
    }:
        responsible.append({"party_type": "payment_provider", "party_id": None})
    if not responsible:
        responsible.append({"party_type": "unknown", "party_id": None})

    claim_assessments = []
    for claim in claims:
        topic = claim.get("topic", "")
        if topic == "requested_full_refund":
            if recommended <= 0:
                verdict = "unsupported"
            elif captured > 0 and recommended < refundable:
                verdict = "partially_supported"
            else:
                verdict = "supported"
        elif topic == primary_issue:
            verdict = "supported"
        elif primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        else:
            verdict = "unsupported"
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": ledger.consumed_refs,
            }
        )

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [resolved_order] if resolved_order else [],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": "resolved" if resolved_order else "not_found",
            "resolved_order_ids": [resolved_order] if resolved_order else [],
            "rejected_candidates": rejected,
            "confidence": 0.98 if resolved_order else 0.1,
        },
        "customer_context": {
            "customer_unique_id": first(customer_data, "customer_unique_id")
            or case.get("customer_unique_id_hint"),
            "related_order_ids": unique_strings(collect(customer_data, "order_id")),
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_sellers,
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": json_money(captured),
            "refunded_total_brl": json_money(refunded),
            "refundable_total_brl": json_money(refundable),
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": responsible,
        },
        "evidence_refs": ledger.consumed_refs,
        "data_conflicts": _data_conflicts(item_data, shipment_data),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": json_money(recommended),
            "refund_lines": (
                [
                    {
                        "reason_code": primary_issue.upper(),
                        "amount_brl": json_money(recommended),
                        "entity_id": resolved_order,
                    }
                ]
                if recommended > 0
                else []
            ),
        },
        "resolution_actions": (
            [str(policy_rule["recommended_action"]).upper()]
            if policy_rule.get("recommended_action")
            else (
                ["ISSUE_REFUND"]
                if recommended > 0
                else (["MANUAL_INVESTIGATION"] if not supported else [])
            )
        ),
    }
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy",
        target="coordinator",
        decision_code=primary_issue.upper(),
        evidence_refs=[records["policy"].evidence_ref] if records.get("policy") else None,
    )
    trace.record_metrics(ledger.metrics())
    verify_output(output, case=case, ledger=ledger, contracts=contracts)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="VERIFIED",
        attributes={"mcp_calls": ledger.call_count},
    )
    return output
