"""kg4 处置方案的确定性装配、Markdown 渲染与内容校验。

本模块只消费 ``ScenarioEvidence`` 与人员抽样结果：步骤/安全措施/资源/人员的格式化、
方案 Markdown 与“思考过程”全部由代码确定，不调用大模型、不连接图谱。
``verify_report`` 是最后一道门——分支值必须与 Redis/用户确认值一致、条目必须来自检索证据、
报告中的数字必须能在证据中找到，不通过则 ``answer_verified`` 为 False。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from yuxi.knowledge.maintenance_plan.contracts import (
    BRANCH_USER_CONFIRMED_NOTE,
    ScenarioEvidence,
    ScenarioPlanReport,
    PersonnelAssignment,
    report_payload,
)
from yuxi.knowledge.maintenance_plan.sources import format_personnel

logger = logging.getLogger(__name__)

_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")


def format_step(item) -> str:
    """检修步骤：``{步骤序号}. {操作内容}``。"""

    return f"{item.sequence}. {item.content}".strip()


def format_safety(item) -> str:
    """安全措施：``{名称}：{具体内容}``；名称为空时只输出内容。"""

    if item.name and item.content:
        return f"{item.name}：{item.content}"
    return item.name or item.content


def format_resource(item) -> str:
    """资源：数量>0 用 名称+数量+单位；数量=0 用 名称（数量要求）+单位。"""

    if item.quantity is None:
        text = item.name
    elif item.quantity > 0:
        text = f"{item.name} {item.quantity} {item.unit}".strip() if item.unit else f"{item.name} {item.quantity}"
    else:
        requirement = item.quantity_requirement or item.unit
        if requirement and item.unit and item.quantity_requirement:
            text = f"{item.name}（{requirement}，{item.unit}）"
        elif requirement:
            text = f"{item.name}（{requirement}）"
        else:
            text = item.name
    if item.stock_note:
        text = f"{text}【{item.stock_note}】"
    return text


def format_personnel_requirement(item) -> str:
    """没有人员名单时，至少给出岗位与人数。"""

    return f"{item.role}：{item.count} 人" if item.role else f"{item.count} 人"


def assemble_report(
    evidence: ScenarioEvidence,
    assignment: PersonnelAssignment,
    *,
    data_notes: list[str],
    elapsed_seconds: float,
) -> ScenarioPlanReport:
    """把证据确定性地装配为可发布的方案。"""

    branch = evidence.branch
    personnel_lines = format_personnel(assignment)
    if not personnel_lines:
        personnel_lines = [format_personnel_requirement(item) for item in evidence.personnel]

    notes = list(data_notes)
    if branch is not None and branch.source == "user" and BRANCH_USER_CONFIRMED_NOTE not in notes:
        notes.append(BRANCH_USER_CONFIRMED_NOTE)
    if assignment.note and assignment.note not in notes:
        notes.append(assignment.note)
    for warning in evidence.warnings:
        if warning not in notes:
            notes.append(warning)

    return ScenarioPlanReport(
        title=f"{evidence.device.name} {evidence.scenario.name}处置方案",
        device_label=evidence.device.label(),
        scenario_name=evidence.scenario.name,
        level=evidence.scenario.level,
        branch_dimension=branch.dimension if branch is not None else "",
        branch_condition=branch.condition if branch is not None else "",
        branch_value=branch.condition if branch is not None else "",
        branch_source=branch.source if branch is not None else "",
        branch=branch.branch if branch is not None else "",
        steps=[format_step(item) for item in evidence.steps],
        safety=[format_safety(item) for item in evidence.safety],
        resources=[format_resource(item) for item in evidence.resources],
        personnel=personnel_lines,
        team=evidence.team.name if evidence.team is not None else "未检索到管辖班组",
        personnel_total=sum(item.count for item in evidence.personnel),
        data_notes=notes,
        thinking=build_thinking(evidence, elapsed_seconds=elapsed_seconds),
    )


def build_plan_markdown(report: ScenarioPlanReport) -> str:
    """方案 Markdown：设备/场景/分支 + 四段业务内容 + 数据说明。"""

    lines = [
        f"# {report.title}",
        "",
        f"- 目标设备：{report.device_label}",
        f"- 检修作业场景：{report.scenario_name}" + (f"（{report.level}）" if report.level else ""),
        f"- 执行分支：{report.branch_dimension} = {report.branch_condition}（{report.branch}）"
        if report.branch_dimension
        else "- 执行分支：不区分分支",
        "",
    ]

    lines += ["## 一、检修步骤", ""]
    lines += _list_block(report.steps, ordered=False, bullet=False)

    lines += ["## 二、安全措施", ""]
    lines += _list_block(report.safety, ordered=False)

    lines += ["## 三、所需工器具及材料", ""]
    lines += _list_block(report.resources, ordered=False)

    lines += [
        "## 四、责任班组与人员安排",
        "",
        f"- 责任班组：{report.team}",
        f"- 所需人数：{report.personnel_total} 人",
        "",
    ]
    lines += _list_block(report.personnel, ordered=False)

    if report.data_notes:
        lines += ["## 五、数据说明", ""]
        lines += [f"- {note}" for note in report.data_notes]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _list_block(items: list[str], *, ordered: bool, bullet: bool = True) -> list[str]:
    """列表块；没有内容时给一句可执行的缺省说明。``bullet=False`` 用于已自带序号的步骤。"""

    if not items:
        return ["（图谱中未检索到对应内容）", ""]
    block: list[str] = []
    for index, item in enumerate(items, start=1):
        if not bullet:
            block.append(item)
        elif ordered:
            block.append(f"{index}. {item}")
        else:
            block.append(f"- {item}")
    block.append("")
    return block


def build_thinking(evidence: ScenarioEvidence, *, elapsed_seconds: float) -> str:
    """按检索记录生成“思考过程”，所有计数取自证据真实长度。"""

    lines = [
        f"已思考（用时 {_format_seconds(elapsed_seconds)} 秒）",
        f"正在解析用户提交的运维任务……目标设备：“{evidence.device.name}”，"
        f"设备类型：“{evidence.device.device_type or '未识别'}”。",
        f"按设备类型检索检修作业场景：“{evidence.scenario.name}”（{evidence.scenario.level or '未标注等级'}）。",
        f"检修步骤 {len(evidence.steps)} 道，安全措施 {len(evidence.safety)} 条，"
        f"资源需求 {len(evidence.resources)} 项，人员需求 {sum(item.count for item in evidence.personnel)} 人。",
        "方案生成完成。",
    ]
    if evidence.retrieval_log:
        lines.append("")
        lines.append("检索记录：")
        lines.extend(f"• {record}" for record in evidence.retrieval_log)
    return "\n".join(lines)


def _format_seconds(elapsed_seconds: float) -> str:
    value = float(elapsed_seconds)
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


# ---------------------------------------------------------------------------
# 校验与载荷
# ---------------------------------------------------------------------------
def verify_report(
    report: ScenarioPlanReport,
    evidence: ScenarioEvidence,
    assignment: PersonnelAssignment,
) -> bool:
    """校验分支、条目与数字；不通过返回 False，不抛异常。"""

    try:
        if not _branch_matches(report, evidence):
            logger.warning("处置方案校验未通过：分支与证据不一致")
            return False
        if report.steps != [format_step(item) for item in evidence.steps]:
            logger.warning("处置方案校验未通过：步骤与证据不一致")
            return False
        if report.safety != [format_safety(item) for item in evidence.safety]:
            logger.warning("处置方案校验未通过：安全措施与证据不一致")
            return False
        if report.resources != [format_resource(item) for item in evidence.resources]:
            logger.warning("处置方案校验未通过：资源清单与证据不一致")
            return False
        expected_personnel = format_personnel(assignment) or [
            format_personnel_requirement(item) for item in evidence.personnel
        ]
        if report.personnel != expected_personnel:
            logger.warning("处置方案校验未通过：人员安排与证据不一致")
            return False
        if report.personnel_total != sum(item.count for item in evidence.personnel):
            logger.warning("处置方案校验未通过：人员合计与证据不一致")
            return False
        named = sum(len(names) for names in assignment.roles.values())
        if named and named != assignment.total:
            logger.warning("处置方案校验未通过：人员名单数量与需求人数不一致")
            return False
        corpus = _evidence_corpus(evidence, assignment, report)
        for number in _NUMBER_PATTERN.findall(_report_text(report)):
            if number not in corpus:
                logger.warning(f"处置方案校验未通过：数值 {number} 不在证据中")
                return False
        return True
    except Exception as exc:  # noqa: BLE001 - 校验门的契约是不抛异常，异常即判定未通过
        logger.warning(f"处置方案校验异常，判定为未通过：{exc}")
        return False


def _branch_matches(report: ScenarioPlanReport, evidence: ScenarioEvidence) -> bool:
    branch = evidence.branch
    if branch is None:
        return not report.branch_dimension and report.branch_value == ""
    return (
        report.branch_dimension == branch.dimension
        and report.branch_condition == branch.condition
        and report.branch_value == branch.condition
        and report.branch_source == branch.source
        and report.branch == branch.branch
    )


def _report_text(report: ScenarioPlanReport) -> str:
    """报告里供校验扫描的业务文本（不含 thinking）。"""

    parts: list[str] = [
        report.title,
        report.device_label,
        report.scenario_name,
        report.level,
        report.branch_dimension,
        report.branch_condition,
        report.branch_value,
        report.branch,
        *report.steps,
        *report.safety,
        *report.resources,
        *report.personnel,
        report.team,
        str(report.personnel_total),
    ]
    return " ".join(str(part) for part in parts if part)


def _evidence_corpus(
    evidence: ScenarioEvidence, assignment: PersonnelAssignment, report: ScenarioPlanReport
) -> set[str]:
    """证据数字全集：报告里出现的数字必须能在这里找到。"""

    parts: list[str] = [
        evidence.device.name,
        evidence.device.path,
        evidence.device.device_type or "",
        evidence.device.voltage_level or "",
        *[str(item) for item in evidence.device.ledger_ids],
        evidence.scenario.name,
        evidence.scenario.description,
        evidence.scenario.level,
        evidence.scenario.branch_dimension,
        evidence.scenario.branch1_condition,
        evidence.scenario.branch2_condition,
    ]
    for step in evidence.steps:
        parts.extend([str(step.sequence), step.content])
    for item in evidence.safety:
        parts.extend([item.name, item.content, item.applicable_branch])
    for item in evidence.resources:
        parts.extend(
            [
                item.name,
                item.unit,
                item.quantity_requirement,
                item.applicable_branch,
                str(item.quantity) if item.quantity is not None else "",
            ]
        )
    for item in evidence.personnel:
        parts.extend([item.role, str(item.count), item.applicable_branch])
    if evidence.team is not None:
        parts.extend([evidence.team.name, evidence.team.managed_area])
    if evidence.branch is not None:
        parts.extend([evidence.branch.dimension, evidence.branch.condition, evidence.branch.branch])
    for names in assignment.roles.values():
        parts.extend(names)
    parts.extend([str(assignment.total), str(report.personnel_total)])
    parts.extend([str(index) for index in range(1, len(evidence.steps) + 1)])
    return set(_NUMBER_PATTERN.findall(" ".join(parts)))


def produce_maintenance_plan_answer(
    evidence: ScenarioEvidence,
    assignment: PersonnelAssignment,
    *,
    data_notes: list[str],
    elapsed_seconds: float,
) -> dict[str, Any]:
    """装配 → Markdown → 校验 → 组装工具返回载荷。"""

    report = assemble_report(evidence, assignment, data_notes=data_notes, elapsed_seconds=elapsed_seconds)
    answer_markdown = build_plan_markdown(report)
    answer_verified = verify_report(report, evidence, assignment)
    return report_payload(report, answer_markdown=answer_markdown, answer_verified=answer_verified)
