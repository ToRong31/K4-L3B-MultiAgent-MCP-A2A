from __future__ import annotations

from typing import Any

from ...core.agent_messages import Finding, WorkOrder
from ..base import Specialist
from ..domain_helpers import (
    fact,
    fetch,
    field,
    finding,
    in_snapshot,
    order_ids,
    records,
    timestamp,
)


def analyze_shipment(data: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("shipping_limits"), list):
        limits = records(data["shipping_limits"])
        rows = [
            {
                **data,
                **limit,
                "handoff_deadline": field(limit, "shipping_limit_at", "shipping_limit_date"),
                "handed_to_carrier_at": field(data, "delivered_carrier_at"),
                "delivered_at": field(data, "delivered_customer_at"),
            }
            for limit in limits
        ]
        if not rows:
            rows = [data]
    else:
        rows = records(data, "shipments", "items")
    if not rows:
        return {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        }, {"missing_fields": ["shipment"]}
    verdicts = []
    late_sellers = []
    missing = set()
    shipment_ids = []
    for row in rows:
        shipment_id = field(row, "shipment_id", "tracking_id")
        if isinstance(shipment_id, str):
            shipment_ids.append(shipment_id)
        state = str(field(row, "status", "shipment_status") or "").lower()
        if state in {"lost", "returned"}:
            verdicts.append(state)
            continue
        handoff_due = timestamp(field(row, "handoff_deadline", "shipping_limit_date"))
        handoff = timestamp(
            field(row, "handed_to_carrier_at", "carrier_handoff_at", "order_delivered_carrier_date")
        )
        delivery_due = timestamp(
            field(
                row, "delivery_deadline", "estimated_delivery_at", "order_estimated_delivery_date"
            )
        )
        delivered = timestamp(field(row, "delivered_at", "order_delivered_customer_date"))
        if not all((handoff_due, handoff, delivery_due, delivered)):
            missing.update(
                name
                for name, value in (
                    ("handoff_deadline", handoff_due),
                    ("carrier_handoff", handoff),
                    ("delivery_deadline", delivery_due),
                    ("delivery", delivered),
                )
                if value is None
            )
            verdicts.append("insufficient_evidence")
            continue
        created = timestamp(field(row, "created_at", "order_purchase_timestamp"))
        if delivered < handoff or (created is not None and handoff < created):
            verdicts.append("conflicting")
        elif delivered <= delivery_due:
            verdicts.append("on_time")
        elif handoff > handoff_due:
            verdicts.append("seller_delay")
            seller = field(row, "seller_id")
            if isinstance(seller, str):
                late_sellers.append(seller)
        else:
            verdicts.append("logistics_delay")
    distinct = set(verdicts)
    if "conflicting" in distinct or len(distinct - {"insufficient_evidence"}) > 1:
        verdict = "conflicting"
    elif "insufficient_evidence" in distinct:
        verdict = "insufficient_evidence"
    else:
        verdict = verdicts[0]
    event_types = {
        str(field(e, "event_type", "type") or "").lower()
        for e in records(data.get("events", []) if isinstance(data, dict) else [], "events")
    }
    event_conflict = ("delivered_late" in event_types and verdict == "on_time") or (
        "delivered_on_time" in event_types and verdict in {"seller_delay", "logistics_delay"}
    )
    if event_conflict:
        verdict = "conflicting"
    detail = {"shipment_ids": sorted(set(shipment_ids))[:20], "missing_fields": sorted(missing)}
    if event_conflict:
        detail["source_conflicts"] = [
            {
                "field": "shipment_analysis.verdict",
                "sources": [
                    "get_shipment_summary.delivery_timeline",
                    "get_shipment_summary.events",
                ],
                "selected_source": None,
                "resolution_code": "UNRESOLVED_TIMELINE_CONFLICT",
            }
        ]
    return {
        "verdict": verdict,
        "late_seller_ids": sorted(set(late_sellers))[:20],
        "timeline_complete": not missing and verdict != "insufficient_evidence",
    }, detail


class ShipmentAgent(Specialist):
    name = "shipment"
    description = "Reconstruct shipment timeline and delay ownership."

    async def investigate(self, work: WorkOrder) -> Finding:
        candidates = order_ids(work)
        if not candidates:
            return finding(
                work, self.name, "needs_evidence", [], ["No resolved or candidate order ID"]
            )
        facts = []
        try:
            for order_id in candidates:
                snapshot = work.input.get("snapshot")
                topics = {
                    str(claim.get("topic"))
                    for claim in (work.input.get("case") or {})
                    .get("customer_request", {})
                    .get("claims", [])
                    if isinstance(claim, dict)
                }
                shipment_claim = bool(
                    topics & {"late_delivery_seller", "late_delivery_logistics"}
                )
                response = await fetch(self, work, "get_shipment_summary", order_id=order_id)
                data = response.get("data")
                refs = [response["evidence_ref"]]
                if isinstance(snapshot, dict) and (
                    snapshot.get("order_id") == order_id
                    and isinstance(snapshot.get("order"), dict)
                ):
                    selected = snapshot["order"]
                    if shipment_claim:
                        data = {
                            **data,
                            "delivered_carrier_at": selected.get("order_delivered_carrier_date"),
                            "delivered_customer_at": selected.get("order_delivered_customer_date"),
                            "estimated_delivery_at": selected.get("order_estimated_delivery_date"),
                            "shipping_limits": [
                                row for row in data.get("shipping_limits", [])
                                if isinstance(row, dict)
                                and in_snapshot(
                                    field(row, "shipping_limit_at", "shipping_limit_date"), snapshot
                                )
                            ],
                            "events": [
                                row for row in data.get("events", [])
                                if isinstance(row, dict)
                                and in_snapshot(row.get("event_at"), snapshot)
                            ],
                        }
                    else:
                        delivered = timestamp(selected.get("order_delivered_customer_date"))
                        due = timestamp(selected.get("order_estimated_delivery_date"))
                        data = (
                            {
                                "handoff_deadline": selected.get(
                                    "order_delivered_carrier_date"
                                ),
                                "handed_to_carrier_at": selected.get(
                                    "order_delivered_carrier_date"
                                ),
                                "delivery_deadline": selected.get(
                                    "order_estimated_delivery_date"
                                ),
                                "delivered_at": selected.get(
                                    "order_delivered_customer_date"
                                ),
                            }
                            if delivered is not None and due is not None and delivered <= due
                            else {}
                        )
                    refs.append(snapshot["evidence_ref"])
                analysis, detail = analyze_shipment(data)
                facts.extend(
                    (
                        fact("shipment_analysis", analysis, refs),
                        fact("shipment_timeline", {"order_id": order_id, **detail}, refs),
                    )
                )
                if detail.get("source_conflicts"):
                    facts.append(
                        fact("source_conflicts", {"items": detail["source_conflicts"]}, refs)
                    )
        except Exception as exc:
            return finding(work, self.name, "failed", facts, [f"Shipment MCP error: {exc}"])
        return finding(work, self.name, "completed", facts)
