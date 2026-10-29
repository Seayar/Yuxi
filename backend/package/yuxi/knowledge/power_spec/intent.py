from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import json_repair
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from yuxi.knowledge.power_spec.contracts import PowerSpecIntent, PowerSpecQuery
from yuxi.utils import logger


# ---------------------------------------------------------------------------
# 规则解析（原 query.py）
# ---------------------------------------------------------------------------


_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("设备参数查询", ("参数", "容量", "额定", "型号", "电压", "电流", "阻抗", "温升", "限值")),
    ("检修标准查询", ("检修", "试验", "巡检", "周期", "工艺", "预防性")),
    ("安全规范查询", ("安全", "距离", "带电", "防护", "许可", "作业")),
)
_DEVICE_TYPES = ("变压器", "开关柜", "断路器", "隔离开关", "电缆", "配电柜", "互感器", "避雷器")
_VOLTAGE_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*[kKＫｋ][vVＶｖ]")
_BARE_VOLTAGE_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:千伏|kV|KV|千伏级)?\s*(?:的)?\s*(?=变压器|开关柜|断路器|电缆)"
)
_DEVICE_NAME_PATTERN = re.compile(
    r"(?P<name>\d+\s*号\s*[\u4e00-\u9fff]{1,8}?(?=的|[，。；;\s]|$)|"
    r"[^，。；;\s]{1,12}\s*#\s*[A-Za-z0-9一二三四五六七八九十]+)"
)


def parse_power_spec_query(query: str) -> PowerSpecQuery:
    """按电力规范关键词和实体规则解析用户查询。"""

    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("查询内容不能为空")

    intent = _resolve_intent(normalized_query)
    device_type = next((item for item in _DEVICE_TYPES if item in normalized_query), None)
    device_name_match = _DEVICE_NAME_PATTERN.search(normalized_query)
    voltage_match = _VOLTAGE_PATTERN.search(normalized_query)
    if voltage_match:
        voltage_level = f"{voltage_match.group('value')}kV"
    else:
        bare_voltage_match = _BARE_VOLTAGE_PATTERN.search(normalized_query)
        voltage_level = f"{bare_voltage_match.group('value')}kV" if bare_voltage_match else None
    clarification_required = intent == "其他类型" and not device_type and not voltage_level
    answer_dimensions = _resolve_answer_dimensions(normalized_query)
    normalized_terms = _resolve_normalized_terms(normalized_query, answer_dimensions, device_type, voltage_level)
    unknown_terms = [term for term in normalized_terms if term not in answer_dimensions]

    return PowerSpecQuery(
        query=normalized_query,
        intent=intent,
        device_name=device_name_match.group("name").replace(" ", "") if device_name_match else None,
        device_type=device_type,
        voltage_level=voltage_level,
        answer_dimensions=answer_dimensions,
        normalized_terms=normalized_terms,
        unknown_terms=unknown_terms,
        clarification_required=clarification_required,
        clarification_message=("未能识别完整的查询信息，请明确查询类型或设备信息" if clarification_required else None),
    )


def _resolve_intent(query: str) -> PowerSpecIntent:
    for intent, keywords in _INTENT_KEYWORDS:
        if any(keyword in query for keyword in keywords):
            return intent  # type: ignore[return-value]
    if any(keyword in query for keyword in ("色谱", "多久做一回", "多久做一次", "多长时间做", "超标", "判据")):
        return "检修标准查询"
    return "其他类型"


def _resolve_answer_dimensions(query: str) -> list[str]:
    dimensions = []
    for term in (
        "额定容量",
        "温升",
        "色谱",
        "周期",
        "分析周期",
        "溶解气体",
        "氢气",
        "总烃",
        "注意值",
        "超标",
        "判据",
        "绝缘电阻",
        "吸收比",
        "极化指数",
        "测量条件",
        "温度要求",
        "电阻表",
        "试验条件",
        "合格条件",
    ):
        if term in query and term not in dimensions:
            dimensions.append(term)
    if any(term in query for term in ("多久做一回", "多久做一次", "多长时间做")) and "周期" not in dimensions:
        dimensions.append("周期")
    order = (
        "额定容量",
        "温升",
        "色谱",
        "周期",
        "分析周期",
        "溶解气体",
        "氢气",
        "总烃",
        "注意值",
        "超标",
        "判据",
        "绝缘电阻",
        "吸收比",
        "极化指数",
        "测量条件",
        "温度要求",
        "电阻表",
        "试验条件",
        "合格条件",
    )
    return sorted(dimensions, key=order.index)


def _resolve_normalized_terms(
    query: str,
    dimensions: list[str],
    device_type: str | None,
    voltage_level: str | None,
) -> list[str]:
    """保留稳定维度和原始问题中的未知业务短语，供检索排序使用。"""

    terms = list(dimensions)
    if device_type:
        terms.append(device_type)
    if voltage_level:
        terms.append(voltage_level)
    stop_terms = {"那个", "这个", "多少", "怎么", "什么", "是否", "要求", "请问", "查询", "一下"}
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,8}", query):
        if phrase not in stop_terms and phrase not in terms:
            terms.append(phrase)
    return list(dict.fromkeys(terms))


# ---------------------------------------------------------------------------
# 模型意图识别（原 intent.py）
# ---------------------------------------------------------------------------

PowerSpecIntentModelCall = Callable[[str], Awaitable[str | dict[str, Any]]]

INTENT_VALUES: tuple[str, ...] = ("设备参数查询", "检修标准查询", "安全规范查询", "其他类型")
MAX_KEYWORDS = 12
MAX_DIMENSIONS = 8

INTENT_PROMPT = """你是电力规范检索的意图识别助手。请阅读用户问题，只输出一个 JSON 对象。

字段要求：
1. question：把用户问题改写成一个完整、独立、可检索的规范问题，补齐被省略的语境。
只能使用用户已经提到的设备、电压等级和数值，不要补充用户没有提供的信息。
2. intent：只能是 设备参数查询、检修标准查询、安全规范查询、其他类型 之一。
3. device_type：设备类别，例如 变压器、开关柜、断路器、电缆。无法确定时为 null。
4. device_name：具体设备标识，例如 1号主变。无法确定时为 null。
5. voltage_level：电压等级，统一写成 110kV 形式。无法确定时为 null。
6. answer_dimensions：用户真正想了解的维度，例如 温升、周期、绝缘电阻、注意值。不要罗列问题中没问到的维度。
7. keywords：用于知识库检索的关键词或短语，3 到 8 个，覆盖设备、电压等级和维度。
不要包含 请问、多少、怎么、是否 这类没有检索价值的词。

只输出 JSON，不要解释，不要 Markdown 代码块。"""


class PowerSpecIntentResult(BaseModel):
    """表示模型识别出的问题意图。"""

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1)
    intent: str = Field(default="其他类型")
    device_name: str | None = None
    device_type: str | None = None
    voltage_level: str | None = None
    answer_dimensions: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    @field_validator("answer_dimensions", "keywords", mode="before")
    @classmethod
    def _coerce_terms(cls, value: Any) -> Any:
        """容忍模型把维度或关键词写成字符串、分隔串或单个值。"""

        if value is None or isinstance(value, (list, tuple)):
            return value
        return [part.strip() for part in re.split(r"[、,，;；/]", str(value)) if part.strip()]


async def extract_power_spec_intent(
    query_text: str,
    *,
    model_call: PowerSpecIntentModelCall | None = None,
    previous: PowerSpecQuery | None = None,
    feedback: str | None = None,
) -> PowerSpecQuery:
    """优先由模型识别意图，模型不可用或输出非法时回退规则解析。"""

    if model_call is None:
        return parse_power_spec_query(query_text)
    try:
        raw = await model_call(build_intent_prompt(query_text, previous=previous, feedback=feedback))
        result = _parse_intent(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"电力规范意图识别失败，回退规则解析: {exc}")
        return parse_power_spec_query(query_text)
    return build_power_spec_query(result, fallback_query=query_text)


def build_intent_prompt(
    query_text: str,
    *,
    previous: PowerSpecQuery | None = None,
    feedback: str | None = None,
) -> str:
    """构造意图识别提示词，重识别时带上上一轮结果与用户修正。"""

    sections = [INTENT_PROMPT, f"\n用户问题：\n{query_text}"]
    if previous is not None:
        sections.append(
            "\n上一轮识别结果：\n"
            + json.dumps(
                {
                    "question": previous.query,
                    "intent": previous.intent,
                    "device_type": previous.device_type,
                    "voltage_level": previous.voltage_level,
                    "answer_dimensions": previous.answer_dimensions,
                },
                ensure_ascii=False,
            )
        )
    if feedback:
        sections.append(f"\n用户的修正说明：\n{feedback}\n请根据修正说明重新识别。")
    return "\n".join(sections)


def build_power_spec_query(result: PowerSpecIntentResult, *, fallback_query: str) -> PowerSpecQuery:
    """把模型识别结果转换为下游使用的查询模型。"""

    intent: PowerSpecIntent = result.intent if result.intent in INTENT_VALUES else "其他类型"  # type: ignore[assignment]
    dimensions = _clean_terms(result.answer_dimensions, limit=MAX_DIMENSIONS)
    keywords = _clean_terms(result.keywords, limit=MAX_KEYWORDS)
    device_type = _clean_optional(result.device_type)
    voltage_level = _clean_optional(result.voltage_level)
    terms = list(
        dict.fromkeys(
            [
                *dimensions,
                *([device_type] if device_type else []),
                *([voltage_level] if voltage_level else []),
                *keywords,
            ]
        )
    )
    clarification_required = intent == "其他类型" and not device_type and not voltage_level
    return PowerSpecQuery(
        query=result.question.strip() or fallback_query,
        intent=intent,
        device_name=_clean_optional(result.device_name),
        device_type=device_type,
        voltage_level=voltage_level,
        answer_dimensions=dimensions,
        normalized_terms=terms,
        unknown_terms=[term for term in terms if term not in dimensions],
        clarification_required=clarification_required,
        clarification_message=("未能识别完整的查询信息，请明确查询类型或设备信息" if clarification_required else None),
    )


def _parse_intent(raw: str | dict[str, Any]) -> PowerSpecIntentResult:
    """解析模型输出，容忍代码块围栏和 JSON 语法瑕疵。"""

    if isinstance(raw, dict):
        payload = raw
    else:
        text = str(raw).strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        payload = json_repair.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("模型未返回意图 JSON 对象")
    try:
        return PowerSpecIntentResult.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("意图 JSON 不符合契约") from exc


def _clean_optional(value: Any) -> str | None:
    """把模型给出的空值写法统一收敛为 None。"""

    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "无", "未知", "未提及", "不明确"}:
        return None
    return text


def _clean_terms(values: Any, *, limit: int) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    terms: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in terms:
            terms.append(text)
    return terms[:limit]


# ---------------------------------------------------------------------------
# 意图确认卡片与用户修正（原 tool.py）
# ---------------------------------------------------------------------------

MAX_INTENT_ATTEMPTS = 3
INTENT_QUESTION_ID = "power_spec_intent_confirm"
CORRECTION_QUESTION_ID = "power_spec_intent_correction"


def build_intent_plan(query: PowerSpecQuery) -> dict[str, Any]:
    """构造展示给用户的意图计划。"""

    return {
        "intent": query.intent,
        "device_type": query.device_type,
        "voltage_level": query.voltage_level,
        "answer_dimensions": query.answer_dimensions,
        "normalized_terms": query.normalized_terms,
        "unknown_terms": query.unknown_terms,
        "normalized_query": query.query,
    }


# 确认卡片把「用户确认的计划」整体编码进选项值，随用户选择持久化到 checkpoint。
# resume 重放时直接用它作为检索条件：用户确认的就是最终口径，不拿模型重算结果去校验它。
CONFIRM_VALUE_PREFIX = "confirm#"


def encode_confirmed_plan(intent_plan: dict[str, Any]) -> str:
    """把确认计划编码进选项值；只保留重建检索条件所需的字段。"""

    payload = json.dumps(intent_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_confirmed_plan(value: Any) -> dict[str, Any] | None:
    """从确认结果里还原用户确认过的计划；没有或不合法时返回 None。"""

    if isinstance(value, dict):
        for item in value.values():
            plan = decode_confirmed_plan(item)
            if plan is not None:
                return plan
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            plan = decode_confirmed_plan(item)
            if plan is not None:
                return plan
        return None
    text = str(value or "").strip()
    if not text.startswith(CONFIRM_VALUE_PREFIX):
        return None
    encoded = text[len(CONFIRM_VALUE_PREFIX) :]
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        plan = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return plan if isinstance(plan, dict) else None


def query_from_confirmed_plan(plan: dict[str, Any], *, fallback: PowerSpecQuery) -> PowerSpecQuery:
    """用用户确认的计划重建检索条件；字段不合法时退回调用方给出的查询。"""

    query_text = str(plan.get("normalized_query") or "").strip()
    if not query_text:
        return fallback
    try:
        return PowerSpecQuery(
            query=query_text,
            intent=str(plan.get("intent") or fallback.intent),
            device_name=plan.get("device_name") or fallback.device_name,
            device_type=plan.get("device_type"),
            voltage_level=plan.get("voltage_level"),
            answer_dimensions=[str(item) for item in (plan.get("answer_dimensions") or [])],
            normalized_terms=[str(item) for item in (plan.get("normalized_terms") or [])],
            unknown_terms=[str(item) for item in (plan.get("unknown_terms") or [])],
        )
    except (ValueError, TypeError):
        return fallback


def build_confirmation_payload(intent_plan: dict[str, Any]) -> dict[str, Any]:
    """构造意图确认卡片，展示模型对问题的重述与识别结果。"""

    return {
        "source": "power_spec_intent_confirmation",
        "questions": [
            {
                "question_id": INTENT_QUESTION_ID,
                "question": (
                    "我对您问题的理解是："
                    f"{intent_plan['normalized_query']}。"
                    f"查询类型={intent_plan['intent']}；"
                    f"设备={intent_plan['device_type'] or '未识别'}；"
                    f"电压等级={intent_plan['voltage_level'] or '未识别'}；"
                    f"问题维度={', '.join(intent_plan['answer_dimensions']) or '未识别'}。是否正确？"
                ),
                "options": [
                    {"label": "正确，继续检索", "value": f"{CONFIRM_VALUE_PREFIX}{encode_confirmed_plan(intent_plan)}"},
                    {"label": "不正确，我要补充/修改", "value": "correct"},
                ],
                "multi_select": False,
                "allow_other": True,
            }
        ],
        "intent_plan": intent_plan,
    }


def build_correction_payload(intent_plan: dict[str, Any]) -> dict[str, Any]:
    """用户只表示不正确时，追问需要修正的内容。"""

    return {
        "source": "power_spec_intent_correction",
        "questions": [
            {
                "question_id": CORRECTION_QUESTION_ID,
                "question": (
                    "请补充需要修正的内容，例如设备类型、电压等级或要查询的维度。"
                    f"当前理解：{intent_plan['normalized_query']}"
                ),
                "options": [],
                "multi_select": False,
                "allow_other": True,
            }
        ],
        "intent_plan": intent_plan,
    }


def parse_intent_confirmation(value: Any) -> tuple[str, str | None]:
    """解析确认结果，返回 (决定, 用户修正文本)。决定取值 confirm、reject 或 invalid。"""

    if isinstance(value, dict):
        if INTENT_QUESTION_ID in value or CORRECTION_QUESTION_ID in value:
            for key in (INTENT_QUESTION_ID, CORRECTION_QUESTION_ID):
                if key in value:
                    return parse_intent_confirmation(value[key])
        text = str(value.get("text") or "").strip()
        if text:
            return "reject", text
        selected = value.get("selected")
        if isinstance(selected, (list, tuple)) and selected:
            return parse_intent_confirmation(selected[0])
        return "invalid", None
    if isinstance(value, (list, tuple)):
        for item in value:
            decision, correction = parse_intent_confirmation(item)
            if decision != "invalid":
                return decision, correction
        return "invalid", None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(CONFIRM_VALUE_PREFIX):
            return "confirm", None
        if text.lower() in {"confirm", "confirmed"} or text in {"正确，继续检索", "正确"}:
            return "confirm", None
        if text.lower() in {"correct", "reject"} or text in {"不正确，我要补充/修改", "不正确"}:
            return "reject", None
        return ("reject", text) if text else ("invalid", None)
    return "invalid", None


def extract_correction_text(value: Any) -> str | None:
    """从修正追问的返回结果中取出用户文本。"""

    if isinstance(value, dict):
        if CORRECTION_QUESTION_ID in value:
            return extract_correction_text(value[CORRECTION_QUESTION_ID])
        text = str(value.get("text") or "").strip()
        if text:
            return text
        selected = value.get("selected")
        if isinstance(selected, (list, tuple)) and selected:
            return extract_correction_text(selected[0])
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            text = extract_correction_text(item)
            if text:
                return text
        return None
    if isinstance(value, str):
        text = value.strip()
        return text if text and text not in {"__other__", "other", "其他"} else None
    return None
