import json

import pytest

from yuxi.knowledge.power_spec.intent import (
    build_intent_prompt,
    extract_power_spec_intent,
    parse_power_spec_query,
)

pytestmark = pytest.mark.asyncio

MODEL_OUTPUT = {
    "question": "110kV 变压器的温升限值是多少",
    "intent": "设备参数查询",
    "device_name": None,
    "device_type": "变压器",
    "voltage_level": "110kV",
    "answer_dimensions": ["温升"],
    "keywords": ["110kV", "变压器", "温升限值"],
}


def _model_call(payload):
    async def call(_prompt):
        return payload

    return call


async def test_extract_power_spec_intent_uses_model_result():
    query = await extract_power_spec_intent("110kV变压器温升限值", model_call=_model_call(MODEL_OUTPUT))

    assert query.query == "110kV 变压器的温升限值是多少"
    assert query.intent == "设备参数查询"
    assert query.device_type == "变压器"
    assert query.voltage_level == "110kV"
    assert query.answer_dimensions == ["温升"]
    assert query.normalized_terms[:3] == ["温升", "变压器", "110kV"]
    assert query.clarification_required is False


async def test_extract_power_spec_intent_parses_fenced_json():
    fenced = f"```json\n{json.dumps(MODEL_OUTPUT, ensure_ascii=False)}\n```"

    query = await extract_power_spec_intent("110kV变压器温升限值", model_call=_model_call(fenced))

    assert query.intent == "设备参数查询"


async def test_extract_power_spec_intent_falls_back_on_invalid_json():
    query = await extract_power_spec_intent(
        "220 的变压器色谱多久做一回",
        model_call=_model_call("模型输出的截断内容"),
    )

    assert query.voltage_level == "220kV"
    assert "色谱" in query.answer_dimensions


async def test_extract_power_spec_intent_falls_back_on_model_error():
    async def boom(_prompt):
        raise RuntimeError("模型不可用")

    query = await extract_power_spec_intent("110kV变压器温升限值", model_call=boom)

    assert query.device_type == "变压器"
    assert query.voltage_level == "110kV"


async def test_extract_power_spec_intent_falls_back_without_model_call():
    query = await extract_power_spec_intent("110kV变压器温升限值")

    assert query.device_type == "变压器"
    assert query.voltage_level == "110kV"


async def test_extract_power_spec_intent_normalizes_unknown_values():
    payload = {
        **MODEL_OUTPUT,
        "intent": "未知意图",
        "device_type": "无",
        "voltage_level": "",
        "device_name": "  ",
        "answer_dimensions": ["温升", "温升", ""],
        "keywords": ["温升", "温升", "变压器"],
    }

    query = await extract_power_spec_intent("111", model_call=_model_call(payload))

    assert query.intent == "其他类型"
    assert query.device_type is None
    assert query.voltage_level is None
    assert query.device_name is None
    assert query.answer_dimensions == ["温升"]
    assert query.query == MODEL_OUTPUT["question"]
    assert query.clarification_required is True


async def test_extract_power_spec_intent_falls_back_when_question_blank():
    payload = {**MODEL_OUTPUT, "question": "   "}

    query = await extract_power_spec_intent("110kV变压器温升限值", model_call=_model_call(payload))

    assert query.query == "110kV变压器温升限值"


async def test_build_intent_prompt_includes_previous_and_feedback():
    previous = parse_power_spec_query("220 的变压器色谱多久做一回")

    prompt = build_intent_prompt("应该是 110kV", previous=previous, feedback="电压等级是 110kV")

    assert "上一轮识别结果" in prompt
    assert "用户的修正说明" in prompt
    assert previous.query in prompt
    assert "电压等级是 110kV" in prompt
