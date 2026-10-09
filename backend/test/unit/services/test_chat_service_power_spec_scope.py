"""电力规范发布门的轮次归属。

checkpoint 里包含线程完整历史，发布门如果扫描整条历史，第二轮没有调用检索 Tool 时
就会把上一轮的卡片当成本轮答案。这里锁住"只认本轮工具结果"。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.messages import AIMessage, HumanMessage, ToolMessage

from yuxi.knowledge.power_spec.contracts import NO_EVIDENCE_MESSAGE
from yuxi.services import chat_service as svc


OLD_CARDS = "### 规范卡片 1 · DL/T596—2021 条款 9.8"
NEW_CARDS = "### 规范卡片 1 · DL/T393—2021 条款 6.1.2.2"


def _cards_payload(markdown: str) -> str:
    return f'{{"status": "ok", "answer_markdown": "{markdown}"}}'


def _history(*, with_second_turn_tool: bool, second_turn_tool_content: str = _cards_payload(NEW_CARDS)):
    """构造两轮对话的 checkpoint 消息：第一轮有卡片，第二轮可能没有工具调用。"""

    messages = [
        HumanMessage(content="110kV变压器额定容量与温升限值是多少？"),
        AIMessage(content="", tool_calls=[{"id": "call-1", "name": "search_power_spec", "args": {}}]),
        ToolMessage(content=_cards_payload(OLD_CARDS), tool_call_id="call-1"),
        AIMessage(content=OLD_CARDS),
        HumanMessage(content="测试"),
    ]
    if with_second_turn_tool:
        messages.extend(
            [
                AIMessage(content="", tool_calls=[{"id": "call-2", "name": "search_power_spec", "args": {}}]),
                ToolMessage(content=second_turn_tool_content, tool_call_id="call-2"),
            ]
        )
    messages.append(AIMessage(content="模型自己的回答"))
    return messages


def test_second_turn_without_tool_call_does_not_reuse_previous_cards():
    """负向：本轮没有检索结果时必须回退模型内容，不能重复发布上一轮卡片。

    本轮未调用工具时 `current_tool_operation_ids` 为空集，因此走按用户消息划界的路径。
    """

    messages = _history(with_second_turn_tool=False)

    result = svc._resolve_power_spec_output(messages, "模型自己的回答", run_tool_call_ids=set())

    assert result == "模型自己的回答"
    assert OLD_CARDS not in result


def test_second_turn_with_tool_call_uses_current_run_result_only():
    """本轮有检索结果时用本轮结果，历史里的旧卡片不参与。"""

    messages = _history(with_second_turn_tool=True)

    result = svc._resolve_power_spec_output(messages, "模型自己的回答", run_tool_call_ids={"call-2"})

    assert NEW_CARDS in result
    assert OLD_CARDS not in result


def test_current_turn_scope_excludes_previous_run_tool_results():
    """归属按运行判定：只保留本轮 tool_call_id 对应的工具消息。"""

    messages = _history(with_second_turn_tool=True)

    scoped = svc._current_turn_tool_messages(messages, {"call-1"})

    assert [svc._message_field(message, "tool_call_id") for message in scoped] == ["call-1"]


def test_scope_without_run_ownership_falls_back_to_last_user_message():
    """运行归属不可用时按最后一条用户消息划界，仍然不跨轮。"""

    messages = _history(with_second_turn_tool=True)

    scoped = svc._current_turn_tool_messages(messages, set())

    assert [svc._message_field(message, "tool_call_id") for message in scoped] == ["call-2"]


def test_plain_tool_message_from_current_turn_is_passed_through():
    messages = _history(with_second_turn_tool=True, second_turn_tool_content=NO_EVIDENCE_MESSAGE)

    result = svc._resolve_power_spec_output(messages, "模型自己的回答", run_tool_call_ids={"call-2"})

    assert result == NO_EVIDENCE_MESSAGE


def test_dict_messages_are_supported():
    """checkpoint 消息可能是普通字典，字段读取要同时支持两种形态。"""

    messages = [
        {"type": "human", "content": "测试"},
        {"type": "tool", "tool_call_id": "call-1", "content": _cards_payload(NEW_CARDS)},
    ]

    result = svc._resolve_power_spec_output(messages, "模型自己的回答", run_tool_call_ids={"call-1"})

    assert NEW_CARDS in result


@pytest.mark.asyncio
async def test_saved_second_turn_content_keeps_model_answer(monkeypatch) -> None:
    """端到端：第二轮保存的助手内容不能是上一轮的卡片。"""

    saved: list[dict] = []

    class FakeConversationRepository:
        def __init__(self, _db):
            pass

        async def get_message_source_ids_by_thread_id(self, _thread_id: str):
            return set()

        async def add_message_by_thread_id(self, **kwargs):
            saved.append(kwargs)
            return SimpleNamespace(id=len(saved))

        async def get_attachments_by_request_id(self, _conversation_id: int, _request_id: str):
            return []

        async def bind_attachments_to_request(self, _conversation_id: int, _request_id: str, _file_ids: list[str]):
            return []

        async def add_tool_call(self, **_kwargs):
            return SimpleNamespace(id=1)

        async def update_tool_call_output(self, **_kwargs):
            return None

    state = SimpleNamespace(values={"messages": _history(with_second_turn_tool=False)})
    conv_repo = FakeConversationRepository(None)

    await svc.save_messages_from_langgraph_state(
        state=state,
        thread_id="thread-1",
        conv_repo=conv_repo,
        trace_info=None,
        agent_context=SimpleNamespace(skills=["power-spec"]),
    )

    contents = [item["content"] for item in saved]
    # 历史消息会照常落库（第一轮的卡片本来就是第一轮的答案）；本轮消息不能用它顶替。
    assert contents[-1] == "模型自己的回答"
