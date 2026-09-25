from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append(tool_name)
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name}_abcdefghijklmnopqrstuv",
            "result_hash": "sha256:" + "a" * 64,
            "domain": "order",
            "data": self.payloads[tool_name],
        }


def make_case(topic: str) -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_TEST",
        "customer_request": {
            "claimed_order_id": "order-1",
            "message": "investigate",
            "claims": [
                {"claim_id": "claim-primary", "topic": topic},
                {"claim_id": "claim-refund", "topic": "requested_full_refund"},
            ],
        },
        "candidate_order_ids": ["order-1", "candidate-fake"],
        "customer_unique_id_hint": "customer-1",
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
            "require_independent_verification": True,
        },
    }


def base_payloads() -> dict[str, Any]:
    return {
        "get_order": {"order": {"order_id": "order-1", "order_status": "delivered"}},
        "get_customer_history": {
            "orders": [{"order_id": "order-1"}],
            "customer_unique_id": "customer-1",
        },
        "get_order_items": {
            "items": [
                {
                    "order_item_id": "item-1",
                    "seller_id": "seller-1",
                    "product_id": "product-1",
                    "price": "100.00",
                    "freight_value": "10.00",
                }
            ]
        },
        "get_product_context": {"products": [{"product_id": "product-1"}]},
        "get_shipment_summary": {
            "summary": {
                "order_status": "delivered",
                "order_delivered_carrier_date": "2018-01-02T09:00:00",
                "shipping_limit_date": "2018-01-03T09:00:00",
                "order_delivered_customer_date": "2018-01-12T09:00:00",
                "order_estimated_delivery_date": "2018-01-10T09:00:00",
            }
        },
        "get_order_payments": {"payments": [{"payment_sequential": 1, "payment_value": "110.00"}]},
        "get_payment_timeline": {"events": []},
        "get_refund_timeline": {"refunds": []},
        "get_policy": {"version": "EC_POLICY_V2"},
    }


def trace_writer(tmp_path: Path) -> TraceWriter:
    root = Path(__file__).resolve().parents[1]
    return TraceWriter(tmp_path / "trace.jsonl", Contracts(root / "contracts" / "schemas"))


def test_nested_string_evidence_produces_late_logistics_decision(tmp_path: Path) -> None:
    gateway = FakeGateway(base_payloads())
    output = asyncio.run(
        solve_case(make_case("late_delivery_logistics"), gateway, trace_writer(tmp_path))
    )

    assert output["shipment_analysis"]["verdict"] == "logistics_delay"
    assert output["payment_analysis"]["captured_total_brl"] == 110.0
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
    assert "get_sellers" not in gateway.calls
    assert gateway.calls == [
        "get_order",
        "get_customer_history",
        "get_order_items",
        "get_product_context",
        "get_shipment_summary",
        "get_order_payments",
        "get_policy",
    ]


def test_duplicate_capture_refunds_only_the_overcharge(tmp_path: Path) -> None:
    payloads = base_payloads()
    payloads["get_shipment_summary"]["summary"]["order_delivered_customer_date"] = (
        "2018-01-08T09:00:00"
    )
    payloads["get_order_payments"] = {
        "payments": [
            {"payment_id": "pay-1", "payment_value": "110.00"},
            {"payment_id": "pay-2", "payment_value": "110.00"},
        ]
    }
    payloads["get_payment_timeline"] = {
        "events": [{"event_type": "duplicate_capture", "status": "confirmed"}]
    }
    gateway = FakeGateway(payloads)

    output = asyncio.run(solve_case(make_case("duplicate_charge"), gateway, trace_writer(tmp_path)))

    assert output["payment_analysis"]["verdict"] == "duplicate_capture"
    assert output["payment_analysis"]["captured_total_brl"] == 220.0
    assert output["payment_analysis"]["refundable_total_brl"] == 110.0
    assert output["assessment"]["primary_issue"] == "duplicate_charge"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] == 110.0
    assert output["claim_assessments"][1]["verdict"] == "partially_supported"
    assert "get_payment_timeline" in gateway.calls
    assert "get_refund_timeline" not in gateway.calls


def test_refund_case_uses_only_authoritative_refund_timeline(tmp_path: Path) -> None:
    payloads = base_payloads()
    payloads["get_refund_timeline"] = {
        "refunds": [{"refund_status": "pending", "amount": "110.00"}]
    }
    gateway = FakeGateway(payloads)

    output = asyncio.run(solve_case(make_case("refund_pending"), gateway, trace_writer(tmp_path)))

    assert output["payment_analysis"]["verdict"] == "refund_pending"
    assert output["payment_analysis"]["refunded_total_brl"] == 0.0
    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert output["payment_analysis"]["refundable_total_brl"] == 110.0
    assert output["claim_assessments"][1]["verdict"] == "supported"
    assert "get_refund_timeline" in gateway.calls
    assert "get_payment_timeline" not in gateway.calls
    assert len(gateway.calls) == 8


def test_valid_split_payment_does_not_call_redundant_timeline(tmp_path: Path) -> None:
    payloads = base_payloads()
    payloads["get_order_payments"] = {
        "payments": [
            {"payment_id": "pay-1", "payment_value": "55.00"},
            {"payment_id": "pay-2", "payment_value": "55.00"},
        ]
    }
    gateway = FakeGateway(payloads)

    output = asyncio.run(
        solve_case(make_case("valid_split_payment"), gateway, trace_writer(tmp_path))
    )

    assert output["assessment"]["primary_issue"] == "valid_split_payment"
    assert output["assessment"]["case_status"] == "no_action"
    assert output["payment_analysis"]["captured_total_brl"] == 110.0
    assert output["payment_analysis"]["refundable_total_brl"] == 0.0
    assert "get_payment_timeline" not in gateway.calls
    assert len(gateway.calls) == 7


def test_shipment_source_wins_and_conflict_is_reported(tmp_path: Path) -> None:
    payloads = base_payloads()
    payloads["get_order"]["order"]["order_delivered_customer_date"] = "2018-01-09T09:00:00"
    gateway = FakeGateway(payloads)

    output = asyncio.run(
        solve_case(make_case("late_delivery_logistics"), gateway, trace_writer(tmp_path))
    )

    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["confidence"] == 0.8
    assert output["data_conflicts"] == [
        {
            "field": "delivered_at",
            "sources": ["get_order", "get_shipment_summary"],
            "selected_source": "get_shipment_summary",
            "resolution_code": "authoritative_shipment_precedence",
        }
    ]


def test_canceled_paid_order_does_not_query_missing_refund_record(tmp_path: Path) -> None:
    payloads = base_payloads()
    payloads["get_order"]["order"]["order_status"] = "canceled"
    gateway = FakeGateway(payloads)

    output = asyncio.run(
        solve_case(make_case("canceled_order_paid"), gateway, trace_writer(tmp_path))
    )

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 110.0
    assert output["shipment_analysis"]["verdict"] == "returned"
    assert "get_refund_timeline" not in gateway.calls
    assert len(gateway.calls) == 7


def test_topic_specific_verdicts_are_normalized_when_evidence_exists(tmp_path: Path) -> None:
    payloads = base_payloads()
    payloads["get_payment_timeline"] = {"events": [{"event_type": "captured"}]}
    gateway = FakeGateway(payloads)

    output = asyncio.run(solve_case(make_case("payment_mismatch"), gateway, trace_writer(tmp_path)))

    assert output["payment_analysis"]["verdict"] == "capture_mismatch"
    assert output["payment_analysis"]["refundable_total_brl"] == 0.0
