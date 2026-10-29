"""确认卡片在真实 LangGraph 中断/恢复下的行为。

用户点“正确”后，恢复值就是选项值 ``confirm``；节点据此继续，不做任何“与重算结果是否
一致”的校验。这里用真实 ``StateGraph`` + ``interrupt`` 证明恢复值按保存内容返回。
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from yuxi.knowledge.fault_trace.intent import (
    CONFIRM_VALUE,
    build_confirmation_payload,
    parse_intent_confirmation,
)

pytestmark = pytest.mark.unit

PLAN = {
    "device_name": "3B-7Y",
    "device_type": "架空线路",
    "voltage_level": "10kV",
    "phenomena": ["树木倒伏"],
    "image_fault_name": None,
}


class _State(TypedDict, total=False):
    result: dict[str, Any]


def _build_graph() -> Any:
    async def confirm(_state: _State):
        answer = interrupt(build_confirmation_payload(PLAN))
        decision, _correction = parse_intent_confirmation(answer)
        return {"result": {"decision": decision, "answer": answer}}

    builder = StateGraph(_State)
    builder.add_node("confirm", confirm)
    builder.add_edge(START, "confirm")
    builder.add_edge("confirm", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.mark.asyncio
async def test_resume_returns_saved_confirm_value():
    """恢复时节点拿到保存的 ``confirm``，直接判定为确认。"""

    graph = _build_graph()
    config = {"configurable": {"thread_id": "confirm-1"}}
    first = await graph.ainvoke({}, config=config)
    question = first["__interrupt__"][0].value["questions"][0]
    assert question["options"][0]["value"] == CONFIRM_VALUE

    final = await graph.ainvoke(
        Command(resume={question["question_id"]: question["options"][0]["value"]}), config=config
    )

    assert final["result"]["decision"] == "confirm"


@pytest.mark.asyncio
async def test_plain_confirm_answer_also_counts():
    """只回传 ``confirm`` 的客户端同样被当作确认，没有额外校验会失败。"""

    graph = _build_graph()
    config = {"configurable": {"thread_id": "confirm-2"}}
    await graph.ainvoke({}, config=config)

    final = await graph.ainvoke(Command(resume={"fault_trace_intent_confirm": "confirm"}), config=config)

    assert final["result"]["decision"] == "confirm"
