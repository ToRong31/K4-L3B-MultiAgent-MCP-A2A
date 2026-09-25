"""Internal fact/context checks; public submission contracts stay unchanged."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from ..core.agent_messages import EVIDENCE_REF


def validate_fact(fact: Any, allowed_refs: set[str] | None = None) -> None:
    if not isinstance(fact, dict) or set(fact) != {"kind", "data", "evidence_refs"}:
        raise ValueError("fact requires kind, data and evidence_refs")
    if not isinstance(fact["kind"], str) or not fact["kind"]:
        raise ValueError("fact kind must be nonempty")
    if not isinstance(fact["data"], dict):
        raise ValueError("fact data must be an object")
    refs = fact["evidence_refs"]
    if not isinstance(refs, list) or any(
        not isinstance(ref, str) or not EVIDENCE_REF.fullmatch(ref) for ref in refs
    ):
        raise ValueError("fact evidence_refs are invalid")
    if allowed_refs is not None and not set(refs).issubset(allowed_refs):
        raise ValueError("fact cites evidence outside the handoff")


def validate_context(context: Any, contracts: Any) -> None:
    if not isinstance(context, dict) or set(context) != {"entity_resolution", "customer_context"}:
        raise ValueError("context requires entity_resolution and customer_context")
    schema = contracts._schemas["l3b-output-v2.schema.json"]
    for name in ("entity_resolution", "customer_context"):
        Draft202012Validator(schema["properties"][name], registry=contracts._registry).validate(
            context[name]
        )
    resolution = context["entity_resolution"]
    selected = set(resolution["resolved_order_ids"])
    rejected = set(resolution["rejected_candidates"])
    if selected & rejected:
        raise ValueError("resolved orders overlap rejected candidates")
    if resolution["status"] == "resolved" and not selected:
        raise ValueError("resolved status requires an order")
    if resolution["status"] != "resolved" and selected:
        raise ValueError("unresolved status cannot carry resolved orders")


def validate_work_input(value: Any) -> None:
    if not isinstance(value, dict) or not set(value).issubset(
        {"case", "context", "snapshot", "findings", "draft_output"}
    ):
        raise ValueError("WorkOrder.input contains unsupported keys")
