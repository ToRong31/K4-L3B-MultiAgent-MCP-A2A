"""L3B coordinator workflow: orchestrates specialist agents to investigate e-commerce disputes."""

from __future__ import annotations

import logging
from typing import Any

from .agents import (
    EvidenceCache,
    run_entity_agent,
    run_order_agent,
    run_payment_agent,
    run_policy_agent,
    run_shipment_agent,
    run_verifier_agent,
)
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3B coordinator and specialist-agent workflow.

    Pipeline:
      1. Entity Resolution Agent  → resolve candidate_order_ids, customer context
      2. Order Agent              → collect order/items/sellers
      3. Shipment Agent           → analyze delivery timeline
      4. Payment Agent            → reconcile payments/refunds
      5. Policy Agent             → deterministic issue, responsibility, financials
      6. Verifier Agent           → cross-field consistency + confidence calibration
    """
    case_id = case["case_id"]
    cache = EvidenceCache()

    # ── Phase 1: Entity Resolution ──
    entity_result = await run_entity_agent(case, gateway, trace, cache)
    resolved_order_ids = entity_result.get("resolved_order_ids", [])

    # ── Phase 2: Order & Product Investigation ──
    order_result = await run_order_agent(case, gateway, trace, cache, resolved_order_ids)
    entities = order_result.get(
        "affected_entities",
        {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
    )

    # ── Phase 3: Shipment Analysis ──
    shipment_result = await run_shipment_agent(
        case,
        gateway,
        trace,
        cache,
        resolved_order_ids,
        entities,
    )

    # ── Phase 4: Payment Analysis ──
    payment_result = await run_payment_agent(
        case,
        gateway,
        trace,
        cache,
        resolved_order_ids,
        entities,
    )

    # ── Phase 5: Deterministic policy decision ──
    policy_result = await run_policy_agent(
        case,
        gateway,
        trace,
        cache,
        entity_result,
        shipment_result,
        payment_result,
        entities,
    )

    # ── Assemble Output ──
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": policy_result["assessment"],
        "affected_entities": entities,
        "entity_resolution": entity_result["entity_resolution"],
        "customer_context": entity_result["customer_context"],
        "shipment_analysis": shipment_result["shipment_analysis"],
        "payment_analysis": payment_result["payment_analysis"],
        "root_cause_analysis": policy_result["root_cause_analysis"],
        "evidence_refs": cache.all_refs[:30],
        "data_conflicts": policy_result.get("data_conflicts", []),
        "financial_resolution": policy_result["financial_resolution"],
        "resolution_actions": policy_result["resolution_actions"],
    }

    # Optional: claim_assessments
    claim_assessments = policy_result.get("claim_assessments", [])
    if claim_assessments:
        output["claim_assessments"] = claim_assessments

    # ── Phase 6: Verification ──
    output = await run_verifier_agent(case, trace, output, cache)

    return output
