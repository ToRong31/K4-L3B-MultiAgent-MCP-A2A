from pathlib import Path

import httpx2
import pytest

from student_agent.cases import CaseSet
from student_agent.config import Settings
from student_agent.workspace import prepare_workspace


@pytest.mark.parametrize("failure", [None, "auth", "version", "endpoint"])
def test_prepare_workspace(monkeypatch, failure):
    settings = Settings(
        "https://competition.example", "test-secret", "https://old.example/mcp", Path()
    )
    cases = CaseSet("l3b-competition-v1", "l3b", (), {})
    metadata = {
        "variant_id": "l3b",
        "case_set_version": cases.version,
        "mcp_endpoint": "https://gateway.example/mcp",
        "expires_at": "2026-09-25T23:00:00Z",
    }
    if failure == "version":
        metadata["case_set_version"] = "different-version"
    if failure == "endpoint":
        metadata["mcp_endpoint"] = "file:///invalid"

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx2.Response(401 if failure == "auth" else 201, json=metadata)

    monkeypatch.setattr("student_agent.workspace.httpx2.post", post)
    if failure:
        with pytest.raises(RuntimeError) as error:
            prepare_workspace(settings, cases)
        assert settings.team_api_key not in str(error.value)
    else:
        assert prepare_workspace(settings, cases) == metadata
    assert len(calls) == 1
    assert calls[0][0] == "https://competition.example/api/v2/runs"
    assert calls[0][1]["json"] == {"variant_id": "l3b"}
    assert calls[0][1]["headers"] == {"Authorization": "Bearer test-secret"}
