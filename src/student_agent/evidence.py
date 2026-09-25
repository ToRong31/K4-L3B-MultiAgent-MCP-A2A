from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .mcp_gateway import EvidenceGateway, MCPToolError
from .trace import TraceWriter

ACTOR_TOOLS: dict[str, frozenset[str]] = {
    "entity-customer": frozenset({"get_order", "get_customer_history"}),
    "entity": frozenset({"get_order", "get_customer_history"}),
    "order-product": frozenset({"get_order_items", "get_product_context", "get_sellers"}),
    "shipment": frozenset({"get_shipment_summary"}),
    "payment-refund": frozenset(
        {"get_order_payments", "get_payment_timeline", "get_refund_timeline"}
    ),
    "policy": frozenset({"get_policy"}),
}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _canonical_arguments(arguments: Mapping[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class EvidenceRecord:
    tool_name: str
    arguments: Mapping[str, Any]
    evidence_ref: str
    result_hash: str
    domain: str
    data: Any
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DerivedFact:
    code: str
    value: Any
    evidence_refs: tuple[str, ...]
    rule: str


@dataclass(frozen=True)
class ToolRequest:
    case_id: str
    actor: str
    tool_name: str
    arguments: Mapping[str, Any]


@dataclass
class EvidenceLedger:
    """Case-scoped immutable evidence store with cache and bounded retries."""

    case_id: str
    gateway: EvidenceGateway
    trace: TraceWriter
    max_calls: int = 16
    retry_limit: int = 2
    _records_by_key: dict[tuple[str, str], EvidenceRecord] = field(default_factory=dict)
    _records_by_ref: dict[str, EvidenceRecord] = field(default_factory=dict)
    _consumed_refs: set[str] = field(default_factory=set)
    _facts: list[DerivedFact] = field(default_factory=list)
    _call_count: int = 0
    _cache_hits: int = 0
    _retry_count: int = 0
    _warning_count: int = 0
    _calls_by_tool: dict[str, int] = field(default_factory=dict)
    _negative_cache: dict[tuple[str, str], RuntimeError] = field(default_factory=dict)
    _key_locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def consumed_refs(self) -> list[str]:
        return sorted(self._consumed_refs)

    @property
    def facts(self) -> tuple[DerivedFact, ...]:
        return tuple(self._facts)

    def metrics(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "mcp_calls": self._call_count,
            "calls_by_tool": dict(sorted(self._calls_by_tool.items())),
            "cache_hits": self._cache_hits,
            "retries": self._retry_count,
            "evidence_count": len(self._records_by_ref),
            "consumed_evidence_count": len(self._consumed_refs),
            "warning_count": self._warning_count,
        }

    async def fetch(self, tool_name: str, *, actor: str, **arguments: Any) -> EvidenceRecord:
        return await self.request(ToolRequest(self.case_id, actor, tool_name, arguments))

    async def request(self, request: ToolRequest) -> EvidenceRecord:
        if request.case_id != self.case_id:
            raise PermissionError("tool request belongs to another case")
        tool_name, actor, arguments = request.tool_name, request.actor, request.arguments
        if tool_name not in ACTOR_TOOLS.get(actor, frozenset()):
            raise PermissionError(f"{actor} cannot call {tool_name}")
        key = (tool_name, _canonical_arguments(arguments))
        lock = self._key_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._fetch_locked(key, tool_name, arguments)

    async def _fetch_locked(
        self, key: tuple[str, str], tool_name: str, arguments: Mapping[str, Any]
    ) -> EvidenceRecord:
        cached = self._records_by_key.get(key)
        if cached is not None:
            self._cache_hits += 1
            return cached
        if key in self._negative_cache:
            self._cache_hits += 1
            raise self._negative_cache[key]
        if self._call_count >= self.max_calls:
            raise RuntimeError(f"MCP call budget exhausted for {self.case_id}")

        last_error: RuntimeError | None = None
        for attempt in range(self.retry_limit + 1):
            if self._call_count >= self.max_calls:
                raise RuntimeError(f"MCP call budget exhausted for {self.case_id}")
            self._call_count += 1
            self._calls_by_tool[tool_name] = self._calls_by_tool.get(tool_name, 0) + 1
            if attempt:
                self._retry_count += 1
            try:
                payload = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
                record = EvidenceRecord(
                    tool_name=tool_name,
                    arguments=_freeze(dict(arguments)),
                    evidence_ref=payload["evidence_ref"],
                    result_hash=payload["result_hash"],
                    domain=payload["domain"],
                    data=_freeze(payload["data"]),
                    warnings=tuple(payload.get("warnings", ())),
                )
                existing = self._records_by_ref.get(record.evidence_ref)
                if existing is not None and existing.result_hash != record.result_hash:
                    raise ValueError("one evidence_ref resolved to multiple result hashes")
                self._records_by_key[key] = record
                self._records_by_ref[record.evidence_ref] = record
                self._warning_count += len(record.warnings)
                return record
            except RuntimeError as exc:
                last_error = exc
                if isinstance(exc, MCPToolError) and not exc.retryable:
                    self._negative_cache[key] = exc
                    break
                if attempt >= self.retry_limit:
                    break
                await asyncio.sleep((0.2 * (2**attempt)) + random.uniform(0, 0.1))
        assert last_error is not None
        raise last_error

    def consume(self, record: EvidenceRecord, *, actor: str) -> None:
        if record.evidence_ref not in self._records_by_ref:
            raise ValueError("cannot consume evidence outside this case ledger")
        if record.evidence_ref in self._consumed_refs:
            return
        self._consumed_refs.add(record.evidence_ref)
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=record.tool_name,
            evidence_refs=[record.evidence_ref],
        )

    def derive(
        self, code: str, value: Any, records: list[EvidenceRecord], *, rule: str
    ) -> DerivedFact:
        refs = tuple(dict.fromkeys(record.evidence_ref for record in records))
        if not refs or any(ref not in self._records_by_ref for ref in refs):
            raise ValueError("derived facts require case-scoped evidence")
        fact = DerivedFact(code=code, value=value, evidence_refs=refs, rule=rule)
        self._facts.append(fact)
        return fact
