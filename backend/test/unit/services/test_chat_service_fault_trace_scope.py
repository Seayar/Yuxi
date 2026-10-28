"""故障溯源发布门的轮次归属。

与电力规范发布门同一套规则：只认本轮 trace_fault 的工具结果，历史卡片不跨轮。
"""

from __future__ import annotations

from langchain.messages import AIMessage, HumanMessage, ToolMessage
from yuxi.knowledge.fault_trace.contracts import NO_FAULT_GRAPH_MESSAGE
from yuxi.services import chat_service as svc

OLD_REPORT = "### 上一轮溯源报告"
NEW_REPORT = "### 本轮溯源报告"


def _report_payload(markdown: str) -> str:
    return (
        '{"status": "ok", "answer_source": "fault_trace", "answer_verified": true, '
        f'"answer_markdown": "{markdown}"}}'
    )


def _history(*, with_second_turn_tool: bool, second_turn_tool_content: str = _report_payload(NEW_REPORT)):
    messages = [
        HumanMessage(content="1号主变冷却器风扇停转"),
        AIMessage(content="", tool_calls=[{"id": "call-1", "name": "trace_fault", "args": {}}]),
        ToolMessage(content=_report_payload(OLD_REPORT), tool_call_id="call-1"),
        AIMessage(content=OLD_REPORT),
        HumanMessage(content="测试"),
    ]
    if with_second_turn_tool:
        messages.extend(
            [
                AIMessage(content="", tool_calls=[{"id": "call-2", "name": "trace_fault", "args": {}}]),
                ToolMessage(content=second_turn_tool_content, tool_call_id="call-2"),
            ]
        )
    messages.append(AIMessage(content="模型自己的回答"))
    return messages


def test_fault_second_turn_without_tool_call_does_not_reuse_previous_report():
    messages = _history(with_second_turn_tool=False)

    result = svc._resolve_fault_trace_output(messages, "模型自己的回答", run_tool_call_ids=set())

    assert result == "模型自己的回答"
    assert OLD_REPORT not in result


def test_fault_second_turn_with_tool_call_uses_current_run_result_only():
    messages = _history(with_second_turn_tool=True)

    result = svc._resolve_fault_trace_output(messages, "模型自己的回答", run_tool_call_ids={"call-2"})

    assert NEW_REPORT in result
    assert OLD_REPORT not in result


def test_fault_plain_tool_message_is_passed_through():
    messages = _history(with_second_turn_tool=True, second_turn_tool_content=NO_FAULT_GRAPH_MESSAGE)

    result = svc._resolve_fault_trace_output(messages, "模型自己的回答", run_tool_call_ids={"call-2"})

    assert result == NO_FAULT_GRAPH_MESSAGE


def test_fault_context_flag_reads_fault_trace_skill():
    assert svc._is_fault_trace_context(type("C", (), {"skills": ["fault-trace"]})()) is True
    assert svc._is_fault_trace_context(type("C", (), {"skills": ["power-spec"]})()) is False
