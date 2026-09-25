from __future__ import annotations

from typing import Any

from ...core.agent_messages import Finding, WorkOrder
from ..base import Specialist
from ..domain_helpers import fact, fetch, field, finding, in_snapshot, order_ids, records


def analyze_order(
    order_id: str, order: Any, items: Any, snapshot: dict[str, Any] | None = None
) -> tuple[dict, dict]:
    selected = snapshot.get("order") if snapshot and snapshot.get("order_id") == order_id else None
    order_rows = [selected] if isinstance(selected, dict) else records(order, "orders")
    item_rows = records(items, "items", "order_items")
    matching = [r for r in order_rows if field(r, "order_id") == order_id]
    valid_items = [
        r for r in item_rows
        if field(r, "order_id") in (None, order_id)
        and in_snapshot(field(r, "shipping_limit_date", "shipping_limit_at"), snapshot)
    ]
    item_ids = [field(r, "item_id", "order_item_id") for r in valid_items]
    seller_ids = [field(r, "seller_id") for r in valid_items]
    entities = {
        "order_ids": [order_id] if matching else [],
        "item_ids": sorted({x for x in item_ids if isinstance(x, str)})[:20],
        "seller_ids": sorted({x for x in seller_ids if isinstance(x, str)})[:20],
        "payment_references": [],
        "shipment_ids": [],
    }
    state = str(field(matching[0], "order_status", "status") or "").lower() if matching else ""
    detail = {
        "order_id": order_id,
        "order_status": state or None,
        "order_found": bool(matching),
        "item_count": len(valid_items),
        "conflicting_order_ids": sorted(
            {
                str(field(r, "order_id"))
                for r in item_rows
                if field(r, "order_id") not in (None, order_id)
            }
        ),
    }
    by_item: dict[str, set[tuple[str, str, str]]] = {}
    for row in valid_items:
        item_id = field(row, "item_id", "order_item_id")
        if isinstance(item_id, str):
            signature = (
                str(field(row, "seller_id")),
                str(field(row, "price")),
                str(field(row, "shipping_limit_date", "shipping_limit_at")),
            )
            by_item.setdefault(item_id, set()).add(signature)
    detail["conflicting_item_ids"] = sorted(
        item_id for item_id, signatures in by_item.items() if len(signatures) > 1
    )
    return entities, detail


class OrderItemAgent(Specialist):
    name = "order"
    description = "Resolve order items, sellers, products and order-level facts."

    async def investigate(self, work: WorkOrder) -> Finding:
        candidates = order_ids(work)
        if not candidates:
            return finding(
                work, self.name, "needs_evidence", [], ["No resolved or candidate order ID"]
            )
        facts = []
        try:
            for order_id in candidates:
                order = await fetch(self, work, "get_order", order_id=order_id)
                items = await fetch(self, work, "get_order_items", order_id=order_id)
                refs = [order["evidence_ref"], items["evidence_ref"]]
                snapshot = work.input.get("snapshot")
                if isinstance(snapshot, dict) and snapshot.get("order_id") == order_id:
                    refs.append(snapshot["evidence_ref"])
                entities, detail = analyze_order(
                    order_id, order.get("data"), items.get("data"), snapshot
                )
                facts.extend(
                    (fact("affected_entities", entities, refs), fact("order_state", detail, refs))
                )
                if detail["conflicting_order_ids"]:
                    facts.append(
                        fact(
                            "source_conflicts",
                            {
                                "items": [
                                    {
                                        "field": "affected_entities.order_ids",
                                        "sources": [
                                            "get_order.order_id",
                                            "get_order_items.items.order_id",
                                        ],
                                        "selected_source": None,
                                        "resolution_code": "UNRESOLVED_ORDER_ID_CONFLICT",
                                    }
                                ]
                            },
                            refs,
                        )
                    )
                if not entities["order_ids"]:
                    continue
                if (
                    (work.input.get("case") or {})
                    .get("investigation_scope", {})
                    .get("include_product_context", False)
                ):
                    product = await fetch(self, work, "get_product_context", order_id=order_id)
                    facts.append(
                        fact(
                            "product_context",
                            {"order_id": order_id, "data": product.get("data")},
                            [product["evidence_ref"]],
                        )
                    )
                if entities["seller_ids"]:
                    sellers = await fetch(self, work, "get_sellers", order_id=order_id)
                    facts.append(
                        fact(
                            "seller_context",
                            {"order_id": order_id, "data": sellers.get("data")},
                            [sellers["evidence_ref"]],
                        )
                    )
        except Exception as exc:
            return finding(work, self.name, "failed", facts, [f"Order MCP error: {exc}"])
        return finding(work, self.name, "completed", facts)
