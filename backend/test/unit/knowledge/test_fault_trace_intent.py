"""故障溯源意图识别与输入约束单元测试。"""

from __future__ import annotations

import json

import pytest
from yuxi.knowledge.fault_trace import intent as intent_module
from yuxi.knowledge.fault_trace.contracts import FaultTraceQuery
from yuxi.knowledge.fault_trace.intent import (
    apply_input_constraints,
    build_confirmation_payload,
    extract_fault_trace_intent,
    parse_fault_trace_query,
    parse_intent_confirmation,
)

pytestmark = pytest.mark.unit


class _FakeModelCall:
    """替身模型调用，按构造参数返回固定 JSON。"""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.raw


def _intent_json(*, device_name="1号主变", device_type="变压器", voltage="110kV", phenomena=None):
    return json.dumps(
        {
            "question": "1号主变冷却器风扇停转",
            "device_name": device_name,
            "device_type": device_type,
            "voltage_level": voltage,
            "phenomena": phenomena or ["冷却器风扇停转"],
        },
        ensure_ascii=False,
    )


def test_rule_parse_extracts_device_and_phenomena():
    query = parse_fault_trace_query("1号主变冷却器风扇停转，散热片表面积尘严重")

    assert query.device_name == "1号主变"
    assert query.device_type is None  # “主变”不在类型词表，仅名称可定位
    assert "停转" in query.phenomena
    assert "积尘" not in query.phenomena  # 规则词表只收录定义的现象词


@pytest.mark.asyncio
async def test_model_extraction_merges_image_result():
    query = await extract_fault_trace_intent(
        "1号主变风扇不转",
        image_result={"fault": True, "fault_type": "fire", "fault_name": "冒烟/着火"},
        model_call=_FakeModelCall(_intent_json(phenomena=["冷却器风扇停转", "冒烟/着火"])),
    )

    assert query.device_name == "1号主变"
    assert query.image_fault_name == "冒烟/着火"
    assert "冷却器风扇停转" in query.phenomena


@pytest.mark.asyncio
async def test_model_extraction_falls_back_to_rules_on_invalid_output():
    query = await extract_fault_trace_intent(
        "1号主变冷却器风扇停转",
        image_result=None,
        model_call=_FakeModelCall("not json at all"),
    )

    assert query.device_name == "1号主变"
    assert "停转" in query.phenomena


def test_input_constraint_requires_specific_device():
    query = FaultTraceQuery(device_type="变压器", phenomena=["过热"])
    applied = apply_input_constraints(query)

    assert applied.clarification_required is True
    assert applied.clarification_message == "未能识别故障设备，请明确设备名称后重新提交。"


def test_input_constraint_missing_phenomenon():
    query = FaultTraceQuery(device_name="1号主变")
    applied = apply_input_constraints(query)

    assert applied.clarification_required is True
    assert "缺少故障现象" in applied.clarification_message


def test_input_constraint_image_only_still_needs_text():
    query = FaultTraceQuery(device_name="1号主变", image_fault_name="绝缘子破损")
    applied = apply_input_constraints(query)

    # 图片结果也算现象，但设备必须具体；此例设备具体且现象存在，无需澄清。
    assert applied.clarification_required is False


def test_confirmation_payload_and_parse():
    query = FaultTraceQuery(device_name="1号主变", phenomena=["过热"], image_fault_name="冒烟/着火")
    payload = build_confirmation_payload(intent_module.build_intent_plan(query))

    assert payload["source"] == "fault_trace_intent_confirmation"
    assert "1号主变" in payload["questions"][0]["question"]
    assert parse_intent_confirmation(
        {"fault_trace_intent_confirm": {"selected": [{"value": "confirm"}]}}
    )[0] == "confirm"
    assert parse_intent_confirmation({"fault_trace_intent_confirm": {"text": "设备是2号主变"}}) == (
        "reject",
        "设备是2号主变",
    )


@pytest.mark.asyncio
async def test_extract_prompt_includes_image_result(monkeypatch):
    fake = _FakeModelCall(_intent_json())
    _ = await extract_fault_trace_intent(
        "1号主变风扇不转",
        image_result={"fault": True, "fault_type": "fire", "fault_name": "冒烟/着火"},
        model_call=fake,
    )

    assert fake.prompts
    assert "fault_name=冒烟/着火" in fake.prompts[0] or "冒烟/着火" in fake.prompts[0]
