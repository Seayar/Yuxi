"""设备预测维护的设备语义解析：把用户问题解析为 ``contracts.PredictionQuery``。

设备识别口径与处置方案生成一致：复用 ``maintenance_plan.intent`` 的规则抽取与设备类型归一，
再叠加设备预测维护自己的模型抽取提示词（优先模型、失败回退规则）与输入约束（缺设备时固定反问）。
本模块同时提供按当前 Agent 模型执行的非流式单次调用，供意图识别与状态解读复用。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from yuxi.knowledge.device_prediction.contracts import (
    CLARIFY_DEVICE_MESSAGE,
    PredictionQuery,
)
from yuxi.knowledge.maintenance_plan.intent import normalize_device_type, rule_extract
from yuxi.knowledge.power_spec.model_output import parse_model_json
from yuxi.utils import logger

PredictionModelCall = Callable[[str], Awaitable[str]]

# 意图识别开关与模型调用参数（与处置方案同款，只有开关名换成本模块的）
DEVICE_PREDICTION_INTENT_ENABLED = os.getenv("DEVICE_PREDICTION_INTENT_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
DEVICE_PREDICTION_INTENT_TIMEOUT_SECONDS = float(os.getenv("DEVICE_PREDICTION_INTENT_TIMEOUT", "300"))
# 输出额度与提示词共享服务端 max_model_len（当前 20480），需为提示词留出余量。
DEVICE_PREDICTION_INTENT_MAX_TOKENS = int(os.getenv("DEVICE_PREDICTION_INTENT_MAX_TOKENS", "4096"))
# 状态解读只需要一段自然语言总结，额度比意图识别更小
STATUS_READOUT_MAX_TOKENS = int(os.getenv("DEVICE_PREDICTION_READOUT_MAX_TOKENS", "1024"))

# 模型把“没有”写成这些词时统一收敛为 None
_EMPTY_VALUES = frozenset({"null", "none", "无", "未知", "未提及", "不明确", "未说明", "n/a", "na", "-"})
_VOLTAGE_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?:[kKＫｋ][vVＶｖ]|千伏)")


def rule_extract_prediction(text: str) -> PredictionQuery:
    """纯规则抽取：复用处置方案的设备名/设备类型/电压等级/位置线索规则。"""

    plan_query = rule_extract(text)
    return PredictionQuery(
        raw_text=(text or "").strip(),
        device_name=plan_query.device_name,
        device_type=plan_query.device_type,
        voltage_level=plan_query.voltage_level,
        location_hint=plan_query.location_hint,
    )


async def extract_prediction_intent(
    text: str,
    *,
    model_call: PredictionModelCall | None,
    previous: PredictionQuery | None = None,
) -> PredictionQuery:
    """优先用大模型抽取设备信息（严格 JSON），失败/超时/非法输出时回退规则解析。

    ``previous`` 用于用户在设备卡片里自由输入后重新解析：本轮已识别的设备字段优先，
    上一轮结果补齐缺失字段。
    """

    rule_query = rule_extract_prediction(text)
    query = rule_query
    if model_call is not None:
        try:
            raw = await model_call(build_intent_prompt(text, previous=previous))
            query = _merge_model_result(rule_query, parse_model_json(raw))
        except Exception as exc:  # noqa: BLE001 —— 模型侧任何异常都必须降级到规则解析
            logger.warning(f"设备预测维护意图识别失败，回退规则解析: {exc}")
            query = rule_query

    if previous is not None:
        query = _merge_previous(query, previous)
        query.raw_text = _combine_text(previous.raw_text, text)
    else:
        query.raw_text = (text or "").strip()
    return apply_input_constraints(query)


def apply_input_constraints(query: PredictionQuery) -> PredictionQuery:
    """把设备类型收敛到图谱类型名；既没有设备名称也没有设备类型时置固定反问文案。"""

    query.device_type = normalize_device_type(query.device_type)
    query.voltage_level = _normalize_voltage(query.voltage_level)
    query.device_name = _clean_optional(query.device_name)
    query.location_hint = _clean_optional(query.location_hint)
    query.raw_text = (query.raw_text or "").strip()

    if not query.device_name and not query.device_type:
        query.clarification_required = True
        query.clarification_message = CLARIFY_DEVICE_MESSAGE
    else:
        query.clarification_required = False
        query.clarification_message = ""
    return query


def build_intent_prompt(text: str, *, previous: PredictionQuery | None = None) -> str:
    """构造设备识别提示词；再次解析时带上上一轮识别结果。"""

    sections = [INTENT_PROMPT, f"\n用户问题：\n{text}"]
    if previous is not None:
        sections.append(
            "\n上一轮识别结果：\n"
            + json.dumps(
                {
                    "device_name": previous.device_name,
                    "device_type": previous.device_type,
                    "voltage_level": previous.voltage_level,
                    "location_hint": previous.location_hint,
                },
                ensure_ascii=False,
            )
        )
    return "".join(sections)


def _merge_model_result(rule: PredictionQuery, payload: dict[str, Any]) -> PredictionQuery:
    """合并模型与规则：原文已识别到具体编号时以规则结果为准，避免模型把部件并入设备名。"""

    device_name = _clean_optional(payload.get("device_name")) or rule.device_name
    if device_name and rule.device_name and _squeeze(rule.device_name) in _squeeze(device_name):
        device_name = rule.device_name
    return PredictionQuery(
        raw_text=rule.raw_text,
        device_name=device_name,
        device_type=normalize_device_type(_clean_optional(payload.get("device_type"))) or rule.device_type,
        voltage_level=_normalize_voltage(_clean_optional(payload.get("voltage_level"))) or rule.voltage_level,
        location_hint=_clean_optional(payload.get("location_hint")) or rule.location_hint,
    )


def _merge_previous(query: PredictionQuery, previous: PredictionQuery) -> PredictionQuery:
    """用上一轮结果补齐本轮缺失字段；本轮已识别的值优先。"""

    return PredictionQuery(
        raw_text=query.raw_text,
        device_name=query.device_name or previous.device_name,
        device_type=query.device_type or previous.device_type,
        voltage_level=query.voltage_level or previous.voltage_level,
        location_hint=query.location_hint or previous.location_hint,
    )


def _combine_text(previous_text: str, text: str) -> str:
    """把上一轮原文与本次补充拼成一条完整描述，供追溯使用。"""

    head = (previous_text or "").strip()
    tail = (text or "").strip()
    if not head:
        return tail
    if not tail or tail in head:
        return head
    return f"{head}；{tail}"


def _normalize_voltage(raw: str | None) -> str | None:
    """把 10KV / 10 kV / 10千伏 之类的写法收敛为 10kV。"""

    text = _clean_optional(raw)
    if text is None:
        return None
    match = _VOLTAGE_PATTERN.search(text)
    if match is None:
        return None
    value = match.group("value")
    return f"{value[:-2]}kV" if value.endswith(".0") else f"{value}kV"


def _clean_optional(value: Any) -> str | None:
    """空值、空白与“未知/无”等写法统一收敛为 None。"""

    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _EMPTY_VALUES:
        return None
    return text


def _squeeze(text: str) -> str:
    """去掉空白并忽略大小写，用于判断模型结果是否包含原文指称。"""

    return "".join(str(text or "").split()).casefold()


def context_model_call(
    context: Any, *, max_tokens: int = DEVICE_PREDICTION_INTENT_MAX_TOKENS
) -> PredictionModelCall | None:
    """构造按当前 Agent 模型执行的非流式单次调用；上下文没有模型标识时返回 None。"""

    if context is None or not getattr(context, "model", None):
        return None
    if not DEVICE_PREDICTION_INTENT_ENABLED:
        return None

    async def model_call(prompt: str) -> str:
        model = _select_context_model(context, max_tokens=max_tokens)
        response = await model.call(prompt, stream=False)
        return getattr(response, "content", "") or ""

    return model_call


def _select_context_model(context: Any, *, max_tokens: int):
    """按当前 Agent 的模型标识构造聊天模型，关闭思考并固定随机性（temperature 0）。"""

    from yuxi.models.chat import select_model

    return select_model(
        model_spec=str(context.model),
        timeout=DEVICE_PREDICTION_INTENT_TIMEOUT_SECONDS,
        model_params={
            "max_tokens": max_tokens,
            "temperature": 0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
    )


INTENT_PROMPT = """你是电力设备预测维护系统的设备识别器。
请从用户问题中提取目标设备信息，输出且只输出一个 JSON 对象（以 { 开头）：
{
  "device_name": "用户原话中的具体设备指称，如 1号主变、3k18、991SVG开关；只说了设备类型时填 null",
  "device_type": "设备类型，如 变压器、开关柜、断路器、架空线路、开闭所；没有则 null",
  "voltage_level": "电压等级，如 10kV、35kV；没有则 null",
  "location_hint": "位置线索，如 #3开闭所、35kV#1变电站、#62箱变；没有则 null"
}
要求：
- 设备名称必须来自用户原话，不要改写成台账名称，也不要把设备类型当作设备名称。
- 无法确定的字段一律填 null，不要编造。
- 用户只问了健康状态、故障预测或维护计划时，同样只提取设备信息。
- 只输出 JSON，不要输出其他解释。
"""
