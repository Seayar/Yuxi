"""模型输出的 JSON 提取。

整理、审查与条款校对都要求模型"只输出 JSON"，但本地模型经常先写一段分析、或把 JSON
包在代码块里。这里集中提供容错提取：各调用点共用一套实现，避免某处用严格整段解析而
静默丢弃合法结果。
"""

from __future__ import annotations

import re
from typing import Any

import json_repair


_FENCED_JSON_PATTERN = re.compile(r"```(?:json)?\s*(?P<body>.+?)\s*```", re.DOTALL)


def parse_model_json(raw: str | dict[str, Any]) -> dict[str, Any]:
    """从模型输出中取出 JSON 对象；没有可解析对象时抛 ``ValueError``。"""

    if isinstance(raw, dict):
        return raw
    text = str(raw or "")
    candidates: list[str] = [match.group("body") for match in _FENCED_JSON_PATTERN.finditer(text)]
    candidates.append(text)
    for candidate in reversed(candidates):
        payload = _try_loads(candidate)
        if isinstance(payload, dict):
            return payload
    for span in _brace_spans(text):
        payload = _try_loads(span)
        if isinstance(payload, dict):
            return payload
    raise ValueError("模型输出中没有可解析的 JSON 对象")


def _try_loads(candidate: str) -> Any:
    """尝试解析一段候选文本，失败时返回 None。"""

    if not candidate.strip():
        return None
    try:
        return json_repair.loads(candidate)
    except Exception:  # noqa: BLE001
        return None


def _brace_spans(text: str) -> list[str]:
    """返回括号配对的顶层 `{...}` 片段，忽略字符串字面量里的括号。"""

    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start : index + 1])
                start = -1
    return spans
