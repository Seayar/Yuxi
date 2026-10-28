"""确认卡片在真实 LangGraph 中断/恢复下的行为。

用真实 ``StateGraph`` + ``interrupt`` 证明：resume 时会拿到 checkpoint 里保存的选项值，
节点重算出的计划与保存值里的指纹可以比对；这样 ``workflow`` 里的“重放重算不一致”守卫
才有运行时依据，而不是只靠假 ``interrupt`` 的模拟。
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from yuxi.knowledge.fault_trace.intent import (
    build_confirmation_payload,
    parse_confirmation_token,
    parse_intent_confirmation,
    plan_token,
)

pytestmark = pytest.mark.unit

PLANS = [
    {"device_name": "3B-7Y", "device_type": "架空线路", "voltage_level": "10kV", "phenomena": ["树木倒伏"]},
    {"device_name": "3B-7Y", "device_type": "架空线路", "voltage_level": "10kV", "phenomena": ["异物搭挂"]},
]


class _State(TypedDict):
    plan_index: int
    result: dict[str, Any]


def _build_graph(plans: list[dict[str, Any]]):
    async def confirm(state: _State):
        plan = plans[state["plan_index"]]
        answer = interrupt(build_confirmation_payload(plan))
        decision, _correction = parse_intent_confirmation(answer)
        return {
            "result": {
                "decision": decision,
                "confirmed_token": parse_confirmation_token(answer),
                "expected_token": plan_token(plan),
            }
        }

    builder = StateGraph(_State)
    builder.add_node("confirm", confirm)
    builder.add_edge(START, "confirm")
    builder.add_edge("confirm", END)
    return builder.compile(checkpointer=InMemorySaver())


CONFIG = {"configurable": {"thread_id": "t-1"}}


async def _interrupt_value(graph: Any, plan_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑到中断点，返回展示给用户的载荷与 pending 状态。"""

    await graph.ainvoke({"plan_index": plan_index, "result": {}}, CONFIG)
    state = await graph.aget_state(CONFIG)
    payload = state.tasks[0].interrupts[0].value
    return payload, state.values


def _confirm_answer(payload: dict[str, Any]) -> dict[str, Any]:
    question = payload["questions"][0]
    return {question["question_id"]: question["options"][0]["value"]}


@pytest.mark.asyncio
async def test_resume_returns_saved_option_value():
    """同一份计划恢复时，节点拿到的是 checkpoint 保存的选项值，指纹一致。"""

    graph = _build_graph(PLANS)
    payload, _values = await _interrupt_value(graph, 0)

    final = await graph.ainvoke(Command(resume=_confirm_answer(payload)), CONFIG)

    assert final["result"]["decision"] == "confirm"
    assert final["result"]["confirmed_token"] == final["result"]["expected_token"]


@pytest.mark.asyncio
async def test_resume_with_changed_plan_is_detectable():
    """重放重算出另一份计划时，保存值里的指纹与新计划不一致，可被判为漂移。"""

    graph = _build_graph(PLANS)
    payload, _values = await _interrupt_value(graph, 0)
    saved_answer = _confirm_answer(payload)

    # 新一次运行（等价于 resume 后重放）用的是不同计划，但恢复值仍是用户确认过的那个。
    graph_replay = _build_graph(PLANS)
    await graph_replay.ainvoke({"plan_index": 1, "result": {}}, {"configurable": {"thread_id": "t-2"}})
    final = await graph_replay.ainvoke(Command(resume=saved_answer), {"configurable": {"thread_id": "t-2"}})

    assert final["result"]["decision"] == "confirm"
    assert final["result"]["confirmed_token"] != final["result"]["expected_token"]


@pytest.mark.asyncio
async def test_plain_confirm_without_token_stays_detectable():
    """恢复值只有“confirm”时没有指纹，同样能被识别出来（守卫按不一致处理）。"""

    graph = _build_graph(PLANS)
    await graph.ainvoke({"plan_index": 0, "result": {}}, CONFIG)

    final = await graph.ainvoke(Command(resume={"fault_trace_intent_confirm": "confirm"}), CONFIG)

    assert final["result"]["decision"] == "confirm"
    assert final["result"]["confirmed_token"] is None
    assert final["result"]["confirmed_token"] != final["result"]["expected_token"]
