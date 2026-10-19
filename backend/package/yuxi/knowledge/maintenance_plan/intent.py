"""处置方案意图解析：把用户的运维任务原文解析为 ``contracts.PlanQuery``。

优先用大模型抽取（只允许输出 JSON），模型不可用、超时或输出非法时回退 ``rule_extract``；
上一轮结果（``previous``）与用户补充说明（``feedback``）会与本次结果合并，
最后按需求说明“方案二/方案三”的输入约束标注是否需要反问澄清。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from yuxi.knowledge.maintenance_plan.contracts import (
    CLARIFY_ALL_MESSAGE,
    DEVICE_TYPES,
    SCHEME1_TO_DEVICE_TYPE,
    TASK_PATROL,
    TASK_REPAIR,
    TASK_REPLACE,
    TASK_TEST,
    TASK_TYPES,
    PlanQuery,
)

logger = logging.getLogger(__name__)

PlanIntentModelCall = Callable[[str], Awaitable[str]]

# kg 图中的设备类型名称清单由 contracts 统一拥有（归一结果必须落在其中）
KG_DEVICE_TYPES = DEVICE_TYPES

# ---------------------------------------------------------------------------
# 规则抽取用的词表与正则
# ---------------------------------------------------------------------------

# 任务类型关键词，按“越具体越先匹配”排序（同一条描述里出现多个动词时取先命中者）
_TASK_TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("更换", TASK_REPLACE),
    ("替换", TASK_REPLACE),
    ("换新", TASK_REPLACE),
    ("试验", TASK_TEST),
    ("测试", TASK_TEST),
    ("耐压", TASK_TEST),
    ("巡视", TASK_PATROL),
    ("巡检", TASK_PATROL),
    ("特巡", TASK_PATROL),
    ("检修", TASK_REPAIR),
    ("维修", TASK_REPAIR),
    ("消缺", TASK_REPAIR),
    ("大修", TASK_REPAIR),
    ("小修", TASK_REPAIR),
    ("处理", TASK_REPAIR),
)

_VOLTAGE_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?:[kKＫｋ][vVＶｖ]|千伏)")

# 位置线索，按“越具体越先匹配”排序
_LOCATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d+(?:\.\d+)?\s*[kKＫｋ][vVＶｖ]\s*#\s*\d+\s*变电站"),
    re.compile(r"#\s*\d+\s*变电站"),
    re.compile(r"#\s*\d+\s*开闭所"),
    re.compile(r"#\s*\d+\s*箱变"),
    re.compile(r"箱变群\s*\d+\s*-\s*\d+"),
    re.compile(r"\d+(?:\.\d+)?\s*[kKＫｋ][vVＶｖ]\s*开闭所"),
    re.compile(r"\d+[A-Za-z]+\s*-\s*\d+[A-Za-z]+(?:\s*-\s*\d+)?"),
)

# 设备名称：保留用户原话里的指称（台账名称如 3k18、#62箱变13馈出柜、991SVG开关）
_DEVICE_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"#\s*\d+\s*箱变\s*\d*\s*(?:变压器出线柜|馈出柜|环出柜|环进柜|出线柜|进线柜|柜)?"),
    re.compile(r"箱变\s*\d+\s*(?:变压器出线柜|馈出柜|环出柜|环进柜|出线柜|进线柜|柜)"),
    re.compile(r"\d+\s*SVG\s*开关", re.IGNORECASE),
    re.compile(
        r"#\s*\d+\s*(?:主变压器|站用变压器|接地变压器|所用变压器|变压器|站用变|接地变|所用变|主变"
        r"|断路器|隔离开关|开关柜|母线|电缆|避雷器|绝缘子)"
    ),
    re.compile(
        r"\d+\s*号\s*(?:主变压器|站用变压器|接地变压器|所用变压器|变压器|主变|开关柜|断路器"
        r"|隔离开关|母线|电缆|避雷器|绝缘子|柜)"
    ),
    re.compile(r"[A-Za-z]{1,4}\s*\d+\s*杆塔"),
    re.compile(r"\d+\s*#\s*杆塔"),
    re.compile(r"(?<![0-9])\d+\s*[kKＫｋ]\s*\d+"),
)

# 设备类型关键词 → kg 设备类型名称；顺序即优先级（“开关柜”优先于“断路器”，“杆塔”优先于“绝缘子”）
_DEVICE_TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("开关柜", "开关柜"),
    ("断路器柜", "开关柜"),
    ("馈出柜", "开关柜"),
    ("环出柜", "开关柜"),
    ("环进柜", "开关柜"),
    ("出线柜", "开关柜"),
    ("进线柜", "开关柜"),
    ("PT柜", "开关柜"),
    ("固定柜", "开关柜"),
    ("低压柜", "开关柜"),
    ("高压柜", "开关柜"),
    ("辅柜", "开关柜"),
    ("箱式变电站", "箱式变电站"),
    ("箱变", "箱式变电站"),
    ("杆塔", "杆塔"),
    ("铁塔", "杆塔"),
    ("主变压器", "变压器"),
    ("主变", "变压器"),
    ("站用变", "变压器"),
    ("接地变", "变压器"),
    ("所用变", "变压器"),
    ("变压器", "变压器"),
    ("隔离开关", "隔离开关"),
    ("刀闸", "隔离开关"),
    ("断路器", "断路器"),
    ("开关", "断路器"),
    ("电压互感器", "电压互感器"),
    ("电流互感器", "电流互感器"),
    ("避雷器", "避雷器"),
    ("母线", "母线"),
    ("电缆", "电缆"),
    ("绝缘子", "绝缘子"),
    ("集电线路", "架空线路"),
    ("配电线路", "架空线路"),
    ("输电线路", "架空线路"),
    ("线路", "架空线路"),
    ("开闭所", "开闭所"),
    ("柜", "开关柜"),
    ("变电站", "电力设备"),
)

# 作业对象/部件：更换、检修的对象通常比设备类型更细，用于下游挑选作业模板
_WORK_OBJECT_TERMS: tuple[str, ...] = (
    "有载分接开关",
    "分接开关",
    "冷却系统",
    "冷却器",
    "冷却风扇",
    "手车机构",
    "操作机构",
    "机构箱",
    "手车",
    "二次线",
    "二次回路",
    "储油柜",
    "套管",
    "触头",
    "油枕",
    "隔离开关",
    "断路器",
    "避雷器",
    "绝缘子",
    "熔断器",
    "互感器",
    "母线",
    "电缆",
    "引线",
    "金具",
    "风扇",
    "油泵",
    "气泵",
)

# 模型把“没有”写成这些词时统一收敛为 None
_EMPTY_VALUES = frozenset({"null", "none", "无", "未知", "未提及", "不明确", "未说明", "n/a", "na", "-"})

# PT/CT 是 ASCII 缩写，要求两侧不是字母，避免误命中英文单词
_PT_PATTERN = re.compile(r"(?<![A-Za-z])PT(?![A-Za-z])")
_CT_PATTERN = re.compile(r"(?<![A-Za-z])CT(?![A-Za-z])")


# ---------------------------------------------------------------------------
# 规则抽取
# ---------------------------------------------------------------------------
def rule_extract(text: str) -> PlanQuery:
    """纯规则抽取：任务类型关键词、电压等级、设备类型关键词、位置线索、设备名称片段。"""

    normalized = (text or "").strip()
    return PlanQuery(
        raw_text=normalized,
        task_type=_match_task_type(normalized),
        device_name=_match_device_name(normalized),
        device_type=_match_device_type(normalized),
        voltage_level=_match_voltage(normalized),
        work_object=_match_work_object(normalized),
        location_hint=_match_location(normalized),
    )


def _match_task_type(text: str) -> str | None:
    """按关键词判定任务类型；只有“修”字时归为检修，完全没有则为 None。"""

    if not text:
        return None
    for keyword, task_type in _TASK_TYPE_KEYWORDS:
        if keyword in text:
            return task_type
    return TASK_REPAIR if "修" in text else None


def _match_voltage(text: str) -> str | None:
    """识别 0.4kV/10kV/35kV 等电压等级，兼容“10千伏”“10 kV”“10KV”。"""

    match = _VOLTAGE_PATTERN.search(text)
    return _format_voltage(match.group("value")) if match else None


def _format_voltage(value: str) -> str:
    """统一电压等级写法：去掉多余的 .0，补上 kV。"""

    return f"{value[:-2]}kV" if value.endswith(".0") else f"{value}kV"


def _match_location(text: str) -> str | None:
    """抽取站/开闭所/箱变群/集电线路等位置线索。"""

    for pattern in _LOCATION_PATTERNS:
        match = pattern.search(text)
        if match:
            return _squeeze(match.group(0))
    return None


def _match_device_name(text: str) -> str | None:
    """抽取用户原话中的设备指称（如 3k18、991SVG开关、#62箱变13馈出柜）。"""

    for pattern in _DEVICE_NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            return _squeeze(match.group(0))
    return None


def _match_device_type(text: str) -> str | None:
    """按关键词把口语设备类型归一到 kg 设备类型名称。"""

    if not text:
        return None
    for keyword, device_type in _DEVICE_TYPE_KEYWORDS:
        if keyword in text:
            return device_type
    if _PT_PATTERN.search(text):
        return "电压互感器"
    if _CT_PATTERN.search(text):
        return "电流互感器"
    return None


def _match_work_object(text: str) -> str | None:
    """抽取作业对象/部件；取词表中最先命中的一项（词表按长词优先排列）。"""

    if not text:
        return None
    for term in _WORK_OBJECT_TERMS:
        if term in text:
            return term
    return None


def _squeeze(text: str) -> str:
    """去掉抽取结果里的空白，使“991 SVG开关”“#3 开闭所”收敛为台账写法。"""

    return re.sub(r"\s+", "", text)


def normalize_device_type(raw: str | None) -> str | None:
    """把口语设备类型归一到 kg 的设备类型名称。

    例：主变/站用变/接地变→变压器；箱变→箱式变电站；开关/断路器→断路器（但“开关柜”→开关柜）；
    刀闸→隔离开关；PT→电压互感器；CT→电流互感器；集电线路/配电线路/输电线路/线路→架空线路。
    无法归一时返回 None。
    """

    text = _clean_optional(raw)
    if text is None:
        return None
    if text in KG_DEVICE_TYPES:
        return text
    if text in SCHEME1_TO_DEVICE_TYPE:
        return SCHEME1_TO_DEVICE_TYPE[text]
    return _match_device_type(text)


# ---------------------------------------------------------------------------
# 输入约束（方案二/方案三）
# ---------------------------------------------------------------------------
def missing_fields(query: PlanQuery) -> list[str]:
    """返回缺失项，取值只能是 "task_type"、"device"。

    ``device`` 指既没有设备名称、也没有“位置线索 + 设备类型”的组合。
    """

    missing: list[str] = []
    if query.task_type not in TASK_TYPES:
        missing.append("task_type")
    if not query.device_name and not (query.location_hint and query.device_type):
        missing.append("device")
    return missing


def apply_input_constraints(query: PlanQuery) -> PlanQuery:
    """按输入约束标注澄清：

    - 任务类型与目标设备都缺 → ``clarification_required=True`` 并给固定文案（方案三）；
    - 只缺一项时不设置澄清（由工具弹出候选或任务类型选项，即“方案二”），但要保证字段已归一化。
    """

    query.task_type = query.task_type if query.task_type in TASK_TYPES else None
    query.device_type = normalize_device_type(query.device_type)
    query.voltage_level = _normalize_voltage(query.voltage_level)
    query.device_name = _clean_optional(query.device_name)
    if query.device_name in DEVICE_TYPES:
        # 模型把设备类型（如“开关柜”）误填为设备名称：类型不是具体设备，转入 device_type
        query.device_type = query.device_type or query.device_name
        query.device_name = None
    query.location_hint = _clean_optional(query.location_hint)
    query.work_object = _clean_optional(query.work_object)
    query.raw_text = (query.raw_text or "").strip()

    if missing_fields(query) == ["task_type", "device"]:
        query.clarification_required = True
        query.clarification_message = CLARIFY_ALL_MESSAGE
    else:
        query.clarification_required = False
        query.clarification_message = ""
    return query


# ---------------------------------------------------------------------------
# 大模型抽取 + 规则兜底
# ---------------------------------------------------------------------------
async def extract_plan_intent(
    text: str,
    *,
    model_call: PlanIntentModelCall | None,
    previous: PlanQuery | None = None,
    feedback: str | None = None,
) -> PlanQuery:
    """优先用大模型抽取（返回严格 JSON），失败/超时/非法 JSON 时回退 ``rule_extract``；

    ``previous`` + ``feedback`` 用于用户补充信息后的再次解析（把补充内容与上次结果合并）。
    """

    rule_query = rule_extract(text)
    query = rule_query
    if model_call is not None:
        try:
            raw = await model_call(build_intent_prompt(text, previous=previous, feedback=feedback))
            query = _merge_model_result(rule_query, _parse_intent_json(raw))
        except Exception as exc:  # noqa: BLE001 —— 模型侧任何异常都必须降级到规则解析
            logger.warning(f"处置方案意图识别失败，回退规则解析: {exc}")
            query = rule_query

    if previous is not None:
        query = _merge_previous(query, previous)
        query.raw_text = _combine_text(previous.raw_text, text)
    else:
        query.raw_text = (text or "").strip()
    return apply_input_constraints(query)


def build_intent_prompt(
    text: str,
    *,
    previous: PlanQuery | None = None,
    feedback: str | None = None,
) -> str:
    """构造意图识别提示词；再次解析时带上上一轮结果与用户补充说明。"""

    sections = [INTENT_PROMPT, f"\n用户任务描述：\n{text}"]
    if previous is not None:
        sections.append(
            "\n上一轮解析结果：\n"
            + json.dumps(
                {
                    "task_type": previous.task_type,
                    "device_name": previous.device_name,
                    "device_type": previous.device_type,
                    "voltage_level": previous.voltage_level,
                    "work_object": previous.work_object,
                    "location_hint": previous.location_hint,
                },
                ensure_ascii=False,
            )
        )
    if feedback:
        sections.append(f"\n用户的补充说明：\n{feedback}\n请结合补充说明重新解析。")
    return "".join(sections)


def _merge_model_result(rule: PlanQuery, payload: dict[str, Any]) -> PlanQuery:
    """合并模型与规则：保留原文明确编号，避免模型把部件或位置并入设备名。"""

    model_task = _clean_optional(payload.get("task_type"))
    task_type = rule.task_type if model_task is None else _normalize_task_type(model_task)
    if task_type not in TASK_TYPES:
        task_type = None

    device_name = _clean_optional(payload.get("device_name")) or rule.device_name
    if device_name and rule.device_name and _squeeze(rule.device_name).casefold() in _squeeze(device_name).casefold():
        # 模型可能抽成“3k18断路器”；原文已识别到编号时，部件仍由 work_object 承载。
        device_name = rule.device_name

    return PlanQuery(
        raw_text=rule.raw_text,
        task_type=task_type,
        device_name=device_name,
        device_type=normalize_device_type(_clean_optional(payload.get("device_type"))) or rule.device_type,
        voltage_level=_normalize_voltage(_clean_optional(payload.get("voltage_level"))) or rule.voltage_level,
        work_object=_clean_optional(payload.get("work_object")) or rule.work_object,
        location_hint=_clean_optional(payload.get("location_hint")) or rule.location_hint,
    )


def _merge_previous(query: PlanQuery, previous: PlanQuery) -> PlanQuery:
    """用上一轮结果补齐本轮缺失的字段；本轮已有的值优先（用户补充会覆盖旧值）。"""

    return PlanQuery(
        raw_text=query.raw_text,
        task_type=query.task_type or previous.task_type,
        device_name=query.device_name or previous.device_name,
        device_type=query.device_type or previous.device_type,
        voltage_level=query.voltage_level or previous.voltage_level,
        work_object=query.work_object or previous.work_object,
        location_hint=query.location_hint or previous.location_hint,
    )


def _combine_text(previous_text: str, text: str) -> str:
    """把上一轮原文与本次补充拼成一条完整任务原文，供追溯使用。"""

    head = (previous_text or "").strip()
    tail = (text or "").strip()
    if not head:
        return tail
    if not tail or tail in head:
        return head
    return f"{head}；{tail}"


def _normalize_task_type(raw: str) -> str | None:
    """把模型给出的任务类型收敛到四类之一；同义词（替换）允许，其它一律 None。"""

    text = _clean_optional(raw)
    if text is None:
        return None
    return text if text in TASK_TYPES else _match_task_type(text)


def _normalize_voltage(raw: str | None) -> str | None:
    """把模型给出的电压等级写法（10KV、10 kV）收敛为 10kV。"""

    text = _clean_optional(raw)
    if text is None:
        return None
    return _match_voltage(text)


def _clean_optional(value: Any) -> str | None:
    """把模型给出的空值写法统一收敛为 None。"""

    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _EMPTY_VALUES:
        return None
    return text


# ---------------------------------------------------------------------------
# 模型输出的 JSON 解析
# ---------------------------------------------------------------------------
def _parse_intent_json(raw: str | dict[str, Any]) -> dict[str, Any]:
    """解析模型输出，容忍代码块围栏和轻微 JSON 语法瑕疵；非对象时抛 ValueError。"""

    payload = raw if isinstance(raw, dict) else _loads_json_object(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("模型未返回意图 JSON 对象")
    return payload


def _loads_json_object(text: str) -> Any:
    """从模型输出里截出第一个 JSON 对象并解析；解析不了就抛 ValueError。"""

    cleaned = _strip_code_fence(text)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("模型输出中没有 JSON 对象")
    candidate = cleaned[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        try:
            return json.loads(_repair_json_text(candidate))
        except json.JSONDecodeError as repair_exc:
            raise ValueError("模型输出的 JSON 无法解析") from repair_exc


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏。"""

    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = stripped[3:]
    if stripped[:4].lower() == "json":
        stripped = stripped[4:]
    end = stripped.rfind("```")
    if end != -1:
        stripped = stripped[:end]
    return stripped.strip()


def _repair_json_text(text: str) -> str:
    """修掉最常见的 JSON 瑕疵：尾随逗号、Python 字面量。"""

    repaired = re.sub(r",\s*([}\]])", r"\1", text)
    for python_literal, json_literal in (("True", "true"), ("False", "false"), ("None", "null")):
        repaired = re.sub(rf"\b{python_literal}\b", json_literal, repaired)
    return repaired


INTENT_PROMPT = """你是配电网运维任务处置方案生成系统的意图识别器。
请从用户的运维任务描述中提取以下 JSON 字段（整段必须且只是一个 JSON 对象，以 { 开头）：
{
  "task_type": "任务类型，只能是 更换/检修/试验/巡视 之一；没有则 null",
  "device_name": "用户原话中的具体设备指称，如 3k18、991SVG开关、1号主变；只说设备类型时填 null",
  "device_type": "设备类型，如 开关柜、断路器、变压器、箱式变电站、杆塔、架空线路；没有则 null",
  "voltage_level": "电压等级，如 10kV、35kV；没有则 null",
  "work_object": "具体作业对象/部件，如 断路器、有载分接开关、冷却系统、手车机构；没有则 null",
  "location_hint": "位置线索，如 #3开闭所、35kV#1变电站、#62箱变、1B-1K-1；没有则 null"
}
要求：
- 设备名称必须是用户原话里的具体设备指称，不要改写成台账名称，也不要把设备类型当作设备名称。
- 无法确定的字段一律填 null，不要编造。
- 只输出 JSON，不要输出其他解释。
"""
