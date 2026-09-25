from __future__ import annotations

from decimal import Decimal
from typing import Any

from ...core.agent_messages import Finding, WorkOrder
from ..base import Specialist
from ..domain_helpers import (
    fact,
    fetch,
    field,
    finding,
    in_snapshot,
    money,
    order_ids,
    records,
    status,
    timestamp,
)

COMPLETED_REFUND = {"completed", "succeeded", "success", "refunded", "settled"}
PENDING_REFUND = {"pending", "processing", "initiated"}
FAILED_REFUND = {"failed", "rejected", "error"}


def _refund_state(refunds: Any) -> tuple[Decimal | None, set[str], list[str], list[str]]:
    """Select each refund's latest *timestamped* state without trusting array order.

    refund_id identifies a refund; event_id identifies one lifecycle update. An
    event ID cannot substitute for a refund ID, because several updates can
    describe the same refund. Ambiguous histories make the total unknown.
    """
    rows = records(refunds, "refunds", "events", "refund_timeline")
    by_refund: dict[str, list[dict[str, Any]]] = {}
    events: dict[str, dict[str, Any]] = {}
    unknown = False
    for row in rows:
        refund_id = field(row, "refund_id", "refund_reference", "refund_transaction_id")
        event_id = field(row, "event_id", "refund_event_id")
        if not isinstance(refund_id, str) or not refund_id:
            unknown = True
            continue
        if isinstance(event_id, str) and event_id:
            previous = events.get(event_id)
            if previous is not None:
                if previous != row:
                    unknown = True
                continue
            events[event_id] = row
        if row not in by_refund.setdefault(refund_id, []):
            by_refund[refund_id].append(row)
    total = Decimal(0)
    current_statuses: set[str] = set()
    for history in by_refund.values():
        amounts = {money(field(row, "amount_brl", "amount", "value")) for row in history}
        known_amounts = amounts - {None}
        if len(known_amounts) > 1:
            unknown = True
            continue
        if len(history) == 1:
            latest = history[0]
        else:
            dated = [
                (timestamp(field(row, "event_at", "occurred_at", "updated_at")), row)
                for row in history
            ]
            if any(when is None for when, _ in dated):
                unknown = True
                continue
            latest_time = max(when for when, _ in dated)
            latest_rows = [row for when, row in dated if when == latest_time]
            if (
                len(
                    {
                        (status(row), money(field(row, "amount_brl", "amount", "value")))
                        for row in latest_rows
                    }
                )
                > 1
            ):
                unknown = True
                continue
            latest = latest_rows[0]
        current = status(latest)
        if current not in COMPLETED_REFUND | PENDING_REFUND | FAILED_REFUND:
            unknown = True
            continue
        current_statuses.add(current)
        if current in COMPLETED_REFUND:
            amount = money(field(latest, "amount_brl", "amount", "value"))
            if amount is None:
                unknown = True
            else:
                total += amount
    return (None if unknown else total), current_statuses, sorted(by_refund), sorted(events)


def _snapshot_refund_state(
    refunds: Any, snapshot: dict[str, Any]
) -> tuple[Decimal | None, set[str], list[str], list[str]]:
    if refunds is None:
        return None, set(), [], []
    rows = [
        row for row in records(refunds, "refunds", "events", "refund_timeline")
        if in_snapshot(field(row, "event_at", "occurred_at", "updated_at"), snapshot)
    ]
    if not rows:
        return Decimal(0), set(), [], []
    if all(field(row, "refund_id", "refund_reference", "refund_transaction_id") for row in rows):
        return _refund_state(rows)
    # A single scoped lifecycle event has an observable state even without an ID.
    if len(rows) != 1:
        return None, set(), [], []
    current = status(rows[0])
    if current in PENDING_REFUND | FAILED_REFUND:
        return Decimal(0), {current}, [], []
    if current in COMPLETED_REFUND:
        amount = money(field(rows[0], "amount_brl", "amount", "value"))
        return amount, {current}, [], []
    return None, set(), [], []


def analyze_payment(
    payments: Any,
    timeline: Any,
    refunds: Any,
    snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deduplicate by transaction ID, never by amount alone."""
    captures = [
        r
        for r in records(timeline, "events", "captures", "payment_timeline")
        if str(field(r, "event_type", "type") or "capture").lower() in {"capture", "captured"}
        and in_snapshot(field(r, "event_at", "occurred_at"), snapshot)
    ]
    if not captures and snapshot is None:
        captures = [
            r
            for r in records(payments, "payments", "captures")
            if status(r) in {"captured", "paid", "approved", "settled"}
        ]
    unique: dict[str, dict[str, Any]] = {}
    incomplete = not captures
    for row in captures:
        capture_id = field(row, "capture_id", "transaction_id", "event_id")
        if capture_id is None and snapshot:
            capture_id = field(row, "event_at", "occurred_at")
        amount = money(field(row, "amount_brl", "amount", "value", "payment_value"))
        if not isinstance(capture_id, str) or amount is None:
            incomplete = True
            continue
        if capture_id in unique and unique[capture_id] != row:
            incomplete = True
        unique.setdefault(capture_id, row)
    total = sum(
        (
            money(field(r, "amount_brl", "amount", "value", "payment_value")) or Decimal(0)
            for r in unique.values()
        ),
        Decimal(0),
    )
    by_payment: dict[str, list[tuple[str, Decimal]]] = {}
    for capture_id, row in unique.items():
        payment_ref = field(row, "payment_reference", "payment_id", "payment_ref")
        amount = money(field(row, "amount_brl", "amount", "value", "payment_value"))
        if isinstance(payment_ref, str) and amount is not None:
            by_payment.setdefault(payment_ref, []).append((capture_id, amount))
    duplicate_ids = sorted(
        {
            cid
            for group in by_payment.values()
            for cid, amount in group
            if sum(1 for _, other in group if other == amount) > 1
        }
    )
    if snapshot:
        refunded, refund_statuses, refund_ids, refund_event_ids = _snapshot_refund_state(
            refunds, snapshot
        )
    else:
        refunded, refund_statuses, refund_ids, refund_event_ids = _refund_state(refunds)
    payment_rows = records(payments, "payments", "captures")
    payment_signatures: set[str] = set()
    if snapshot and unique:
        capture_amounts = {
            money(field(row, "amount_brl", "amount", "value", "payment_value"))
            for row in unique.values()
        }
        payment_signatures = {
            f"{field(row, 'payment_type')}:{field(row, 'payment_sequential')}"
            for row in payment_rows
            if money(field(row, "payment_value", "amount_brl")) in capture_amounts
            and field(row, "payment_type") is not None
        }
    mismatch_event = bool(snapshot) and any(
        field(row, "event_type", "type") == "reconciliation_mismatch"
        and in_snapshot(field(row, "event_at", "occurred_at"), snapshot)
        for row in records(timeline, "events", "payment_timeline")
    )
    if incomplete or (refunded is None and not snapshot) or (
        refunded is not None and refunded > total
    ):
        verdict = "insufficient_evidence"
    elif duplicate_ids:
        verdict = "duplicate_capture"
    elif mismatch_event:
        verdict = "capture_mismatch"
    elif refund_statuses & FAILED_REFUND:
        verdict = "refund_failed"
    elif refund_statuses & PENDING_REFUND:
        verdict = "refund_pending"
    elif total and refunded is not None and refunded == total:
        verdict = "refunded"
    elif refunded is None and snapshot:
        verdict = "reconciled" if unique else "insufficient_evidence"
    else:
        expected = next(
            (
                money(field(r, "order_total_brl", "expected_total_brl"))
                for r in records(payments, "summary")
                if field(r, "order_total_brl", "expected_total_brl") is not None
            ),
            None,
        )
        verdict = "capture_mismatch" if expected is not None and total != expected else "reconciled"
    analysis = {
        "verdict": verdict,
        "captured_total_brl": float(total) if unique and not incomplete else None,
        "refunded_total_brl": float(refunded) if refunded is not None else None,
        "refundable_total_brl": (
            float(total - refunded) if refunded is not None and not incomplete else None
        ),
    }
    detail = {
        "capture_ids": sorted(unique),
        "duplicate_capture_ids": duplicate_ids,
        "payment_references": sorted(payment_signatures if snapshot else by_payment),
        "refund_ids": refund_ids,
        "refund_event_ids": refund_event_ids,
        "refund_statuses": sorted(refund_statuses),
    }
    return analysis, detail


class PaymentAgent(Specialist):
    name = "payment"
    description = "Reconcile capture, refund and refundable amounts."

    async def investigate(self, work: WorkOrder) -> Finding:
        candidates = order_ids(work)
        if not candidates:
            return finding(
                work, self.name, "needs_evidence", [], ["No resolved or candidate order ID"]
            )
        facts = []
        questions = []
        try:
            for order_id in candidates:
                payment = await fetch(self, work, "get_order_payments", order_id=order_id)
                timeline = await fetch(self, work, "get_payment_timeline", order_id=order_id)
                refs = [payment["evidence_ref"], timeline["evidence_ref"]]
                snapshot = work.input.get("snapshot")
                if isinstance(snapshot, dict) and snapshot.get("order_id") == order_id:
                    refs.append(snapshot["evidence_ref"])
                try:
                    refund = await fetch(self, work, "get_refund_timeline", order_id=order_id)
                except RuntimeError as exc:
                    refund = None
                    questions.append(f"Refund timeline unavailable: {exc}")
                if refund is not None:
                    refs.append(refund["evidence_ref"])
                analysis, detail = analyze_payment(
                    payment.get("data"), timeline.get("data"),
                    refund.get("data") if refund is not None else None,
                    snapshot,
                )
                facts.append(fact("payment_analysis", analysis, refs))
                facts.append(fact("payment_reconciliation", {"order_id": order_id, **detail}, refs))
        except Exception as exc:
            return finding(work, self.name, "failed", facts, [f"Payment MCP error: {exc}"])
        return finding(work, self.name, "completed", facts, questions)
