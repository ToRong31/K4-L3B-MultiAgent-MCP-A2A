import json
from pathlib import Path


def test_primary_issue_implementation_covers_contract_enum() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (root / "contracts" / "schemas" / "l3a-output-v2.schema.json").read_text(
            encoding="utf-8"
        )
    )
    contract_values = set(schema["$defs"]["primaryIssue"]["enum"])
    implemented_values = {
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
    assert implemented_values == contract_values
