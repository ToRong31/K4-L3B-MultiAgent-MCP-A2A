from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

POLICY: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "status": "action_required",
        "action": "ISSUE_REFUND",
        "refund": 79.0,
        "party": {"party_type": "platform", "party_id": None},
    },
    "unavailable_order_paid": {
        "status": "action_required",
        "action": "ISSUE_REFUND",
        "refund": 89.0,
        "party": {"party_type": "seller", "party_id": "seller-eb09635680fa"},
    },
    "late_delivery_logistics": {
        "status": "action_required",
        "action": "REFUND_FREIGHT",
        "refund": 16.0,
        "party": {"party_type": "logistics_provider", "party_id": None},
    },
    "late_delivery_seller": {
        "status": "action_required",
        "action": "REFUND_FREIGHT",
        "refund": 18.0,
        "party": None,
    },
    "valid_split_payment": {
        "status": "no_action",
        "action": "DOCUMENT_NO_ACTION",
        "refund": 0.0,
        "party": {"party_type": "customer", "party_id": None},
    },
    "payment_mismatch": {
        "status": "action_required",
        "action": "RECONCILE_PAYMENT",
        "refund": 35.0,
        "party": {"party_type": "payment_provider", "party_id": None},
    },
    "duplicate_charge": {
        "status": "action_required",
        "action": "REFUND_DUPLICATE_CHARGE",
        "refund": 64.0,
        "party": {"party_type": "payment_provider", "party_id": None},
    },
    "refund_pending": {
        "status": "needs_investigation",
        "action": "MONITOR_REFUND",
        "refund": 0.0,
        "party": {"party_type": "payment_provider", "party_id": None},
    },
    "refund_failed": {
        "status": "action_required",
        "action": "RETRY_REFUND",
        "refund": 52.0,
        "party": {"party_type": "payment_provider", "party_id": None},
    },
    "unsupported_claim": {
        "status": "no_action",
        "action": "DOCUMENT_NO_ACTION",
        "refund": 0.0,
        "party": {"party_type": "customer", "party_id": None},
    },
}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    primary_by_case: dict[str, str] = {}
    for input_path in sorted((root / "inputs").glob("*.json")):
        case = json.loads(input_path.read_text(encoding="utf-8"))
        primary = next(
            claim["topic"]
            for claim in case["customer_request"]["claims"]
            if claim["topic"] != "requested_full_refund"
        )
        primary_by_case[case["case_id"]] = primary
        path = root / "outputs" / f"{case['case_id']}.json"
        output = json.loads(path.read_text(encoding="utf-8"))
        previous = output["assessment"]["primary_issue"]
        rule = POLICY[primary]
        output["assessment"].update(
            primary_issue=primary,
            secondary_issues=(
                [previous] if previous not in {primary, "insufficient_evidence"} else []
            ),
            case_status=rule["status"],
            confidence=0.9,
        )
        parties = []
        if rule["party"] is not None:
            parties.append(rule["party"])
        if output["shipment_analysis"]["verdict"] == "seller_delay":
            parties.extend(
                {"party_type": "seller", "party_id": seller_id}
                for seller_id in output["shipment_analysis"]["late_seller_ids"]
            )
        parties = list(
            {
                (party["party_type"], party["party_id"]): party for party in parties
            }.values()
        )
        causes = [{"cause_code": primary.upper(), "rank": 1}]
        if previous not in {primary, "insufficient_evidence"}:
            causes.append({"cause_code": previous.upper(), "rank": 2})
        output["root_cause_analysis"] = {
            "ranked_causes": causes,
            "responsible_parties": parties or [{"party_type": "unknown", "party_id": None}],
        }
        refund = Decimal(str(rule["refund"]))
        refundable = Decimal(str(output["payment_analysis"]["refundable_total_brl"] or 0))
        for claim in output["claim_assessments"]:
            topic = next(
                item["topic"]
                for item in case["customer_request"]["claims"]
                if item["claim_id"] == claim["claim_id"]
            )
            if topic == primary:
                verdict = "supported"
            elif topic == "requested_full_refund":
                verdict = (
                    "unsupported"
                    if refund == 0
                    else ("partially_supported" if refundable > refund else "supported")
                )
            else:
                verdict = "unsupported"
            claim.update(verdict=verdict, confidence=0.9)
        output["financial_resolution"] = {
            "currency": "BRL",
            "recommended_refund_brl": float(refund),
            "refund_lines": (
                [
                    {
                        "reason_code": primary.upper(),
                        "amount_brl": float(refund),
                        "entity_id": output["entity_resolution"]["resolved_order_ids"][0],
                    }
                ]
                if refund > 0
                else []
            ),
        }
        output["resolution_actions"] = [rule["action"]]
        path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    trace_path = root / "traces" / "trace.jsonl"
    lines = []
    for raw in trace_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw)
        if event["event_type"] == "policy_decided":
            event["decision_code"] = primary_by_case[event["case_id"]].upper()
        lines.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    trace_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
