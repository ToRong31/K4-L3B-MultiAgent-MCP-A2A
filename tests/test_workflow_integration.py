import asyncio
from pathlib import Path

import pytest

from student_agent.agents.order_item import OrderItemAgent
from student_agent.agents.policy.agent import analyze_policy
from student_agent.agents.shipment import ShipmentAgent
from student_agent.agents.verifier import VerifierAgent
from student_agent.contracts import Contracts
from student_agent.core.agent_messages import Finding, WorkOrder
from student_agent.core.memory import AgentMemory
from student_agent.orchestrator.coordinator import DEFAULTS, Coordinator, assemble_output
from student_agent.trace import TraceWriter


class FakeEvidence:
    pass


async def resolver(case, evidence, memory):
    return {
        "context": {
            "entity_resolution": DEFAULTS["entity_resolution"],
            "customer_context": DEFAULTS["customer_context"],
        },
        "evidence_refs": [],
        "open_questions": [],
    }


async def resolved(case, evidence, memory):
    return {
        "context": {
            "entity_resolution": {
                "status": "resolved",
                "resolved_order_ids": ["order-1"],
                "rejected_candidates": [],
                "confidence": 0.9,
            },
            "customer_context": DEFAULTS["customer_context"],
        },
        "evidence_refs": [],
        "open_questions": [],
    }


def section(kind, data):
    return {
        "kind": kind,
        "data": {"items": data} if isinstance(data, list) else data,
        "evidence_refs": [],
    }


def supported_refund_findings(*, optional=False):
    payment = section(
        "payment_analysis",
        {
            "verdict": "refund_pending",
            "captured_total_brl": 100,
            "refunded_total_brl": 0,
            "refundable_total_brl": 100,
        },
    )
    policy_facts = [
        section(
            "assessment",
            {
                "primary_issue": "refund_pending",
                "secondary_issues": [],
                "case_status": "action_required",
                "confidence": 0.8,
            },
        ),
        section("root_cause_analysis", DEFAULTS["root_cause_analysis"]),
        section(
            "financial_resolution",
            {
                "currency": "BRL",
                "recommended_refund_brl": 20,
                "refund_lines": [{"reason_code": "PENDING", "amount_brl": 20, "entity_id": None}],
            },
        ),
        section("resolution_actions", ["REVIEW_REFUND"]),
    ]
    if optional:
        policy_facts.extend((section("claim_assessments", []), section("data_conflicts", [])))
    return [
        Finding(
            "CASE_001",
            "order",
            "order-task",
            "completed",
            facts=[section("affected_entities", DEFAULTS["affected_entities"])],
        ),
        Finding("CASE_001", "payment", "payment-task", "completed", facts=[payment]),
        Finding(
            "CASE_001",
            "shipment",
            "shipment-task",
            "completed",
            facts=[section("shipment_analysis", DEFAULTS["shipment_analysis"])],
        ),
        Finding("CASE_001", "policy", "policy-task", "completed", facts=policy_facts),
    ]


def test_missing_optional_arrays_preserve_supported_action_and_refund():
    context = (asyncio.run(resolved({}, None, None)))["context"]
    output = assemble_output({"case_id": "CASE_001"}, context, supported_refund_findings())
    with_optional = assemble_output(
        {"case_id": "CASE_001"}, context, supported_refund_findings(optional=True)
    )
    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["resolution_actions"] == ["REVIEW_REFUND"]
    assert output["financial_resolution"]["recommended_refund_brl"] == 20
    assert output["claim_assessments"] == output["data_conflicts"] == []
    assert output == with_optional


def test_missing_refund_amount_is_flagged_without_erasing_action():
    context = (asyncio.run(resolved({}, None, None)))["context"]
    findings = supported_refund_findings()
    policy = findings[-1]
    findings[-1] = Finding(
        policy.case_id,
        policy.agent,
        policy.task_id,
        policy.status,
        facts=[fact for fact in policy.facts if fact["kind"] != "financial_resolution"],
    )
    output = assemble_output({"case_id": "CASE_001"}, context, findings)
    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert "refund_amount_unverified" in output["assessment"]["secondary_issues"]
    assert output["resolution_actions"] == ["REVIEW_REFUND"]
    assert output["financial_resolution"]["recommended_refund_brl"] == 0


def test_missing_unrelated_specialist_does_not_erase_payment_resolution():
    context = (asyncio.run(resolved({}, None, None)))["context"]
    findings = supported_refund_findings()
    findings[2] = Finding("CASE_001", "shipment", "shipment-task", "needs_evidence")
    output = assemble_output({"case_id": "CASE_001"}, context, findings)
    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["shipment_analysis"]["verdict"] == "insufficient_evidence"
    assert output["resolution_actions"] == ["REVIEW_REFUND"]
    assert output["financial_resolution"]["recommended_refund_brl"] == 20


def test_missing_dependent_specialist_keeps_supported_policy_details():
    context = (asyncio.run(resolved({}, None, None)))["context"]
    findings = supported_refund_findings()
    findings[1] = Finding("CASE_001", "payment", "payment-task", "needs_evidence")
    output = assemble_output({"case_id": "CASE_001"}, context, findings)
    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["resolution_actions"] == ["REVIEW_REFUND"]
    assert output["financial_resolution"]["recommended_refund_brl"] == 20


def test_array_contributions_merge_without_source_conflict():
    context = (asyncio.run(resolved({}, None, None)))["context"]
    findings = supported_refund_findings()
    findings[0] = Finding(
        "CASE_001",
        "order",
        "order-task",
        "completed",
        facts=[
            section(
                "affected_entities",
                {
                    **DEFAULTS["affected_entities"],
                    "order_ids": ["order-1"],
                },
            ),
            section(
                "affected_entities",
                {
                    **DEFAULTS["affected_entities"],
                    "item_ids": ["item-1"],
                    "order_ids": ["order-1"],
                },
            ),
        ],
    )
    findings[-1] = Finding(
        "CASE_001",
        "policy",
        "policy-task",
        "completed",
        facts=findings[-1].facts
        + [
            section("resolution_actions", ["REVIEW_REFUND", "CONTACT_PROVIDER"]),
            section(
                "data_conflicts",
                [
                    {
                        "field": "shipment_analysis",
                        "sources": ["policy", "shipment"],
                        "selected_source": None,
                        "resolution_code": "UNRESOLVED",
                    }
                ],
            ),
            section(
                "data_conflicts",
                [
                    {
                        "field": "shipment_analysis",
                        "sources": ["shipment", "policy"],
                        "selected_source": None,
                        "resolution_code": "UNRESOLVED",
                    }
                ],
            ),
        ],
    )
    output = assemble_output({"case_id": "CASE_001"}, context, findings)
    assert output["affected_entities"]["order_ids"] == ["order-1"]
    assert output["affected_entities"]["item_ids"] == ["item-1"]
    assert output["resolution_actions"] == ["REVIEW_REFUND", "CONTACT_PROVIDER"]
    assert len(output["data_conflicts"]) == 1


def test_real_policy_and_verifier_keep_supported_action(tmp_path: Path):
    async def run():
        memory = AgentMemory(tmp_path / "memory.db")
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        refs = {
            name: "ev_" + letter * 24
            for name, letter in (
                ("order", "o"),
                ("payment", "p"),
                ("shipment", "s"),
                ("policy", "q"),
            )
        }

        async def send(url, work):
            if work.target == "verifier":
                return await VerifierAgent(memory, contracts=contracts).handle(work)
            target = work.target
            ref = refs[target]
            memory.append(
                work.case_id,
                "orchestrator",
                work.task_id,
                "tool_result",
                {
                    "agent": target,
                    "tool_name": "fixture_" + target,
                    "evidence": {"evidence_ref": ref, "data": {}},
                },
            )
            if target == "order":
                facts = [
                    section(
                        "affected_entities",
                        {
                            **DEFAULTS["affected_entities"],
                            "order_ids": ["order-1"],
                        },
                    )
                ]
            elif target == "payment":
                facts = [
                    section(
                        "payment_analysis",
                        {
                            "verdict": "refund_pending",
                            "captured_total_brl": 100,
                            "refunded_total_brl": 0,
                            "refundable_total_brl": 100,
                        },
                    )
                ]
            elif target == "shipment":
                facts = [section("shipment_analysis", DEFAULTS["shipment_analysis"])]
            else:
                policy = {
                    "rules": {
                        "refund_pending": {
                            "case_status": "action_required",
                            "cause_code": "REFUND_DELAY",
                            "responsible_parties": [
                                {"party_type": "payment_provider", "party_id": None}
                            ],
                            "recommended_action": "CHECK_REFUND",
                            "refund_brl": 0,
                            "refund_lines": [],
                        }
                    }
                }
                facts, _ = analyze_policy(policy, work.input["findings"], ref)
            all_refs = sorted({ref, *(r for f in facts for r in f["evidence_refs"])})
            facts = [{**f, "evidence_refs": f["evidence_refs"] or [ref]} for f in facts]
            return Finding(
                work.case_id,
                target,
                work.task_id,
                "completed",
                facts=facts,
                evidence_refs=all_refs,
                tool_uses=[{"tool_name": "fixture_" + target, "evidence_ref": ref}],
            )

        try:
            output = await Coordinator(
                memory, trace, contracts, FakeEvidence(), sender=send, resolver=resolved
            ).solve({"case_id": "CASE_001", "policy_version": "v1"})
            assert output["assessment"]["primary_issue"] == "refund_pending"
            assert output["assessment"]["case_status"] == "action_required"
            assert output["resolution_actions"] == ["CHECK_REFUND"]
            assert output["financial_resolution"]["recommended_refund_brl"] == 0
            assert output["claim_assessments"] == output["data_conflicts"] == []
        finally:
            memory.close()

    asyncio.run(run())


def test_real_policy_conflict_fact_survives_assembly(tmp_path: Path):
    context = (asyncio.run(resolved({}, None, None)))["context"]
    shipment_ref = "ev_" + "s" * 24
    policy_ref = "ev_" + "q" * 24
    source = {
        "field": "shipment_timeline",
        "sources": ["carrier_scan", "delivery_record"],
        "selected_source": None,
        "resolution_code": "UNRESOLVED_TIMELINE",
    }
    shipment = Finding(
        "CASE_001",
        "shipment",
        "shipment-task",
        "completed",
        facts=[
            {
                "kind": "source_conflicts",
                "data": {"items": [source]},
                "evidence_refs": [shipment_ref],
            },
            {
                "kind": "shipment_analysis",
                "data": {
                    "verdict": "conflicting",
                    "late_seller_ids": [],
                    "timeline_complete": True,
                },
                "evidence_refs": [shipment_ref],
            },
        ],
        evidence_refs=[shipment_ref],
    )
    policy_facts, _ = analyze_policy({"rules": {}}, [shipment.to_dict()], policy_ref)
    assert any(f["kind"] == "data_conflicts" for f in policy_facts)
    policy = Finding(
        "CASE_001",
        "policy",
        "policy-task",
        "completed",
        facts=policy_facts,
        evidence_refs=[shipment_ref, policy_ref],
    )
    output = assemble_output({"case_id": "CASE_001"}, context, [shipment, policy])
    assert output["data_conflicts"] == [source]
    assert shipment_ref in output["evidence_refs"]
    assert policy_ref in output["evidence_refs"]
    contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
    contracts.validate_output(output, "policy-conflict")
    memory = AgentMemory(tmp_path / "memory.db")
    try:
        for agent, ref in (("shipment", shipment_ref), ("policy", policy_ref)):
            memory.append(
                "CASE_001",
                "orchestrator",
                agent,
                "tool_result",
                {
                    "agent": agent,
                    "tool_name": "fixture_" + agent,
                    "evidence": {"evidence_ref": ref, "data": {}},
                },
            )
        work = WorkOrder(
            "CASE_001",
            "verifier",
            "verify-conflict",
            {
                "case": {"case_id": "CASE_001"},
                "context": context,
                "findings": [shipment.to_dict(), policy.to_dict()],
                "draft_output": output,
            },
            output["evidence_refs"],
        )
        verification = asyncio.run(VerifierAgent(memory, contracts=contracts).handle(work))
        assert verification.facts[0]["data"]["approved"] is True
    finally:
        memory.close()


def test_real_domain_conflicts_reach_public_output():
    class Evidence:
        def __init__(self, data):
            self.data = data

        async def call(self, _agent, tool, **_kwargs):
            return {"evidence_ref": "ev_" + tool[-1] * 24, "data": self.data[tool]}

    case = {"case_id": "CASE_001", "investigation_scope": {"include_product_context": False}}
    context = asyncio.run(resolved({}, None, None))["context"]
    shipment_data = {
        "shipping_limits": [{"shipping_limit_at": "2018-01-02T00:00:00-03:00"}],
        "delivered_carrier_at": "2018-01-02T00:00:00-03:00",
        "delivered_customer_at": "2018-01-04T00:00:00-03:00",
        "estimated_delivery_at": "2018-01-05T00:00:00-03:00",
        "events": [{"event_type": "delivered_late"}],
    }
    scenarios = [
        (
            ShipmentAgent,
            "shipment",
            {"get_shipment_summary": shipment_data},
            "shipment_analysis.verdict",
        ),
        (
            OrderItemAgent,
            "order",
            {
                "get_order": {"order_id": "order-1"},
                "get_order_items": {"items": [{"order_id": "other-order", "item_id": "i1"}]},
            },
            "affected_entities.order_ids",
        ),
    ]
    for agent_class, target, payloads, conflict_field in scenarios:
        work = WorkOrder(case["case_id"], target, "domain-task", {"case": case, "context": context})
        domain = asyncio.run(agent_class(None, evidence=Evidence(payloads)).investigate(work))
        assert domain.status == "completed"
        assert any(f["kind"] == "source_conflicts" for f in domain.facts)
        policy_ref = "ev_" + "p" * 24
        policy_facts, _ = analyze_policy({"rules": {}}, [domain.to_dict()], policy_ref)
        policy = Finding(case["case_id"], "policy", "policy-task", "completed", facts=policy_facts)
        output = assemble_output(case, context, [domain, policy])
        assert any(item["field"] == conflict_field for item in output["data_conflicts"])
        Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas").validate_output(
            output, "domain-conflict"
        )


def test_missing_specialists_still_reach_verifier(tmp_path: Path):
    async def run():
        memory = AgentMemory(tmp_path / "memory.db")
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        calls = []

        async def send(url, work):
            calls.append(work.target)
            if work.target == "verifier":
                return await VerifierAgent(memory, contracts=contracts).handle(work)
            return Finding(work.case_id, work.target, work.task_id, "needs_evidence")

        try:
            output = await Coordinator(
                memory, trace, contracts, FakeEvidence(), sender=send, resolver=resolver
            ).solve({"case_id": "CASE_001"})
            assert output["assessment"]["case_status"] == "needs_investigation"
            assert calls[-1] == "verifier"
        finally:
            memory.close()

    asyncio.run(run())


def test_completed_but_rejected_verification_does_not_finalize(tmp_path: Path):
    async def run():
        memory = AgentMemory(tmp_path / "memory.db")
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

        async def send(url, work):
            if work.target == "verifier":
                return Finding(
                    work.case_id,
                    "verifier",
                    work.task_id,
                    "completed",
                    facts=[
                        {
                            "kind": "verification",
                            "data": {
                                "approved": False,
                                "errors": [
                                    {"code": "SCHEMA", "message": "bad", "target": "coordinator"}
                                ],
                            },
                            "evidence_refs": [],
                        }
                    ],
                )
            return Finding(work.case_id, work.target, work.task_id, "needs_evidence")

        try:
            with pytest.raises(RuntimeError, match="rejected coordinator draft"):
                await Coordinator(
                    memory, trace, contracts, FakeEvidence(), sender=send, resolver=resolver
                ).solve({"case_id": "CASE_001"})
        finally:
            memory.close()

    asyncio.run(run())


def test_verifier_routes_one_repair_to_named_specialist(tmp_path: Path):
    async def run():
        memory = AgentMemory(tmp_path / "memory.db")
        contracts = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        calls = []
        rounds = 0

        async def send(url, work):
            nonlocal rounds
            calls.append(work.target)
            if work.target == "verifier":
                rounds += 1
                return Finding(
                    work.case_id,
                    "verifier",
                    work.task_id,
                    "completed",
                    facts=[
                        {
                            "kind": "verification",
                            "data": {
                                "approved": rounds == 2,
                                "errors": []
                                if rounds == 2
                                else [
                                    {
                                        "code": "PAYMENT_ISSUE",
                                        "message": "repair",
                                        "target": "payment",
                                    }
                                ],
                            },
                            "evidence_refs": [],
                        }
                    ],
                )
            return Finding(work.case_id, work.target, work.task_id, "needs_evidence")

        try:
            await Coordinator(
                memory, trace, contracts, FakeEvidence(), sender=send, resolver=resolved
            ).solve({"case_id": "CASE_001"})
            assert calls == [
                "order",
                "payment",
                "shipment",
                "policy",
                "verifier",
                "payment",
                "verifier",
            ]
        finally:
            memory.close()

    asyncio.run(run())
