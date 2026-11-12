"""设备预测维护的设备语义解析单测（不连接图谱、外部服务与大模型）。"""

from __future__ import annotations

import asyncio

import pytest

from yuxi.knowledge.device_prediction import intent
from yuxi.knowledge.device_prediction.contracts import CLARIFY_DEVICE_MESSAGE

pytestmark = pytest.mark.unit


def _extract(text, *, model=None, previous=None):
    return asyncio.run(intent.extract_prediction_intent(text, model_call=model, previous=previous))


def test_rule_extract_keeps_user_device_reference():
    """需求示例“评估1号主变的健康状态”能抽出设备指称与设备类型。"""

    query = intent.rule_extract_prediction("评估1号主变的健康状态")

    assert query.device_name == "1号主变"
    assert query.device_type == "变压器"


def test_type_only_question_does_not_clarify():
    """只说设备类型时按类型召回候选，不进入反问。"""

    query = _extract("预测10kV开关柜未来有没有过热风险")

    assert query.clarification_required is False
    assert query.device_type == "开关柜"
    assert query.voltage_level == "10kV"


@pytest.mark.parametrize("text", ["你好", "帮我看看设备情况"])
def test_missing_device_clarifies(text):
    """既没有设备名称也没有设备类型 → 固定澄清文案。"""

    query = _extract(text)

    assert query.clarification_required is True
    assert query.clarification_message == CLARIFY_DEVICE_MESSAGE


def test_model_result_fills_missing_device_name():
    """规则抽不出设备名时采用模型结果，并把设备类型归一。"""

    async def model_call(prompt: str) -> str:
        assert "设备识别器" in prompt
        return '{"device_name": "馈线八_922", "device_type": "线路", "voltage_level": "10KV"}'

    query = _extract("这条馈线当前状态怎样", model=model_call)

    assert query.device_name == "馈线八_922"
    assert query.device_type == "架空线路"
    assert query.voltage_level == "10kV"


def test_rule_reference_wins_over_model_expansion():
    """原文已识别到具体编号时，模型扩写成整句设备名不能覆盖规则结果。"""

    async def model_call(prompt: str) -> str:
        return '{"device_name": "3k18开关柜", "device_type": "开关柜"}'

    query = _extract("评估3k18的健康状态", model=model_call)

    assert query.device_name == "3k18"


def test_model_failure_falls_back_to_rules():
    """模型输出非法 JSON 时回退规则解析，不抛出。"""

    async def model_call(prompt: str) -> str:
        return "我先想一想"

    query = _extract("评估1号主变的健康状态", model=model_call)

    assert query.device_name == "1号主变"
    assert query.clarification_required is False


def test_previous_result_backfills_missing_fields():
    """用户重新提问时，上一轮结果用于补齐本轮缺失字段。"""

    previous = intent.apply_input_constraints(intent.rule_extract_prediction("9号馈线状态"))

    async def model_call(prompt: str) -> str:
        assert "上一轮识别结果" in prompt
        return '{"device_name": "9号馈线"}'

    query = _extract("9号馈线", model=model_call, previous=previous)

    assert query.device_name == "9号馈线"
    assert query.raw_text == "9号馈线状态"


def test_context_model_call_requires_model():
    """上下文没有模型标识时不构造模型调用。"""

    assert intent.context_model_call(None) is None
    assert intent.context_model_call(object()) is None
