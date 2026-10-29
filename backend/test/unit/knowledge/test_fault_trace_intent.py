"""故障溯源意图识别与输入约束单元测试。"""

from __future__ import annotations

import json

import pytest
from yuxi.knowledge.fault_trace import intent as intent_module
from yuxi.knowledge.fault_trace.contracts import DEVICE_NOT_FOUND_MESSAGE, FaultTraceQuery
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
    assert applied.clarification_message == DEVICE_NOT_FOUND_MESSAGE


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


def test_confirmation_payload_uses_plain_confirm_value():
    """确认卡片的“正确”选项就是普通确认值，恢复时不需要再和重算结果比对。"""

    payload = intent_module.build_confirmation_payload(
        {
            "device_name": "3B-7Y",
            "device_type": "架空线路",
            "voltage_level": "10kV",
            "phenomena": ["树木倒伏"],
            "image_fault_name": None,
        }
    )

    option = payload["questions"][0]["options"][0]
    assert option["label"] == "正确，继续溯源"
    assert option["value"] == intent_module.CONFIRM_VALUE
    assert intent_module.parse_intent_confirmation({payload["questions"][0]["question_id"]: option["value"]}) == (
        "confirm",
        None,
    )


def test_confirmation_payload_and_parse():
    query = FaultTraceQuery(device_name="1号主变", phenomena=["过热"], image_fault_name="冒烟/着火")
    payload = build_confirmation_payload(intent_module.build_intent_plan(query))

    assert payload["source"] == "fault_trace_intent_confirmation"
    assert "1号主变" in payload["questions"][0]["question"]
    assert (
        parse_intent_confirmation({"fault_trace_intent_confirm": {"selected": [{"value": "confirm"}]}})[0] == "confirm"
    )
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


def test_rule_parse_extracts_equipment_code_as_device():
    query = parse_fault_trace_query("3B-7Y 10kV 线路通道内树木倒伏，枝干压覆导线，现场有放电痕迹")

    assert query.device_name == "3B-7Y"
    assert query.voltage_level == "10kV"
    assert {"倒伏", "压覆", "放电"} <= set(query.phenomena)
    assert query.clarification_required is False


@pytest.mark.asyncio
async def test_model_missing_device_is_backfilled_from_rules():
    """模型漏识别设备时用规则补齐，保证确认卡与恢复重放后的意图一致。"""

    raw = json.dumps(
        {
            "question": "3B-7Y 树木倒伏",
            "device_name": None,
            "device_type": None,
            "voltage_level": None,
            "phenomena": ["树木倒伏"],
        },
        ensure_ascii=False,
    )
    query = await extract_fault_trace_intent(
        "3B-7Y 10kV 线路通道内树木倒伏，枝干压覆导线",
        image_result=None,
        model_call=_FakeModelCall(raw),
    )

    assert query.device_name == "3B-7Y"
    assert query.voltage_level == "10kV"
    assert query.clarification_required is False


def test_rule_parse_keeps_full_device_name_for_numbered_transformer():
    """带编号的主变压器名必须整体命中，避免截成“#1主变”后匹配到间隔/开关柜。"""

    query = parse_fault_trace_query("35kV#1变电站#1主变压器运行中绕组区域冒烟，本体温度报警，请分析原因。")

    assert query.device_name == "#1主变压器"
    assert query.voltage_level == "35kV"
    assert {"冒烟", "温度报警"} <= set(query.phenomena)


@pytest.mark.asyncio
async def test_extract_defers_phenomenon_check_to_image_step():
    """第一步只要求设备具体；现象可以等设备图片识别后再判定。"""

    fake = _FakeModelCall(
        json.dumps(
            {
                "question": "1号主变异常",
                "device_name": "1号主变",
                "device_type": "变压器",
                "voltage_level": None,
                "phenomena": [],
            },
            ensure_ascii=False,
        )
    )
    query = await extract_fault_trace_intent("1号主变异常", image_result=None, model_call=fake)

    assert query.device_name == "1号主变"
    assert query.phenomena == []
    assert query.clarification_required is False


@pytest.mark.asyncio
async def test_rule_specific_query_skips_model_for_deterministic_confirmation():
    """规则已能确定设备与现象时不调用模型，恢复重放得到同一份确认计划。"""

    fake = _FakeModelCall(_intent_json(device_name="1号主变"))
    query = await extract_fault_trace_intent(
        "3B-7Y 10kV 线路通道内树木倒伏，枝干压覆导线",
        image_result=None,
        model_call=fake,
    )

    assert query.device_name == "3B-7Y"
    assert fake.prompts == []
