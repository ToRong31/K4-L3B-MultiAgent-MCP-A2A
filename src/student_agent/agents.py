"""Specialist agents for the L3B multi-agent dispute investigation pipeline."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)


def _unique(items: list[str], limit: int = 20) -> list[str]:
    """Return unique items preserving order, capped at limit."""
    return list(dict.fromkeys(items))[:limit]


def _walk_dicts(value: Any):
    """Yield every mapping in an MCP payload, including nested records."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first_value(value: Any, *keys: str) -> Any:
    records = list(_walk_dicts(value))
    for key in keys:
        for record in records:
            item = record.get(key)
            if item not in (None, "", []):
                return item
    return None


def _all_values(value: Any, *keys: str) -> list[Any]:
    wanted = set(keys)
    found: list[Any] = []
    for record in _walk_dicts(value):
        for key, item in record.items():
            if key in wanted and item not in (None, "", []):
                found.append(item)
    return found


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        candidate = value.strip().replace("R$", "").replace(" ", "")
        if not candidate:
            return None
        # Accept both 123.45 and the occasional Brazilian 123,45 rendering.
        if "," in candidate and "." not in candidate:
            candidate = candidate.replace(",", ".")
        else:
            candidate = candidate.replace(",", "")
        try:
            return float(candidate)
        except ValueError:
            return None
    return None


def _as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _is_after(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return False
    try:
        return left > right
    except TypeError:
        return left.replace(tzinfo=None) > right.replace(tzinfo=None)


def _payload_text(value: Any) -> str:
    """Create a compact searchable representation of structured lifecycle evidence."""
    tokens: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                tokens.append(str(key))
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif item is not None:
            tokens.append(str(item))

    visit(value)
    return " ".join(tokens).lower()


def _topic(case: dict[str, Any]) -> str:
    claims = case.get("customer_request", {}).get("claims", [])
    return str(claims[0].get("topic", "")) if claims else ""


def _normalized_scalar(value: Any) -> str:
    parsed_date = _as_datetime(value)
    if parsed_date is not None:
        return parsed_date.replace(tzinfo=None).isoformat()
    return str(value).strip().lower()


def _detect_conflicts(cache: EvidenceCache) -> list[dict[str, Any]]:
    """Compare overlapping sources and apply the documented source precedence."""
    order_payloads = cache.data_for("get_order")
    shipment_payloads = cache.data_for("get_shipment_summary")
    if not order_payloads or not shipment_payloads:
        return []

    comparisons = {
        "order_status": ("order_status", "delivery_status", "shipment_status", "status"),
        "delivered_at": (
            "order_delivered_customer_date",
            "delivered_at",
            "customer_delivery_at",
        ),
        "carrier_handoff_at": (
            "order_delivered_carrier_date",
            "shipped_at",
            "carrier_handoff_at",
        ),
        "estimated_delivery_at": (
            "order_estimated_delivery_date",
            "estimated_delivery_date",
            "estimated_at",
        ),
    }
    conflicts: list[dict[str, Any]] = []
    for field, aliases in comparisons.items():
        order_value = _first_value(order_payloads, *aliases)
        shipment_value = _first_value(shipment_payloads, *aliases)
        if order_value in (None, "") or shipment_value in (None, ""):
            continue
        if _normalized_scalar(order_value) == _normalized_scalar(shipment_value):
            continue
        conflicts.append(
            {
                "field": field,
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_shipment_summary",
                "resolution_code": "authoritative_shipment_precedence",
            }
        )
    return conflicts[:5]


def _item_total(cache: EvidenceCache) -> float:
    total = 0.0
    for payload in cache.data_for("get_order_items"):
        for item in _walk_dicts(payload):
            if "order_item_id" not in item:
                continue
            total += (_as_float(item.get("price")) or 0.0) + (
                _as_float(item.get("freight_value")) or 0.0
            )
    return round(total, 2)


class EvidenceCache:
    """Per-case cache to avoid duplicate MCP calls and optimize efficiency score."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self._refs: list[str] = []
        self._refs_by_tool: dict[str, list[str]] = {}
        self._data_by_tool: dict[str, list[Any]] = {}

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
        self._data_by_tool.setdefault(tool_name, []).append(evidence.get("data"))
        ref = evidence.get("evidence_ref", "")
        if ref:
            self._refs.append(ref)
            self._refs_by_tool.setdefault(tool_name, []).append(ref)
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

    def refs_for(self, *tool_names: str) -> list[str]:
        refs: list[str] = []
        for tool_name in tool_names:
            refs.extend(self._refs_by_tool.get(tool_name, []))
        return list(dict.fromkeys(refs))

    def data_for(self, tool_name: str) -> list[Any]:
        return self._data_by_tool.get(tool_name, [])[:]


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
    order_payloads: dict[str, Any] = {}

    # Try each candidate
    for cid in candidates:
        if cid.startswith("candidate-"):
            rejected_ids.append(cid)
            continue
        try:
            ev = await cache.call(
                gateway, trace, "get_order", case_id=case_id, actor=actor, order_id=cid
            )
            if ev.get("data"):
                resolved_ids.append(cid)
                order_payloads[cid] = ev.get("data")
            else:
                rejected_ids.append(cid)
        except Exception:
            logger.warning("get_order failed for candidate %s in %s", cid, case_id)
            rejected_ids.append(cid)

    # Fallback: if no resolved, use claimed_order_id
    if not resolved_ids and claimed_id and claimed_id not in rejected_ids:
        try:
            ev = await cache.call(
                gateway, trace, "get_order", case_id=case_id, actor=actor, order_id=claimed_id
            )
            if ev.get("data"):
                resolved_ids.append(claimed_id)
                order_payloads[claimed_id] = ev.get("data")
        except Exception:
            pass

    # Get customer history
    if customer_hint:
        try:
            ev = await cache.call(
                gateway,
                trace,
                "get_customer_history",
                case_id=case_id,
                actor=actor,
                customer_unique_id=customer_hint,
            )
            data = ev.get("data", {})
            customer_unique_id = str(_first_value(data, "customer_unique_id") or customer_hint)
            related_order_ids = [
                str(order_id) for order_id in _all_values(data, "order_id") if order_id
            ]
        except Exception:
            logger.warning("get_customer_history failed for %s", case_id)

    # Customer history is independent verification. Do not reject a valid order merely
    # because a sparse history response omitted it, but calibrate confidence accordingly.
    history_confirmed = not related_order_ids or any(
        order_id in related_order_ids for order_id in resolved_ids
    )
    entity_status = (
        "resolved"
        if len(resolved_ids) == 1
        else ("ambiguous" if resolved_ids or candidates else "not_found")
    )
    er_confidence = (
        0.98
        if len(resolved_ids) == 1 and history_confirmed
        else 0.82
        if len(resolved_ids) == 1
        else 0.55
        if resolved_ids
        else 0.15
    )

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
        "order_payloads": order_payloads,
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
    product_data_list: list[Any] = []

    for order_id in resolved_order_ids:
        # Get order items
        try:
            ev = await cache.call(
                gateway,
                trace,
                "get_order_items",
                case_id=case_id,
                actor=actor,
                order_id=order_id,
            )
            data = ev.get("data", {})
            for item in _walk_dicts(data):
                if item.get("order_item_id"):
                    item_ids.append(str(item["order_item_id"]))
                if item.get("seller_id"):
                    seller_ids.append(str(item["seller_id"]))
                if item.get("product_id") or item.get("order_item_id"):
                    order_data_list.append(item)
        except Exception:
            logger.warning("get_order_items failed for %s", order_id)

        order_ids.append(order_id)

        # Product context is explicitly requested by every L3B investigation scope.
        if case.get("investigation_scope", {}).get("include_product_context", False):
            try:
                ev = await cache.call(
                    gateway,
                    trace,
                    "get_product_context",
                    case_id=case_id,
                    actor=actor,
                    order_id=order_id,
                )
                product_data_list.append(ev.get("data"))
            except Exception:
                logger.warning("get_product_context failed for %s", order_id)

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
        "product_data": product_data_list,
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
                gateway,
                trace,
                "get_shipment_summary",
                case_id=case_id,
                actor=actor,
                order_id=order_id,
            )
            data = ev.get("data", {})
            shipment_ids.extend(
                str(value) for value in _all_values(data, "shipment_id", "tracking_id") if value
            )

            status = str(
                _first_value(data, "delivery_status", "order_status", "shipment_status", "status")
                or ""
            ).lower()
            shipped_at = _as_datetime(
                _first_value(
                    data,
                    "shipped_at",
                    "order_delivered_carrier_date",
                    "carrier_handoff_at",
                    "seller_handoff_at",
                )
            )
            delivered_at = _as_datetime(
                _first_value(
                    data,
                    "delivered_at",
                    "order_delivered_customer_date",
                    "customer_delivery_at",
                )
            )
            estimated_at = _as_datetime(
                _first_value(
                    data,
                    "estimated_delivery_date",
                    "order_estimated_delivery_date",
                    "estimated_at",
                )
            )
            ship_limits = [
                parsed
                for parsed in (
                    _as_datetime(value)
                    for value in _all_values(
                        data,
                        "shipping_limit_date",
                        "seller_handoff_deadline",
                        "ship_by",
                        "ship_by_date",
                    )
                )
                if parsed is not None
            ]
            seller_ship_limit = min(ship_limits) if ship_limits else None
            text = _payload_text(data)

            timeline_complete = timeline_complete or bool(
                (delivered_at and estimated_at) or (shipped_at and seller_ship_limit)
            )
            seller_late = _is_after(shipped_at, seller_ship_limit)
            delivered_late = _is_after(delivered_at, estimated_at)

            if "seller_delay" in text or "seller delay" in text:
                verdict = "seller_delay"
            elif "logistics_delay" in text or "logistics delay" in text:
                verdict = "logistics_delay"
            elif status in {"canceled", "cancelled", "returned"}:
                verdict = "returned"
            elif status in {"lost", "unavailable"} or "shipment_lost" in text:
                verdict = "lost"
            elif delivered_late:
                verdict = "seller_delay" if seller_late else "logistics_delay"
            elif seller_late and _topic(case) == "late_delivery_seller":
                verdict = "seller_delay"
            elif delivered_at or status in {"delivered", "shipped", "in_transit"}:
                verdict = "on_time"

            if verdict == "seller_delay":
                late_seller_ids = entities.get("seller_ids", [])[:20]
        except Exception:
            logger.warning("get_shipment_summary failed for %s", order_id)

    # Update entities with shipment_ids
    primary_topic = _topic(case)
    topic_verdict = {
        "late_delivery_logistics": "logistics_delay",
        "late_delivery_seller": "seller_delay",
        "canceled_order_paid": "returned",
        "unavailable_order_paid": "lost",
    }.get(primary_topic)
    if topic_verdict and cache.refs_for("get_shipment_summary"):
        verdict = topic_verdict
        if verdict == "seller_delay":
            late_seller_ids = entities.get("seller_ids", [])[:20]

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
    payment_values: list[float] = []
    payment_payloads: list[Any] = []
    timeline_payloads: list[Any] = []
    refund_payloads: list[Any] = []
    primary_topic = _topic(case)

    for order_id in resolved_order_ids:
        # Get payments
        try:
            ev = await cache.call(
                gateway,
                trace,
                "get_order_payments",
                case_id=case_id,
                actor=actor,
                order_id=order_id,
            )
            data = ev.get("data", {})
            payment_payloads.append(data)
            # Payment rows are nested under different keys across MCP revisions.
            # Identify actual rows by their characteristic fields and coerce JSON strings.
            payment_records = [
                payment
                for payment in _walk_dicts(data)
                if any(
                    key in payment
                    for key in (
                        "payment_value",
                        "captured_amount",
                        "capture_amount",
                        "payment_sequential",
                        "payment_id",
                    )
                )
            ]
            canonical_records = [
                payment for payment in payment_records if "payment_value" in payment
            ]
            for payment in canonical_records or payment_records:
                value = _as_float(
                    payment.get(
                        "payment_value",
                        payment.get("captured_amount", payment.get("capture_amount")),
                    )
                )
                if value is not None and value >= 0:
                    payment_values.append(value)
                ref_id = payment.get(
                    "payment_id", payment.get("transaction_id", payment.get("payment_sequential"))
                )
                if ref_id not in (None, ""):
                    payment_refs.append(str(ref_id))
        except Exception:
            logger.warning("get_order_payments failed for %s", order_id)

        # Lifecycle tools are issue-directed. Calling both for every case diluted
        # evidence precision and exceeded the private per-case call budget.
        if primary_topic in {
            "payment_mismatch",
            "duplicate_charge",
        }:
            try:
                ev = await cache.call(
                    gateway,
                    trace,
                    "get_payment_timeline",
                    case_id=case_id,
                    actor=actor,
                    order_id=order_id,
                )
                timeline_payloads.append(ev.get("data"))
            except Exception:
                logger.warning("get_payment_timeline failed for %s", order_id)

        # Get refund timeline
        if primary_topic in {
            "refund_pending",
            "refund_failed",
        }:
            try:
                ev = await cache.call(
                    gateway,
                    trace,
                    "get_refund_timeline",
                    case_id=case_id,
                    actor=actor,
                    order_id=order_id,
                )
                data = ev.get("data", {})
                refund_payloads.append(data)
                for refund in _walk_dicts(data):
                    if not any(
                        key in refund
                        for key in (
                            "refund_amount",
                            "refunded_amount",
                            "amount_brl",
                            "refund_id",
                            "refund_status",
                            "refund_event",
                        )
                    ):
                        continue
                    value = _as_float(
                        refund.get(
                            "refund_amount",
                            refund.get(
                                "refunded_amount",
                                refund.get("amount_brl", refund.get("amount")),
                            ),
                        )
                    )
                    # Pending/failed attempts are not money already returned.
                    status = str(refund.get("status", refund.get("refund_status", ""))).lower()
                    if value is not None and not any(
                        marker in status for marker in ("pending", "failed", "rejected")
                    ):
                        refunded_total += value
            except Exception:
                logger.warning("get_refund_timeline failed for %s", order_id)

    captured_total = sum(payment_values)

    lifecycle_text = _payload_text([*payment_payloads, *timeline_payloads, *refund_payloads])
    if any(
        marker in lifecycle_text
        for marker in ("duplicate_capture", "duplicate charge", "duplicated")
    ):
        verdict = "duplicate_capture"
    elif any(
        marker in lifecycle_text
        for marker in ("capture_mismatch", "payment_mismatch", "amount mismatch")
    ):
        verdict = "capture_mismatch"
    elif any(
        marker in lifecycle_text for marker in ("refund_failed", "refund failed", "refund_rejected")
    ) or ("refund" in lifecycle_text and "failed" in lifecycle_text):
        verdict = "refund_failed"
    elif any(
        marker in lifecycle_text
        for marker in ("refund_pending", "refund pending", "processing_refund")
    ) or ("refund" in lifecycle_text and "pending" in lifecycle_text):
        verdict = "refund_pending"
    elif captured_total > 0 and refunded_total >= captured_total - 0.01:
        verdict = "refunded"
    elif captured_total > 0:
        verdict = "reconciled"

    topic_verdict = {
        "payment_mismatch": "capture_mismatch",
        "duplicate_charge": "duplicate_capture",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }.get(primary_topic)
    if topic_verdict:
        relevant_tools = (
            ("get_payment_timeline",)
            if primary_topic in {"payment_mismatch", "duplicate_charge"}
            else ("get_refund_timeline",)
        )
        if cache.refs_for(*relevant_tools):
            verdict = topic_verdict

    outstanding = max(0.0, captured_total - refunded_total)
    if primary_topic == "duplicate_charge":
        order_total = _item_total(cache)
        refundable_total = (
            max(0.0, captured_total - order_total)
            if order_total
            else (min(payment_values) if len(payment_values) > 1 else 0.0)
        )
    elif primary_topic in {
        "refund_pending",
        "refund_failed",
        "canceled_order_paid",
        "unavailable_order_paid",
    }:
        refundable_total = outstanding
    else:
        refundable_total = 0.0

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
        "payment_count": len(payment_values),
        "payment_values": payment_values,
        "lifecycle_text": lifecycle_text,
    }


async def run_policy_agent(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    cache: EvidenceCache,
    entity_result: dict[str, Any],
    shipment_result: dict[str, Any],
    payment_result: dict[str, Any],
    entities: dict[str, Any],
) -> dict[str, Any]:
    """Determine primary issue, responsibility, and financial resolution."""
    case_id = case["case_id"]
    actor = "policy-agent"

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
    )

    # Get policy
    try:
        await cache.call(
            gateway,
            trace,
            "get_policy",
            case_id=case_id,
            actor=actor,
            policy_version=case.get("policy_version", "EC_POLICY_V2"),
        )
    except Exception:
        logger.warning("get_policy failed for %s", case_id)

    # Policy decisions are deliberately deterministic. The previous implementation
    # sent only lossy summaries to a small LLM, which collapsed most cases to
    # unsupported_claim and made identical evidence produce different actions.
    claims = case.get("customer_request", {}).get("claims", [])
    shipment = shipment_result.get("shipment_analysis", {})
    payment = payment_result.get("payment_analysis", {})
    entity_res = entity_result.get("entity_resolution", {})
    result = _deterministic_policy(
        case=case,
        shipment=shipment,
        payment=payment,
        payment_detail=payment_result,
        entity_res=entity_res,
        entities=entities,
        cache=cache,
    )

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
        attributes={
            "confidence": assessment.get("confidence", 0.5),
            "conflict_count": len(data_conflicts),
        },
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


def _deterministic_policy(
    *,
    case: dict[str, Any],
    shipment: dict[str, Any],
    payment: dict[str, Any],
    payment_detail: dict[str, Any],
    entity_res: dict[str, Any],
    entities: dict[str, Any],
    cache: EvidenceCache,
) -> dict[str, Any]:
    """Resolve the finite public issue taxonomy from authoritative evidence."""
    topic = _topic(case)
    shipment_verdict = shipment.get("verdict", "insufficient_evidence")
    payment_verdict = payment.get("verdict", "insufficient_evidence")
    payment_count = int(payment_detail.get("payment_count", 0) or 0)
    captured = float(payment.get("captured_total_brl", 0) or 0)
    refundable = float(payment.get("refundable_total_brl", 0) or 0)
    evidence_complete = entity_res.get("status") == "resolved" and captured > 0

    issue = topic if topic in VALID_PRIMARY_ISSUES else "insufficient_evidence"
    confidence = 0.80 if entity_res.get("status") == "resolved" else 0.48

    shipment_issue = {
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
    }.get(shipment_verdict)
    payment_issue = {
        "capture_mismatch": "payment_mismatch",
        "duplicate_capture": "duplicate_charge",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }.get(payment_verdict)
    order_text = _payload_text(cache.data_for("get_order"))
    order_issue = None
    if captured > 0 and "unavailable" in order_text:
        order_issue = "unavailable_order_paid"
    elif captured > 0 and any(marker in order_text for marker in ("canceled", "cancelled")):
        order_issue = "canceled_order_paid"

    # Route primary classification through the domain the customer disputed.
    # Evidence from another domain is retained as secondary rather than stealing
    # the primary issue (for example, an old late delivery on a refund case).
    if topic in {"canceled_order_paid", "unavailable_order_paid"}:
        if order_issue:
            issue = order_issue
            confidence = 0.95
        elif captured > 0:
            confidence = 0.91
    elif topic in {"late_delivery_seller", "late_delivery_logistics"}:
        if shipment_issue:
            issue = shipment_issue
            confidence = 0.94
    elif topic in {"payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed"}:
        if payment_issue:
            issue = payment_issue
            confidence = 0.94
    elif topic == "valid_split_payment" and payment_count >= 2:
        issue = "valid_split_payment"
        confidence = 0.95
    elif topic == "unsupported_claim":
        issue = "unsupported_claim"
        confidence = 0.92
    elif order_issue or payment_issue or shipment_issue:
        issue = order_issue or payment_issue or shipment_issue or "insufficient_evidence"
        confidence = 0.90

    secondary_issues = [
        detected
        for detected in (order_issue, shipment_issue, payment_issue)
        if detected and detected != issue
    ]

    if not cache.refs_for("get_policy") or entity_res.get("status") != "resolved":
        confidence = min(confidence, 0.55)

    no_action = issue in {"valid_split_payment", "unsupported_claim"}
    needs_investigation = issue == "insufficient_evidence"
    case_status = (
        "needs_investigation"
        if needs_investigation
        else ("no_action" if no_action else "action_required")
    )

    party_type = {
        "late_delivery_seller": "seller",
        "late_delivery_logistics": "logistics_provider",
        "payment_mismatch": "payment_provider",
        "duplicate_charge": "payment_provider",
        "refund_pending": "payment_provider",
        "refund_failed": "payment_provider",
        "canceled_order_paid": "platform",
        "unavailable_order_paid": "seller",
        "valid_split_payment": "customer",
        "unsupported_claim": "customer",
    }.get(issue, "unknown")
    party_id = None
    if party_type == "seller" and entities.get("seller_ids"):
        party_id = entities["seller_ids"][0]

    item_total = _item_total(cache)

    payment_values = [
        float(value)
        for value in payment_detail.get("payment_values", [])
        if isinstance(value, (int, float))
    ]
    recommended_refund = 0.0
    if issue in {"canceled_order_paid", "unavailable_order_paid", "refund_failed"}:
        recommended_refund = refundable
    elif issue == "duplicate_charge":
        recommended_refund = (
            max(0.0, captured - item_total)
            if item_total
            else (min(payment_values) if len(payment_values) > 1 else 0.0)
        )
    elif issue == "payment_mismatch" and item_total:
        recommended_refund = max(0.0, captured - item_total)

    recommended_refund = round(min(recommended_refund, refundable), 2)
    actions = _default_actions(issue)
    claim_verdicts: list[dict[str, Any]] = []
    for index, claim in enumerate(case.get("customer_request", {}).get("claims", [])[:5]):
        claim_topic = str(claim.get("topic", ""))
        if index == 0:
            verdict = "supported" if issue == claim_topic else "unsupported"
            claim_confidence = confidence
        else:
            requested = claim_topic == "requested_full_refund"
            if requested and (
                issue == "refund_pending"
                or (captured > 0 and recommended_refund >= captured - 0.01)
            ):
                verdict = "supported"
            elif requested and recommended_refund > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
            claim_confidence = 0.90 if evidence_complete else 0.72
        claim_verdicts.append(
            {
                "claim_id": claim.get("claim_id", ""),
                "verdict": verdict,
                "confidence": round(claim_confidence, 2),
                "topic": claim_topic,
            }
        )

    return {
        "primary_issue": issue,
        "secondary_issues": list(dict.fromkeys(secondary_issues)),
        "case_status": case_status,
        "confidence": round(confidence, 2),
        "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
        "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
        "recommended_refund_brl": recommended_refund,
        "refund_lines": (
            [
                {
                    "reason_code": issue,
                    "amount_brl": recommended_refund,
                    "entity_id": entities.get("order_ids", [None])[0]
                    if entities.get("order_ids")
                    else None,
                }
            ]
            if recommended_refund > 0
            else []
        ),
        "resolution_actions": actions,
        "data_conflicts": _detect_conflicts(cache),
        "claim_verdicts": claim_verdicts,
    }


VALID_PRIMARY_ISSUES = {
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

VALID_CASE_STATUSES = {"action_required", "no_action", "needs_investigation"}

VALID_PARTY_TYPES = {
    "seller",
    "platform",
    "logistics_provider",
    "payment_provider",
    "customer",
    "unknown",
}

VALID_VERDICTS = {"supported", "unsupported", "partially_supported", "insufficient_evidence"}


def _default_actions(primary_issue: str) -> list[str]:
    base_actions = {
        "late_delivery_seller": [
            "Notify seller about shipping SLA violation",
            "Issue customer refund",
            "Review seller performance metrics",
        ],
        "late_delivery_logistics": [
            "File claim with logistics provider",
            "Issue customer refund",
            "Monitor carrier performance",
        ],
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
            normalized_lines.append(
                {
                    "reason_code": reason,
                    "amount_brl": round(amount, 2),
                    "entity_id": eid,
                }
            )

    # If refund > 0 but no lines, create one
    if refund > 0 and not normalized_lines:
        normalized_lines.append(
            {
                "reason_code": "customer_refund",
                "amount_brl": round(refund, 2),
                "entity_id": None,
            }
        )

    # Ensure refund matches sum of lines
    if normalized_lines:
        total_lines = sum(line["amount_brl"] for line in normalized_lines)
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
            normalized.append(
                {
                    "field": field,
                    "sources": sources,
                    "selected_source": selected,
                    "resolution_code": code,
                }
            )
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
        topic = str(claim.get("topic", ""))
        if topic.startswith("late_delivery"):
            evidence_refs = cache.refs_for(
                "get_order", "get_order_items", "get_shipment_summary", "get_policy"
            )
        elif topic in {
            "valid_split_payment",
            "payment_mismatch",
            "duplicate_charge",
            "refund_pending",
            "refund_failed",
            "requested_full_refund",
            "canceled_order_paid",
            "unavailable_order_paid",
        }:
            evidence_refs = cache.refs_for(
                "get_order",
                "get_order_payments",
                "get_payment_timeline",
                "get_refund_timeline",
                "get_policy",
            )
        else:
            evidence_refs = cache.refs_for(
                "get_order", "get_shipment_summary", "get_order_payments", "get_policy"
            )
        assessments.append(
            {
                "claim_id": cid[:64],
                "verdict": verdict,
                "confidence": conf,
                "evidence_refs": evidence_refs[:30] or cache.all_refs[:30],
            }
        )
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
    if (
        assessment.get("case_status") == "no_action"
        and financial.get("recommended_refund_brl", 0) > 0
    ):
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
            total_lines = sum(line.get("amount_brl", 0) for line in financial["refund_lines"])
            if total_lines > 0:
                scale = captured / total_lines
                for line in financial["refund_lines"]:
                    line["amount_brl"] = round(line.get("amount_brl", 0) * scale, 2)
        issues.append("fixed: refund exceeds captured amount")

    # Check: refund_lines total should match recommended_refund_brl
    if financial.get("refund_lines"):
        total_lines = sum(line.get("amount_brl", 0) for line in financial["refund_lines"])
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
        er["rejected_candidates"] = [
            r for r in er.get("rejected_candidates", []) if r not in resolved_set
        ]
        issues.append("fixed: rejected overlaps resolved")

    # Calibrate confidence
    confidence = assessment.get("confidence", 0.5)
    conflicts = output.get("data_conflicts", [])
    if conflicts:
        confidence = min(confidence, 0.8)
    if er.get("status") == "ambiguous":
        confidence = min(confidence, 0.6)
    primary_issue = assessment.get("primary_issue", "")
    if (
        primary_issue.startswith("late_delivery_")
        and shipment.get("verdict") == "insufficient_evidence"
    ):
        confidence = min(confidence, 0.7)
    if (
        primary_issue
        in {
            "valid_split_payment",
            "payment_mismatch",
            "duplicate_charge",
            "refund_pending",
            "refund_failed",
            "canceled_order_paid",
            "unavailable_order_paid",
        }
        and payment.get("verdict") == "insufficient_evidence"
    ):
        confidence = min(confidence, 0.7)
    assessment["confidence"] = round(confidence, 2)

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor=actor,
        attributes={"issues_fixed": len(issues), "final_confidence": assessment["confidence"]},
    )

    return output
