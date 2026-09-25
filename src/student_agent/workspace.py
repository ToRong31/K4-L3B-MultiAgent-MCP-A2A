"""Prepare the server-side competition session before starting a local batch."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx2

from .cases import CaseSet
from .config import Settings


def prepare_workspace(settings: Settings, case_set: CaseSet) -> dict[str, Any]:
    # This is the same operation performed when opening the competition workspace.
    # L3B_RUN_ID scopes local memory only; it cannot create a server-side session.
    try:
        response = httpx2.post(
            f"{settings.competition_api_url}/api/v2/runs",
            headers={"Authorization": f"Bearer {settings.team_api_key}"},
            json={"variant_id": case_set.variant_id},
            timeout=30.0,
        )
    except httpx2.RequestError:
        raise RuntimeError("Cannot reach competition API to prepare workspace") from None
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Workspace preparation failed (HTTP {response.status_code}); "
            "check team credentials and competition availability"
        )
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("Competition API returned invalid workspace JSON") from None
    if not isinstance(data, dict):
        raise RuntimeError("Competition API returned invalid workspace metadata")
    if (
        data.get("variant_id") != case_set.variant_id
        or data.get("case_set_version") != case_set.version
    ):
        raise RuntimeError("Server workspace does not match the local variant/case-set version")
    endpoint = data.get("mcp_endpoint")
    if not isinstance(endpoint, str):
        raise RuntimeError("Server workspace has no MCP endpoint")
    url = urlsplit(endpoint)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise RuntimeError("Server workspace has an invalid MCP endpoint")
    return {
        "variant_id": data["variant_id"],
        "case_set_version": data["case_set_version"],
        "mcp_endpoint": endpoint,
        "expires_at": data.get("expires_at"),
    }
