"""设备预测维护报告装配、状态解读与证据校验单测（不连接图/服务/模型）。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from yuxi.knowledge.device_prediction import answer as answer_module
from yuxi.knowledge.device_prediction.contracts import (
    HEALTH_FAULT_RATE_MISSING_NOTE,
    MAINTENANCE_TIME_DERIVED_NOTE,
    STATUS_READOUT_TEMPLATE_NOTE,
    DeviceCandidate,
    FaultPrediction,
    HealthAssessment,
    HealthIndicator,
    MaintenanceGuidance,
    PredictionEvidence,
)

pytestmark = pytest.mark.unit

DEVICE = DeviceCandidate(
    "LGR#tr1",
    "1号主变",
    device_type="变压器",
    voltage_level="110kV",
    path="变电设施 > 35kV#1变电站 > 1号主变",
    ledger_ids=("bb1baf15",),
)
HEALTH = HealthAssessment(
    health_score="72",
    health_level="注意",
    indicators=(
        HealthIndicator(
            indicator_name="绕组温度",
            indicator_value="85",
            indicator_unit="℃",
            weight_ratio="30%",
            indicator_score="-8 分",
            criteria="正常区间 ≤ 75℃；每超 5℃ 扣 2 分",
        ),
        HealthIndicator(
            indicator_name="顶层油温",
            indicator_value="68",
            indicator_unit="℃",
            weight_ratio="25%",
            indicator_score="0 分",
            criteria="正常区间 ≤ 85℃",
        ),
    ),
    fault_rate="0.8 次/年",
    average_downtime_hours="6.5 小时/次",
)
PREDICTION = FaultPrediction(
    predicted_fault_type="触头过热 / 冷却效能下降",
    probability="67%",
    expected_failure_time="未来 30–60 天",
    degradation_trend="负荷率连续 3 个月上升，近 30 天最高达 78%",
    maintenance_time="建议在 30 天内完成针对性检查维护",
)
GUIDANCE = MaintenanceGuidance(
    fault_type="触头过热",
    resources=["红外热像仪"],
    cautions=["停电范围提前报备"],
)


def _evidence(*, health=None, prediction=None, guidance=None) -> PredictionEvidence:
    return PredictionEvidence(
        device_name="1号主变",
        device_label="1号主变（变压器，110kV）",
        device_type="变压器",
        voltage_level="110kV",
        device_id="bb1baf15",
        device_id_source="台账ID列表",
        health=health or HEALTH,
        prediction=prediction or PREDICTION,
        guidance=guidance or GUIDANCE,
        retrieval_log=["按名称在知识图谱中定位设备“1号主变”"],
    )


def _payload(evidence=None, *, status_readout=None, generated=False):
    evidence = evidence or _evidence()
    return answer_module.produce_prediction_answer(
        evidence,
        status_readout=status_readout or answer_module.build_template_readout(evidence),
        status_readout_generated=generated,
        elapsed_seconds=1.4,
    )


# ---------------------------------------------------------------------------
# 装配与渲染
# ---------------------------------------------------------------------------
def test_verified_report_covers_all_required_fields():
    """四个需求段落与字段齐全，且通过证据校验。"""

    payload = _payload()
    markdown = payload["answer_markdown"]

    assert payload["answer_source"] == "device_prediction"
    assert payload["answer_verified"] is True
    assert payload["report"]["device_id"] == "bb1baf15"
    assert payload["report"]["health_score"] == "72"
    assert payload["report"]["health_level"] == "注意"
    assert payload["report"]["fault_rate"] == "0.8 次/年"
    assert payload["report"]["average_downtime_hours"] == "6.5 小时/次"
    assert payload["report"]["maintenance_time"] == "建议在 30 天内完成针对性检查维护"
    assert payload["report"]["matched_phenomenon"] == "触头过热"
    assert payload["report"]["resources"] == ["红外热像仪"]
    assert payload["report"]["cautions"] == ["停电范围提前报备"]

    assert markdown.startswith("# 设备健康评估-1号主变（变压器 · 110kV）")
    assert "## 一、设备健康评估" in markdown
    assert "## 二、故障预测" in markdown
    assert "## 三、可靠性与风险评估" in markdown
    assert "## 四、维护计划建议" in markdown
    assert "健康评分：72 分" in markdown
    assert "健康等级：注意" in markdown
    assert "预测故障类型：触头过热 / 冷却效能下降" in markdown
    assert "发生概率：67%" in markdown
    assert "预计发生时间：未来 30–60 天" in markdown
    assert "| 绕组温度 | 85 | ℃ | 30% | -8 分 |" in markdown
    assert "匹配故障现象：触头过热" in markdown
    assert "1. 红外热像仪" in markdown
    assert "1. 停电范围提前报备" in markdown
    assert "维护内容" not in markdown


def test_missing_optional_parts_are_marked_not_invented():
    """可靠性指标缺失、维护知识未匹配时如实标注，不写替代内容。"""

    health = HealthAssessment(health_score="72", health_level="注意", indicators=HEALTH.indicators)
    prediction = FaultPrediction(
        predicted_fault_type="触头过热",
        probability="67%",
        expected_failure_time="未来 30–60 天",
        degradation_trend="负荷率上升",
        maintenance_time="建议在 30 天内完成",
        maintenance_time_derived=True,
    )
    guidance = MaintenanceGuidance(notes=["知识图谱中未匹配到该故障类型的故障现象，所需资源与注意事项未匹配"])

    payload = _payload(_evidence(health=health, prediction=prediction, guidance=guidance))
    report = payload["report"]

    assert payload["answer_verified"] is True
    assert HEALTH_FAULT_RATE_MISSING_NOTE in report["data_notes"]
    assert MAINTENANCE_TIME_DERIVED_NOTE in report["data_notes"]
    assert guidance.notes[0] in report["data_notes"]
    assert "- 故障率：未返回" in payload["answer_markdown"]
    assert "- 匹配故障现象：未匹配" in payload["answer_markdown"]
    assert "（知识图谱中未匹配到该故障现象的所需资源）" in payload["answer_markdown"]
    assert "（知识图谱中未匹配到该故障现象的注意事项）" in payload["answer_markdown"]


def test_template_readout_uses_only_evidence_numbers():
    """确定性解读只引用评分、扣分指标与可靠性指标。"""

    readout = answer_module.build_template_readout(_evidence())

    assert readout.startswith("1号主变当前健康评分为 72 分，健康等级为“注意”。")
    assert "绕组温度 85℃（-8 分）" in readout
    assert "顶层油温" not in readout
    assert "故障率 0.8 次/年，平均停运时间 6.5 小时/次" in readout


def test_indicator_readout_separates_word_units_only():
    """符号单位紧贴数值，词类单位留空格。"""

    assert answer_module._indicator_reading(HealthIndicator("绕组温度", "85", "℃", "30%", "-8 分", "")) == "绕组温度 85℃"
    assert answer_module._indicator_reading(HealthIndicator("母线电压", "10", "kV", "25%", "0 分", "")) == "母线电压 10kV"
    assert answer_module._indicator_reading(HealthIndicator("基础沉降", "4", "比值", "25%", "-40 分", "")) == "基础沉降 4 比值"
    assert answer_module._indicator_reading(HealthIndicator("温升", "3", "", "15%", "-24 分", "")) == "温升 3"


def test_numbered_items_are_not_double_numbered():
    """图谱里已带编号的条目按原编号渲染，不再叠加一层序号。"""

    guidance = MaintenanceGuidance(
        fault_type="杆塔倾斜故障",
        resources=["全站仪 1 台"],
        cautions=["1. 先复核整基倾斜。", "2. 办理工作票。"],
    )

    payload = _payload(_evidence(guidance=guidance))
    markdown = payload["answer_markdown"]

    assert "1. 1. " not in markdown
    assert "\n1. 先复核整基倾斜。\n2. 办理工作票。" in markdown
    assert "- 匹配故障现象：杆塔倾斜故障" in markdown


# ---------------------------------------------------------------------------
# 状态解读：模型文本必须落在健康评估数值范围内
# ---------------------------------------------------------------------------
def test_grounded_model_readout_is_used():
    """模型解读里的数字都能在健康评估结果中找到时采用，并记为模型生成。"""

    async def model_call(prompt: str) -> str:
        assert "健康评估结果" in prompt
        return "该设备健康评分 72 分，处于“注意”等级，绕组温度 85℃ 偏高，需加强红外测温跟踪。"

    readout, generated = asyncio.run(answer_module.generate_status_readout(_evidence(), model_call=model_call))

    assert generated is True
    assert readout.startswith("该设备健康评分 72 分")


@pytest.mark.parametrize(
    ("level", "text", "expected"),
    [
        # 服务实测取值域：一般 / 良好
        ("一般", "该设备健康等级为“一般”，健康评分 72 分。", True),
        ("一般", "该设备状态：一般，健康评分 72 分。", True),
        ("良好", "该设备状态：良好，健康评分 72 分。", True),
        ("一般", "该设备处于“良好”等级，健康评分 72 分。", False),
        ("一般", "该设备状态良好，健康评分 72 分。", False),
        ("一般", "该设备状态：一般，部分指标状态：异常。", False),
        ("注意", "该设备状态：注意，绕组温度 85℃。", True),
    ],
)
def test_level_claim_vocabulary_covers_real_service_levels(level, text, expected):
    """等级声明按服务真实取值域校验，且检查文本里所有声明。"""

    evidence = _evidence(health=replace(HEALTH, health_level=level))

    async def model_call(prompt: str) -> str:
        return text

    readout, generated = asyncio.run(answer_module.generate_status_readout(evidence, model_call=model_call))

    assert generated is expected
    if not expected:
        assert readout == answer_module.build_template_readout(evidence)


def test_ungrounded_model_readout_falls_back_to_template():
    """模型写出证据之外的数字时退回确定性解读。"""

    async def model_call(prompt: str) -> str:
        return "该设备健康评分 95 分，处于“正常”等级。"

    readout, generated = asyncio.run(answer_module.generate_status_readout(_evidence(), model_call=model_call))

    assert generated is False
    assert readout == answer_module.build_template_readout(_evidence())


@pytest.mark.parametrize(
    "text",
    [
        "该设备健康评分为九十五，运行正常。",
        "该设备健康评分为九十五分，建议加强跟踪。",
        "该设备绕组温度为一百二十度，需立即停电。",
        "该设备健康评分为百分之九十五，需要注意。",
        "该设备健康评分 72 分，等级“严重”。",
    ],
)
def test_readout_with_unverifiable_claims_falls_back(text):
    """中文数字或与评估不一致的等级结论都不能被采用。"""

    async def model_call(prompt: str) -> str:
        return text

    readout, generated = asyncio.run(answer_module.generate_status_readout(_evidence(), model_call=model_call))

    assert generated is False
    assert readout == answer_module.build_template_readout(_evidence())


def test_grounded_chinese_numeral_is_accepted():
    """换算后能在证据里找到的中文数字仍可采用，不必一律退回模板。"""

    async def model_call(prompt: str) -> str:
        return "该设备健康评分为七十二分，处于“注意”等级。"

    readout, generated = asyncio.run(answer_module.generate_status_readout(_evidence(), model_call=model_call))

    assert generated is True
    assert readout == "该设备健康评分为七十二分，处于“注意”等级。"


def test_quantifier_words_are_not_treated_as_numbers():
    """“一次”“一项”这类量词不算编造数值，正常解读应采用。"""

    async def model_call(prompt: str) -> str:
        return "该设备健康评分 72 分，近一年有一次异常记录，建议关注一项指标。"

    readout, generated = asyncio.run(answer_module.generate_status_readout(_evidence(), model_call=model_call))

    assert generated is True
    assert readout == "该设备健康评分 72 分，近一年有一次异常记录，建议关注一项指标。"


@pytest.mark.parametrize("run", ["九十五", "一百二十", "七十二", "一万", "一万二千", "十一"])
def test_cjk_values_are_converted(run):
    """中文数字换算覆盖十/百/千/万与连续写法，供可追溯校验使用。"""

    expected = {"九十五": 95, "一百二十": 120, "七十二": 72, "一万": 10000, "一万二千": 12000, "十一": 11}[run]

    assert answer_module._cjk_value(run) == expected


def test_model_failure_falls_back_to_template():
    """模型调用异常不阻断报告，退回确定性解读。"""

    async def model_call(prompt: str) -> str:
        raise RuntimeError("模型服务不可用")

    readout, generated = asyncio.run(answer_module.generate_status_readout(_evidence(), model_call=model_call))

    assert generated is False
    assert readout == answer_module.build_template_readout(_evidence())


def test_template_readout_is_recorded_in_data_notes():
    """模型不可用时在数据说明里标注解读来源。"""

    payload = _payload(generated=False)

    assert STATUS_READOUT_TEMPLATE_NOTE in payload["report"]["data_notes"]


# ---------------------------------------------------------------------------
# 证据校验：篡改必须被拦下
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"health_score": "95"}, "健康评分"),
        ({"probability": "90%"}, "发生概率"),
        ({"fault_type": "绝缘击穿"}, "故障类型"),
        ({"device_id": "other-id"}, "设备 ID"),
        ({"matched_phenomenon": "高温过热故障"}, "匹配故障现象"),
        ({"resources": []}, "所需资源"),
        ({"cautions": ["编造的注意事项"]}, "注意事项"),
        ({"indicators": []}, "监测指标"),
        ({"health_level": "严重"}, "健康等级"),
        ({"fault_rate": "9.9 次/年"}, "故障率"),
        ({"average_downtime_hours": "99 小时/次"}, "平均停运时间"),
        ({"expected_failure_time": "未来 999 天"}, "预计发生时间"),
        ({"degradation_trend": "编造趋势 999"}, "退化趋势"),
        ({"maintenance_time": "编造维护时机 999"}, "维护时机"),
        ({"status_readout": "健康评分 99 分，运行良好"}, "状态解读"),
    ],
)
def test_tampered_report_is_rejected(overrides, reason):
    """报告任何字段与服务/图谱证据不一致都判为未通过。"""

    evidence = _evidence()
    report = answer_module.assemble_report(
        evidence,
        status_readout=answer_module.build_template_readout(evidence),
        data_notes=[],
        elapsed_seconds=1.0,
    )
    tampered = replace(report, **overrides)

    assert answer_module.verify_report(tampered, evidence) is False, reason


def test_empty_status_readout_is_rejected():
    """状态解读为空同样不通过校验。"""

    evidence = _evidence()
    report = answer_module.assemble_report(evidence, status_readout="  ", data_notes=[], elapsed_seconds=1.0)

    assert answer_module.verify_report(report, evidence) is False
