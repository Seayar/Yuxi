from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from yuxi.repositories.agent_repository import (
    POWER_SPEC_AGENT_SLUG,
    POWER_SPEC_SYSTEM_PROMPT,
    AgentRepository,
)


@pytest.mark.asyncio
async def test_ensure_power_spec_agent_creates_visible_main_agent(monkeypatch):
    repo = AgentRepository(object())
    created = []

    async def create_agent(**kwargs):
        created.append(kwargs)
        return kwargs

    monkeypatch.setattr(repo, "get_by_slug", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "_ensure_builtin_agent", create_agent)

    result = await repo.ensure_power_spec_agent()

    assert result["slug"] == POWER_SPEC_AGENT_SLUG
    assert result["backend_id"] == "ChatbotAgent"
    assert result["is_subagent"] is False
    assert result["config_context"]["skills"] == ["knowledge-base", "power-spec"]
    assert "knowledges" not in result["config_context"]
    assert len(created) == 1


@pytest.mark.asyncio
async def test_ensure_power_spec_agent_is_idempotent(monkeypatch):
    repo = AgentRepository(object())
    existing = SimpleNamespace(
        config_json={
            "context": {
                "system_prompt": POWER_SPEC_SYSTEM_PROMPT,
                "skills": ["power-spec"],
                "preload_skills": ["power-spec"],
                "knowledges": None,
                "max_execution_steps": 300,
            }
        }
    )
    ensure = AsyncMock(return_value=existing)
    monkeypatch.setattr(repo, "get_by_slug", AsyncMock(return_value=existing))
    monkeypatch.setattr(repo, "_ensure_builtin_agent", ensure)

    result = await repo.ensure_power_spec_agent()

    assert result is existing
    ensure.assert_not_awaited()
