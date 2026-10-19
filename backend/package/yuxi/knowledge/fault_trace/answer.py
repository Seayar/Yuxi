"""五段式溯源报告的装配与校验门。

V1 报告结构由图谱证据确定性装配（不依赖模型生成事实），``summary`` 与
``evidence_note`` 两段自由文本允许模型润色，但必须通过允许词与数字前置门，
否则回退确定性文案——不允许模型自造设备、原因、措施或数值。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import json_repair

from yuxi.knowledge.fault_trace.contracts import (
    NO_FAULT_GRAPH_MESSAGE,
    FaultReasoningRow,
    FaultTraceReport,
    report_payload,
)
from yuxi.knowledge.fault_trace.graph import (
    FaultGraphEvidence,
    build_reasoning_rows,
    find_device_nodes,
    pick_located_cause,
    resolve_fault_modes,
    trace_topology,
)
from yuxi.utils import logger

_UNGROUNDED_CONNECTORS = frozenset(
    {
        # 中文功能词：助词/介词/连词/常用表达
        "根据",
        "结合",
        "识别",
        "结果",
        "故障",
        "现象",
        "设备",
        "原因",
        "措施",
        "报告",
        "建议",
        "说明",
        "涉及",
        "存在",
        "判定",
        "认为",
        "依据",
        "确认",
        "核实",
        "图片",
        "后续",
        "导致",
        "引起",
        "可能",
        "疑似",
        "属于",
        "主要",
        "包括",
        "以及",
        "通过",
        "按照",
        "针对",
        "相关",
        "异常",
        "正常",
        "描述",
        "补充",
        "现场",
        "情况",
        "信息",
        "内容",
        "正在",
        "步骤",
        "显示",
        "一般",
        "常见",
        "图谱",
        "证据",
        "数据",
        "实测",
        "严重",
        "轻微",
        "明显",
        "表面",
        "层面",
        "程度",
        "对",
        "于",
        "由",
        "为",
        "因",
        "其",
        "该",
        "此",
        "本",
        "在",
        "从",
        "向",
        "与",
        "和",
        "及",
        "并",
        "且",
        "或",
        "等",
        "已",
        "将",
        "要",
        "会",
        "可",
        "能",
        "被",
        "把",
        "让",
        "使",
        "成",
        "至",
        "到",
        "经",
        "过",
        "的",
        "了",
        "着",
        "后",
        "前",
        "时",
        "中",
        "内",
        "外",
        "上",
        "下",
        "就",
        "都",
        "也",
        "而",
        "则",
        "均",
        "应",
        "需",
        "请",
        "以",
        "之",
        "所",
        "若",
        "如",
        "即",
        "又",
        "再",
        "较",
        "更",
        "最",
        "尚",
        "仍",
        "未",
        "非",
        "但",
        # 标点
        "，",
        "。",
        "、",
        "；",
        "：",
        "（",
        "）",
        "「",
        "」",
        "！",
        "？",
        "～",
        "·",
        "—",
        "-",
        " ",
    }
)


async def assemble_report(
    query: Any,
    evidence: FaultGraphEvidence,
    *,
    call_compose: Callable[[str], Awaitable[str]] | None = None,
) -> FaultTraceReport | None:
    """由图谱证据装配五段式报告；证据不足返回 None。"""

    device_names = _resolve_device_names(query, evidence)
    if not device_names:
        return None

    device_node_ids = [str(node.get("id")) for node in find_device_nodes(evidence, device_names=device_names)]
    modes = resolve_fault_modes(evidence, device_node_ids)
    raw_rows = build_reasoning_rows(evidence, modes)
    located_cause, located_measures = pick_located_cause(raw_rows, evidence)
    if not raw_rows and not located_cause:
        return None

    rows = [
        FaultReasoningRow(
            phenomenon=str(row.get("phenomenon") or "故障现象"),
            causes=list(row.get("causes") or []),
            measures=list(row.get("measures") or []),
        )
        for row in raw_rows
    ]
    upstream, downstream, topology_paths = trace_topology(evidence, device_node_ids)

    allowed_terms = _allowed_terms(query, evidence, located_cause, rows, upstream, downstream)
    summary, evidence_note = await _resolve_free_text(
        query=query,
        located_cause=located_cause,
        allowed_terms=allowed_terms,
        call_compose=call_compose,
    )

    suggestions: list[dict[str, Any]] = (
        [{"cause": located_cause, "measures": list(dict.fromkeys(located_measures))}] if located_cause else []
    )
    return FaultTraceReport(
        summary=summary,
        device_names=device_names,
        reasoning=rows,
        located_cause=located_cause,
        rule=_resolve_rule(evidence),
        evidence_note=evidence_note,
        upstream=upstream,
        downstream=downstream,
        topology_paths=topology_paths,
        suggestions=suggestions,
    )


async def produce_fault_trace_answer(
    query: Any,
    evidence: FaultGraphEvidence,
    *,
    call_compose: Callable[[str], Awaitable[str]] | None = None,
) -> dict[str, Any] | str:
    """生成溯源报告载荷；证据不足时返回固定文案（供发布门原样透传）。"""

    if evidence is None or not evidence.nodes:
        return NO_FAULT_GRAPH_MESSAGE
    report = await assemble_report(query, evidence, call_compose=call_compose)
    if report is None:
        return NO_FAULT_GRAPH_MESSAGE
    return report_payload(report, answer_markdown=build_fault_answer_markdown(report), answer_verified=True)


def build_fault_answer_markdown(report: FaultTraceReport) -> str:
    """把五段式报告渲染为发布门展示用的 Markdown。"""

    lines = ["**① 故障摘要**", f"- 故障现象简述：{report.summary}", f"- 涉及设备：{'、'.join(report.device_names)}", ""]
    lines.append("**② 推理过程**")
    for row in report.reasoning:
        causes = "、".join(row.causes) if row.causes else "—"
        measures = "、".join(row.measures) if row.measures else "—"
        lines.append(f"- 现象「{row.phenomenon}」：可能原因 {causes}；处置建议 {measures}")
    lines.append("")
    lines.append("**③ 原因定位**")
    lines.append(f"- 定位原因：{report.located_cause}")
    if report.rule:
        lines.append(f"- 推理依据：{report.rule}")
    lines.append(f"- 说明：{report.evidence_note}")
    lines.append("")
    lines.append("**④ 影响范围**")
    lines.append(f"- 上游根源设备：{'、'.join(report.upstream) if report.upstream else '—'}")
    lines.append(f"- 下游受影响设备：{'、'.join(report.downstream) if report.downstream else '—'}")
    for path in report.topology_paths:
        lines.append(f"- 拓扑关系：{path}")
    lines.append("")
    lines.append("**⑤ 处置建议**")
    if report.suggestions:
        for suggestion in report.suggestions:
            measures = "、".join(suggestion.get("measures") or []) or "见推理过程"
            lines.append(f"- {suggestion.get('cause')}：{measures}")
    else:
        lines.append("- 暂无处置建议")
    return "\n".join(lines)


def _resolve_device_names(query: Any, evidence: FaultGraphEvidence) -> list[str]:
    """设备名称只允许来自用户输入与图谱实体，不重复。"""

    names: list[str] = []
    if query.has_specific_device() and query.device_name not in names:
        names.append(str(query.device_name))
    for node in find_device_nodes(evidence, device_names=[str(query.device_name or query.device_type or "")]):
        name = str(node.get("name") or "")
        if name and name not in names:
            names.append(name)
    return names


def _allowed_terms(
    query: Any,
    evidence: FaultGraphEvidence,
    located_cause: str,
    rows: list[FaultReasoningRow],
    upstream: list[str],
    downstream: list[str],
) -> set[str]:
    """自由文本只能引用这些允许词：用户输入、图片结果与图谱证据。"""

    terms: set[str] = set()
    for token in [
        query.device_name,
        query.device_type,
        query.voltage_level,
        query.image_fault_name,
        query.image_fault_type,
        located_cause,
        *query.phenomena,
        *upstream,
        *downstream,
    ] + [node.get("name") for node in evidence.nodes]:
        if token:
            terms.add(str(token))
    for row in rows:
        terms.add(row.phenomenon)
        terms.update(row.causes)
        terms.update(row.measures)
    return {term for term in terms if term}


async def _resolve_free_text(
    *,
    query: Any,
    located_cause: str,
    allowed_terms: set[str],
    call_compose: Callable[[str], Awaitable[str]] | None,
) -> tuple[str, str]:
    """生成 ① 故障现象简述与 ③ 推理说明；模型润色通过前置门才生效。"""

    fallback_summary = _fallback_summary(query, located_cause)
    fallback_note = (
        f"依据知识图谱因果链匹配，定位原因「{located_cause}」"
        f"{'与图片识别结果相关' if query.image_fault_name else '与现场故障现象相符'}，"
        "其余候选原因因缺乏现象或数据证据被排除。"
    )
    if call_compose is None:
        return fallback_summary, fallback_note
    try:
        raw = await call_compose(_COMPOSE_PROMPT.format(summary=fallback_summary, note=fallback_note))
        payload = _parse_compose(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"故障溯源报告润色失败，回退确定性文案: {exc}")
        return fallback_summary, fallback_note
    summary = str(payload.get("summary") or "").strip()
    note = str(payload.get("evidence_note") or "").strip()
    if not summary or not note:
        return fallback_summary, fallback_note
    if not _gate_free_text(summary, allowed_terms) or not _gate_free_text(note, allowed_terms):
        logger.warning("故障溯源报告润色未通过允许词/数字门，回退确定性文案")
        return fallback_summary, fallback_note
    return summary, note


def _fallback_summary(query: Any, located_cause: str) -> str:
    brief = "、".join(query.phenomena) if query.phenomena else (query.image_fault_name or "故障现象")
    device = query.device_name or query.device_type or "相关设备"
    return f"根据对「{device}」故障的描述（{brief}），结合图谱证据判定涉及{located_cause or '相关故障因素'}。"


def _parse_compose(raw: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    payload = json_repair.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("报告润色未返回 JSON 对象")
    return payload


def _gate_free_text(text: str, allowed_terms: set[str]) -> bool:
    """允许词门：对文本做“最长允许词 / 功能词 / 标点”的贪心消费。

    不允许词也不属于功能词/标点的字符（例如虚构的设备、原因、措施或数值）
    会让文本无法被完整消费而拒绝。功能词表只覆盖通用连接表达，
    具体实体名词必须来自图谱证据或用户输入。
    """

    allowed = sorted((term for term in allowed_terms if term), key=len, reverse=True)
    connectors = sorted(_UNGROUNDED_CONNECTORS, key=len, reverse=True)
    index = 0
    length = len(text)
    while index < length:
        matched = next((term for term in allowed if text.startswith(term, index)), None)
        if matched is not None:
            index += len(matched)
            continue
        matched = next((connector for connector in connectors if text.startswith(connector, index)), None)
        if matched is not None:
            index += len(matched)
            continue
        if text[index].isspace():
            index += 1
            continue
        return False
    return True


def _resolve_rule(evidence: FaultGraphEvidence) -> str | None:
    """③ 推理依据：优先取故障模式的诱发因素（has_trigger_condition）文本。"""

    triggers = evidence.nodes_with_label("trigger")
    for node in triggers:
        name = str(node.get("name") or "")
        if name:
            return name
    return None


_COMPOSE_PROMPT = """你是配电网故障原因溯源报告的文案整理器。
请把下面给出的故障摘要与推理说明改写成面向运维人员的简洁中文（整段必须且只是一个 JSON 对象）：
{{"summary": "故障现象简述（系统理解用户描述后的完整概述，非逐字复述）", "evidence_note": "定位原因的推理说明"}}

注意：
- 只能使用原文中出现过的设备名称、故障现象、原因与图谱证据词，禁止新增任何设备、原因、措施或数字。
输入：
{summary}
{note}"""
