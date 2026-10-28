"""智能体思考过程注册表、四步契约与发射器单测。"""

from __future__ import annotations

import pytest
from yuxi.agents import thinking
from yuxi.agents.thinking import (
    ThinkingEmitter,
    ThinkingStepSpec,
    get_thinking_steps,
    normalize_thinking_event,
    register_thinking_steps,
    thinking_steps_payload,
)
from yuxi.knowledge.fault_trace.contracts import FAULT_TRACE_THINKING_STEPS
from yuxi.knowledge.power_spec.contracts import POWER_SPEC_THINKING_STEPS

pytestmark = pytest.mark.unit


def _steps() -> tuple[ThinkingStepSpec, ...]:
    return (
        ThinkingStepSpec("one", "第一步"),
        ThinkingStepSpec("two", "第二步"),
    )


def test_register_and_payload_are_agent_owned():
    register_thinking_steps("test-thinking-agent", _steps())

    assert [step.step_id for step in get_thinking_steps("test-thinking-agent")] == ["one", "two"]
    assert thinking_steps_payload("test-thinking-agent") == [
        {"step_id": "one", "title": "第一步", "description": ""},
        {"step_id": "two", "title": "第二步", "description": ""},
    ]


def test_register_rejects_more_than_four_steps_and_duplicate_ids():
    too_many = tuple(ThinkingStepSpec(f"step-{index}", f"步骤{index}") for index in range(5))
    with pytest.raises(ValueError, match="最多"):
        register_thinking_steps("test-too-many", too_many)
    with pytest.raises(ValueError, match="不能重复"):
        register_thinking_steps("test-duplicate", (ThinkingStepSpec("same", "A"), ThinkingStepSpec("same", "B")))


def test_normalize_rejects_unknown_step_and_wrong_index():
    steps = _steps()

    assert normalize_thinking_event({"step_id": "unknown", "step_index": 1, "state": "completed"}, steps) is None
    assert normalize_thinking_event({"step_id": "one", "step_index": 2, "state": "completed"}, steps) is None
    normalized = normalize_thinking_event(
        {"step_id": "one", "step_index": 1, "state": "completed", "title": "旧标题", "content": "完成"},
        steps,
    )
    assert normalized["title"] == "第一步"
    assert normalized["content"] == "完成"


def test_emitter_writes_custom_event_and_upserts_snapshot(monkeypatch):
    written = []
    monkeypatch.setattr(thinking, "get_stream_writer", lambda: written.append)

    emitter = ThinkingEmitter(agent_slug="test-emitter", steps=_steps())
    emitter.emit("one", "started", content="开始")
    emitter.emit("one", "completed", content="完成", data={"count": 1})

    assert len(written) == 2
    assert written[-1]["type"] == thinking.THINKING_EVENT_TYPE
    assert written[-1]["thinking"]["title"] == "第一步"
    assert emitter.snapshot()[-1]["state"] == "completed"
    assert [item["step_id"] for item in emitter.snapshot()] == ["one"]


def test_power_spec_and_fault_trace_have_exact_four_prototype_titles():
    assert [step.title for step in POWER_SPEC_THINKING_STEPS] == [
        "解析自然语言查询",
        "检索规程标准知识库",
        "定位相关条款",
        "生成检索结果",
    ]
    assert [step.title for step in FAULT_TRACE_THINKING_STEPS] == [
        "解析故障描述",
        "匹配因果链",
        "拓扑关系追溯",
        "生成溯源报告",
    ]
    assert len(POWER_SPEC_THINKING_STEPS) == 4
    assert len(FAULT_TRACE_THINKING_STEPS) == 4
