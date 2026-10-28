"""故障现象简述与原因定位推理依据的模型生成。

两段文本都由模型生成，但不拥有事实：摘要只能复述已知设备与故障现象，推理依据
只能引用已求值的判据和实时值。任一证据门不通过或模型失败时返回 ``None``，由
``answer.py`` 回退到确定性文案。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from yuxi.knowledge.fault_trace.contracts import CauseEvaluation
from yuxi.utils import logger

SUMMARY_MAX_CHARS = 120
SUMMARY_MIN_CHARS = 8
REASONING_MAX_CHARS = 260
REASONING_MIN_CHARS = 15
_FORBIDDEN_TERMS = ("建议", "处置", "检修", "更换", "措施")
_STATUS = {"established": "成立", "rejected": "不成立", "unknown": "待确认"}

ModelCall = Callable[[str], Awaitable[str]]


async def compose_summary_text(
    *,
    query_text: str,
    device_name: str,
    device_type: str | None,
    phenomena: list[str],
    image_fault_name: str | None,
    model_call: ModelCall | None,
) -> str | None:
    """让模型生成一句故障现象简述；不满足证据门时返回 None。"""

    if model_call is None:
        return None
    prompt = _SUMMARY_PROMPT.format(
        query_text=query_text.strip(),
        device_name=device_name,
        device_type=device_type or "未知",
        phenomena="、".join(phenomena) or "无",
        image_fault_name=image_fault_name or "无",
    )
    text = await _invoke(model_call, prompt, "故障现象简述")
    if text is None or len(text) > SUMMARY_MAX_CHARS or len(text) < SUMMARY_MIN_CHARS:
        return None
    if device_name not in text:
        return None
    terms = [term for term in [*phenomena, image_fault_name] if term]
    if terms and not _grounded(text, terms):
        return None
    if _has_forbidden_term(text):
        return None
    return text


async def compose_location_reasoning(
    *,
    query_text: str,
    device_name: str,
    located_causes: list[CauseEvaluation],
    causes: list[CauseEvaluation],
    phenomena: list[str] | None = None,
    model_call: ModelCall | None,
) -> str | None:
    """让模型生成原因定位的推理依据；多个成立原因必须全部覆盖。"""

    if model_call is None:
        return None
    if not located_causes:
        # 未定位时没有任何可核对的判据，模型文本会直接替换确定性推理依据，因此不采纳。
        return None
    located_ids = {cause.cause_id for cause in located_causes}
    others = (
        "、".join(
            f"{cause.name}（{_STATUS.get(cause.status, cause.status)})"
            for cause in causes
            if cause.cause_id not in located_ids
        )
        or "无"
    )
    supporting = [item for item in phenomena or [] if item]
    located_name = "、".join(cause.name for cause in located_causes) or "未定位"
    details = "\n".join(
        f"- {cause.name}：命中 {_trigger_lines(cause)[0] or '无'}；未命中 {_trigger_lines(cause)[1] or '无'}"
        for cause in located_causes
    )
    prompt = _REASONING_PROMPT.format(
        query_text=query_text.strip(),
        device_name=device_name,
        located_name=located_name,
        located_details=details or "无",
        supporting_phenomena="、".join(supporting) or "无",
        missing="、".join(sorted({item for cause in located_causes for item in cause.missing})) or "无",
        other_causes=others,
    )
    text = await _invoke(model_call, prompt, "原因定位推理依据")
    if text is None or len(text) > REASONING_MAX_CHARS or len(text) < REASONING_MIN_CHARS:
        return None
    if located_causes and not all(_grounded(text, [cause.name]) for cause in located_causes):
        return None
    if supporting and not _grounded(text, supporting):
        return None
    if _has_forbidden_term(text):
        return None
    return text


def _trigger_lines(located: CauseEvaluation | None) -> tuple[str, str]:
    if located is None:
        return "", ""
    hit: list[str] = []
    missed: list[str] = []
    for trigger in located.triggers:
        if trigger.qualitative:
            continue
        values = "，".join(f"{name}={value}" for name, value in (trigger.values or {}).items())
        line = f"{trigger.name}（{trigger.expression or '无表达式'}；{values or '无数据'}）"
        if trigger.result == "true":
            hit.append(line)
        else:
            missed.append(line)
    return "；".join(hit), "；".join(missed)


def _has_forbidden_term(text: str) -> bool:
    return any(term in text for term in _FORBIDDEN_TERMS)


def _grounded(text: str, terms: list[str]) -> bool:
    """文本需覆盖任一给定术语至少 60% 的汉字，允许模型合理缩写。"""

    for term in terms:
        if term in text:
            return True
        chars = [char for char in term if "\u4e00" <= char <= "\u9fff" and char not in "的了与和及或"]
        if chars and sum(1 for char in chars if char in text) / len(chars) >= 0.6:
            return True
    return False


async def _invoke(model_call: ModelCall, prompt: str, purpose: str) -> str | None:
    try:
        raw = await model_call(prompt)
    except Exception as exc:  # noqa: BLE001 - 模型失败回退确定性文案
        logger.warning(f"{purpose}模型调用失败，回退确定性文案: {exc}")
        return None
    text = " ".join(str(raw or "").split())
    for prefix in (f"{purpose}：", f"{purpose}:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text.strip("“”\"' ") or None


_SUMMARY_PROMPT = """你是电力设备故障溯源助手。只根据下面给出的信息，用一句不超过 80 字的中文写“故障现象简述”。

要求：
1. 必须原样包含设备名称「{device_name}」；
2. 只能使用给出的故障现象，不得补充原因、处置建议、后果或未给出的数字；
3. 不要输出标题、编号、引号或换行；只输出这一句话。

用户问题：{query_text}
设备名称：{device_name}
设备类型：{device_type}
文字描述中的故障现象：{phenomena}
图片识别到的故障现象：{image_fault_name}
"""

_REASONING_PROMPT = """你是电力设备故障溯源助手。根据实时判据，写一段不超过 150 字、逻辑通顺的中文“推理依据”。

要求：
1. 只能引用下面给出的原因、现象、判据和实时值，不得新增原因、设备、数字或处置建议；
2. 必须原样包含每一个定位原因的名称；如果有判定成立的故障现象，必须提及至少一个现象；
3. 多个定位原因并存时，要说明它们共同解释故障现象；
4. 不要输出标题、编号或换行；只输出这一段话。

用户问题：{query_text}
设备名称：{device_name}
定位原因：{located_name}
成立原因与判据：
{located_details}
判定成立的故障现象：{supporting_phenomena}
缺失实时数据：{missing}
其他候选原因：{other_causes}
"""
