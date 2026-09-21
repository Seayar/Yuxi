from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.agents.buildin.chatbot import graph as chatbot_graph
from yuxi.agents.buildin.subagent import graph as subagent_graph


def _context(summary_threshold: int = 123) -> SimpleNamespace:
    return SimpleNamespace(
        model="test-provider:test-model",
        summary_threshold=summary_threshold,
        summary_keep_messages=7,
        summary_prompt="SUMMARY {messages}",
        summary_tool_result_token_limit=300,
        summary_l2_trigger_ratio=0.75,
        tool_token_limit=3,
        model_retry_times=1,
        workdir_relative_path="projects/11111111-1111-4111-8111-111111111111",
        workdir_path="/home/gem/user-data/projects/11111111-1111-4111-8111-111111111111",
    )


def _patch_common_graph_deps(monkeypatch: pytest.MonkeyPatch, graph_module, captured: dict) -> None:
    def fake_load_chat_model(fully_specified_name, **kwargs):
        captured.setdefault("load_kwargs", []).append(kwargs)
        return object()

    monkeypatch.setattr(graph_module, "load_chat_model", fake_load_chat_model)
    monkeypatch.setattr(graph_module, "create_agent_filesystem_middleware", lambda *_args, **_kwargs: object())

    def create_summary_middleware(**kwargs):
        captured["summary_kwargs"] = kwargs
        return object()

    monkeypatch.setattr(graph_module, "create_summary_middleware", create_summary_middleware)


@pytest.mark.parametrize(
    ("graph_module", "threshold", "build_args", "patch_subagent_task"),
    [
        (chatbot_graph, 123, (object(),), True),
        (subagent_graph, 64, (object(), "default"), False),
    ],
)
@pytest.mark.unit
@pytest.mark.asyncio
async def test_summary_trim_limit_matches_summary_threshold(
    monkeypatch: pytest.MonkeyPatch, graph_module, threshold: int, build_args, patch_subagent_task: bool
) -> None:
    captured: dict = {}
    _patch_common_graph_deps(monkeypatch, graph_module, captured)

    async def no_subagent_middleware(_context):
        return None

    if patch_subagent_task:
        monkeypatch.setattr(graph_module, "create_subagent_task_middleware", no_subagent_middleware)

    middlewares = await graph_module._build_middlewares(_context(summary_threshold=threshold), *build_args)

    assert captured["summary_kwargs"]["trigger"] == ("tokens", threshold * 1024)
    assert captured["summary_kwargs"]["trim_tokens_to_summarize"] == threshold * 1024
    assert captured["summary_kwargs"]["l1_l2_trigger_ratio"] == 0.75
    middleware_names = [type(middleware).__name__ for middleware in middlewares]
    assert middleware_names.index("ModelRetryMiddleware") < middleware_names.index("ImageInputCompatibilityMiddleware")


@pytest.mark.parametrize(
    ("graph_module", "build_args", "expect_disable_thinking"),
    [
        (chatbot_graph, (object(),), False),
        (subagent_graph, (object(), "default"), True),
    ],
)
@pytest.mark.unit
@pytest.mark.asyncio
async def test_subagent_model_disables_thinking(
    monkeypatch: pytest.MonkeyPatch, graph_module, build_args, expect_disable_thinking: bool
) -> None:
    """子智能体模型关闭思考；主聊天模型保持供应商默认。"""

    captured: dict = {}
    _patch_common_graph_deps(monkeypatch, graph_module, captured)

    async def no_subagent_middleware(_context):
        return None

    if graph_module is chatbot_graph:
        monkeypatch.setattr(graph_module, "create_subagent_task_middleware", no_subagent_middleware)

    await graph_module._build_middlewares(_context(), *build_args)

    assert captured["load_kwargs"][0].get("disable_thinking", False) is expect_disable_thinking
