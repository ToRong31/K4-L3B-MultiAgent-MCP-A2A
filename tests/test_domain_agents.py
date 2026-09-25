from __future__ import annotations

import asyncio

import pytest

from student_agent.agents.order_item.agent import analyze_order
from student_agent.agents.payment.agent import PaymentAgent, analyze_payment
from student_agent.agents.shipment.agent import analyze_shipment
from student_agent.core.agent_messages import WorkOrder


def test_split_payment_and_duplicate_capture() -> None:
    split = {
        "events": [
            {"event_id": "c1", "payment_reference": "p1", "amount_brl": "40.00", "type": "capture"},
            {"event_id": "c2", "payment_reference": "p2", "amount_brl": "60.00", "type": "capture"},
        ]
    }
    result, detail = analyze_payment({}, split, {"refunds": []})
    assert result["verdict"] == "reconciled"
    assert result["captured_total_brl"] == 100.0
    assert detail["duplicate_capture_ids"] == []
    duplicate = {
        "events": [
            split["events"][0],
            {"event_id": "c3", "payment_reference": "p1", "amount_brl": "40.00", "type": "capture"},
        ]
    }
    result, detail = analyze_payment({}, duplicate, [])
    assert result["verdict"] == "duplicate_capture"
    assert detail["duplicate_capture_ids"] == ["c1", "c3"]
    repeated = {"events": [split["events"][0], split["events"][0]]}
    result, _ = analyze_payment({}, repeated, [])
    assert result["captured_total_brl"] == 40.0


def test_partial_pending_failed_refunds() -> None:
    captures = {
        "events": [
            {"event_id": "c1", "payment_reference": "p1", "amount_brl": "100.00", "type": "capture"}
        ]
    }
    partial = {"refunds": [{"refund_id": "r1", "status": "completed", "amount_brl": "25.00"}]}
    result, _ = analyze_payment({}, captures, partial)
    assert result["refunded_total_brl"] == 25.0
    assert result["verdict"] == "reconciled"
    for state, verdict in (("pending", "refund_pending"), ("failed", "refund_failed")):
        refunds = {
            "refunds": [
                *partial["refunds"],
                {"refund_id": "r2", "status": state, "amount_brl": "75.00"},
            ]
        }
        result, _ = analyze_payment({}, captures, refunds)
        assert result["verdict"] == verdict
        assert result["refunded_total_brl"] == 25.0


def test_refund_timeline_latest_status_is_order_independent() -> None:
    captures = {
        "events": [
            {"event_id": "c1", "payment_reference": "p1", "amount_brl": "100.00", "type": "capture"}
        ]
    }
    pending = {
        "refund_id": "r1",
        "event_id": "e1",
        "status": "pending",
        "amount_brl": "20.00",
        "event_at": "2018-01-01T10:00:00-03:00",
    }
    completed = {
        "refund_id": "r1",
        "event_id": "e2",
        "status": "completed",
        "amount_brl": "20.00",
        "event_at": "2018-01-02T10:00:00-03:00",
    }
    for events in ([pending, completed], [completed, pending], [completed, pending, completed]):
        result, detail = analyze_payment({}, captures, {"events": events})
        assert result["verdict"] == "reconciled"
        assert result["refunded_total_brl"] == 20.0
        assert detail["refund_ids"] == ["r1"]
        assert detail["refund_event_ids"] == ["e1", "e2"]
        assert detail["refund_statuses"] == ["completed"]


def test_refund_status_or_amount_conflict_is_unknown() -> None:
    captures = {
        "events": [
            {"event_id": "c1", "payment_reference": "p1", "amount_brl": "100.00", "type": "capture"}
        ]
    }
    pending = {"refund_id": "r1", "status": "pending", "amount_brl": "20.00"}
    completed = {"refund_id": "r1", "status": "completed", "amount_brl": "20.00"}
    for events in ([pending, completed], [completed, pending]):
        result, _ = analyze_payment({}, captures, {"events": events})
        assert result["verdict"] == "insufficient_evidence"
        assert result["refunded_total_brl"] is None
    amount_change = [
        {**pending, "event_at": "2018-01-01T10:00:00-03:00"},
        {**completed, "amount_brl": "25.00", "event_at": "2018-01-02T10:00:00-03:00"},
    ]
    result, _ = analyze_payment({}, captures, {"events": amount_change})
    assert result["verdict"] == "insufficient_evidence"
    assert result["refunded_total_brl"] is None
    same_time = [
        {**pending, "event_at": "2018-01-01T10:00:00-03:00"},
        {**completed, "event_at": "2018-01-01T10:00:00-03:00"},
    ]
    result, _ = analyze_payment({}, captures, {"events": same_time})
    assert result["verdict"] == "insufficient_evidence"
    assert result["refunded_total_brl"] is None
    no_refund_id = [{"event_id": "e1", "status": "completed", "amount_brl": "20.00"}]
    result, _ = analyze_payment({}, captures, {"events": no_refund_id})
    assert result["refunded_total_brl"] is None


def test_multiple_refunds_partial_total_counts_each_refund_once() -> None:
    captures = {
        "events": [
            {"event_id": "c1", "payment_reference": "p1", "amount_brl": "100.00", "type": "capture"}
        ]
    }
    events = [
        {"refund_id": "r1", "event_id": "e1", "status": "completed", "amount_brl": "20.00"},
        {"refund_id": "r1", "event_id": "e1", "status": "completed", "amount_brl": "20.00"},
        {"refund_id": "r2", "event_id": "e2", "status": "completed", "amount_brl": "30.00"},
        {"refund_id": "r3", "event_id": "e3", "status": "pending", "amount_brl": "10.00"},
    ]
    result, detail = analyze_payment({}, captures, {"events": events})
    assert result["verdict"] == "refund_pending"
    assert result["refunded_total_brl"] == 50.0
    assert detail["refund_ids"] == ["r1", "r2", "r3"]


def test_seller_and_logistics_delay_and_missing() -> None:
    base = {
        "shipment_id": "s1",
        "seller_id": "seller1",
        "handoff_deadline": "2018-01-02T00:00:00-03:00",
        "delivery_deadline": "2018-01-05T00:00:00-03:00",
        "delivered_at": "2018-01-06T00:00:00-03:00",
    }
    result, _ = analyze_shipment({**base, "handed_to_carrier_at": "2018-01-03T00:00:00-03:00"})
    assert result["verdict"] == "seller_delay"
    assert result["late_seller_ids"] == ["seller1"]
    result, _ = analyze_shipment({**base, "handed_to_carrier_at": "2018-01-02T00:00:00-03:00"})
    assert result["verdict"] == "logistics_delay"
    result, detail = analyze_shipment({"shipment_id": "s1"})
    assert result["verdict"] == "insufficient_evidence"
    assert "delivery" in detail["missing_fields"]


def test_order_rejects_other_order_items() -> None:
    entities, detail = analyze_order(
        "o1",
        {"order_id": "o1", "status": "canceled"},
        {
            "items": [
                {"order_id": "o1", "item_id": "i1", "seller_id": "s1"},
                {"order_id": "o2", "item_id": "i2", "seller_id": "s2"},
            ]
        },
    )
    assert entities["item_ids"] == ["i1"]
    assert detail["conflicting_order_ids"] == ["o2"]


def test_conflicting_item_and_shipment_event() -> None:
    _, detail = analyze_order(
        "o1",
        {"order_id": "o1"},
        {
            "items": [
                {"order_id": "o1", "order_item_id": "i1", "shipping_limit_date": "2018-01-01"},
                {"order_id": "o1", "order_item_id": "i1", "shipping_limit_date": "2018-01-02"},
            ]
        },
    )
    assert detail["conflicting_item_ids"] == ["i1"]
    shipment = {
        "shipping_limits": [{"shipping_limit_at": "2018-01-02T00:00:00-03:00"}],
        "delivered_carrier_at": "2018-01-02T00:00:00-03:00",
        "delivered_customer_at": "2018-01-04T00:00:00-03:00",
        "estimated_delivery_at": "2018-01-05T00:00:00-03:00",
        "events": [{"event_type": "delivered_late"}],
    }
    result, _ = analyze_shipment(shipment)
    assert result["verdict"] == "conflicting"


def test_tool_error_is_failed() -> None:
    class Broken:
        async def call(self, *_args, **_kwargs):
            raise RuntimeError("tool unavailable")

        def tool_uses(self, *_args):
            return []

    agent = PaymentAgent(memory=None, evidence=Broken())
    work = WorkOrder("CASE_001", "payment", "task1", {"case": {"candidate_order_ids": ["o1"]}})
    result = asyncio.run(agent.investigate(work))
    assert result.status == "failed"


@pytest.mark.parametrize("embedded", [True, False])
def test_payment_uses_embedded_ledger_with_legacy_fallback(embedded: bool) -> None:
    ledger = [{"payment_type": "credit_card", "payment_sequential": "1", "payment_value": "30.00"}]
    timeline = {"events": [{"event_id": "c1", "amount_brl": "30.00", "type": "capture"}]}
    if embedded:
        timeline["payments"] = ledger

    class Evidence:
        def __init__(self):
            self.calls = []

        async def call(self, _agent, tool, **_kwargs):
            self.calls.append(tool)
            return {
                "evidence_ref": "ev_" + ("t" if tool == "get_payment_timeline" else "p") * 24,
                "data": timeline if tool == "get_payment_timeline" else ledger,
            }

    evidence = Evidence()
    work = WorkOrder("CASE_001", "payment", "task1", {"case": {"candidate_order_ids": ["o1"]}})
    result = asyncio.run(PaymentAgent(None, evidence=evidence).investigate(work))
    assert result.status == "completed"
    assert evidence.calls == (
        ["get_payment_timeline"] if embedded else ["get_payment_timeline", "get_order_payments"]
    )
    assert next(f["data"] for f in result.facts if f["kind"] == "payment_analysis")[
        "captured_total_brl"
    ] == 30
    assert len(result.evidence_refs) == (1 if embedded else 2)


def test_payment_parser_uses_ledger_when_timeline_omits_payments() -> None:
    snapshot = {"purchase_at": "2018-01-01T09:00:00Z", "opened_at": "2018-01-02T09:00:00Z"}
    ledger = [{"payment_type": "credit_card", "payment_sequential": "1", "payment_value": "30.00"}]
    timeline = {
        "events": [
            {"event_at": "2018-01-01T10:00:00Z", "amount_brl": "30.00", "type": "capture"}
        ]
    }
    _, detail = analyze_payment(ledger, timeline, [], snapshot)
    assert detail["payment_references"] == ["credit_card:1"]
