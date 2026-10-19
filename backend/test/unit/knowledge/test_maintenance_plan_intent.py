"""T1 意图解析单元测试。

覆盖需求示例、真实台账写法、任务类型/电压/设备类型/位置线索的规则抽取、
输入约束（方案二/方案三）、大模型抽取的合并与各类失败回退、上一轮结果合并。
全部使用本地假对象（假 model_call），不连接数据库、图谱或在线大模型。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from yuxi.knowledge.maintenance_plan import intent
from yuxi.knowledge.maintenance_plan.intent import (
    apply_input_constraints,
    extract_plan_intent,
    missing_fields,
    normalize_device_type,
    rule_extract,
)
from yuxi.knowledge.maintenance_plan.contracts import CLARIFY_ALL_MESSAGE, TASK_TYPES, PlanQuery

pytestmark = pytest.mark.unit


def _run(coro):
    """同步跑异步函数，避免依赖 pytest-asyncio 插件。"""

    return asyncio.run(coro)


def _model_call(payload, *, recorder: list[str] | None = None, error: Exception | None = None):
    """构造假 model_call：返回给定 JSON 文本，或按需抛异常，并记录收到的提示词。"""

    async def _call(prompt: str) -> str:
        if recorder is not None:
            recorder.append(prompt)
        if error is not None:
            raise error
        return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    return _call


# ---------------------------------------------------------------------------
# 需求示例四类任务
# ---------------------------------------------------------------------------
def test_demand_example_replace_switchgear_breaker():
    query = rule_extract("更换10kV开闭所1号开关柜断路器")

    assert query.task_type == "更换"
    assert query.device_name == "1号开关柜"
    assert query.device_type == "开关柜"
    assert query.voltage_level == "10kV"
    assert query.work_object == "断路器"


def test_demand_example_repair_main_transformer_cooling():
    query = rule_extract("检修1号主变冷却系统")

    assert query.task_type == "检修"
    assert query.device_name == "1号主变"
    assert query.device_type == "变压器"
    assert query.work_object == "冷却系统"
    assert query.voltage_level is None


def test_demand_example_switchgear_preventive_test():
    query = rule_extract("10kV开关柜例行预防性试验")

    assert query.task_type == "试验"
    assert query.device_type == "开关柜"
    assert query.voltage_level == "10kV"
    assert query.device_name is None


def test_demand_example_closed_switch_station_patrol():
    query = rule_extract("10kV开闭所日常巡视")

    assert query.task_type == "巡视"
    assert query.device_type == "开闭所"
    assert query.voltage_level == "10kV"
    assert query.location_hint == "10kV开闭所"


# ---------------------------------------------------------------------------
# 真实台账写法
# ---------------------------------------------------------------------------
def test_ledger_replace_3k18_breaker():
    query = rule_extract("更换#3开闭所3k18断路器")

    assert query.task_type == "更换"
    assert query.device_name == "3k18"
    assert query.location_hint == "#3开闭所"
    assert query.device_type == "断路器"


def test_ledger_repair_991svg_switch():
    query = rule_extract("35kV#1变电站991SVG开关检修")

    assert query.task_type == "检修"
    assert query.device_name == "991SVG开关"
    assert query.location_hint == "35kV#1变电站"
    assert query.device_type == "断路器"
    assert query.voltage_level == "35kV"


def test_ledger_patrol_box_substation_feeder_cabinet():
    query = rule_extract("#62箱变13馈出柜巡视")

    assert query.task_type == "巡视"
    assert query.device_name == "#62箱变13馈出柜"
    assert query.location_hint == "#62箱变"
    assert query.device_type == "开关柜"


def test_ledger_replace_tower_insulator_on_collector_line():
    query = rule_extract("1B-1K-1线路103#杆塔绝缘子更换")

    assert query.task_type == "更换"
    assert query.device_name == "103#杆塔"
    assert query.location_hint == "1B-1K-1"
    assert query.device_type == "杆塔"
    assert query.work_object == "绝缘子"


def test_ledger_repair_auxiliary_cabinet():
    query = rule_extract("#5开闭所5k18辅柜检修")

    assert query.task_type == "检修"
    assert query.device_name == "5k18"
    assert query.location_hint == "#5开闭所"
    assert query.device_type == "开关柜"


def test_ledger_patrol_tower_with_letter_prefix():
    query = rule_extract("BN18杆塔金具测温巡视")

    assert query.task_type == "巡视"
    assert query.device_name == "BN18杆塔"
    assert query.device_type == "杆塔"


def test_ledger_spaces_inside_description_are_tolerated():
    query = rule_extract("更换 #3 开闭所 3k18 断路器")

    assert query.device_name == "3k18"
    assert query.location_hint == "#3开闭所"


def test_ledger_voice_transcript_with_punctuation():
    query = rule_extract("麻烦更换一下，#3开闭所的3k18断路器。")

    assert query.task_type == "更换"
    assert query.device_name == "3k18"
    assert query.location_hint == "#3开闭所"


# ---------------------------------------------------------------------------
# 任务类型关键词
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["替换3k18断路器", "3k18断路器换新"])
def test_task_type_replace_synonyms(text):
    assert rule_extract(text).task_type == "更换"


@pytest.mark.parametrize("text", ["10kV开关柜耐压", "10kV开关柜测试", "10kV开关柜例行试验"])
def test_task_type_test_synonyms(text):
    assert rule_extract(text).task_type == "试验"


@pytest.mark.parametrize("text", ["10kV开关柜巡检", "#3开闭所特巡"])
def test_task_type_patrol_synonyms(text):
    assert rule_extract(text).task_type == "巡视"


@pytest.mark.parametrize("text", ["3k18断路器维修", "3k18断路器消缺", "1号主变大修", "1号主变小修", "3k18断路器处理"])
def test_task_type_repair_synonyms(text):
    assert rule_extract(text).task_type == "检修"


def test_task_type_bare_xiu_falls_back_to_repair():
    assert rule_extract("修一下3k18").task_type == "检修"


def test_task_type_missing_returns_none():
    assert rule_extract("3k18断路器").task_type is None
    assert rule_extract("帮我出个方案").task_type is None


def test_task_type_prefers_replace_when_multiple_verbs_appear():
    assert rule_extract("更换并试验3k18断路器").task_type == "更换"


# ---------------------------------------------------------------------------
# 电压等级
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10千伏开关柜检修", "10kV"),
        ("10 KV开关柜检修", "10kV"),
        ("10KV开关柜检修", "10kV"),
        ("10kV开关柜检修", "10kV"),
        ("35kV#1变电站991SVG开关检修", "35kV"),
        ("0.4kV低压柜巡视", "0.4kV"),
        ("110kV线路巡视", "110kV"),
    ],
)
def test_voltage_variants(text, expected):
    assert rule_extract(text).voltage_level == expected


def test_voltage_trailing_zero_is_normalized():
    assert rule_extract("10.0kV开关柜检修").voltage_level == "10kV"


@pytest.mark.parametrize("text", ["3k18断路器更换", "1B-1K-1线路巡视", "#62箱变13馈出柜巡视"])
def test_voltage_absent_returns_none(text):
    assert rule_extract(text).voltage_level is None


# ---------------------------------------------------------------------------
# 设备类型归一
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("主变", "变压器"),
        ("主变压器", "变压器"),
        ("站用变", "变压器"),
        ("接地变", "变压器"),
        ("所用变", "变压器"),
    ],
)
def test_normalize_transformer_aliases(raw, expected):
    assert normalize_device_type(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("箱变", "箱式变电站"), ("箱式变电站", "箱式变电站"), ("开关", "断路器"), ("开关柜", "开关柜"), ("馈出柜", "开关柜")],
)
def test_normalize_box_and_switch_aliases(raw, expected):
    assert normalize_device_type(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("刀闸", "隔离开关"), ("隔离开关", "隔离开关"), ("PT", "电压互感器"), ("CT", "电流互感器")],
)
def test_normalize_disconnector_and_instrument_transformers(raw, expected):
    assert normalize_device_type(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("集电线路", "架空线路"), ("配电线路", "架空线路"), ("输电线路", "架空线路"), ("线路", "架空线路")],
)
def test_normalize_line_aliases(raw, expected):
    assert normalize_device_type(raw) == expected


def test_normalize_scheme1_labels_are_mapped():
    assert normalize_device_type("变电站") == "电力设备"
    assert normalize_device_type("配电线路") == "架空线路"
    assert normalize_device_type("绝缘子") == "绝缘子"


def test_normalize_unknown_and_empty_returns_none():
    assert normalize_device_type("空调") is None
    assert normalize_device_type("") is None
    assert normalize_device_type("   ") is None
    assert normalize_device_type(None) is None
    assert normalize_device_type("未知") is None


def test_normalize_canonical_names_pass_through():
    for device_type in ("变压器", "开闭所", "母线", "电缆", "避雷器", "杆塔"):
        assert normalize_device_type(device_type) == device_type


# ---------------------------------------------------------------------------
# 位置线索
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("更换#3开闭所3k18断路器", "#3开闭所"),
        ("35kV#1变电站991SVG开关检修", "35kV#1变电站"),
        ("#62箱变13馈出柜巡视", "#62箱变"),
        ("箱变群3-6 #73箱变12环出柜巡视", "#73箱变"),
        ("1B-1K-1线路103#杆塔绝缘子更换", "1B-1K-1"),
        ("3B-6K线路巡视", "3B-6K"),
    ],
)
def test_location_hints(text, expected):
    assert rule_extract(text).location_hint == expected


def test_device_name_is_not_the_location_hint():
    query = rule_extract("更换#3开闭所3k18断路器")

    assert query.device_name == "3k18"
    assert query.location_hint == "#3开闭所"


# ---------------------------------------------------------------------------
# missing_fields 与输入约束（方案二/方案三）
# ---------------------------------------------------------------------------
def test_missing_fields_reports_both():
    assert missing_fields(PlanQuery()) == ["task_type", "device"]


def test_missing_fields_reports_task_type_only():
    assert missing_fields(PlanQuery(device_name="3k18")) == ["task_type"]


def test_missing_fields_reports_device_only():
    assert missing_fields(PlanQuery(task_type="更换")) == ["device"]


def test_missing_fields_device_satisfied_by_location_and_type():
    query = PlanQuery(task_type="更换", location_hint="#3开闭所", device_type="开关柜")

    assert missing_fields(query) == []


def test_missing_fields_device_type_alone_is_not_enough():
    assert missing_fields(PlanQuery(task_type="更换", device_type="开关柜")) == ["device"]


def test_apply_input_constraints_sets_clarification_when_both_missing():
    query = apply_input_constraints(PlanQuery(raw_text="帮我出个方案"))

    assert query.clarification_required is True
    assert query.clarification_message == CLARIFY_ALL_MESSAGE


def test_apply_input_constraints_no_clarification_when_single_missing():
    only_device = apply_input_constraints(PlanQuery(task_type="更换"))
    only_task = apply_input_constraints(PlanQuery(device_name="3k18"))

    assert only_device.clarification_required is False
    assert only_device.clarification_message == ""
    assert only_task.clarification_required is False
    assert only_task.clarification_message == ""


def test_apply_input_constraints_normalizes_fields():
    query = apply_input_constraints(
        PlanQuery(task_type="更换", device_name=" 1号主变 ", device_type="主变", voltage_level="10KV")
    )

    assert query.device_type == "变压器"
    assert query.voltage_level == "10kV"
    assert query.device_name == "1号主变"


def test_apply_input_constraints_clamps_unknown_task_type():
    query = apply_input_constraints(PlanQuery(task_type="安装", device_name="3k18"))

    assert query.task_type is None
    assert query.clarification_required is False
    assert missing_fields(query) == ["task_type"]


def test_apply_input_constraints_clears_stale_clarification():
    query = apply_input_constraints(
        PlanQuery(
            task_type="巡视",
            device_name="3k18",
            clarification_required=True,
            clarification_message="上一次的旧文案",
        )
    )

    assert query.clarification_required is False
    assert query.clarification_message == ""


# ---------------------------------------------------------------------------
# 大模型抽取：正常、合并与回退
# ---------------------------------------------------------------------------
def test_model_result_wins_over_rule():
    payload = {
        "task_type": "检修",
        "device_name": "3k18",
        "device_type": "开关柜",
        "voltage_level": "10kV",
        "work_object": "手车机构",
        "location_hint": "#3开闭所",
    }

    query = _run(extract_plan_intent("更换#3开闭所3k18断路器", model_call=_model_call(payload)))

    assert query.task_type == "检修"
    assert query.device_type == "开关柜"
    assert query.voltage_level == "10kV"
    assert query.work_object == "手车机构"


def test_model_null_fields_fall_back_to_rule():
    payload = dict.fromkeys(("task_type", "device_name", "device_type", "voltage_level", "work_object", "location_hint"))

    query = _run(extract_plan_intent("更换#3开闭所3k18断路器", model_call=_model_call(payload)))

    assert query.task_type == "更换"
    assert query.device_name == "3k18"
    assert query.device_type == "断路器"
    assert query.location_hint == "#3开闭所"


def test_model_invalid_json_falls_back_to_rule():
    for bad in ("抱歉，我无法解析", "[1, 2, 3]", "{不是合法 JSON}"):
        query = _run(extract_plan_intent("更换#3开闭所3k18断路器", model_call=_model_call(bad)))

        assert query.task_type == "更换"
        assert query.device_name == "3k18"
        assert query.device_type == "断路器"


def test_model_exception_falls_back_to_rule():
    call = _model_call(None, error=RuntimeError("模型超时"))

    query = _run(extract_plan_intent("更换#3开闭所3k18断路器", model_call=call))

    assert query.task_type == "更换"
    assert query.device_name == "3k18"


def test_model_code_fence_json_is_parsed():
    raw = '```json\n{"task_type": "试验", "device_name": "3k18"}\n```'

    query = _run(extract_plan_intent("3k18断路器", model_call=_model_call(raw)))

    assert query.task_type == "试验"
    assert query.device_name == "3k18"


def test_model_json_with_prose_and_trailing_comma_is_parsed():
    raw = '好的，结果如下：{"task_type": "检修", "device_name": "3k18", "work_object": None,}'

    query = _run(extract_plan_intent("更换#3开闭所3k18断路器", model_call=_model_call(raw)))

    assert query.task_type == "检修"
    assert query.work_object == "断路器"


def test_model_unknown_task_type_becomes_none():
    payload = {"task_type": "安装", "device_name": "3k18"}

    query = _run(extract_plan_intent("更换#3开闭所3k18断路器", model_call=_model_call(payload)))

    assert query.task_type is None


def test_model_task_type_synonym_is_normalized():
    payload = {"task_type": "替换", "device_name": "3k18"}

    query = _run(extract_plan_intent("3k18断路器", model_call=_model_call(payload)))

    assert query.task_type == "更换"
    assert query.task_type in TASK_TYPES


def test_model_device_type_is_normalized():
    payload = {"task_type": "试验", "device_type": "箱变"}

    query = _run(extract_plan_intent("10kV开关柜试验", model_call=_model_call(payload)))

    assert query.device_type == "箱式变电站"


def test_model_voltage_written_uppercase_is_normalized():
    payload = {"task_type": "试验", "voltage_level": "10KV"}

    query = _run(extract_plan_intent("开关柜试验", model_call=_model_call(payload)))

    assert query.voltage_level == "10kV"


def test_model_unknown_device_type_falls_back_to_rule():
    payload = {"task_type": "检修", "device_type": "空调"}

    query = _run(extract_plan_intent("10kV开关柜检修", model_call=_model_call(payload)))

    assert query.device_type == "开关柜"


def test_missing_model_call_uses_rule():
    query = _run(extract_plan_intent("更换#3开闭所3k18断路器", model_call=None))

    assert query.task_type == "更换"
    assert query.device_name == "3k18"


@pytest.mark.parametrize("model_name", ["3k18断路器", "#3开闭所3k18断路器", "3K18 断路器"])
def test_model_expanded_name_preserves_explicit_device_identifier(model_name):
    """真实模型把部件并入设备名时保留原文编号，避免唯一设备多弹确认。"""

    query = _run(extract_plan_intent(
        "更换#3开闭所3k18断路器",
        model_call=_model_call({"task_type": "更换", "device_name": model_name, "location_hint": "#3开闭所"}),
    ))
    assert query.device_name == "3k18"
    assert query.work_object == "断路器"
    assert query.location_hint == "#3开闭所"


def test_prompt_carries_previous_result_and_feedback():
    recorder: list[str] = []
    previous = PlanQuery(task_type="更换", location_hint="#3开闭所")
    call = _model_call({"task_type": "更换", "device_name": "3k18"}, recorder=recorder)

    _run(extract_plan_intent("3k18", model_call=call, previous=previous, feedback="就是3k18那个柜子"))

    assert len(recorder) == 1
    assert "上一轮解析结果" in recorder[0]
    assert "就是3k18那个柜子" in recorder[0]
    assert "#3开闭所" in recorder[0]


# ---------------------------------------------------------------------------
# previous + feedback 合并
# ---------------------------------------------------------------------------
def test_previous_fills_fields_missing_from_follow_up():
    previous = PlanQuery(task_type="更换", location_hint="#3开闭所", device_type="断路器")

    query = _run(extract_plan_intent("3k18", model_call=None, previous=previous, feedback="3k18"))

    assert query.task_type == "更换"
    assert query.device_name == "3k18"
    assert query.device_type == "断路器"
    assert query.location_hint == "#3开闭所"
    assert query.clarification_required is False


def test_follow_up_overrides_previous_values():
    previous = PlanQuery(task_type="更换", device_name="991SVG开关", location_hint="35kV#1变电站")

    query = _run(extract_plan_intent("检修3k18断路器", model_call=None, previous=previous))

    assert query.task_type == "检修"
    assert query.device_name == "3k18"


def test_previous_merge_combines_raw_text():
    previous = PlanQuery(raw_text="更换#3开闭所断路器", task_type="更换")

    query = _run(extract_plan_intent("3k18", model_call=None, previous=previous))

    assert query.raw_text == "更换#3开闭所断路器；3k18"


def test_follow_up_still_clarifies_when_both_still_missing():
    previous = PlanQuery()

    query = _run(extract_plan_intent("还是不知道", model_call=None, previous=previous))

    assert query.clarification_required is True
    assert query.clarification_message == CLARIFY_ALL_MESSAGE


def test_extract_returns_clarification_for_unrelated_text():
    query = _run(extract_plan_intent("帮我出个方案", model_call=None))

    assert query.task_type is None
    assert query.clarification_required is True
    assert query.clarification_message == CLARIFY_ALL_MESSAGE


def test_extract_returns_clarification_for_empty_text():
    query = _run(extract_plan_intent("", model_call=None))

    assert missing_fields(query) == ["task_type", "device"]
    assert query.clarification_required is True


def test_extract_single_missing_does_not_clarify():
    """只缺任务类型时由工具弹候选（方案二），不反问。"""

    query = _run(extract_plan_intent("3k18", model_call=None))

    assert query.clarification_required is False
    assert missing_fields(query) == ["task_type"]


def test_intent_module_exposes_required_api():
    for name in ("extract_plan_intent", "rule_extract", "missing_fields", "apply_input_constraints", "normalize_device_type"):
        assert callable(getattr(intent, name))
