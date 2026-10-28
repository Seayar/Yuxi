"""五段式故障溯源报告的装配与发布载荷。

报告结构由故障图谱因果链、实时判据与拓扑证据确定性装配；定位原因、判定与推理说明都
来自可回溯的节点/触发条件，不由模型生成事实。最终 ``answer_markdown`` 供发布门
覆盖助手消息，``report`` 供卡片与审计。
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from yuxi.knowledge.fault_trace.contracts import (
    GRAPH_SCHEMA_VERSION,
    MAX_REPORT_DEVICES,
    MAX_REPORT_PATHS,
    REPORT_SCHEMA_VERSION,
    CauseEvaluation,
    FaultTraceQuery,
)
from yuxi.knowledge.fault_trace.graph import FaultTopology
from yuxi.knowledge.fault_trace.rules import (
    located_causes,
    missing_attributes,
    overall_verdict,
    trigger_summary,
)


def build_fault_trace_payload(
    *,
    query: FaultTraceQuery,
    device: dict[str, Any],
    device_type: str | None,
    reasoning_rows: list[dict[str, Any]],
    causes: list[CauseEvaluation],
    topology: FaultTopology,
    cause_links: str,
    image_used: bool,
    elapsed_ms: int,
    warnings: list[str],
    summary_text: str | None = None,
    location_reasoning: str | None = None,
    cause_measures: dict[str, list[str]] | None = None,
    graph: str = "kg3",
) -> dict[str, Any]:
    """装配五段式报告与发布 Markdown；模型文本通过参数注入，失败时用模板回退。"""

    located_all = located_causes(causes)
    located = located_all[0] if located_all else None
    verdict = overall_verdict(causes)
    missing = missing_attributes(causes)
    location_missing = located.missing if located is not None else missing
    phenomenon_name = _matched_phenomenon_name(reasoning_rows)
    measures = cause_measures or {}
    summary = {
        "text": (summary_text or "").strip() or build_summary_text(query, device, factor_phenomenon(query, None)),
        "device_names": [str(device.get("name") or "")],
        "phenomenon": phenomenon_name,
        "image_used": image_used,
    }
    location = {
        "cause_id": located.cause_id if located else None,
        "cause_name": located.name if located else None,
        "verdict": verdict,
        "causes": [
            {
                "cause_id": cause.cause_id,
                "name": cause.name,
                "true_count": cause.true_count,
                "rule": location_rule(cause),
            }
            for cause in located_all
        ],
        "phenomena": supporting_phenomena(reasoning_rows, located_all),
        "rule": location_rule(located),
        "reasoning": (location_reasoning or "").strip()
        or _location_reasoning(located_all, verdict, causes, location_missing),
        "evidence": _location_evidence(located, causes),
        "missing_data": location_missing,
    }
    impact = {
        "upstream": topology.upstream[:MAX_REPORT_DEVICES],
        "downstream": topology.downstream[:MAX_REPORT_DEVICES],
        "paths": topology.paths[:MAX_REPORT_PATHS],
        "consequences": topology.consequences,
        "warnings": topology.warnings,
    }
    suggestions = [
        {
            "cause_id": cause.cause_id,
            "cause_name": cause.name,
            "measures": list(measures.get(cause.cause_id, [])),
        }
        for cause in located_all
    ]
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "summary": summary,
        "reasoning": [
            {
                "phenomenon_id": row.get("phenomenon_id"),
                "phenomenon": row.get("phenomenon"),
                "causes": [
                    {
                        "cause_id": cause.cause_id,
                        "name": cause.name,
                        "verdict": cause.status,
                        "trigger_summary": trigger_summary(cause),
                        "measures": list(measures.get(cause.cause_id, [])),
                    }
                    for cause in row["causes"]
                ],
            }
            for row in reasoning_rows
        ],
        "location": location,
        "impact": impact,
        "suggestions": suggestions,
        "meta": {
            "graph": graph,
            "schema_version": GRAPH_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "elapsed_ms": int(elapsed_ms),
            "data_missing": missing,
            "device_type": device_type,
            "cause_link_level": cause_links,
            "warnings": warnings,
        },
    }
    return {
        "status": "ok",
        "answer_source": "fault_trace",
        "report": report,
        "answer_markdown": build_fault_answer_markdown(report),
        "answer_verified": _report_verified(located_all),
    }


def _report_verified(located_all: list[CauseEvaluation]) -> bool:
    """报告是否通过证据校验：每个定位原因都由非定性、结果为真的判据支持。

    没有定位原因时不存在待校验的结论，视为通过；有定位原因却没有数据判据支持时
    不发布，避免把定性推断或规则误判当作已校验报告。
    """

    for cause in located_all:
        if not any(trigger.result == "true" and not trigger.qualitative for trigger in cause.triggers):
            return False
    return True


def build_fault_answer_markdown(report: dict[str, Any]) -> str:
    """把五段式报告渲染为发布门展示用的 Markdown；摘要只保留现象简述与设备。"""

    summary = report.get("summary") or {}
    location = report.get("location") or {}
    impact = report.get("impact") or {}
    lines = [
        "**① 故障摘要**",
        f"- 故障现象简述：{summary.get('text') or '—'}",
        f"- 涉及设备：{'、'.join(summary.get('device_names') or ['—'])}",
        "",
        "**② 推理过程**",
    ]
    lines.extend(_reasoning_table(report.get("reasoning") or []))
    lines.append("")
    lines.append("**③ 原因定位**")
    located = [item for item in location.get("causes") or [] if item.get("name")]
    names = "、".join(str(item["name"]) for item in located) or location.get("cause_name") or "未定位"
    lines.append(f"- 定位原因：{names}")
    phenomena = [str(item) for item in location.get("phenomena") or [] if item]
    lines.append(f"- 判定成立的故障现象：{'、'.join(phenomena) if phenomena else '—'}")
    if len(located) > 1:
        lines.append("- 命中规则：")
        for item in located:
            lines.append(f"  - {item['name']}：{item.get('rule') or '—'}")
    else:
        lines.append(f"- 命中规则：{location.get('rule') or '—'}")
    lines.append(f"- 推理依据：{location.get('reasoning') or '—'}")
    lines.append("")
    lines.append("**④ 影响范围**")
    lines.append(f"- 上游根源设备：{_join_names(impact.get('upstream'))}")
    lines.append(f"- 下游受影响设备：{_join_names(impact.get('downstream'))}")
    if impact.get("paths"):
        for path in impact["paths"]:
            lines.append(f"- 拓扑关系：{path}")
    if impact.get("consequences"):
        names = "、".join(str(item.get("name") or "") for item in impact["consequences"])
        lines.append(f"- 故障后果：{names}")
    lines.append("")
    lines.append("**⑤ 处置建议**")
    lines.extend(_suggestions_table(report.get("suggestions") or []))
    return "\n".join(lines)


def _reasoning_table(rows: list[dict[str, Any]]) -> list[str]:
    """按现象分组合并单元格的图谱表格：故障现象 / 可能原因。"""

    lines = [
        "<table>",
        "<thead><tr><th>故障现象</th><th>可能原因</th></tr></thead>",
        "<tbody>",
    ]
    for row in rows:
        phenomenon = _html(row.get("phenomenon") or "—")
        causes = row.get("causes") or []
        if not causes:
            lines.append(f'<tr><td rowspan="1">{phenomenon}</td><td>未匹配到原因</td></tr>')
            continue
        for index, cause in enumerate(causes):
            cause_name = _html(cause.get("name") or "—")
            if index == 0:
                lines.append(f'<tr><td rowspan="{len(causes)}">{phenomenon}</td><td>{cause_name}</td></tr>')
            else:
                lines.append(f"<tr><td>{cause_name}</td></tr>")
    lines.extend(["</tbody>", "</table>"])
    return lines


def _suggestions_table(suggestions: list[dict[str, Any]]) -> list[str]:
    """只列成立原因的处置建议；措施取图谱节点的说明内容。"""

    lines = [
        "<table>",
        "<thead><tr><th>故障原因</th><th>建议措施</th></tr></thead>",
        "<tbody>",
    ]
    if not suggestions:
        lines.append('<tr><td colspan="2">未定位成立原因，暂无处置建议</td></tr>')
    for item in suggestions:
        measures = [str(measure) for measure in item.get("measures") or [] if measure]
        measure_cell = "<br>".join(_html(measure) for measure in measures) if measures else "—"
        lines.append(f"<tr><td>{_html(item.get('cause_name') or '—')}</td><td>{measure_cell}</td></tr>")
    lines.extend(["</tbody>", "</table>"])
    return lines


def _html(value: Any) -> str:
    return html.escape(str(value), quote=False).replace("\n", "<br>")


def supporting_phenomena(
    rows: list[dict[str, Any]],
    located: list[CauseEvaluation] | CauseEvaluation | None,
) -> list[str]:
    """成立原因所在推理行的现象；多个原因取并集去重。"""

    located_list = located if isinstance(located, list) else ([located] if located else [])
    located_ids = {cause.cause_id for cause in located_list}
    if not located_ids:
        return []
    names: list[str] = []
    for row in rows:
        if not any(cause.cause_id in located_ids for cause in row.get("causes") or []):
            continue
        name = str(row.get("phenomenon") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def location_rule(located: CauseEvaluation | None) -> str | None:
    """定位原因命中的表达式组成的规则文本。"""

    if located is None:
        return None
    hits = [str(trigger.expression) for trigger in located.triggers if trigger.result == "true" and trigger.expression]
    if not hits:
        return None
    return f"{' 且 '.join(hits)} → {located.name}"


def _join_names(devices: list[dict[str, Any]] | None) -> str:
    names = [str(device.get("name") or "") for device in devices or [] if device.get("name")]
    return "、".join(names) if names else "无"


def _matched_phenomenon_name(rows: list[dict[str, Any]]) -> str | None:
    for row in rows:
        name = str(row.get("phenomenon") or "").strip()
        if name:
            return name
    return None


def factor_phenomenon(query: FaultTraceQuery, phenomenon: str | None) -> str | None:
    """优先用图谱匹配到的现象，其次用文字/图片识别到的现象。"""

    if phenomenon:
        return phenomenon
    if query.phenomena:
        return "、".join(query.phenomena)
    return query.image_fault_name


def build_summary_text(query: FaultTraceQuery, device: dict[str, Any], phenomenon: str | None) -> str:
    """确定性回退的故障现象简述；不包含原因结论。"""

    name = str(device.get("name") or query.device_name or "相关设备")
    voltage = str(device.get("voltage_level") or query.voltage_level or "").strip()
    subject = f"{voltage} {name}" if voltage and not name.startswith(voltage) else name
    phenomenon_text = phenomenon or "故障现象待补充"
    image_name = str(query.image_fault_name or "").strip()
    image_text = f"，图片识别结果包含「{image_name}」" if image_name and image_name not in phenomenon_text else ""
    return f"{subject}出现「{phenomenon_text}」{image_text}。"


def _location_reasoning(
    located: list[CauseEvaluation],
    verdict: str,
    causes: list[CauseEvaluation],
    missing: list[str],
) -> str:
    if located:
        names = "」「".join(cause.name for cause in located)
        hits = [trigger.name for cause in located for trigger in cause.triggers if trigger.result == "true"]
        hit_text = "、".join(dict.fromkeys(hits)) if hits else "定性条件"
        text = f"依据图谱因果链与实时判据，定位原因「{names}」；命中判据：{hit_text}。"
        if missing:
            text += f" 仍缺少实时数据：{'、'.join(missing)}。"
        return text
    if verdict == "unknown":
        detail = f"缺少实时数据：{'、'.join(missing)}。" if missing else "缺少可求值的实时判据。"
        return f"候选原因均未能判定成立或排除，{detail}"
    if not causes:
        return "图谱未返回可判定的故障原因。"
    return "全部候选原因的实时判据均不成立。"


def _location_evidence(
    located: CauseEvaluation | None,
    causes: list[CauseEvaluation],
) -> list[dict[str, Any]]:
    """定位原因的触发条件证据；未定位时给出未知原因的证据（最多 20 条）。"""

    target = located or next((cause for cause in causes if cause.status == "unknown"), None)
    if target is None:
        return []
    evidence: list[dict[str, Any]] = []
    for trigger in target.triggers[:20]:
        evidence.append(
            {
                "type": "trigger",
                "cause_id": target.cause_id,
                "trigger_id": trigger.trigger_id,
                "name": trigger.name,
                "expression": trigger.expression,
                "values": dict(trigger.values or {}),
                "result": trigger.result,
                "reason": trigger.reason,
            }
        )
    return evidence
