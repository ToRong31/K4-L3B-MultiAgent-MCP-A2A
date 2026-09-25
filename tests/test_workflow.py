from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import MCPToolError
from student_agent.trace import TraceWriter
from student_agent.verification import VerificationError, verify_output
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self, contracts: Contracts) -> None:
        self.contracts = contracts
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        order_id = arguments.get("order_id", "order-1")
        payloads = {
            "get_order": {"order_id": order_id, "order_status": "delivered"},
            "get_order_items": {
                "items": [
                    {
                        "order_item_id": "1",
                        "seller_id": "seller-1",
                        "price": "90.00",
                        "freight_value": "10.00",
                    }
                ]
            },
            "get_product_context": {"products": [{"product_id": "product-1"}]},
            "get_shipment_summary": {
                "delivered_carrier_at": "2018-01-03T00:00:00Z",
                "delivered_customer_at": "2018-01-08T00:00:00Z",
                "estimated_delivery_at": "2018-01-10T00:00:00Z",
                "shipping_limits": [{"shipping_limit_at": "2018-01-02T00:00:00Z"}],
                "events": [
                    {
                        "event_type": "delivered_late",
                        "actor": "logistics_provider",
                        "status": "confirmed",
                    }
                ],
                "shipment_id": "shipment-1",
            },
            "get_order_payments": {
                "payments": [
                    {"payment_value": "84.00", "payment_id": "payment-1"},
                    {"payment_value": "16.00", "payment_id": "payment-2"},
                ]
            },
            "get_payment_timeline": {
                "events": [
                    {"event_type": "captured", "amount_brl": "84.00"},
                    {"event_type": "captured", "amount_brl": "16.00"},
                ]
            },
            "get_refund_timeline": {"events": []},
            "get_policy": {
                "policy_version": "EC_POLICY_V2",
                "rules": {
                    "late_delivery_logistics": {
                        "case_status": "action_required",
                        "recommended_action": "refund_freight",
                        "refund_brl": 16.0,
                        "responsible_parties": [
                            {"party_type": "logistics_provider", "party_id": None}
                        ],
                    }
                },
            },
            "get_customer_history": {
                "customer_unique_id": "customer-1",
                "orders": [{"order_id": order_id}],
            },
        }
        data = payloads[tool_name]
        digest = hashlib.sha256(f"{tool_name}:{case_id}".encode()).hexdigest()
        ref_suffix = hashlib.sha256(tool_name.encode()).hexdigest()[:24]
        domain = {
            "get_order": "order",
            "get_order_items": "item",
            "get_product_context": "product",
            "get_shipment_summary": "shipment",
            "get_order_payments": "payment",
            "get_payment_timeline": "payment",
            "get_refund_timeline": "refund",
            "get_policy": "policy",
            "get_customer_history": "customer",
        }[tool_name]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{ref_suffix}",
            "result_hash": f"sha256:{digest}",
            "domain": domain,
            "data": data,
        }


def test_workflow_produces_verified_schema_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    gateway = FakeGateway(contracts)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    case = {
        "case_id": "CASE_001",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-1", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
        "candidate_order_ids": ["order-1", "candidate-1"],
        "customer_unique_id_hint": "customer-1",
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {"include_customer_history": True},
    }

    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]

    contracts.validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-1"]
    assert len(gateway.calls) == 8
    events = [
        json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines() if line
    ]
    event_types = {event["event_type"] for event in events}
    assert {
        "task_assigned",
        "handoff",
        "tool_result_consumed",
        "policy_decided",
        "verification_completed",
    }.issubset(event_types)

    class LedgerView:
        consumed_refs = output["evidence_refs"]

    output["financial_resolution"]["recommended_refund_brl"] = 99.0
    with pytest.raises(VerificationError, match="refund lines"):
        verify_output(
            output,
            case=case,
            ledger=LedgerView(),  # type: ignore[arg-type]
            contracts=contracts,
        )


def test_mcp_infrastructure_error_cannot_become_empty_evidence_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    class BrokenGateway(FakeGateway):
        async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
            raise MCPToolError("MCP tool get_order failed: Error executing tool", retryable=False)

    case = {
        "case_id": "CASE_001",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-1", "topic": "late_delivery_logistics"}],
        },
        "candidate_order_ids": ["order-1"],
        "policy_version": "EC_POLICY_V2",
    }
    with pytest.raises(MCPToolError, match="Error executing tool"):
        asyncio.run(solve_case(case, BrokenGateway(contracts), trace))  # type: ignore[arg-type]
