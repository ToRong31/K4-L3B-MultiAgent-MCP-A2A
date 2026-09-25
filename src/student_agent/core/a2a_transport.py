"""A2A SDK v1 server/client adapters for the specialist work contract."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import httpx2
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_message_text, get_stream_response_text, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Role,
    SendMessageRequest,
)
from starlette.applications import Starlette

from ..agents.base import Specialist
from .agent_messages import Finding, WorkOrder


class SpecialistExecutor(AgentExecutor):
    def __init__(self, specialist: Specialist) -> None:
        self.specialist = specialist

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        try:
            data = json.loads(get_message_text(context.message))
            work = WorkOrder.from_dict(data)
            try:
                result = await self.specialist.handle(work)
            except (TimeoutError, ConnectionError, httpx2.TimeoutException, httpx2.NetworkError):
                result = Finding(
                    work.case_id,
                    work.target,
                    work.task_id,
                    "failed",
                    open_questions=["MCP_TIMEOUT_OR_NETWORK"],
                )
            except RuntimeError:
                result = Finding(
                    work.case_id,
                    work.target,
                    work.task_id,
                    "failed",
                    open_questions=["MCP_OR_AGENT_ERROR"],
                )
        except (ValueError, TypeError, KeyError) as exc:
            # A2A message-only mode: emit exactly one response message.
            result = {"error": str(exc)}
        else:
            result = result.to_dict()
        await event_queue.enqueue_event(new_text_message(json.dumps(result), role=Role.ROLE_AGENT))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancellation is not supported by this skeleton")


def specialist_app(specialist: Specialist, base_url: str) -> Starlette:
    rpc_url = "/a2a/jsonrpc/"
    card = AgentCard(
        name=specialist.name,
        description=specialist.description,
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=f"{base_url.rstrip('/')}{rpc_url}")
        ],
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id=f"investigate_{specialist.name}",
                name=specialist.name,
                description=specialist.description,
                tags=["commerce", "investigation"],
            )
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=SpecialistExecutor(specialist),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    return Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url=rpc_url),
        ]
    )


async def send_work(url: str, work: WorkOrder, *, timeout_seconds: float = 120) -> Finding:
    async def _send() -> Finding:
        async with httpx.AsyncClient(timeout=timeout_seconds) as http_client:
            client = await create_client(url, ClientConfig(httpx_client=http_client))
            async with client:
                message = new_text_message(json.dumps(work.to_dict()), role=Role.ROLE_USER)
                request = SendMessageRequest(message=message)
                replies: list[str] = []
                async for chunk in client.send_message(request):
                    if chunk.HasField("message") or chunk.HasField("artifact_update"):
                        replies.append(get_stream_response_text(chunk))
                if len(replies) != 1:
                    raise RuntimeError(f"expected one A2A reply, got {len(replies)}")
                payload: dict[str, Any] = json.loads(replies[0])
                if "error" in payload:
                    raise RuntimeError(f"{work.target} agent: {payload['error']}")
                finding = Finding.from_dict(payload)
                if (finding.case_id, finding.agent, finding.task_id) != (
                    work.case_id,
                    work.target,
                    work.task_id,
                ):
                    raise ValueError("A2A reply correlation mismatch")
                return finding

    return await asyncio.wait_for(_send(), timeout=timeout_seconds)
