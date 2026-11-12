import json

import pytest

from yuxi.knowledge.power_spec.intent import (
    build_confirmation_payload,
    build_intent_plan,
    build_intent_prompt,
    decode_confirmed_plan,
    extract_power_spec_intent,
    parse_power_spec_query,
    query_from_confirmed_plan,
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


MULTI_MODEL_OUTPUT = {
    "question": "500kV 变压器注油后静置时间，以及局放超标时结合 DGA 和糠醛判断绝缘状态的方法",
    "intent": "检修标准查询",
    "device_type": "变压器",
    "device_name": None,
    "voltage_level": "500kV",
    "answer_dimensions": "静置时间、绝缘状态判断",
    "keywords": ["500kV", "变压器", "静置时间", "DGA", "糠醛"],
    "sub_questions": [
        {
            "question": "500kV 变压器大修真空注油后，进行交流耐压与局部放电试验前的最低静置时间是多少小时？",
            "intent": "检修标准查询",
            "device_type": "变压器",
            "voltage_level": "500kV",
            "answer_dimensions": "静置时间",
            "keywords": ["500kV", "变压器", "静置时间"],
        },
        {
            "question": "局放量超标时，如何结合油中溶解气体分析（DGA）及糠醛含量判断绝缘状态？",
            "intent": "检修标准查询",
            "device_type": "变压器",
            "voltage_level": "500kV",
            "answer_dimensions": "绝缘状态判断",
            "keywords": ["DGA", "糠醛", "绝缘状态"],
        },
    ],
}


async def test_extract_power_spec_intent_builds_multiple_sub_queries():
    query = await extract_power_spec_intent("问题1...问题2...", model_call=_model_call(MULTI_MODEL_OUTPUT))

    assert len(query.sub_queries) == 2
    assert [sub.intent for sub in query.sub_queries] == ["检修标准查询", "检修标准查询"]
    assert query.sub_queries[0].answer_dimensions == ["静置时间"]
    assert query.sub_queries[1].answer_dimensions == ["绝缘状态判断"]
    assert "静置时间" in query.normalized_terms
    assert "DGA" in query.normalized_terms
    assert query.clarification_required is False


async def test_multi_question_confirmation_lists_each_question_and_round_trips():
    query = await extract_power_spec_intent("问题1...问题2...", model_call=_model_call(MULTI_MODEL_OUTPUT))

    plan = build_intent_plan(query)
    payload = build_confirmation_payload(plan)
    question = payload["questions"][0]["question"]

    assert "2 个问题" in question
    assert "500kV 变压器大修真空注油后" in question
    assert "局放量超标时" in question
    confirmed = decode_confirmed_plan(payload["questions"][0]["options"][0]["value"])
    assert confirmed is not None
    restored = query_from_confirmed_plan(confirmed, fallback=query)
    assert [sub.query for sub in restored.sub_queries] == [sub.query for sub in query.sub_queries]
    assert restored.sub_queries[1].voltage_level == "500kV"


async def test_single_question_does_not_enable_multi_question_mode():
    query = await extract_power_spec_intent("110kV变压器温升限值", model_call=_model_call(MODEL_OUTPUT))

    assert query.sub_queries == []


async def test_explicit_subquestion_stems_are_kept_verbatim():
    """显式“问题1./问题2.”的题干必须以用户原句进入 sub_queries，计算尾句不得被模型改写吞掉。"""

    original = (
        "问题1. 500kV变压器大修真空注油后，进行交流耐压与局部放电试验前的最低静置时间是多少小时？ "
        "问题2. 局放量超标时，如何结合油中溶解气体分析（DGA）及糠醛含量判断绝缘状态，两处安全距离相差多少米？"
    )

    query = await extract_power_spec_intent(original, model_call=_model_call(MULTI_MODEL_OUTPUT))

    assert len(query.sub_queries) == 2
    assert query.sub_queries[0].query.endswith("最低静置时间是多少小时？")
    assert query.sub_queries[1].query.endswith("两处安全距离相差多少米？")
