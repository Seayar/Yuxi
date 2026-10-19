"""六段式处置方案的确定性装配、Markdown 渲染与内容校验。

本模块只消费 ``PlanEvidence`` 里的图谱证据：占位符实例化、六段式字段拼装、
思考过程与发布门 Markdown 全部由代码确定，不调用大模型、不连接图谱。
``verify_report`` 是最后一道门——报告中出现的数值必须能在证据文本中找到、
条款号必须来自 ``WorkTemplate.clauses``，不通过则 ``answer_verified`` 为 False。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from yuxi.knowledge.maintenance_plan.contracts import (
    DEMO_RESOURCE_NOTE,
    DEMO_SOURCE_MARK,
    NO_SAFETY_DISTANCE_NOTE,
    NO_TEAM_TEXT,
    OUTAGE_DERIVATION_NOTE,
    PATROL_ITEM_MARK,
    TASK_PATROL,
    TEST_ITEM_MARK,
    TASK_TEST,
    ClauseRef,
    PlanEvidence,
    PlanQuery,
    PlanReport,
    SafetyDistance,
    SpareInfo,
    TeamInfo,
    WorkTemplate,
    report_payload,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 总体设计允许的四个占位符及其兜底取值
# ---------------------------------------------------------------------------
PLACEHOLDER_DEVICE_NAME = "{设备名称}"
PLACEHOLDER_VOLTAGE_LEVEL = "{电压等级}"
PLACEHOLDER_LOCATION = "{所在位置}"
PLACEHOLDER_UPSTREAM_POWER = "{上级电源}"
LEGAL_PLACEHOLDERS = frozenset(
    {
        PLACEHOLDER_DEVICE_NAME,
        PLACEHOLDER_VOLTAGE_LEVEL,
        PLACEHOLDER_LOCATION,
        PLACEHOLDER_UPSTREAM_POWER,
    }
)
FALLBACK_VOLTAGE_LEVEL = "相应电压等级"
FALLBACK_LOCATION = "设备所在位置"
FALLBACK_UPSTREAM_POWER = "上级电源"

# ---------------------------------------------------------------------------
# 模块固定文案（这些句子的数值来自规程标准编号本身，不参与数值校验）
# ---------------------------------------------------------------------------
OUTAGE_NOT_REQUIRED = "不停电"
OUTAGE_NOT_REQUIRED_TEXT = "本作业不停电"
NO_OUTAGE_SCOPE_TEXT = "未检索到停电范围，请以调度下达的停电范围为准"
NO_REFERENCE_TEXT = "未检索到该作业模板的依据条款"

_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]+)\}")
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
# 标题里作业对象部分的最长字数；去掉括号后仍超过就省略对象部分（只留 设备名 + 作业类型 + “方案”）
TITLE_OBJECT_LIMIT = 12
# 作业对象里的全角/半角括号及其内容（T5 线路类模板的作业对象括号里是部件清单）
_BRACKET_PATTERN = re.compile(r"[（(][^（）()]*[）)]")


# ---------------------------------------------------------------------------
# 占位符
# ---------------------------------------------------------------------------
def build_placeholder_context(evidence: PlanEvidence) -> dict[str, str]:
    """占位符取值：设备名称、电压等级、所在位置、上级电源。"""

    device = evidence.device
    voltage_level = device.voltage_level
    if not voltage_level and evidence.safety_distance is not None:
        voltage_level = evidence.safety_distance.voltage_level

    upstream_power = FALLBACK_UPSTREAM_POWER
    if len(evidence.outage_scope) >= 2:
        upstream_power = evidence.outage_scope[-1]

    return {
        PLACEHOLDER_DEVICE_NAME: device.name,
        PLACEHOLDER_VOLTAGE_LEVEL: voltage_level or FALLBACK_VOLTAGE_LEVEL,
        PLACEHOLDER_LOCATION: " > ".join(reversed(evidence.ancestors)) or FALLBACK_LOCATION,
        PLACEHOLDER_UPSTREAM_POWER: upstream_power,
    }


def fill_placeholders(text: str, ctx: dict[str, str]) -> str:
    """只替换四个合法占位符；未知 ``{xxx}`` 原样保留并记 warning，不抛异常。"""

    if not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        key = "{" + match.group(1) + "}"
        if key in LEGAL_PLACEHOLDERS and key in ctx:
            return ctx[key]
        logger.warning(f"处置方案模板存在未知占位符 {key}，已原样保留")
        return match.group(0)

    return _PLACEHOLDER_PATTERN.sub(_replace, text)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
def assemble_report(query: PlanQuery, evidence: PlanEvidence, *, elapsed_seconds: float) -> PlanReport:
    """把证据确定性地装配成六段式方案；模板缺失时各段留空，不编造内容。"""

    ctx = build_placeholder_context(evidence)
    task_type = query.task_type or (evidence.template.task_type if evidence.template is not None else "") or ""
    template = evidence.template or WorkTemplate(
        template_id="",
        name="",
        task_type=task_type,
        device_type=evidence.device.device_type or "",
    )

    work_safety = [fill_placeholders(item, ctx) for item in template.work_safety]
    safety_distance = _safety_distance_dict(evidence.safety_distance)
    if safety_distance is None:
        work_safety.append(NO_SAFETY_DISTANCE_NOTE)

    acceptance = [fill_placeholders(item, ctx) for item in template.acceptance]
    acceptance = _merge_knowledge_items(acceptance, task_type, evidence, template, ctx)

    # 标题的作业对象：用户明确说出的优先，其次模板作业对象，最后退回模板设备类型
    object_part = (query.work_object or "").strip() or template.work_object or template.device_type

    return PlanReport(
        title=_build_title(evidence.device.name, object_part, task_type),
        task_type=task_type,
        device_label=evidence.device.label(),
        outage_scope=_resolve_outage_scope(task_type, evidence, template),
        isolation_measures=fill_placeholders(template.isolation_measures, ctx),
        step_sequence=[fill_placeholders(item, ctx) for item in template.step_sequence],
        key_points=[fill_placeholders(item, ctx) for item in template.key_points],
        tech_params=[fill_placeholders(item, ctx) for item in template.tech_params],
        personal_protection=fill_placeholders(template.personal_protection, ctx),
        safety_distance=safety_distance,
        grounding_requirements=fill_placeholders(template.grounding_requirements, ctx),
        work_safety=work_safety,
        tools=[fill_placeholders(item, ctx) for item in template.tools],
        materials=[fill_placeholders(item, ctx) for item in template.materials],
        team=_team_text(evidence.team),
        spares=[_spare_text(spare) for spare in evidence.spares],
        resource_note=_resource_note(evidence),
        acceptance=acceptance,
        notes=[fill_placeholders(item, ctx) for item in template.notes],
        references=_reference_dicts(template.clauses),
        thinking=build_thinking(query, evidence, elapsed_seconds=elapsed_seconds),
        staffing=fill_placeholders(template.staffing, ctx),
    )


def _build_title(device_name: str, object_part: str, task_type: str) -> str:
    """标题：设备名称 + 作业对象（或设备类型）+ 作业类型 + “方案”。

    作业对象优先用用户明确说出的 ``query.work_object``；没有时用模板的作业对象并去掉全角/
    半角括号及其内容（T5 线路类模板的作业对象带很长的括号部件清单）；去掉括号后仍超过
    ``TITLE_OBJECT_LIMIT`` 个字时省略对象部分，只留 设备名 + 作业类型 + “方案”。

    任务卡给的两个示例“3k18 断路器更换方案”“#2站用变压器 检修方案”说明：设备名称里
    已经含有的部件/类型名不再重复（“#2站用变压器” + 设备类型“变压器”只留前者），
    标题里“方案”二字也只出现一次。
    """

    head = _strip_tail_plan(device_name.strip())
    tail = _strip_tail_plan(_strip_brackets(object_part or ""))
    if not tail or tail in head or len(tail) > TITLE_OBJECT_LIMIT:
        tail = ""
    return f"{head} {tail}{task_type}方案".strip()


def _strip_brackets(text: str) -> str:
    """去掉全角/半角括号及其内容，并去掉首尾空白。"""

    return _BRACKET_PATTERN.sub("", text).strip()


def _strip_tail_plan(text: str) -> str:
    """去掉尾部已有的“方案”，避免拼出“××方案方案”。"""

    while text.endswith("方案"):
        text = text[: -len("方案")].strip()
    return text


def _resolve_outage_scope(task_type: str, evidence: PlanEvidence, template: WorkTemplate) -> str:
    """一、停电范围：巡视且模板为不停电时固定文案，否则用证据里的停电范围。"""

    if task_type == TASK_PATROL and template.outage_required == OUTAGE_NOT_REQUIRED:
        return OUTAGE_NOT_REQUIRED_TEXT
    if not evidence.outage_scope:
        return NO_OUTAGE_SCOPE_TEXT
    return "、".join(evidence.outage_scope) + OUTAGE_DERIVATION_NOTE


def _merge_knowledge_items(
    acceptance: list[str],
    task_type: str,
    evidence: PlanEvidence,
    template: WorkTemplate,
    ctx: dict[str, str],
) -> list[str]:
    """试验类补 test_items、巡视类补 patrol_items，与模板或已有条目重复的不再追加。"""

    if task_type == TASK_TEST:
        items, mark = evidence.test_items, TEST_ITEM_MARK
    elif task_type == TASK_PATROL:
        items, mark = evidence.patrol_items, PATROL_ITEM_MARK
    else:
        return acceptance

    template_text = _template_text(template)
    merged = list(acceptance)
    seen = " ".join(acceptance)
    for item in items:
        text = fill_placeholders(item, ctx)
        if not text or text in seen or text in template_text:
            continue
        merged.append(f"{text}{mark}")
        seen += text
    return merged


def _safety_distance_dict(safety_distance: SafetyDistance | None) -> dict[str, Any] | None:
    """安全距离转前端可渲染的字典；无数据返回 None。"""

    if safety_distance is None:
        return None
    result = {
        "voltage_level": safety_distance.voltage_level,
        "no_outage_m": safety_distance.no_outage_m,
        "work_range_m": safety_distance.work_range_m,
        "live_work_m": safety_distance.live_work_m,
    }
    if safety_distance.remark:
        result["remark"] = safety_distance.remark
    return result


def _team_text(team: TeamInfo | None) -> str:
    """五、责任班组：“{班组名}（{人数}人），负责人：{负责人}”。"""

    if team is None:
        return NO_TEAM_TEXT
    text = team.name
    if team.member_count is not None:
        text += f"（{team.member_count}人）"
    if team.leader:
        text += f"，负责人：{team.leader}"
    return text


def _spare_text(spare: SpareInfo) -> str:
    """备件条目：“{名称} {型号规格}（库存 {数量}，{存放地点}）”，缺项自动省略。"""

    text = f"{spare.name} {spare.model}".strip()
    details: list[str] = []
    if spare.quantity is not None:
        details.append(f"库存 {spare.quantity}")
    if spare.location:
        details.append(spare.location)
    return f"{text}（{'，'.join(details)}）" if details else text


def _resource_note(evidence: PlanEvidence) -> str:
    """班组/备件来自演示数据时如实标注，供前端与验收看到数据口径。"""

    sources = [evidence.team.source_note] if evidence.team is not None else []
    sources.extend(spare.source_note for spare in evidence.spares)
    if any(DEMO_SOURCE_MARK in source for source in sources):
        return DEMO_RESOURCE_NOTE
    return ""


def _reference_dicts(clauses: list[ClauseRef]) -> list[dict[str, str]]:
    """依据条款去重（标准号 + 条款号）后转 dict。"""

    seen: set[tuple[str, str]] = set()
    references: list[dict[str, str]] = []
    for clause in clauses:
        key = (clause.standard_no, clause.clause_no)
        if key in seen:
            continue
        seen.add(key)
        references.append({"standard_no": clause.standard_no, "clause_no": clause.clause_no, "title": clause.title})
    return references


# ---------------------------------------------------------------------------
# 思考过程
# ---------------------------------------------------------------------------
def build_thinking(query: PlanQuery, evidence: PlanEvidence, *, elapsed_seconds: float) -> str:
    """按需求“思考过程模版”的句式生成思考过程，所有计数取自证据真实长度。"""

    device = evidence.device
    template = evidence.template
    task_type = query.task_type or (template.task_type if template is not None else "") or "未识别"
    device_type = (template.device_type if template is not None else "") or device.device_type or "未识别"
    work_object = template.work_object if template is not None else ""
    work_type = f"{task_type}{work_object}" if work_object else task_type
    voltage_level = build_placeholder_context(evidence)[PLACEHOLDER_VOLTAGE_LEVEL]

    step_count = len(template.step_sequence) if template is not None else 0
    tool_material_count = len(template.tools) + len(template.materials) if template is not None else 0

    lines = [
        f"已思考（用时 {_format_seconds(elapsed_seconds)} 秒）",
        f"正在解析您提交的运维任务……识别任务类型为“{task_type}”，目标设备：设备名称为“{device.name}”，"
        f"设备类型为“{device_type}”，电压等级为“{voltage_level}”。",
        f"正在按作业类型“{work_type}”和设备类型“{device_type}”组合检索规程知识节点……",
        "检索完成，获取以下知识：",
        "• 检修前准备：从知识图谱中读取“检修前准备”节点，获取其属性——停电范围来源于拓扑关系"
        "（录入知识图谱或从拓扑结构自动推导），安全隔离措施从节点属性中读取",
        f"• 检修步骤：共{step_count}道工序，含工序顺序、操作要点、技术参数要求",
        "• 安全措施：含个人防护要求、安全距离要求、验电接地要求",
        f"• 工器具及材料清单：共{tool_material_count}项",
        "同步通过知识图谱检索业务关系……" + _business_relation_text(evidence),
    ]
    if template is not None and template.acceptance:
        lines.append("验收标准已从规程知识节点的“验收标准”字段中提取。")
    lines.append("方案生成完成。")
    if evidence.retrieval_log:
        lines.append("")
        lines.append("检索记录：")
        lines.extend(f"• {record}" for record in evidence.retrieval_log)
    return "\n".join(lines)


def _business_relation_text(evidence: PlanEvidence) -> str:
    """业务关系那一句：班组与备件都来自证据，缺哪段就如实说缺哪段。"""

    team = evidence.team
    if team is None:
        team_clause = "未在知识图谱中检索到该设备关联的管辖班组与责任人员"
    else:
        count = f"（{team.member_count}人）" if team.member_count is not None else ""
        team_clause = f"通过管辖关系获取目标设备关联的运维班组“{team.name}{count}”"
        team_clause += f"、责任人员“{team.leader}”" if team.leader else "，未登记责任人员"

    if evidence.spares:
        spares = "、".join(_spare_text(spare) for spare in evidence.spares)
        spare_clause = f"；通过备品备件关系获取备品备件“{spares}”，工器具库存信息待接入外部接口后补充。"
    else:
        spare_clause = "；未在知识图谱中检索到该设备类型的备品备件。"
    return f"{team_clause}{spare_clause}"


def _format_seconds(elapsed_seconds: float) -> str:
    """用时展示：整数秒不带小数，其余保留一位小数。"""

    value = float(elapsed_seconds)
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


# ---------------------------------------------------------------------------
# Markdown 兜底文本
# ---------------------------------------------------------------------------
def build_plan_markdown(report: PlanReport) -> str:
    """六段式 Markdown：# 标题 + 一～六段 + 依据规程；安全距离用标准 Markdown 表格。"""

    work_safety = list(report.work_safety)
    if report.safety_distance is None and NO_SAFETY_DISTANCE_NOTE in work_safety:
        work_safety.remove(NO_SAFETY_DISTANCE_NOTE)

    lines = [f"# {report.title}", "", f"- 任务类型：{report.task_type}", f"- 目标设备：{report.device_label}", ""]
    lines += ["## 一、检修前准备", "", f"**停电范围：**{report.outage_scope}", ""]
    lines += [f"**安全隔离措施：**{report.isolation_measures}", ""]

    lines += ["## 二、检修步骤", ""]
    lines += _list_block("工序顺序", report.step_sequence, ordered=True)
    lines += _list_block("操作要点", report.key_points, ordered=True)
    lines += _list_block("技术参数要求", report.tech_params, ordered=True)

    lines += ["## 三、安全措施", "", f"**个人防护要求：**{report.personal_protection}", ""]
    lines += ["**安全距离要求**", ""]
    lines += _safety_distance_table(report.safety_distance)
    lines += ["", f"**验电接地要求：**{report.grounding_requirements}", ""]
    lines += _list_block("其他作业安全要求", work_safety, ordered=True)

    lines += ["## 四、所需工器具及材料", ""]
    lines += _list_block("工器具清单", report.tools, ordered=False)
    lines += _list_block("材料清单", report.materials, ordered=False)

    lines += ["## 五、责任班组与资源安排", "", f"**责任班组：**{report.team}", ""]
    if report.staffing:
        lines += [f"**人员配置建议：**{report.staffing}", ""]
    lines += _list_block("备品备件", report.spares, ordered=False)
    if report.resource_note:
        lines += [f"**说明：**{report.resource_note}", ""]

    lines += ["## 六、验收标准", ""]
    lines += _list_block("", report.acceptance, ordered=True)
    lines += _list_block("注意事项", report.notes, ordered=True)

    lines += ["## 依据规程", ""]
    if report.references:
        for reference in report.references:
            title = f" {reference['title']}" if reference.get("title") else ""
            lines.append(f"- {reference['standard_no']} {reference['clause_no']}{title}")
    else:
        lines.append(f"- {NO_REFERENCE_TEXT}")
    return "\n".join(lines).rstrip() + "\n"


def _list_block(title: str, items: list[str], *, ordered: bool) -> list[str]:
    """一段带小标题的列表；没有内容时整段省略，``title`` 为空时不再重复小标题。"""

    if not items:
        return []
    block = [f"**{title}**", ""] if title else []
    for index, item in enumerate(items, start=1):
        block.append(f"{index}. {item}" if ordered else f"- {item}")
    block.append("")
    return block


def _safety_distance_table(safety_distance: dict[str, Any] | None) -> list[str]:
    """安全距离标准 Markdown 表格；无数据时给一句可执行的兜底提示。

    列序与 ref/原型_V0.1.0.0.html 的 ``safetyTable`` 一致：
    电压等级 / 带电作业最小安全距离 / 设备不停电安全距离 / 正常活动范围安全距离。
    """

    if safety_distance is None:
        return [NO_SAFETY_DISTANCE_NOTE]
    lines = [
        "| 电压等级 | 带电作业最小安全距离（m） | 设备不停电安全距离（m） | 正常活动范围安全距离（m） |",
        "| --- | --- | --- | --- |",
        f"| {safety_distance['voltage_level']} | {_meter(safety_distance['live_work_m'])} "
        f"| {_meter(safety_distance['no_outage_m'])} | {_meter(safety_distance['work_range_m'])} |",
    ]
    if safety_distance.get("remark"):
        lines += ["", f"**安全距离适用条件：**{safety_distance['remark']}"]
    return lines


def _meter(value: float | None) -> str:
    """表格里的距离单元格：无数据用“-”。"""

    return "-" if value is None else str(value)


# ---------------------------------------------------------------------------
# 校验与载荷
# ---------------------------------------------------------------------------
def verify_report(report: PlanReport, evidence: PlanEvidence) -> bool:
    """数值与条款号校验；不通过返回 False，不抛异常。

    数值口径为“完整数字 token 集合比对”：报告中每个完整数字（如 ``0.7``、``60``）必须作为
    完整数字出现在证据中，因此能拦住“60 改成 6”这类同源篡改（子串包含拦不住）。

    数值校验覆盖报告的全部业务字段，**不含思考过程**：思考过程里的工序数、
    清单项数是按证据长度直接算出来的计数，用时是系统计时，都不是图谱技术数值。
    模块自己注入的两句固定文案（停电范围推导说明、安全距离缺失提示）在扫描前剔除，
    它们携带的数字是规程标准编号本身，不是本次方案的技术数值。
    """

    try:
        corpus_numbers = set(_NUMBER_PATTERN.findall(_evidence_corpus(evidence)))
        text = _report_text(report).replace(OUTAGE_DERIVATION_NOTE, " ").replace(NO_SAFETY_DISTANCE_NOTE, " ")
        for number in _NUMBER_PATTERN.findall(text):
            if number not in corpus_numbers:
                logger.warning(f"处置方案校验未通过：数值 {number} 不在图谱证据中")
                return False
        return _clauses_grounded(report, evidence)
    except Exception as exc:  # noqa: BLE001 - 校验门的契约是不抛异常，异常即判定未通过
        logger.warning(f"处置方案校验异常，判定为未通过：{exc}")
        return False


def produce_maintenance_plan_answer(
    query: PlanQuery, evidence: PlanEvidence, *, elapsed_seconds: float
) -> dict[str, Any]:
    """装配 → Markdown → 校验 → 组装工具返回载荷。"""

    report = assemble_report(query, evidence, elapsed_seconds=elapsed_seconds)
    answer_markdown = build_plan_markdown(report)
    answer_verified = verify_report(report, evidence)
    return report_payload(report, answer_markdown=answer_markdown, answer_verified=answer_verified)


def _clauses_grounded(report: PlanReport, evidence: PlanEvidence) -> bool:
    """references 里的标准号 + 条款号必须来自模板的 ClauseRef。"""

    clauses = evidence.template.clauses if evidence.template is not None else []
    known = {(clause.standard_no, clause.clause_no) for clause in clauses}
    for reference in report.references:
        key = (reference.get("standard_no", ""), reference.get("clause_no", ""))
        if key not in known:
            logger.warning(f"处置方案校验未通过：条款 {key[0]} {key[1]} 不在模板依据条款中")
            return False
    return True


def _report_text(report: PlanReport) -> str:
    """报告里供校验扫描的全部业务文本（不含 thinking）。"""

    parts: list[str] = [
        report.title,
        report.task_type,
        report.device_label,
        report.outage_scope,
        report.isolation_measures,
        *report.step_sequence,
        *report.key_points,
        *report.tech_params,
        report.personal_protection,
        report.grounding_requirements,
        *report.work_safety,
        *report.tools,
        *report.materials,
        report.team,
        report.staffing,
        *report.spares,
        report.resource_note,
        *report.acceptance,
        *report.notes,
    ]
    if report.safety_distance is not None:
        parts.extend(str(value) for value in report.safety_distance.values())
    for reference in report.references:
        parts.extend(reference.values())
    return " ".join(str(part) for part in parts if part)


def _evidence_corpus(evidence: PlanEvidence) -> str:
    """证据文本全集：报告里出现的任何数值都必须能在这里找到。

    覆盖任务卡点名的口径——模板各字段、安全距离、班组、备件、设备属性，外加装配时
    会直接拼进报告的层级路径（ancestors）、停电范围（outage_scope）与电连接邻居。
    ``retrieval_log`` 只是检索过程记录，不参与报告正文，故意不计入语料，
    避免它把校验门放宽。
    """

    device = evidence.device
    parts: list[str] = [
        device.device_id,
        device.name,
        device.device_type or "",
        device.voltage_level or "",
        device.path,
        device.ledger_class or "",
        *evidence.ancestors,
        *evidence.outage_scope,
        *evidence.electrical_neighbors,
        *evidence.test_items,
        *evidence.patrol_items,
    ]
    if evidence.template is not None:
        parts.append(_template_text(evidence.template))

    safety_distance = evidence.safety_distance
    if safety_distance is not None:
        parts.append(safety_distance.voltage_level)
        parts.append(safety_distance.remark)
        parts.extend(
            str(value)
            for value in (safety_distance.no_outage_m, safety_distance.work_range_m, safety_distance.live_work_m)
        )
        if safety_distance.clause is not None:
            parts.append(_clause_text(safety_distance.clause))

    team = evidence.team
    if team is not None:
        parts.append(f"{team.name} {team.leader} {team.managed_area} {team.source_note}")
        if team.member_count is not None:
            parts.append(str(team.member_count))

    for spare in evidence.spares:
        parts.append(f"{spare.name} {spare.model} {spare.location} {spare.source_note}")
        if spare.quantity is not None:
            parts.append(str(spare.quantity))
    return " ".join(str(part) for part in parts if part)


def _template_text(template: WorkTemplate) -> str:
    """作业模板的全部文本（含依据条款）。"""

    parts: list[str] = [
        template.template_id,
        template.name,
        template.task_type,
        template.device_type,
        template.work_object,
        template.outage_required,
        template.isolation_measures,
        *template.step_sequence,
        *template.key_points,
        *template.tech_params,
        template.personal_protection,
        template.grounding_requirements,
        *template.work_safety,
        *template.tools,
        *template.materials,
        *template.acceptance,
        *template.notes,
        template.staffing,
        template.source_note,
    ]
    parts.extend(_clause_text(clause) for clause in template.clauses)
    return " ".join(part for part in parts if part)


def _clause_text(clause: ClauseRef) -> str:
    return f"{clause.standard_no} {clause.clause_no} {clause.title} {clause.excerpt}"
