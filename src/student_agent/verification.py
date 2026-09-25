from __future__ import annotations

from decimal import Decimal
from typing import Any

from .contracts import Contracts
from .evidence import EvidenceLedger


class VerificationError(ValueError):
    pass


def verify_output(
    output: dict[str, Any], *, case: dict[str, Any], ledger: EvidenceLedger, contracts: Contracts
) -> None:
    contracts.validate_output(output, f"outputs/{case['case_id']}.json")
    if output["case_id"] != case["case_id"]:
        raise VerificationError("case_id mismatch")

    resolved = set(output["entity_resolution"]["resolved_order_ids"])
    rejected = set(output["entity_resolution"]["rejected_candidates"])
    candidates = set(case.get("candidate_order_ids", ()))
    if resolved & rejected:
        raise VerificationError("resolved and rejected candidates overlap")
    if not resolved.issubset(candidates):
        raise VerificationError("resolved order is outside candidate scope")
    if not rejected.issubset(candidates):
        raise VerificationError("rejected order is outside candidate scope")

    submitted_refs = set(output["evidence_refs"])
    consumed_refs = set(ledger.consumed_refs)
    if submitted_refs != consumed_refs:
        raise VerificationError("submitted evidence must exactly match consumed evidence")
    for claim in output.get("claim_assessments", ()):
        if not set(claim["evidence_refs"]).issubset(submitted_refs):
            raise VerificationError("claim references evidence outside final evidence set")

    financial = output["financial_resolution"]
    recommended = Decimal(str(financial["recommended_refund_brl"]))
    line_total = sum(
        (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]),
        start=Decimal("0"),
    )
    if line_total != recommended:
        raise VerificationError("refund lines do not sum to recommended refund")
    if recommended > 0 and output["assessment"]["case_status"] == "no_action":
        raise VerificationError("positive refund cannot have no_action status")

    late_sellers = set(output["shipment_analysis"]["late_seller_ids"])
    responsible_sellers = {
        party["party_id"]
        for party in output["root_cause_analysis"]["responsible_parties"]
        if party["party_type"] == "seller" and party["party_id"] is not None
    }
    if late_sellers and not late_sellers.issubset(responsible_sellers):
        raise VerificationError("late sellers must be represented as responsible parties")
