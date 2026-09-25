"""Evidence-backed resolution of the customer and candidate orders."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from .evidence import EvidenceCollector
from .memory import AgentMemory


async def resolve_case_context(
    case: dict[str, Any], evidence: EvidenceCollector, memory: AgentMemory
) -> dict[str, Any]:
    """Resolve only when order evidence and customer history agree on an ID.

    The caller may pass the returned refs in WorkOrder.evidence_refs. Recipients
    retrieve their contents with memory.get_evidence(case_id, ref) in the same run.
    """
    case_id = case["case_id"]
    turn_id = uuid4().hex
    questions: list[str] = []
    refs: list[str] = []
    candidates = list(dict.fromkeys(case.get("candidate_order_ids") or []))
    claimed = case.get("customer_request", {}).get("claimed_order_id")
    if isinstance(claimed, str) and claimed and claimed not in candidates:
        candidates.append(claimed)
    hint = case.get("customer_unique_id_hint")
    customer_id: str | None = None
    history_orders: set[str] = set()
    history_rows: dict[str, set[str]] = {}
    history_records: dict[str, list[dict[str, Any]]] = {}
    history_ref: str | None = None
    schema = await evidence.tool_schema("get_customer_history")
    if isinstance(hint, str) and hint and "customer_unique_id" in schema.get("properties", {}):
        history = await evidence.call(
            "orchestrator",
            "get_customer_history",
            case_id=case_id,
            turn_id=turn_id,
            customer_unique_id=hint,
        )
        refs.append(history["evidence_ref"])
        history_ref = history["evidence_ref"]
        data = history.get("data")
        if isinstance(data, dict) and data.get("customer_unique_id") == hint:
            customer_id = hint
            for row in data.get("orders", []):
                if isinstance(row, dict) and isinstance(row.get("order_id"), str):
                    order_id = row["order_id"]
                    history_orders.add(order_id)
                    history_records.setdefault(order_id, []).append(row)
                    if isinstance(row.get("customer_id"), str):
                        history_rows.setdefault(order_id, set()).add(row["customer_id"])
        else:
            questions.append("Customer hint was not confirmed by customer history.")
    else:
        questions.append("Customer history cannot be queried with a discovered input parameter.")

    confirmed: list[str] = []
    rejected: list[str] = []
    order_schema = await evidence.tool_schema("get_order")
    if "order_id" not in order_schema.get("properties", {}):
        questions.append("get_order has no discovered order_id parameter.")
    else:
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate:
                continue
            if customer_id is not None and candidate not in history_orders:
                rejected.append(candidate)
                continue
            try:
                result = await evidence.call(
                    "orchestrator",
                    "get_order",
                    case_id=case_id,
                    turn_id=turn_id,
                    order_id=candidate,
                )
            except RuntimeError as exc:
                if not str(exc).startswith("MCP tool get_order failed:"):
                    raise
                if customer_id and candidate not in history_orders:
                    rejected.append(candidate)
                else:
                    questions.append(f"Order lookup for {candidate} failed.")
                continue
            refs.append(result["evidence_ref"])
            data = result.get("data")
            valid_order = (
                result.get("domain") == "order"
                and isinstance(data, dict)
                and data.get("order_id") == candidate
            )
            row_id = data.get("customer_id") if isinstance(data, dict) else None
            linked = (
                customer_id is not None
                and candidate in history_orders
                and isinstance(row_id, str)
                and row_id in history_rows.get(candidate, set())
            )
            if valid_order and linked:
                confirmed.append(candidate)
            elif valid_order and customer_id is None:
                questions.append(f"Customer link for {candidate} is unverified.")
            else:
                rejected.append(candidate)

    status = "resolved" if len(confirmed) == 1 else "ambiguous" if confirmed else "not_found"
    if status != "resolved":
        questions.append(
            "A unique order was not established by matching order and customer evidence."
        )
    snapshot: dict[str, Any] | None = None
    if status == "resolved" and history_ref:
        opened_at = case.get("opened_at")
        try:
            opened = datetime.fromisoformat(opened_at)
        except (TypeError, ValueError):
            opened = None
        dated: list[tuple[datetime, dict[str, Any]]] = []
        for row in history_records.get(confirmed[0], []):
            try:
                purchased = datetime.fromisoformat(row["order_purchase_timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            if opened is not None and purchased <= opened:
                dated.append((purchased, row))
        if dated:
            latest = max(purchased for purchased, _ in dated)
            matching = {
                json.dumps(row, sort_keys=True)
                for purchased, row in dated if purchased == latest
            }
            if len(matching) == 1:
                selected = json.loads(matching.pop())
                snapshot = {
                    "order_id": confirmed[0],
                    "purchase_at": selected["order_purchase_timestamp"],
                    "opened_at": opened_at,
                    "order": selected,
                    "evidence_ref": history_ref,
                }
            else:
                questions.append(
                    "Customer history has conflicting rows at the latest purchase time."
                )
        else:
            questions.append("Customer history has no order purchase before the case opened.")
    context = {
        "entity_resolution": {
            "status": status,
            "resolved_order_ids": confirmed if status == "resolved" else [],
            "rejected_candidates": rejected,
            "confidence": 0.95 if status == "resolved" else 0.3 if confirmed else 0.0,
        },
        "customer_context": {
            "customer_unique_id": customer_id,
            "related_order_ids": sorted(history_orders) if customer_id else [],
        },
    }
    memory.append(case_id, "orchestrator", turn_id, "entity_resolution", context)
    return {
        "context": context,
        "evidence_refs": list(dict.fromkeys(refs)),
        "open_questions": questions,
        "snapshot": snapshot,
    }
