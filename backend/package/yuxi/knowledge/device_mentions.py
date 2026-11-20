"""线路与杆塔设备指称的共享规则解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_REQUEST_PREFIXES = tuple(
    sorted(
        {
            "麻烦",
            "帮忙",
            "帮我",
            "请",
            "评估",
            "分析",
            "检查",
            "检修",
            "维修",
            "处理",
            "更换",
            "巡视",
            "排查",
            "查询",
            "确认",
            "针对",
        },
        key=len,
        reverse=True,
    )
)

_LINE_TOWER_PATTERN = re.compile(
    r"(?P<line>[\u4e00-\u9fff]{2,16}线)\s*"
    r"(?P<code>[A-Za-z]{1,5}\s*\d+[A-Za-z]?)\s*"
    r"(?P<suffix>杆塔|杆|塔)"
)
_LINE_PHASE_PATTERN = re.compile(
    r"(?P<line>[\u4e00-\u9fff]{2,16}线)\s*"
    r"(?P<code>[A-Za-z]{1,5}\s*\d+)\s*"
    r"(?P<phase>[A-Za-z]?相)"
)
_DEVICE_IDENTIFIER_PATTERN = re.compile(
    r"(?:[A-Za-z]{1,5}\s*\d+|\d+\s*#|#\s*\d+|\d+\s*号|\d+[A-Za-z]\s*-)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LineTowerMention:
    """用户原话中的线路位置与杆塔设备身份。"""

    line_name: str
    device_name: str
    device_type: str | None = None
    work_object: str | None = None


def strip_request_prefix(text: str) -> str:
    """反复去掉请求或任务动词前缀，保留其后的线路/设备名称。"""

    value = str(text or "").strip()
    changed = True
    while changed:
        changed = False
        for prefix in _REQUEST_PREFIXES:
            if value.startswith(prefix) and len(value) > len(prefix) + 1:
                value = value[len(prefix) :].strip()
                changed = True
                break
    return value


def extract_line_tower_mention(text: str) -> LineTowerMention | None:
    """抽取“线路名 + 杆塔编号”，识别显式杆塔后缀与相别上下文。"""

    value = str(text or "").strip()
    if not value:
        return None

    tower = _LINE_TOWER_PATTERN.search(value)
    if tower is not None:
        code = re.sub(r"\s+", "", tower.group("code"))
        return LineTowerMention(
            line_name=strip_request_prefix(tower.group("line")),
            device_name=f"{code}{tower.group('suffix')}",
            device_type="杆塔",
        )

    phase = _LINE_PHASE_PATTERN.search(value)
    if phase is not None:
        code = re.sub(r"\s+", "", phase.group("code"))
        phase_text = phase.group("phase")
        return LineTowerMention(
            line_name=strip_request_prefix(phase.group("line")),
            device_name=f"{code}{phase_text}",
            work_object=phase_text,
        )
    return None


def has_device_identifier(text: str | None) -> bool:
    """判断设备名是否含可区分编号，而不是只有设备类型或泛称。"""

    return bool(_DEVICE_IDENTIFIER_PATTERN.search(str(text or "")))


def prefer_rule_device_name(rule_name: str | None, model_name: str | None) -> str | None:
    """规则含具体编号而模型退化为类型/泛称时，保留规则识别结果。"""

    rule = str(rule_name or "").strip()
    model = str(model_name or "").strip()
    if not rule:
        return model or None
    if not model:
        return rule
    if rule.casefold() in model.casefold():
        return rule
    if has_device_identifier(rule) and not has_device_identifier(model):
        return rule
    return model
