"""验证一轮里出现多个同源待恢复中断时，恢复值能按中断 id 逐个投递。

背景：含两个子问题的多跳题会让模型在同一轮并行发起两次 ``search_power_spec``，每个
调用各自 ``interrupt()``，checkpoint 因而留下多个待恢复中断。此时投递裸值 LangGraph
无法判断归属，会抛 ``RuntimeError`` 让整次运行失败（线上实测 MULTI_05/10/15）。
"""

from __future__ import annotations

import operator
import uuid
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from yuxi.agents.base import BaseAgent
from yuxi.agents.context import BaseContext

INTENT_SOURCE = "power_spec_intent_confirmation"
APPROVAL_SOURCE = "tool_approval"


class _State(TypedDict):
    answers: Annotated[list[str], operator.add]


def _build_graph(sources: tuple[str, ...]):
    """构造一轮内并行发起 ``len(sources)`` 个中断的图。"""

    def fan_out(state: _State):
        return [Send("ask", {"index": index}) for index in range(len(sources))]

    def ask(payload: dict[str, Any]):
        answer = interrupt({"source": sources[payload["index"]], "index": payload["index"]})
        return {"answers": [f"{payload['index']}:{answer}"]}

    builder = StateGraph(_State)
    builder.add_node("fan_out", lambda state: {})
    builder.add_node("ask", ask)
    builder.add_edge(START, "fan_out")
    builder.add_conditional_edges("fan_out", fan_out, ["ask"])
    builder.add_edge("ask", END)
    return builder.compile(checkpointer=InMemorySaver())


class _StubAgent(BaseAgent):
    """测试替身：``get_graph`` 直接返回给定图。"""

    name = "stub_agent"
    description = "stub"
    context_schema = BaseContext

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def get_graph(self, **kwargs):
        return self._graph


def _context() -> BaseContext:
    return BaseContext(thread_id=str(uuid.uuid4()), uid="test-uid")


def _config(context: BaseContext) -> dict:
    return {"configurable": {"thread_id": context.thread_id, "uid": context.uid}}


async def test_multiple_same_source_interrupts_are_bound_by_interrupt_id():
    """两个同源中断：裸值必失败，按 id 绑定后两个中断都拿到同一个恢复值。"""

    graph = _build_graph((INTENT_SOURCE, INTENT_SOURCE))
    context = _context()
    config = _config(context)
    await graph.ainvoke({"answers": []}, config)

    state = await graph.aget_state(config)
    assert len(state.interrupts) == 2

    # 缺陷本身：裸值恢复无法定位中断，整次运行失败。
    with pytest.raises(RuntimeError, match="multiple pending interrupts"):
        await graph.ainvoke(Command(resume="confirm"), config)

    agent = _StubAgent(graph)
    bound = await agent._bind_resume_to_pending_interrupts(Command(resume="confirm"), context=context)
    assert isinstance(bound.resume, dict)
    assert sorted(bound.resume) == sorted(item.id for item in state.interrupts)
    assert set(bound.resume.values()) == {"confirm"}

    result = await graph.ainvoke(bound, config)
    assert sorted(result["answers"]) == ["0:confirm", "1:confirm"]


async def test_single_interrupt_keeps_bare_resume_value():
    """只有一个待恢复中断时保持原有裸值行为，不做改写。"""

    graph = _build_graph((INTENT_SOURCE,))
    context = _context()
    await graph.ainvoke({"answers": []}, _config(context))

    agent = _StubAgent(graph)
    command = Command(resume="confirm")
    bound = await agent._bind_resume_to_pending_interrupts(command, context=context)
    assert bound.resume == "confirm"
    assert (await graph.ainvoke(bound, _config(context)))["answers"] == ["0:confirm"]


async def test_different_source_interrupts_keep_bare_resume_value():
    """中断来源不一致时不改写：同一个值不能同时充当意图确认与工具审批。"""

    graph = _build_graph((INTENT_SOURCE, APPROVAL_SOURCE))
    context = _context()
    await graph.ainvoke({"answers": []}, _config(context))

    agent = _StubAgent(graph)
    bound = await agent._bind_resume_to_pending_interrupts(Command(resume="confirm"), context=context)
    assert bound.resume == "confirm"


async def test_resume_value_already_keyed_by_interrupt_id_is_untouched():
    """已按中断 id 组织的恢复值直接透传，不二次包装。"""

    graph = _build_graph((INTENT_SOURCE, INTENT_SOURCE))
    context = _context()
    await graph.ainvoke({"answers": []}, _config(context))

    agent = _StubAgent(graph)
    keyed = {"0" * 32: "confirm"}
    command = Command(resume=keyed)
    bound = await agent._bind_resume_to_pending_interrupts(command, context=context)
    assert bound.resume == keyed


async def test_state_read_failure_falls_back_to_original_command():
    """读取中断失败时按单中断恢复，不能因为读取异常阻断正常恢复。"""

    class _BrokenAgent(_StubAgent):
        async def get_graph(self, **kwargs):
            raise RuntimeError("boom")

    agent = _BrokenAgent(None)
    command = Command(resume="confirm")
    bound = await agent._bind_resume_to_pending_interrupts(command, context=_context())
    assert bound is command


async def test_non_resume_input_is_passed_through():
    """非恢复输入（普通消息流）原样返回。"""

    agent = _StubAgent(_build_graph((INTENT_SOURCE,)))
    payload = {"messages": ["你好"]}
    assert await agent._bind_resume_to_pending_interrupts(payload, context=_context()) is payload
