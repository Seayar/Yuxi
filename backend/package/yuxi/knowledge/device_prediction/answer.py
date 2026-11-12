"""设备预测维护报告的确定性装配、Markdown 渲染与内容校验。

报告四个部分的字段全部来自两处外部事实：健康评估/故障预测服务返回的业务字段，以及
``kg5`` 中按预测故障类型匹配到的维护知识；装配不发明数值。唯一由大模型生成的内容是
“状态解读”，它必须在给定数值范围内说话——生成文本里出现的数字必须能在健康评估结果里
找到，否则退回按评分、等级与指标确定性拼出的解读句。

``verify_report`` 是发布前的最后一道门：报告字段必须与证据逐项相等，状态解读的数字必须
可追溯到健康评估结果，不通过则 ``answer_verified`` 为 False。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from yuxi.knowledge.device_prediction.contracts import (
    HEALTH_DOWNTIME_MISSING_NOTE,
    HEALTH_FAULT_RATE_MISSING_NOTE,
    HEALTH_INDICATORS_MISSING_NOTE,
    MAINTENANCE_TIME_DERIVED_NOTE,
    NUMBERED_ITEM_PATTERN,
    STATUS_READOUT_TEMPLATE_NOTE,
    PredictionEvidence,
    PredictionReport,
    dedup,
    report_payload,
)

logger = logging.getLogger(__name__)

_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
# 中文数字换算：多位数字（“九十五”“一百二十”）一律换算后比对；单字只有在紧跟量值单位时
# 才当作数字，避免把“一次”“一项”这类量词当成编造数值。
_CJK_NUMERAL_RUN = re.compile(r"[零〇一二三四五六七八九十百千万亿两]+")
_CJK_QUANTITY_UNITS = frozenset("分℃度%％")
_CJK_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CJK_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}
# 健康等级声明：只认“等级为/等级：/状态：/处于/评为”等明确句式。设备领域里“异常记录”“注意”
# 都是普通用词，逐个匹配等级词会把正常解读误判成编造。取值域同时覆盖 SRS 的四级与服务实测的
# “良好/一般”，所有声明都必须与健康评估结果的等级一致。
_LEVEL_CLAIM_PATTERN = re.compile(
    r"(?:等级(?:为|是)?|状态(?:为|是)?|处于|评为|评定为|属于)\s*[:：]?\s*[“\"'「]?"
    r"(正常|良好|一般|注意|异常|严重|优秀|较差)"
)
# 状态解读的提示词长度上限：报告里的一句话结论，过长视为模型跑题
MAX_STATUS_READOUT_CHARS = 400

StatusReadoutModelCall = Callable[[str], Awaitable[str]]


# ---------------------------------------------------------------------------
# 状态解读（模型生成 + 确定性兜底）
# ---------------------------------------------------------------------------
async def generate_status_readout(
    evidence: PredictionEvidence, *, model_call: StatusReadoutModelCall | None
) -> tuple[str, bool]:
    """生成“状态解读”，返回 ``(解读文本, 是否由模型生成)``。

    模型不可用、输出为空、超长或出现健康评估结果之外的数字时，退回确定性模板。
    """

    if model_call is not None:
        try:
            text = _normalize_readout(await model_call(build_status_readout_prompt(evidence)))
            if text and _numbers_are_grounded(text, evidence):
                return text, True
            logger.warning("状态解读未通过数值校验或为空，改用确定性解读")
        except Exception as exc:  # noqa: BLE001 —— 解读失败不能阻断报告
            logger.warning(f"状态解读模型调用失败，改用确定性解读: {exc}")
    return build_template_readout(evidence), False


def build_status_readout_prompt(evidence: PredictionEvidence) -> str:
    """构造状态解读提示词：只提供健康评估事实，要求不引入新数字与结论。"""

    health = evidence.health
    facts = {
        "设备": evidence.device_label,
        "健康评分": health.health_score,
        "健康等级": health.health_level,
        "监测指标": [indicator.to_dict() for indicator in health.indicators],
        "故障率": health.fault_rate,
        "平均停运时间": health.average_downtime_hours,
    }
    return (
        "你是电力设备健康状态解读助手。请根据下面的设备健康评估结果，用一两句中文说明设备当前"
        "健康状态、存在扣分的指标与需要关注的方向。\n"
        "要求：\n"
        "1. 只能使用给定结果里出现的数值与指标，不得编造新的数字、指标、故障类型或检修结论；\n"
        "2. 不要输出 Markdown、标题、列表符号或换行分段；\n"
        "3. 只输出解读文本本身，不要输出其他解释。\n\n"
        f"健康评估结果：\n{json.dumps(facts, ensure_ascii=False)}"
    )


def build_template_readout(evidence: PredictionEvidence) -> str:
    """按评分、等级与扣分指标确定性拼出状态解读，不引入证据之外的任何数字。"""

    health = evidence.health
    parts = [f"{evidence.device_name}当前健康评分为 {health.health_score} 分，健康等级为“{health.health_level}”。"]
    deducted = [
        indicator
        for indicator in health.indicators
        if indicator.indicator_score and indicator.indicator_score != "0 分"
    ]
    if deducted:
        details = "；".join(f"{_indicator_reading(item)}（{item.indicator_score}）" for item in deducted)
        parts.append(f"存在扣分的监测指标：{details}。")
    elif health.indicators:
        parts.append("各项监测指标按判分标准均未扣分。")
    if health.fault_rate and health.average_downtime_hours:
        parts.append(f"故障率 {health.fault_rate}，平均停运时间 {health.average_downtime_hours}。")
    return "".join(parts)


def _indicator_reading(item) -> str:
    """指标读数：名称 + 数值 + 单位；符号单位紧贴数值，词类单位留一个空格。"""

    reading = f"{item.indicator_name} {item.indicator_value}".rstrip()
    unit = item.indicator_unit
    if not unit:
        return reading
    return f"{reading}{unit}" if _is_symbol_unit(unit) else f"{reading} {unit}"


def _is_symbol_unit(unit: str) -> bool:
    """单位是否以计量符号开头（% / ℃ / ° / 拉丁或希腊字母）。"""

    first = unit[:1]
    return first in "%℃°μ" or first.isascii() and first.isalpha()


def _normalize_readout(text: str) -> str:
    """去掉模型输出里的换行与多余空白；超长或为空返回空串。"""

    cleaned = re.sub(r"\s*\n+\s*", " ", str(text or "")).strip()
    return cleaned if cleaned and len(cleaned) <= MAX_STATUS_READOUT_CHARS else ""


def _numbers_are_grounded(text: str, evidence: PredictionEvidence) -> bool:
    """状态解读必须落在健康评估结果内：数字可追溯、等级结论与评估一致。

    阿拉伯数字逐个比对健康评估结果；中文数字先换算成数值再比对（“九十五”按 95 处理，
    单字只在紧跟分/度/% 时才算数字，避免把“一次”这类量词误判为编造）。文本中所有明确的
    等级声明（“等级为”“状态：”“处于”“评为”）都必须与健康评估给出的等级一致；没有触发词的
    说法（如“设备整体良好”）不在此护栏内，最终事实仍以报告中逐项校验过的 ``health_level``
    字段为准。
    """

    claimed = _LEVEL_CLAIM_PATTERN.findall(text)
    if any(value != evidence.health.health_level for value in claimed):
        return False
    corpus = _number_corpus(evidence)
    if any(value not in corpus for value in _cjk_quantities(text)):
        return False
    return all(number in corpus for number in _NUMBER_PATTERN.findall(text))


def _cjk_quantities(text: str) -> set[str]:
    """找出中文数字并换算成阿拉伯数字文本；换算不出时返回原文，使其必然不匹配。"""

    values: set[str] = set()
    for match in _CJK_NUMERAL_RUN.finditer(text):
        run = match.group(0)
        following = text[match.end() : match.end() + 1]
        if len(run) < 2 and following not in _CJK_QUANTITY_UNITS:
            continue
        value = _cjk_value(run)
        values.add(str(value) if value is not None else run)
    return values


def _cjk_value(run: str) -> int | None:
    """把“九十五”“一百二十”这类中文数字换算为整数；含无法识别的字符时返回 None。"""

    total = 0
    section = 0
    number = 0
    for char in run:
        digit = _CJK_DIGITS.get(char)
        if digit is not None:
            number = digit
            continue
        unit = _CJK_UNITS.get(char)
        if unit is None:
            return None
        if unit >= 10000:
            total = (total + section + number) * unit
            section = 0
        else:
            section += (number or 1) * unit
        number = 0
    return total + section + number


def _number_corpus(evidence: PredictionEvidence) -> set[str]:
    """状态解读允许引用的数字全集（健康评估结果与设备标识）。"""

    health = evidence.health
    parts: list[str] = [evidence.device_name, evidence.device_label, health.health_score, health.health_level]
    parts.extend([health.fault_rate, health.average_downtime_hours])
    for indicator in health.indicators:
        parts.extend(
            [
                indicator.indicator_name,
                indicator.indicator_value,
                indicator.indicator_unit,
                indicator.weight_ratio,
                indicator.indicator_score,
                indicator.criteria,
            ]
        )
    return set(_NUMBER_PATTERN.findall(" ".join(part for part in parts if part)))


# ---------------------------------------------------------------------------
# 报告装配
# ---------------------------------------------------------------------------
def assemble_report(
    evidence: PredictionEvidence,
    *,
    status_readout: str,
    data_notes: list[str],
    elapsed_seconds: float,
) -> PredictionReport:
    """把证据确定性地装配为可发布的报告。"""

    health = evidence.health
    prediction = evidence.prediction
    guidance = evidence.guidance
    return PredictionReport(
        title=_report_title(evidence),
        device_label=evidence.device_label,
        device_id=evidence.device_id,
        health_score=health.health_score,
        health_level=health.health_level,
        status_readout=status_readout,
        indicators=[indicator.to_dict() for indicator in health.indicators],
        fault_type=prediction.predicted_fault_type,
        probability=prediction.probability,
        expected_failure_time=prediction.expected_failure_time,
        degradation_trend=prediction.degradation_trend,
        fault_rate=health.fault_rate,
        average_downtime_hours=health.average_downtime_hours,
        matched_phenomenon=guidance.fault_type,
        maintenance_time=prediction.maintenance_time,
        resources=list(guidance.resources),
        cautions=list(guidance.cautions),
        data_notes=dedup(data_notes),
        thinking=build_thinking(evidence, elapsed_seconds=elapsed_seconds),
    )


def build_data_notes(evidence: PredictionEvidence, *, status_readout_generated: bool) -> list[str]:
    """数据说明：缺失项与推导项如实标注，不把缺口写成内容。"""

    notes: list[str] = []
    if not evidence.health.indicators:
        notes.append(HEALTH_INDICATORS_MISSING_NOTE)
    if not evidence.health.fault_rate:
        notes.append(HEALTH_FAULT_RATE_MISSING_NOTE)
    if not evidence.health.average_downtime_hours:
        notes.append(HEALTH_DOWNTIME_MISSING_NOTE)
    if evidence.prediction.maintenance_time_derived:
        notes.append(MAINTENANCE_TIME_DERIVED_NOTE)
    if not status_readout_generated:
        notes.append(STATUS_READOUT_TEMPLATE_NOTE)
    notes.extend(evidence.guidance.notes)
    return dedup(notes)


def _report_title(evidence: PredictionEvidence) -> str:
    """需求规定的报告名称：设备健康评估-设备名称（设备类型 · 电压等级）。"""

    meta = " · ".join(item for item in (evidence.device_type, evidence.voltage_level) if item)
    return f"设备健康评估-{evidence.device_name}（{meta}）" if meta else f"设备健康评估-{evidence.device_name}"


def build_report_markdown(report: PredictionReport) -> str:
    """报告 Markdown：四个需求段落 + 数据说明。"""

    lines = [
        f"# {report.title}",
        "",
        f"- 目标设备：{report.device_label}",
        f"- 设备 ID（内部编号，用于对接预测服务）：{report.device_id}",
        "",
        "## 一、设备健康评估",
        "",
        f"- 健康评分：{report.health_score} 分",
        f"- 健康等级：{report.health_level}",
        f"- 状态解读：{report.status_readout}",
        "",
    ]
    lines += _indicator_table(report.indicators)

    lines += [
        "## 二、故障预测",
        "",
        f"- 预测故障类型：{report.fault_type}",
        f"- 发生概率：{report.probability}",
        f"- 预计发生时间：{report.expected_failure_time}",
        f"- 退化趋势：{report.degradation_trend}",
        "",
        "## 三、可靠性与风险评估",
        "",
        f"- 故障率：{report.fault_rate or '未返回'}",
        f"- 平均停运时间：{report.average_downtime_hours or '未返回'}",
        "",
        "## 四、维护计划建议",
        "",
        f"- 维护时机：{report.maintenance_time}",
        f"- 匹配故障现象：{report.matched_phenomenon or '未匹配'}",
        "",
        "所需资源：",
        "",
    ]
    lines += _list_block(report.resources, empty_text="（知识图谱中未匹配到该故障现象的所需资源）")
    lines += ["", "注意事项：", ""]
    lines += _list_block(report.cautions, empty_text="（知识图谱中未匹配到该故障现象的注意事项）")

    if report.data_notes:
        lines += ["", "## 五、数据说明", ""]
        lines += [f"- {note}" for note in report.data_notes]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _indicator_table(indicators: list[dict[str, str]]) -> list[str]:
    """监测指标表格：名称、数值、单位、权重、扣分与判分标准。"""

    if not indicators:
        return ["（设备健康评估服务未返回监测指标）", ""]
    rows = ["| 指标名称 | 指标值 | 单位 | 权重 | 扣分 | 判分标准 |", "|---|---|---|---|---|---|"]
    for item in indicators:
        cells = [
            item.get("indicator_name", ""),
            item.get("indicator_value", ""),
            item.get("indicator_unit", ""),
            item.get("weight_ratio", ""),
            item.get("indicator_score", ""),
            item.get("criteria", ""),
        ]
        rows.append("| " + " | ".join(_escape_cell(cell) for cell in cells) + " |")
    rows.append("")
    return rows


def _escape_cell(value: str) -> str:
    """表格单元格转义：竖线会破坏 Markdown 表格。"""

    return str(value or "").replace("|", "\\|")


def _list_block(items: list[str], *, empty_text: str) -> list[str]:
    """有序列表块；条目自带编号时保留原编号，没有内容时给一句缺省说明。"""

    if not items:
        return [empty_text]
    return [
        item if NUMBERED_ITEM_PATTERN.match(item) else f"{index}. {item}" for index, item in enumerate(items, start=1)
    ]


def build_thinking(evidence: PredictionEvidence, *, elapsed_seconds: float) -> str:
    """按证据生成“思考过程”，所有计数取自证据真实长度。"""

    guidance = evidence.guidance
    lines = [
        f"已思考（用时 {_format_seconds(elapsed_seconds)} 秒）",
        f"正在识别您请求评估的设备……目标设备为“{evidence.device_label}”。",
        f"已按设备台账解析出预测服务设备 ID：{evidence.device_id}（来源：{evidence.device_id_source}）。",
        f"设备健康评估完成：健康评分 {evidence.health.health_score} 分，"
        f"健康等级“{evidence.health.health_level}”，监测指标 {len(evidence.health.indicators)} 项。",
        f"故障预测完成：预测故障类型“{evidence.prediction.predicted_fault_type}”，"
        f"发生概率 {evidence.prediction.probability}，预计发生时间 {evidence.prediction.expected_failure_time}。",
        f"按预测故障类型匹配知识图谱维护知识：匹配故障现象“{guidance.fault_type or '未匹配'}”，"
        f"所需资源 {len(guidance.resources)} 项、注意事项 {len(guidance.cautions)} 项。",
        "设备预测维护报告生成完成。",
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
def verify_report(report: PredictionReport, evidence: PredictionEvidence) -> bool:
    """校验报告字段与服务/图谱证据逐项一致、状态解读数字可追溯；不通过返回 False。"""

    try:
        health = evidence.health
        prediction = evidence.prediction
        guidance = evidence.guidance
        expected = {
            "device_id": evidence.device_id,
            "health_score": health.health_score,
            "health_level": health.health_level,
            "fault_rate": health.fault_rate,
            "average_downtime_hours": health.average_downtime_hours,
            "fault_type": prediction.predicted_fault_type,
            "probability": prediction.probability,
            "expected_failure_time": prediction.expected_failure_time,
            "degradation_trend": prediction.degradation_trend,
            "maintenance_time": prediction.maintenance_time,
        }
        actual = {
            "device_id": report.device_id,
            "health_score": report.health_score,
            "health_level": report.health_level,
            "fault_rate": report.fault_rate,
            "average_downtime_hours": report.average_downtime_hours,
            "fault_type": report.fault_type,
            "probability": report.probability,
            "expected_failure_time": report.expected_failure_time,
            "degradation_trend": report.degradation_trend,
            "maintenance_time": report.maintenance_time,
        }
        mismatched = [key for key, value in expected.items() if actual[key] != value]
        if mismatched:
            logger.warning(f"设备预测报告校验未通过：字段与证据不一致 {mismatched}")
            return False
        if report.indicators != [indicator.to_dict() for indicator in health.indicators]:
            logger.warning("设备预测报告校验未通过：监测指标与健康评估结果不一致")
            return False
        if report.matched_phenomenon != guidance.fault_type:
            logger.warning("设备预测报告校验未通过：匹配故障现象与知识图谱结果不一致")
            return False
        if report.resources != list(guidance.resources):
            logger.warning("设备预测报告校验未通过：所需资源与知识图谱结果不一致")
            return False
        if report.cautions != list(guidance.cautions):
            logger.warning("设备预测报告校验未通过：注意事项与知识图谱结果不一致")
            return False
        if not report.status_readout.strip():
            logger.warning("设备预测报告校验未通过：缺少状态解读")
            return False
        if not _numbers_are_grounded(report.status_readout, evidence):
            logger.warning("设备预测报告校验未通过：状态解读的数值不在健康评估结果中")
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - 校验门的契约是不抛异常，异常即判定未通过
        logger.warning(f"设备预测报告校验异常，判定为未通过：{exc}")
        return False


def produce_prediction_answer(
    evidence: PredictionEvidence,
    *,
    status_readout: str,
    status_readout_generated: bool,
    elapsed_seconds: float,
) -> dict[str, Any]:
    """装配 → 数据说明 → Markdown → 校验 → 组装工具返回载荷。"""

    data_notes = build_data_notes(evidence, status_readout_generated=status_readout_generated)
    report = assemble_report(
        evidence,
        status_readout=status_readout,
        data_notes=data_notes,
        elapsed_seconds=elapsed_seconds,
    )
    answer_markdown = build_report_markdown(report)
    answer_verified = verify_report(report, evidence)
    return report_payload(report, answer_markdown=answer_markdown, answer_verified=answer_verified)
