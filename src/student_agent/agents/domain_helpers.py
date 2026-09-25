"""Conservative, deterministic parsing shared by domain specialists.

MCP's public contract specifies an envelope, not domain payload shapes.  Unknown
fields therefore remain unknown; they are never interpreted as a business rule.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..core.agent_messages import Finding, WorkOrder


def records(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [data]
    return []


def field(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result < 0 or result.as_tuple().exponent < -2:
        return None
    return result


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def in_snapshot(value: Any, snapshot: dict[str, Any] | None) -> bool:
    """Keep events from the selected purchase through the complaint opening."""
    if not snapshot:
        return True
    event_at = timestamp(value)
    purchased = timestamp(snapshot.get("purchase_at"))
    opened = timestamp(snapshot.get("opened_at"))
    return bool(event_at and purchased and opened and purchased <= event_at <= opened)


def ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(v for v in values if isinstance(v, str) and v))[:20]


def order_ids(work: WorkOrder) -> list[str]:
    context = work.input.get("context") or {}
    resolution = context.get("entity_resolution") or {}
    if resolution.get("status") == "resolved":
        return ids(resolution.get("resolved_order_ids"))
    case = work.input.get("case") or {}
    return ids(case.get("candidate_order_ids"))


def fact(kind: str, data: dict[str, Any], refs: list[str]) -> dict[str, Any]:
    return {"kind": kind, "data": data, "evidence_refs": sorted(set(refs))}


def finding(
    work: WorkOrder,
    agent: str,
    status: str,
    facts: list[dict[str, Any]],
    questions: list[str] | None = None,
) -> Finding:
    refs = sorted({ref for item in facts for ref in item["evidence_refs"]})
    return Finding(
        work.case_id, agent, work.task_id, status, facts, refs, open_questions=questions or []
    )


async def fetch(agent: Any, work: WorkOrder, tool: str, **kwargs: str) -> dict[str, Any]:
    if agent.evidence is None:
        raise RuntimeError("MCP evidence collector is unavailable")
    result = await agent.evidence.call(
        agent.name, tool, case_id=work.case_id, turn_id=work.task_id, **kwargs
    )
    if not isinstance(result, dict) or not isinstance(result.get("evidence_ref"), str):
        raise ValueError(f"{tool} returned no evidence reference")
    return result


def status(row: dict[str, Any]) -> str:
    value = field(row, "status", "event_status", "payment_status", "refund_status")
    return str(value).lower() if value is not None else ""
