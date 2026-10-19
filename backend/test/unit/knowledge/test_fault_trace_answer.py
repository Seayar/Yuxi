"""故障溯源报告装配、校验门与发布载荷单元测试。"""

from __future__ import annotations

import json

import pytest
from yuxi.knowledge.fault_trace.answer import (
    _gate_free_text,
    assemble_report,
    build_fault_answer_markdown,
    produce_fault_trace_answer,
)
from yuxi.knowledge.fault_trace.contracts import NO_FAULT_GRAPH_MESSAGE, FaultTraceQuery
from yuxi.knowledge.fault_trace.graph import FaultGraphEvidence

pytestmark = pytest.mark.unit


def _node(node_id: str, name: str, label: str) -> dict:
    return {
        "id": node_id,
        "name": name,
        "type": "Entity",
        "properties": {"label": label, "entity_id": node_id, "name": name},
    }


def _edge(edge_id: str, source: str, target: str, edge_type: str) -> dict:
    return {"id": edge_id, "source_id": source, "target_id": target, "type": edge_type, "properties": {}}


def _transformer_evidence() -> FaultGraphEvidence:
    nodes = [
        _node("dev_main", "1号主变", "device"),
        _node("dev_up", "110kV进线", "device"),
        _node("dev_down", "10kV开闭所", "device"),
        _node("mode_cool", "冷却系统故障", "fault_mode"),
        _node("ph_fan", "冷却器风扇无运转声音", "phenomenon"),
        _node("ph_oil", "顶层油温升高", "phenomenon"),
        _node("cause_motor", "冷却器风扇电机停转", "cause"),
        _node("cause_dust", "散热片积尘严重", "cause"),
        _node("ms_check", "检查风扇电机电源与控制回路", "measure"),
        _node("ms_clean", "清洗散热片积尘", "measure"),
        _node("trig", "冷却器风扇运行电流 = 0A 且顶层油温 > 85℃ → 冷却器风扇电机停转", "trigger"),
    ]
    edges = [
        _edge("e1", "dev_main", "mode_cool", "has_fault_mode"),
        _edge("e2", "mode_cool", "ph_fan", "has_phenomenon"),
        _edge("e3", "mode_cool", "ph_oil", "has_phenomenon"),
        _edge("e4", "mode_cool", "cause_motor", "has_cause"),
        _edge("e5", "mode_cool", "cause_dust", "has_cause"),
        _edge("e6", "cause_motor", "ms_check", "has_measure"),
        _edge("e7", "cause_dust", "ms_clean", "has_measure"),
        _edge("e8", "mode_cool", "trig", "has_trigger_condition"),
        _edge("e9", "dev_up", "dev_main", "electrically_connected_to"),
        _edge("e10", "dev_main", "dev_down", "electrically_connected_to"),
    ]
    return FaultGraphEvidence(nodes=nodes, edges=edges)


def _query() -> FaultTraceQuery:
    return FaultTraceQuery(
        device_name="1号主变",
        device_type="变压器",
        voltage_level="110kV",
        phenomena=["冷却器风扇停转", "散热片积尘"],
    )


async def _noop_compose(prompt: str) -> str:
    del prompt
    return ""


@pytest.mark.asyncio
async def test_assemble_report_produces_five_sections():
    report = await assemble_report(_query(), _transformer_evidence(), call_compose=None)
    assert report is not None

    assert report.device_names == ["1号主变"]
    assert len(report.reasoning) == 2
    assert report.located_cause == "冷却器风扇电机停转"
    assert report.upstream == ["110kV进线"]
    assert report.downstream == ["10kV开闭所"]
    # ⑤ 只列定位原因
    assert [item["cause"] for item in report.suggestions] == ["冷却器风扇电机停转"]
    assert report.rule == "冷却器风扇运行电流 = 0A 且顶层油温 > 85℃ → 冷却器风扇电机停转"


@pytest.mark.asyncio
async def test_assemble_report_returns_none_without_device():
    evidence = FaultGraphEvidence(nodes=[], edges=[])
    report = await assemble_report(_query(), evidence, call_compose=None)

    assert report is None


@pytest.mark.asyncio
async def test_compose_polish_is_rejected_when_fabricating_terms():
    async def fake_compose(prompt: str) -> str:
        del prompt
        return json.dumps(
            {"summary": "1号主变存在绝缘子质量问题", "evidence_note": "依据图谱判定为冷却器风扇电机停转"},
            ensure_ascii=False,
        )

    report = await assemble_report(_query(), _transformer_evidence(), call_compose=fake_compose)

    # “绝缘子质量问题”不在允许词内 → 门拒绝，回退确定性文案。
    assert "绝缘子质量问题" not in report.summary
    assert "冷却器风扇电机停转" in report.summary


@pytest.mark.asyncio
async def test_produce_answer_without_evidence_returns_plain_message():
    result = await produce_fault_trace_answer(
        _query(), FaultGraphEvidence(nodes=[], edges=[]), call_compose=_noop_compose
    )

    assert result == NO_FAULT_GRAPH_MESSAGE


@pytest.mark.asyncio
async def test_produce_answer_returns_payload_with_markdown():
    result = await produce_fault_trace_answer(
        _query(), _transformer_evidence(), call_compose=_noop_compose
    )

    assert isinstance(result, dict)
    assert result["status"] == "ok"
    assert result["answer_verified"] is True
    markdown = result["answer_markdown"]
    for header in ("**① 故障摘要**", "**② 推理过程**", "**③ 原因定位**", "**④ 影响范围**", "**⑤ 处置建议**"):
        assert header in markdown


def test_gate_free_text_rejects_unknown_terms():
    assert _gate_free_text("根据对「1号主变」故障的描述", {"1号主变"}) is True
    assert _gate_free_text("绝缘子质量问题", {"1号主变"}) is False


def test_build_markdown_renders_all_sections():
    report = type(
        "Report",
        (),
        {
            "summary": "概述",
            "device_names": ["1号主变"],
            "reasoning": [],
            "located_cause": "冷却器风扇电机停转",
            "rule": "规则",
            "evidence_note": "说明",
            "upstream": ["110kV进线"],
            "downstream": [],
            "topology_paths": ["110kV进线 → 1号主变"],
            "suggestions": [{"cause": "冷却器风扇电机停转", "measures": ["检查"]}],
        },
    )()
    markdown = build_fault_answer_markdown(report)

    assert "1号主变" in markdown
    assert "冷却器风扇电机停转" in markdown
    assert "110kV进线 → 1号主变" in markdown
