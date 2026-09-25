from __future__ import annotations

from student_agent.agents.policy.agent import analyze_policy

REF = "ev_aaaaaaaaaaaaaaaaaaaa"


def _finding(kind: str, data: dict) -> dict:
    return {"status": "completed", "facts": [{"kind": kind, "data": data, "evidence_refs": [REF]}]}


def test_policy_uses_explicit_codes_and_amount() -> None:
    policy = {
        "rules": [
            {
                "issue": "refund_pending",
                "priority": 1,
                "case_status": "action_required",
                "cause_code": "REFUND_DELAY",
                "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
                "actions": ["CHECK_REFUND"],
                "recommended_refund_brl": "0.00",
                "refund_lines": [],
            }
        ]
    }
    facts, questions = analyze_policy(
        policy, [_finding("payment_analysis", {"verdict": "refund_pending"})], REF
    )
    assert not questions
    assert (
        next(f["data"] for f in facts if f["kind"] == "assessment")["primary_issue"]
        == "refund_pending"
    )
    assert next(f["data"] for f in facts if f["kind"] == "resolution_actions")["items"] == [
        "CHECK_REFUND"
    ]


def test_policy_missing_rule_and_ambiguous_priority() -> None:
    findings = [
        _finding("payment_analysis", {"verdict": "refund_pending"}),
        _finding("shipment_analysis", {"verdict": "seller_delay"}),
    ]
    facts, questions = analyze_policy({"rules": []}, findings, REF)
    assert questions
    assert facts[0]["data"]["primary_issue"] == "insufficient_evidence"
    policy = {"rules": [{"issue": "refund_pending"}, {"issue": "late_delivery_seller"}]}
    facts, questions = analyze_policy(policy, findings, REF)
    assert questions
    assert facts[0]["data"]["case_status"] == "needs_investigation"


def test_actual_policy_shape_and_no_unfounded_cause_code() -> None:
    policy = {
        "rules": {
            "refund_failed": {
                "case_status": "action_required",
                "recommended_action": "retry_refund",
                "refund_brl": 52.0,
                "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
            }
        }
    }
    facts, questions = analyze_policy(
        policy, [_finding("payment_analysis", {"verdict": "refund_failed"})], REF
    )
    assert next(f["data"] for f in facts if f["kind"] == "resolution_actions") == {
        "items": ["retry_refund"]
    }
    assert (
        next(f["data"] for f in facts if f["kind"] == "root_cause_analysis")["ranked_causes"] == []
    )
    assert any("cause" in question for question in questions)


def test_unresolved_source_conflict_stays_visible() -> None:
    facts, questions = analyze_policy(
        {"rules": {}}, [_finding("shipment_analysis", {"verdict": "conflicting"})], REF
    )
    assert next(f["data"] for f in facts if f["kind"] == "source_conflicts")["items"] == [
        {"field": "shipment_timeline"}
    ]
    assert (
        next(f["data"] for f in facts if f["kind"] == "assessment")["case_status"]
        == "needs_investigation"
    )
    assert questions


def test_conflict_emits_one_assessment_even_with_matching_policy() -> None:
    policy = {"rules": {"refund_failed": {"case_status": "action_required", "refund_brl": 52}}}
    facts, _ = analyze_policy(
        policy,
        [
            _finding("shipment_analysis", {"verdict": "conflicting"}),
            _finding("payment_analysis", {"verdict": "refund_failed"}),
        ],
        REF,
    )
    assessments = [f["data"] for f in facts if f["kind"] == "assessment"]
    assert len(assessments) == 1
    assert assessments[0]["primary_issue"] == "insufficient_evidence"


def test_policy_emits_public_data_conflict_with_evidence() -> None:
    source_ref = "ev_bbbbbbbbbbbbbbbbbbbb"
    item = {
        "field": "shipment_analysis.verdict",
        "sources": ["get_order.order_delivered_customer_date", "get_shipment_summary.events"],
        "selected_source": None,
    }
    finding = {
        "status": "completed",
        "facts": [
            {"kind": "source_conflicts", "data": {"items": [item]}, "evidence_refs": [source_ref]}
        ],
    }
    policy = {
        "conflict_resolutions": {
            "shipment_analysis.verdict": {
                "resolution_code": "TIMELINE_UNRESOLVED",
                "selected_source": None,
            }
        }
    }
    facts, questions = analyze_policy(policy, [finding], REF)
    public = next(f for f in facts if f["kind"] == "data_conflicts")
    assert public == {
        "kind": "data_conflicts",
        "data": {"items": [{**item, "resolution_code": "TIMELINE_UNRESOLVED"}]},
        "evidence_refs": [REF, source_ref],
    }
    assert questions


def test_policy_does_not_invent_conflict_sources_or_resolution_code() -> None:
    source_ref = "ev_bbbbbbbbbbbbbbbbbbbb"
    finding = {
        "status": "completed",
        "facts": [
            {
                "kind": "source_conflicts",
                "data": {
                    "items": [
                        {
                            "field": "shipment_analysis.verdict",
                            "sources": ["get_shipment_summary.events"],
                        }
                    ]
                },
                "evidence_refs": [source_ref],
            }
        ],
    }
    facts, _ = analyze_policy({}, [finding], REF)
    assert not any(f["kind"] == "data_conflicts" for f in facts)
    unsupported = {
        "status": "completed",
        "facts": [
            {
                "kind": "source_conflicts",
                "data": {
                    "items": [
                        {
                            "field": "shipment_analysis.verdict",
                            "sources": [
                                "get_order.order_delivered_customer_date",
                                "get_shipment_summary.events",
                            ],
                            "resolution_code": "TIMELINE_UNRESOLVED",
                        }
                    ]
                },
                "evidence_refs": [],
            }
        ],
    }
    facts, _ = analyze_policy({}, [unsupported], REF)
    assert not any(f["kind"] == "data_conflicts" for f in facts)
