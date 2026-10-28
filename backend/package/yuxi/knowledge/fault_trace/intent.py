"""故障溯源意图识别与输入约束（方案二/方案三 + 确认卡片）。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

import json_repair
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from yuxi.knowledge.fault_trace.contracts import (
    CLARIFY_INFO_MISSING_MESSAGE,
    CLARIFY_PHENOMENON_MISSING_MESSAGE,
    DEVICE_NOT_FOUND_MESSAGE,
    FaultTraceQuery,
)
from yuxi.utils import logger

# ---------------------------------------------------------------------------
# 规则解析
# ---------------------------------------------------------------------------

_DEVICE_TYPES = ("变压器", "开关柜", "断路器", "隔离开关", "电缆", "配电柜", "互感器", "避雷器", "母线", "线路")
_VOLTAGE_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*[kKＫｋ][vVＶｖ]")
# SRS：设备名称应指向具体设备；只出现类型词（如“变压器”）不算具体设备。
# 后缀按长度降序排列，保证“#1主变压器”整体命中而不是只截到“#1主变”。
_DEVICE_SUFFIXES = tuple(
    sorted(
        (
            "主变",
            "主变压器",
            "变压器",
            "开关柜",
            "开闭所",
            "断路器",
            "隔离开关",
            "互感器",
            "避雷器",
            "电抗器",
            "母线",
            "电缆",
            "线路",
            "进线",
            "出线",
            "冷却器",
            "接头",
            "泵",
            "风扇",
        ),
        key=len,
        reverse=True,
    )
)
_DEVICE_NAME_PATTERN = re.compile(
    r"(?P<name>"
    r"\d+\s*号\s*(?:" + "|".join(_DEVICE_SUFFIXES) + r")"
    r"|"
    r"\d*\s*[kK][vV]\s*(?:" + "|".join(_DEVICE_SUFFIXES) + r")(?:\s*#\s*[A-Za-z0-9一二三四五六七八九十]+)?"
    r"|"
    r"(?:电缆|线路|母线)[^，。；;\s]{0,8}(?:\s*#\s*[A-Za-z0-9一二三四五六七八九十]+)?"
    r"|"
    # 带编号的设备名：#1主变压器、2号主变、3B-7Y 等
    r"#?\s*\d{1,3}\s*(?:" + "|".join(_DEVICE_SUFFIXES) + r")"
    r"|"
    # 设备编号：3B-7Y、1B-1K-1 等字母数字组合（含 -/# 分段）
    r"\d{1,2}[A-Za-z](?:[-#][A-Za-z0-9]{1,8})+"
    r")"
)
_PHENOMENON_TERMS = (
    "停转",
    "异响",
    "烧蚀",
    "焦糊",
    "渗漏",
    "油渍",
    "油迹",
    "过热",
    "温升",
    "电流",
    "电压",
    "发热",
    "放电",
    "冒烟",
    "着火",
    "断股",
    "散股",
    "破损",
    "裂纹",
    "异物",
    "倒伏",
    "压覆",
    "搭挂",
    "缠绕",
    "跳闸",
    "温度报警",
)


def parse_fault_trace_query(query: str) -> FaultTraceQuery:
    """按电力设备词与现象关键词规则解析故障描述。"""

    normalized_query = query.strip()
    if not normalized_query:
        raise ValueError("故障描述不能为空")

    device_name_match = _DEVICE_NAME_PATTERN.search(normalized_query)
    device_name = device_name_match.group("name").replace(" ", "") if device_name_match else None
    device_type = next((item for item in _DEVICE_TYPES if item in normalized_query), None)
    voltage_match = _VOLTAGE_PATTERN.search(normalized_query)
    voltage_level = f"{voltage_match.group('value')}kV" if voltage_match else None
    phenomena = [term for term in _PHENOMENON_TERMS if term in normalized_query]

    return FaultTraceQuery(
        device_name=device_name,
        device_type=device_type,
        voltage_level=voltage_level,
        phenomena=phenomena,
    )


# ---------------------------------------------------------------------------
# 模型识别
# ---------------------------------------------------------------------------


class FaultTraceIntentResult(BaseModel):
    """表示模型从（文字/语音 + 图片识别结果）中识别出的故障溯源要素。"""

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1)
    device_name: str | None = None
    device_type: str | None = None
    voltage_level: str | None = None
    phenomena: list[str] = Field(default_factory=list)

    @field_validator("phenomena", mode="before")
    @classmethod
    def _coerce_phenomena(cls, value: Any) -> Any:
        """容忍模型把现象写成字符串、分隔串或单个值。"""

        if value is None or isinstance(value, (list, tuple)):
            return value
        return [part.strip() for part in re.split(r"[、,，;；/]", str(value)) if part.strip()]


FaultTraceIntentModelCall = Callable[[str], Awaitable[str]]


async def extract_fault_trace_intent(
    query_text: str,
    *,
    image_result: dict[str, Any] | None,
    model_call: FaultTraceIntentModelCall | None = None,
    previous: FaultTraceQuery | None = None,
    feedback: str | None = None,
) -> FaultTraceQuery:
    """优先由模型识别意图，规则能确定设备与现象时直接使用规则结果。

    中断确认会在恢复时重放工具调用，规则解析保证同一条输入得到同一份确认计划；
    只有规则无法确定具体设备/现象时才调用模型，避免重放后确认卡内容漂移。

    ``image_result`` 是调用方显式传入的 YOLO 识别结果，与用户文字/语音一起进入
    语义融合（SRS：统一语义表示层）；未传入时由工具读取会话中的上传图片。
    """

    rule_query = parse_fault_trace_query(query_text)
    rule_is_specific = rule_query.has_specific_device() and rule_query.has_phenomenon()
    if model_call is None or rule_is_specific:
        query = rule_query
    else:
        try:
            raw = await model_call(
                build_intent_prompt(query_text, image_result=image_result, previous=previous, feedback=feedback)
            )
            result = _parse_intent(raw)
            query = build_fault_query(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"故障溯源意图识别失败，回退规则解析: {exc}")
            query = parse_fault_trace_query(query_text)

    query.image_fault_type = _clean_optional(image_result.get("fault_type")) if image_result else None
    query.image_fault_name = _clean_optional(image_result.get("fault_name")) if image_result else None
    _merge_rule_fallback(query, query_text)
    # 现象可以由图片识别补齐，第一步只要求设备具体；现象校验留到图片步骤之后。
    return apply_input_constraints(query, require_phenomenon=False)


def _merge_rule_fallback(query: FaultTraceQuery, query_text: str) -> None:
    """模型漏识别设备/现象时用规则结果补齐，避免确认卡片与恢复重放结果漂移。"""

    if query.has_specific_device() and query.phenomena:
        return
    fallback = parse_fault_trace_query(query_text)
    if not (query.device_name and query.device_name.strip()):
        query.device_name = fallback.device_name or query.device_name
    query.device_type = query.device_type or fallback.device_type
    query.voltage_level = query.voltage_level or fallback.voltage_level
    if not query.phenomena:
        query.phenomena = fallback.phenomena


def build_intent_prompt(
    query_text: str,
    *,
    image_result: dict[str, Any] | None,
    previous: FaultTraceQuery | None = None,
    feedback: str | None = None,
) -> str:
    """构造意图识别提示词；重识别时带上上一轮结果与用户修正。"""

    image_section = ""
    if image_result and image_result.get("fault_type"):
        image_section = (
            f"\n图片识别结果（YOLO）：\n"
            f"fault_type={image_result.get('fault_type')}，fault_name={image_result.get('fault_name')}。\n"
        )
    sections = [
        INTENT_PROMPT,
        f"\n用户问题：\n{query_text}",
        image_section,
    ]
    if previous is not None:
        sections.append(
            "\n上一轮识别结果：\n"
            + json.dumps(
                {
                    "device_name": previous.device_name,
                    "device_type": previous.device_type,
                    "voltage_level": previous.voltage_level,
                    "phenomena": previous.phenomena,
                },
                ensure_ascii=False,
            )
        )
    if feedback:
        sections.append(f"\n用户的修正说明：\n{feedback}\n请根据修正说明重新识别。")
    return "".join(sections)


def build_fault_query(result: FaultTraceIntentResult) -> FaultTraceQuery:
    """把模型识别结果转换为下游使用的查询模型。"""

    return FaultTraceQuery(
        device_name=_clean_optional(result.device_name),
        device_type=_clean_optional(result.device_type),
        voltage_level=_clean_optional(result.voltage_level),
        phenomena=[str(item).strip() for item in result.phenomena if str(item).strip()],
    )


def _parse_intent(raw: str | dict[str, Any]) -> FaultTraceIntentResult:
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
        return FaultTraceIntentResult.model_validate(payload)
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


# ---------------------------------------------------------------------------
# SRS 输入约束与澄清（方案二/方案三）
# ---------------------------------------------------------------------------


def apply_input_constraints(query: FaultTraceQuery, *, require_phenomenon: bool = True) -> FaultTraceQuery:
    """按 SRS 输入约束设置澄清：

    - 设备名称应指向具体设备，仅输入设备类型视为不明确；
    - 图像本身不可单独诊断，须配合文字或语音描述；
    - 信息不完整时走方案三（反问澄清）话术。

    ``require_phenomenon=False`` 用于图片步骤之前：此时现象还可以由设备图片识别补齐。
    """

    if not query.has_specific_device():
        if query.has_phenomenon():
            query.clarification_required = True
            query.clarification_message = DEVICE_NOT_FOUND_MESSAGE
        else:
            query.clarification_required = True
            query.clarification_message = CLARIFY_INFO_MISSING_MESSAGE
        return query
    if require_phenomenon and not query.has_phenomenon():
        query.clarification_required = True
        query.clarification_message = CLARIFY_PHENOMENON_MISSING_MESSAGE
        return query
    return query


def build_candidate_selection_payload(candidates: list[str]) -> dict[str, Any]:
    """方案二：识别到设备类型但存在多个候选设备时，引导用户选择确认。"""

    options = [{"label": f"{index + 1}）{name}", "value": name} for index, name in enumerate(candidates)]
    question = "请选择具体设备：" + "；".join(f"{index + 1}）{name}" for index, name in enumerate(candidates))
    return {
        "source": "fault_trace_candidate_selection",
        "questions": [
            {
                "question_id": FAULT_QUESTION_ID_CANDIDATE,
                "question": question,
                "options": options,
                "multi_select": False,
                "allow_other": True,
            }
        ],
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# 意图确认卡片与用户修正
# ---------------------------------------------------------------------------

MAX_INTENT_ATTEMPTS = 3
INTENT_QUESTION_ID = "fault_trace_intent_confirm"
CORRECTION_QUESTION_ID = "fault_trace_intent_correction"
FAULT_QUESTION_ID_CANDIDATE = "fault_trace_device_select"


def build_intent_plan(query: FaultTraceQuery) -> dict[str, Any]:
    """构造展示给用户的意图计划。"""

    return {
        "device_name": query.device_name,
        "device_type": query.device_type,
        "voltage_level": query.voltage_level,
        "phenomena": list(query.phenomena),
        "image_fault_type": query.image_fault_type,
        "image_fault_name": query.image_fault_name,
    }


# 确认卡片把计划指纹放进选项值，随用户选择持久化到 checkpoint；resume 重放时按同一指纹
# 校验重算的计划，避免执行用户没有确认过的另一份计划。
CONFIRM_VALUE_PREFIX = "confirm#"


def intent_plan_signature(intent_plan: dict[str, Any]) -> tuple[str, ...]:
    """确认计划的可比对字段：只覆盖确认卡片上用户实际看到并确认的设备与现象。

    现象按集合比较，不因模型输出顺序或重复而判为不一致；图片识别结果属于用户确认过的
    内容，一并纳入。
    """

    phenomena = intent_plan.get("phenomena") or ()
    return (
        f"device_name={str(intent_plan.get('device_name') or '')!r}",
        f"device_type={str(intent_plan.get('device_type') or '')!r}",
        f"voltage_level={str(intent_plan.get('voltage_level') or '')!r}",
        f"image_fault_name={str(intent_plan.get('image_fault_name') or '')!r}",
        f"phenomena={tuple(sorted({str(item) for item in phenomena}))!r}",
    )


def plan_token(intent_plan: dict[str, Any]) -> str:
    """计划指纹的短哈希；作为确认选项值的一部分往返。"""

    payload = json.dumps(intent_plan_signature(intent_plan), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def parse_confirmation_token(value: Any) -> str | None:
    """从确认结果里取出随卡片往返的计划指纹；没有指纹时返回 None。"""

    if isinstance(value, dict):
        for item in value.values():
            token = parse_confirmation_token(item)
            if token is not None:
                return token
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            token = parse_confirmation_token(item)
            if token is not None:
                return token
        return None
    text = str(value or "").strip()
    if text.startswith(CONFIRM_VALUE_PREFIX):
        return text[len(CONFIRM_VALUE_PREFIX) :] or None
    return None


def build_confirmation_payload(intent_plan: dict[str, Any]) -> dict[str, Any]:
    """构造意图确认卡片，展示模型对设备与故障现象的理解。"""

    parts = []
    if intent_plan.get("device_name"):
        parts.append(f"设备={intent_plan['device_name']}")
    if intent_plan.get("device_type"):
        parts.append(f"设备类型={intent_plan['device_type']}")
    if intent_plan.get("voltage_level"):
        parts.append(f"电压等级={intent_plan['voltage_level']}")
    if intent_plan.get("phenomena"):
        parts.append(f"故障现象={', '.join(intent_plan['phenomena'])}")
    if intent_plan.get("image_fault_name"):
        parts.append(f"图片识别={intent_plan['image_fault_name']}")
    question = "我对故障现象的理解是：" + "；".join(parts) + "。是否正确？"
    return {
        "source": "fault_trace_intent_confirmation",
        "questions": [
            {
                "question_id": INTENT_QUESTION_ID,
                "question": question,
                "options": [
                    {"label": "正确，继续溯源", "value": f"{CONFIRM_VALUE_PREFIX}{plan_token(intent_plan)}"},
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
        "source": "fault_trace_intent_correction",
        "questions": [
            {
                "question_id": CORRECTION_QUESTION_ID,
                "question": "请补充需要修正的内容，例如设备名称、设备类型或具体故障现象。",
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
        if INTENT_QUESTION_ID in value or CORRECTION_QUESTION_ID in value or FAULT_QUESTION_ID_CANDIDATE in value:
            for key in (INTENT_QUESTION_ID, CORRECTION_QUESTION_ID, FAULT_QUESTION_ID_CANDIDATE):
                if key in value:
                    return parse_intent_confirmation(value[key])
        text = str(value.get("text") or "").strip()
        if text:
            return "reject", text
        selected = value.get("selected")
        if isinstance(selected, (list, tuple)) and selected:
            return parse_intent_confirmation(selected[0])
        if value.get("value") is not None:
            return "confirm", str(value["value"])
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
        if text.lower() in {"confirm", "confirmed"} or text in {"正确，继续溯源", "正确"}:
            return "confirm", None
        if text.lower() in {"correct", "reject"} or text in {"不正确，我要补充/修改", "不正确"}:
            return "reject", None
        return ("reject", text) if text else ("invalid", None)
    return "invalid", None


def extract_correction_text(value: Any) -> str | None:
    """从修正追问或候选选择中取出用户文本。"""

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


INTENT_PROMPT = """你是配电网故障原因溯源系统的意图识别器。
请从用户对故障现象的描述中提取以下 JSON 字段（整段必须且只是一个 JSON 对象，以 { 开头）：
{
  "question": "对用户问题的简短重述",
  "device_name": "具体设备名称，如 1号主变；没有则 null",
  "device_type": "设备类型，如 变压器、开关柜；没有则 null",
  "voltage_level": "电压等级，如 110kV、35kV、10kV；没有则 null",
  "phenomena": ["故障现象列表，如 冷却器风扇停转、散热片积尘"]
}
要求：
- 设备名称必须是具体设备（如“1号主变”），用户只写“变压器”这类类型词时 device_name 为 null。
- 图片识别结果中的故障类别（如“绝缘子破损”）应并入 phenomena。
- 只输出 JSON，不要输出其他解释。
"""
