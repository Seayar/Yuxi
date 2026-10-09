"""验证意图确认在真实 LangGraph 中断与恢复语义下的循环行为。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from yuxi.knowledge.power_spec import tool as tool_module
from yuxi.knowledge.power_spec.contracts import PowerSpecQuery
from yuxi.knowledge.power_spec.contracts import INTENT_ATTEMPTS_EXHAUSTED_MESSAGE
from yuxi.knowledge.power_spec.tool import _confirm_intent_with_user

QUESTION = "220 的变压器色谱多久做一回？"


class _State(TypedDict, total=False):
    result: str


def _build_graph(monkeypatch, *, model_calls: list[str]) -> tuple[object, dict]:
    """构造只包含意图确认节点的图，模型调用被记录但不访问真实服务。"""

    async def fake_model_call(prompt: str) -> str:
        model_calls.append(prompt)
        voltage = "110kV" if "电压等级是 110kV" in prompt else "220kV"
        return json.dumps(
            {
                "question": f"{voltage} 变压器色谱试验周期",
                "intent": "检修标准查询",
                "device_type": "变压器",
                "voltage_level": voltage,
                "answer_dimensions": ["周期"],
                "keywords": [voltage, "变压器", "色谱"],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(tool_module, "_build_intent_model_call", lambda _context: fake_model_call)

    async def node(_state: _State) -> _State:
        context = SimpleNamespace(model="fake:model")
        query = await _confirm_intent_with_user(QUESTION, context=context)
        assert isinstance(query, PowerSpecQuery) or isinstance(query, str)
        return {"result": query if isinstance(query, str) else query.query}

    graph = StateGraph(_State)
    graph.add_node("confirm", node)
    graph.add_edge(START, "confirm")
    graph.add_edge("confirm", END)
    return graph.compile(checkpointer=InMemorySaver()), {"configurable": {"thread_id": "intent-loop"}}


@pytest.mark.asyncio
async def test_langgraph_interrupt_repeats_confirmation_after_correction(monkeypatch):
    model_calls: list[str] = []
    app, config = _build_graph(monkeypatch, model_calls=model_calls)

    first = await app.ainvoke({}, config=config)
    first_interrupt = first["__interrupt__"][0].value
    assert first_interrupt["source"] == "power_spec_intent_confirmation"
    assert first_interrupt["intent_plan"]["attempt"] == 1
    assert first_interrupt["intent_plan"]["voltage_level"] == "220kV"

    second = await app.ainvoke(
        Command(resume={"power_spec_intent_confirm": {"type": "other", "text": "电压等级是 110kV", "selected": []}}),
        config=config,
    )
    second_interrupt = second["__interrupt__"][0].value
    assert second_interrupt["source"] == "power_spec_intent_confirmation"
    assert second_interrupt["intent_plan"]["attempt"] == 2
    assert second_interrupt["intent_plan"]["voltage_level"] == "110kV"

    third = await app.ainvoke(Command(resume={"power_spec_intent_confirm": "confirm"}), config=config)
    assert "__interrupt__" not in third
    assert third["result"] == "110kV 变压器色谱试验周期"
    # 节点在每次 resume 时重新执行，历次识别会重跑：1 + 2 + 2 = 5 次。
    assert len(model_calls) == 5


@pytest.mark.asyncio
async def test_langgraph_interrupt_asks_for_details_then_reidentifies(monkeypatch):
    model_calls: list[str] = []
    app, config = _build_graph(monkeypatch, model_calls=model_calls)

    await app.ainvoke({}, config=config)

    ask = await app.ainvoke(Command(resume={"power_spec_intent_confirm": "correct"}), config=config)
    assert ask["__interrupt__"][0].value["source"] == "power_spec_intent_correction"

    reidentified = await app.ainvoke(
        Command(resume={"power_spec_intent_correction": {"type": "other", "text": "电压等级是 110kV", "selected": []}}),
        config=config,
    )
    assert reidentified["__interrupt__"][0].value["intent_plan"]["voltage_level"] == "110kV"

    final = await app.ainvoke(Command(resume={"power_spec_intent_confirm": "confirm"}), config=config)
    assert final["result"] == "110kV 变压器色谱试验周期"


@pytest.mark.asyncio
async def test_langgraph_interrupt_stops_after_attempt_limit(monkeypatch):
    model_calls: list[str] = []
    app, config = _build_graph(monkeypatch, model_calls=model_calls)
    rejection = {"power_spec_intent_confirm": {"type": "other", "text": "还是不对", "selected": []}}

    interrupted = await app.ainvoke({}, config=config)
    for expected_attempt in (2, 3):
        interrupted = await app.ainvoke(Command(resume=rejection), config=config)
        assert interrupted["__interrupt__"][0].value["intent_plan"]["attempt"] == expected_attempt

    final = await app.ainvoke(Command(resume=rejection), config=config)

    assert "__interrupt__" not in final
    assert final["result"] == INTENT_ATTEMPTS_EXHAUSTED_MESSAGE
    # 三次识别在四次节点执行中分别重跑：1 + 2 + 3 + 3 = 9 次。
    assert len(model_calls) == 9
