"""四段式故障溯源报告装配与发布载荷单元测试。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.fault_trace.answer import build_fault_answer_markdown, build_fault_trace_payload
from yuxi.knowledge.fault_trace.contracts import (
    REPORT_SCHEMA_VERSION,
    CauseEvaluation,
    FaultTraceQuery,
    RealtimeValue,
    TriggerResult,
)
from yuxi.knowledge.fault_trace.graph import FaultTopology

pytestmark = pytest.mark.unit


def _query() -> FaultTraceQuery:
    return FaultTraceQuery(
        device_name="3B-7Y",
        voltage_level="10kV",
        phenomena=["树木倒伏枝干压覆导线"],
    )


def _device() -> dict:
    return {
        "device_id": "3b7y",
        "graph_id": "LGR#3b7y",
        "name": "3B-7Y",
        "device_type": "架空线路",
        "device_category": "中压线路",
        "voltage_level": "10kV",
    }


def _topology() -> FaultTopology:
    return FaultTopology(
        upstream=[
            {
                "device_id": "LGR#station",
                "name": "35kV#3变电站",
                "device_type": "变电站",
                "voltage_level": "35kV",
            }
        ],
        downstream=[],
        paths=["35kV#3变电站 → 3B-7Y"],
        consequences=[{"consequence_id": "FC-01", "name": "相间短路"}],
        warnings=["未发现 3B-7Y 的下游电气连接设备"],
    )


def _located_cause() -> CauseEvaluation:
    return CauseEvaluation(
        cause_id="CA-04",
        name="通道内树木未及时砍伐或修剪，遇大风倒伏压线",
        status="established",
        true_count=2,
        triggers=[
            TriggerResult(
                "TC-R03",
                "泄漏电流超1mA",
                "leakage_current > 1",
                "true",
                "ok",
                {"leakage_current": 1.2},
            ),
            TriggerResult(
                "TC-R09",
                "线路保护动作",
                "line_protection_trip == true",
                "false",
                "ok",
                {"line_protection_trip": False},
            ),
        ],
        missing=["image_tree_fall_detected"],
    )


def _payload(causes: list[CauseEvaluation] | None = None, *, cause_measures: dict | None = None) -> dict:
    evaluations = causes or [_located_cause()]
    return build_fault_trace_payload(
        query=_query(),
        device=_device(),
        device_type="架空线路",
        reasoning_rows=[
            {
                "phenomenon_id": "PH-04",
                "phenomenon": "树木倒伏枝干压覆导线",
                "link_level": "fault_mode",
                "causes": evaluations,
            }
        ],
        causes=evaluations,
        topology=_topology(),
        cause_links="fault_mode",
        image_used=False,
        elapsed_ms=1200,
        warnings=["故障原因挂在故障模式上（缺少现象→原因边），按模式汇总可能原因"],
        cause_measures=cause_measures,
    )


def test_payload_has_five_sections_and_location():
    measure = "1. 停电检查触头\n2. 打磨烧蚀触头"
    payload = _payload(cause_measures={"CA-04": [measure]})
    report = payload["report"]

    assert payload["answer_source"] == "fault_trace"
    assert payload["answer_verified"] is True
    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["summary"]["phenomenon"] == "树木倒伏枝干压覆导线"
    assert report["location"]["cause_id"] == "CA-04"
    assert report["location"]["verdict"] == "established"
    assert report["location"]["phenomena"] == ["树木倒伏枝干压覆导线"]
    assert report["location"]["rule"] == "leakage_current > 1 → 通道内树木未及时砍伐或修剪，遇大风倒伏压线"
    assert report["location"]["missing_data"] == ["image_tree_fall_detected"]
    assert report["location"]["evidence"][0]["trigger_id"] == "TC-R03"
    assert report["impact"]["upstream"][0]["name"] == "35kV#3变电站"
    assert report["impact"]["consequences"] == [{"consequence_id": "FC-01", "name": "相间短路"}]
    assert report["meta"]["graph"] == "kg3"
    assert report["meta"]["cause_link_level"] == "fault_mode"
    assert report["suggestions"] == [
        {
            "cause_id": "CA-04",
            "cause_name": "通道内树木未及时砍伐或修剪，遇大风倒伏压线",
            "measures": [measure],
        }
    ]

    markdown = payload["answer_markdown"]
    for header in (
        "**① 故障摘要**",
        "**② 推理过程**",
        "**③ 原因定位**",
        "**④ 影响范围**",
        "**⑤ 处置建议**",
    ):
        assert header in markdown
    # ② 合并现象单元格，只保留现象与原因两列。
    assert "<th>故障现象</th><th>可能原因</th>" in markdown
    assert (
        '<tr><td rowspan="1">树木倒伏枝干压覆导线</td><td>通道内树木未及时砍伐或修剪，遇大风倒伏压线</td></tr>'
        in markdown
    )
    assert "处置建议" not in markdown.split("**③ 原因定位**")[0]
    assert "- 判定成立的故障现象：树木倒伏枝干压覆导线" in markdown
    assert "- 命中规则：leakage_current > 1 → 通道内树木未及时砍伐或修剪，遇大风倒伏压线" in markdown
    # ⑤ 只列成立原因及其具体措施说明，换行转成 <br>。
    assert "<th>故障原因</th><th>建议措施</th>" in markdown
    assert "<td>通道内树木未及时砍伐或修剪，遇大风倒伏压线</td><td>1. 停电检查触头<br>2. 打磨烧蚀触头</td>" in markdown
    # 摘要只保留原型中的两个字段，不再附带故障现象与图片识别。
    summary_section = markdown.split("**② 推理过程**")[0]
    assert "- 故障现象：" not in summary_section
    assert "- 图片识别：" not in summary_section
    assert "（成立，" not in markdown


def test_payload_unknown_verdict_when_no_cause_established():
    unknown = CauseEvaluation(
        cause_id="CA-01",
        name="通道附近塑料薄膜类漂浮物被大风掀起搭挂",
        status="unknown",
        true_count=0,
        triggers=[
            TriggerResult(
                "TC-01",
                "大风或强风天气",
                None,
                "unknown",
                "qualitative_condition",
                qualitative=True,
            )
        ],
        missing=["leakage_current"],
        qualitative_only=True,
    )

    payload = _payload([unknown])
    report = payload["report"]

    assert report["location"]["verdict"] == "unknown"
    assert report["location"]["cause_id"] is None
    assert "树木倒伏枝干压覆导线" in report["summary"]["text"]
    assert "原因待确认" not in report["summary"]["text"]
    assert "缺少实时数据" in report["location"]["reasoning"]


def test_payload_lists_all_established_causes():
    second = CauseEvaluation(
        cause_id="CA-05",
        name="线路下方施工防尘网被大风掀起缠绕导线",
        status="established",
        true_count=1,
        triggers=[
            TriggerResult(
                "TC-R09",
                "线路保护动作",
                "line_protection_trip == true",
                "true",
                "ok",
                {"line_protection_trip": True},
            )
        ],
    )

    payload = _payload([_located_cause(), second])
    location = payload["report"]["location"]
    markdown = payload["answer_markdown"]

    assert [item["cause_id"] for item in location["causes"]] == ["CA-04", "CA-05"]
    assert location["cause_id"] == "CA-04"
    assert "- 定位原因：通道内树木未及时砍伐或修剪，遇大风倒伏压线、线路下方施工防尘网被大风掀起缠绕导线" in markdown
    assert "  - 通道内树木未及时砍伐或修剪，遇大风倒伏压线：leakage_current > 1 → " in markdown
    assert "  - 线路下方施工防尘网被大风掀起缠绕导线：line_protection_trip == true → " in markdown


def test_payload_uses_model_text_overrides():
    payload = build_fault_trace_payload(
        query=_query(),
        device=_device(),
        device_type="架空线路",
        reasoning_rows=[{"phenomenon_id": "PH-04", "phenomenon": "树木倒伏枝干压覆导线", "causes": [_located_cause()]}],
        causes=[_located_cause()],
        topology=_topology(),
        cause_links="fault_mode",
        image_used=False,
        elapsed_ms=10,
        warnings=[],
        summary_text="3B-7Y 通道内树木倒伏压覆导线，现场可见放电痕迹。",
        location_reasoning="泄漏电流实测1.2mA超过判据，定位树木倒伏压线。",
    )

    assert payload["report"]["summary"]["text"].startswith("3B-7Y 通道内")
    assert payload["report"]["location"]["reasoning"].startswith("泄漏电流实测1.2mA")


def test_markdown_renders_empty_impact():
    payload = _payload()
    markdown = build_fault_answer_markdown(
        {
            **payload["report"],
            "impact": {**payload["report"]["impact"], "upstream": [], "downstream": [], "paths": []},
        }
    )

    assert "- 上游根源设备：无" in markdown
    assert "- 下游受影响设备：无" in markdown


def test_location_without_data_trigger_is_not_verified():
    """只有定性判据支持的定位原因不算通过证据校验，报告不得标记为已校验。

    该状态由 ``rules.evaluate_causes`` 之外直接构造，用于锁定发布谓词本身；规则求值
    永远给不出「established 且无数据判据」的组合，所以这里是谓词的负向边界。
    """

    qualitative = CauseEvaluation(
        cause_id="CA-Q1",
        name="通道内树木倒伏压线",
        status="established",
        true_count=1,
        triggers=[
            TriggerResult(
                "TC-Q1",
                "现场可见树木倒伏",
                None,
                "true",
                "ok",
                {},
                qualitative=True,
            )
        ],
        qualitative_only=True,
    )

    assert _payload([qualitative])["answer_verified"] is False


def test_evaluated_causes_keep_verification_consistent_with_rule_semantics():
    """规则求值的真实输出与发布谓词一致：成立原因必带数据判据，未定位时仍可发布。"""

    from yuxi.knowledge.fault_trace.graph import FaultGraphEvidence
    from yuxi.knowledge.fault_trace.rules import evaluate_causes

    def evidence_for(expression: str) -> FaultGraphEvidence:
        return FaultGraphEvidence(
            nodes=[
                {"id": "CA-1", "name": "通道内树木倒伏压线", "type": "cause", "properties": {}},
                {
                    "id": "TC-1",
                    "name": "泄漏电流超限",
                    "type": "trigger",
                    "properties": {"表达式": expression},
                },
            ],
            edges=[
                {
                    "id": "CA-1->has_trigger_condition->TC-1",
                    "source_id": "CA-1",
                    "target_id": "TC-1",
                    "type": "has_trigger_condition",
                    "properties": {},
                }
            ],
        )

    established = evaluate_causes(
        evidence_for("leakage_current > 1"),
        {"leakage_current": RealtimeValue("leakage_current", "ok", 1.2)},
    )
    unknown = evaluate_causes(
        evidence_for("leakage_current > 1"),
        {"leakage_current": RealtimeValue("leakage_current", "missing")},
    )

    assert _payload(established)["answer_verified"] is True
    # 未定位时不发布「已校验结论」，但报告仍按确定性推理发布（answer_verified 为 True 才能出卡片）。
    payload = _payload(unknown)
    assert payload["report"]["location"]["cause_id"] is None
    assert payload["answer_verified"] is True
