"""Discovered MCP tools with role permissions, case cache and bounded retries."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

import httpx2
from jsonschema import Draft202012Validator

from ..mcp_gateway import EvidenceGateway
from .memory import AgentMemory

# Names were checked against the competition gateway via `day09 mcp-tools`.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"get_customer_history", "get_order"}),
    "order": frozenset({"get_order", "get_order_items", "get_product_context", "get_sellers"}),
    "payment": frozenset({"get_order_payments", "get_payment_timeline", "get_refund_timeline"}),
    "shipment": frozenset({"get_shipment_summary"}),
    "policy": frozenset({"get_policy"}),
    "verifier": frozenset(),
}
QUERY_BUDGET = {
    "orchestrator": 8,
    "order": 8,
    "payment": 6,
    "shipment": 4,
    "policy": 3,
    "verifier": 0,
}
TRANSIENT_ERRORS = (TimeoutError, ConnectionError, httpx2.TimeoutException, httpx2.NetworkError)


class EvidenceCollector:
    def __init__(self, gateway: EvidenceGateway, memory: AgentMemory) -> None:
        self.gateway = gateway
        self.memory = memory
        self._discovered: frozenset[str] | None = None
        self._schemas: dict[str, dict[str, Any]] = {}
        self._cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._calls: dict[tuple[str, str], int] = defaultdict(int)
        self._uses: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)

    async def discover(self) -> frozenset[str]:
        if self._discovered is None:
            if hasattr(self.gateway, "describe_tools"):
                self._schemas = await self.gateway.describe_tools()
                self._discovered = frozenset(self._schemas)
            else:
                self._discovered = frozenset(await self.gateway.list_tools())
        return self._discovered

    async def tool_schema(self, tool_name: str) -> dict[str, Any]:
        if tool_name not in await self.discover():
            raise ValueError(f"MCP tool was not discovered: {tool_name}")
        return self._schemas.get(tool_name, {})

    def tool_uses(self, case_id: str, agent_id: str, turn_id: str) -> list[dict[str, str]]:
        return list(self._uses[(case_id, agent_id, turn_id)])

    async def call(
        self, agent_id: str, tool_name: str, *, case_id: str, turn_id: str, **arguments: str
    ) -> dict[str, Any]:
        if agent_id not in TOOL_PERMISSIONS or tool_name not in TOOL_PERMISSIONS[agent_id]:
            raise PermissionError(f"{agent_id} cannot call {tool_name}")
        if tool_name not in await self.discover():
            raise ValueError(f"MCP tool was not discovered: {tool_name}")
        schema = self._schemas.get(tool_name)
        if schema:
            Draft202012Validator(schema).validate({"case_id": case_id, **arguments})
        serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        key = (case_id, tool_name, serialized)
        evidence = self._cache.get(key) or self.memory.cached_evidence(
            case_id, tool_name, serialized
        )
        cached = evidence is not None
        if cached:
            self._cache[key] = evidence
            self.memory.append(
                case_id,
                agent_id,
                turn_id,
                "tool_cache_hit",
                {
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
            )
        else:
            scope = (case_id, agent_id)
            used = sum(
                event["kind"] == "tool_attempt"
                for event in self.memory.raw_history(case_id, agent_id)
            )
            if used >= QUERY_BUDGET[agent_id]:
                raise RuntimeError(f"MCP query budget exhausted for {agent_id} in {case_id}")
            self.memory.append(
                case_id,
                agent_id,
                turn_id,
                "tool_call",
                {
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
            )
            self.memory.append(
                case_id,
                "orchestrator",
                turn_id,
                "tool_call",
                {
                    "agent": agent_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
            )
            for attempt in range(2):
                used = sum(
                    event["kind"] == "tool_attempt"
                    for event in self.memory.raw_history(case_id, agent_id)
                )
                if used >= QUERY_BUDGET[agent_id]:
                    raise RuntimeError(f"MCP query budget exhausted for {agent_id} in {case_id}")
                self._calls[scope] += 1
                self.memory.append(
                    case_id,
                    agent_id,
                    turn_id,
                    "tool_attempt",
                    {"tool_name": tool_name, "attempt": attempt + 1},
                )
                try:
                    evidence = await self.gateway.call(tool_name, case_id=case_id, **arguments)
                    break
                except TRANSIENT_ERRORS as exc:
                    self.memory.append(
                        case_id,
                        agent_id,
                        turn_id,
                        "tool_error",
                        {
                            "tool_name": tool_name,
                            "attempt": attempt + 1,
                            "error_type": type(exc).__name__,
                        },
                    )
                    if attempt == 1 or used + 1 >= QUERY_BUDGET[agent_id]:
                        raise
                    await asyncio.sleep(0.25)
            self._cache[key] = evidence
            self.memory.cache_evidence(case_id, tool_name, serialized, evidence)
        self.memory.append(
            case_id,
            agent_id,
            turn_id,
            "tool_result",
            {
                "tool_name": tool_name,
                "evidence": evidence,
                "cached": cached,
            },
        )
        self.memory.append(
            case_id,
            "orchestrator",
            turn_id,
            "tool_result",
            {
                "agent": agent_id,
                "tool_name": tool_name,
                "evidence": evidence,
                "cached": cached,
            },
        )
        use = {"tool_name": tool_name, "evidence_ref": evidence["evidence_ref"]}
        if use not in self._uses[(case_id, agent_id, turn_id)]:
            self._uses[(case_id, agent_id, turn_id)].append(use)
        return evidence
