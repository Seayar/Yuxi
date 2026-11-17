"""设备确认卡片在真实 LangGraph 中断/恢复下的行为。"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from yuxi.knowledge.device_selection import (
    DeviceOption,
    build_device_choice_payload,
    parse_device_choice,
)

pytestmark = pytest.mark.unit

QUESTION_ID = "fault_trace_device_select"
DEVICE = DeviceOption("LGR#3b7y", "3B-7Y", "架空线路", "10kV", "变电设施 > 3B-7Y")


class _State(TypedDict, total=False):
    result: dict[str, Any]


def _build_graph() -> Any:
    async def confirm(_state: _State):
        answer = interrupt(
            build_device_choice_payload(
                source="fault_trace_device_choice",
                question_id=QUESTION_ID,
                question="请确认目标设备",
                options=[DEVICE],
            )
        )
        choice = parse_device_choice(answer, QUESTION_ID)
        return {"result": {"selected_id": choice.selected_id, "answer": answer}}

    builder = StateGraph(_State)
    builder.add_node("confirm", confirm)
    builder.add_edge(START, "confirm")
    builder.add_edge("confirm", END)
    return builder.compile(checkpointer=InMemorySaver())


@pytest.mark.asyncio
async def test_resume_returns_saved_device_id():
    """恢复时节点拿到用户选中的图谱设备 id。"""

    graph = _build_graph()
    config = {"configurable": {"thread_id": "confirm-1"}}
    first = await graph.ainvoke({}, config=config)
    question = first["__interrupt__"][0].value["questions"][0]
    assert question["options"][0]["value"] == DEVICE.device_id

    final = await graph.ainvoke(
        Command(resume={question["question_id"]: question["options"][0]["value"]}), config=config
    )

    assert final["result"]["selected_id"] == DEVICE.device_id


@pytest.mark.asyncio
async def test_plain_device_id_answer_also_counts():
    """只回传候选 id 的客户端同样能恢复为设备选择。"""

    graph = _build_graph()
    config = {"configurable": {"thread_id": "confirm-2"}}
    await graph.ainvoke({}, config=config)

    final = await graph.ainvoke(Command(resume={QUESTION_ID: DEVICE.device_id}), config=config)

    assert final["result"]["selected_id"] == DEVICE.device_id
