"""设备健康评估与 LSTM 故障预测服务客户端。

两个外部服务都是只读 HTTP 接口：

- 故障预测：``GET {prediction_base_url}/api/prediction/predict/{deviceId}``
- 设备健康评估：``GET {health_base_url}/api/device-health/{deviceId}``

本模块只做协议转译与失败投影：把服务返回的业务字段归一为 ``contracts`` 的数据结构；
服务未配置、不可达、非 200、响应不是 JSON 对象或缺少必需字段时一律抛
``PredictionServiceError``，绝不把失败伪装成“没有异常”。字段缺失与失败分开处理：
低优先级的故障率、平均停运时间为空时保留空值，由报告标注未返回。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from yuxi.config.options import device_prediction_opts
from yuxi.knowledge.device_prediction.contracts import (
    HEALTH_SERVICE_INVALID_MESSAGE,
    HEALTH_SERVICE_NOT_CONFIGURED_MESSAGE,
    HEALTH_SERVICE_UNAVAILABLE_MESSAGE,
    HEALTH_SERVICE_UNKNOWN_DEVICE_MESSAGE,
    PREDICTION_SERVICE_INVALID_MESSAGE,
    PREDICTION_SERVICE_NOT_CONFIGURED_MESSAGE,
    PREDICTION_SERVICE_UNAVAILABLE_MESSAGE,
    PREDICTION_SERVICE_UNKNOWN_DEVICE_MESSAGE,
    FaultPrediction,
    HealthAssessment,
    HealthIndicator,
)
from yuxi.utils import get_docker_safe_url, logger

HEALTH_PATH = "/api/device-health/{device_id}"
PREDICTION_PATH = "/api/prediction/predict/{device_id}"
# 两个服务都是“对单台设备做一次实时计算”，连接与读取分别设界，避免上游僵死长期占用请求。
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
_ERROR_DETAIL_LIMIT = 300

# 预测时间窗口里可解析出“X–Y 天”“X–Y 个月”时的维护时机推导规则（服务未返回 maintenance_time 时用）。
# 维护要在故障发生前完成，所以区间取早界：窗口“30–60 天”对应“30 天内”，“3–6 个月”对应“3 个月内”。
_DAY_RANGE_PATTERN = re.compile(r"(?P<start>\d+)\s*[–\-~～至]\s*\d+\s*天")
_DAY_PATTERN = re.compile(r"(?P<start>\d+)\s*天")
_MONTH_RANGE_PATTERN = re.compile(r"(?P<start>\d+)\s*[–\-~～至]\s*\d+\s*个月")
_MONTH_PATTERN = re.compile(r"(?P<months>\d+)\s*个月")


class PredictionServiceError(Exception):
    """预测服务调用失败；``message`` 是可直接展示给用户的固定文案。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def fetch_health_assessment(device_id: str) -> HealthAssessment:
    """调用设备健康评估服务，返回评分、等级、监测指标与可靠性指标。"""

    payload = await _fetch(
        device_id,
        path=HEALTH_PATH,
        base_url_field="health_base_url",
        not_configured=HEALTH_SERVICE_NOT_CONFIGURED_MESSAGE,
        unavailable=HEALTH_SERVICE_UNAVAILABLE_MESSAGE,
        unknown_device=HEALTH_SERVICE_UNKNOWN_DEVICE_MESSAGE,
        invalid=HEALTH_SERVICE_INVALID_MESSAGE,
    )
    return _parse_health_assessment(payload)


async def fetch_fault_prediction(device_id: str) -> FaultPrediction:
    """调用 LSTM 故障预测服务，返回故障类型、概率、时间窗口与退化趋势。"""

    payload = await _fetch(
        device_id,
        path=PREDICTION_PATH,
        base_url_field="prediction_base_url",
        not_configured=PREDICTION_SERVICE_NOT_CONFIGURED_MESSAGE,
        unavailable=PREDICTION_SERVICE_UNAVAILABLE_MESSAGE,
        unknown_device=PREDICTION_SERVICE_UNKNOWN_DEVICE_MESSAGE,
        invalid=PREDICTION_SERVICE_INVALID_MESSAGE,
    )
    return _parse_fault_prediction(payload)


async def _fetch(
    device_id: str,
    *,
    path: str,
    base_url_field: str,
    not_configured: str,
    unavailable: str,
    unknown_device: str,
    invalid: str,
) -> dict[str, Any]:
    """发起一次只读 GET 并返回 JSON 对象；四类失败各投影为固定文案。

    ``404`` 单独区分：预测服务对不在其数据集内的设备返回 404（实测响应
    ``{"detail": "未知设备 ID: …"}``），这属于设备口径问题而不是服务不可用。
    """

    base_url = await _resolve_base_url(base_url_field)
    if base_url is None:
        logger.warning(f"预测服务地址不可用：{device_prediction_opts.key}.{base_url_field} 未配置或读取失败")
        raise PredictionServiceError("not_configured", not_configured)

    url = f"{base_url}{path.format(device_id=quote(device_id, safe=''))}"
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning(f"预测服务不可达（{url}）: {exc}")
        raise PredictionServiceError("unavailable", unavailable) from exc

    if response.status_code == 404:
        logger.warning(f"预测服务不认识该设备（{url}）: {response.text[:_ERROR_DETAIL_LIMIT]}")
        raise PredictionServiceError("unknown_device", unknown_device)
    if response.status_code != 200:
        logger.warning(f"预测服务返回 {response.status_code}（{url}）: {response.text[:_ERROR_DETAIL_LIMIT]}")
        raise PredictionServiceError("unavailable", unavailable)

    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(f"预测服务响应不是 JSON（{url}）: {response.text[:_ERROR_DETAIL_LIMIT]}")
        raise PredictionServiceError("invalid", invalid) from exc
    if not isinstance(payload, dict):
        logger.warning(f"预测服务响应不是 JSON 对象（{url}）")
        raise PredictionServiceError("invalid", invalid)
    return payload


async def _resolve_base_url(field_key: str) -> str | None:
    """读取管理员配置的服务地址；缺失时返回 None，由调用方投影为“未配置”。

    配置项读取失败（数据库/缓存不可用）与字段留空一样返回 None：对工具而言两者都是
    “拿不到服务地址”，日志里分别记录了真实原因。
    """

    try:
        resolved = await device_prediction_opts.get()
    except Exception as exc:  # noqa: BLE001 —— 配置项读取失败等同未配置
        logger.warning(f"读取设备预测维护服务配置失败: {exc}")
        return None
    base_url = str(resolved.get(field_key) or "").strip()
    if not base_url:
        return None
    return get_docker_safe_url(base_url.rstrip("/"))


def _parse_health_assessment(payload: dict[str, Any]) -> HealthAssessment:
    """解析健康评估响应；缺评分或等级按失败处理，指标与可靠性指标缺失时保留空值。"""

    body = _business_payload(payload)
    health_score = _text(body.get("health_score"))
    health_level = _text(body.get("health_level"))
    if not health_score or not health_level:
        raise PredictionServiceError("invalid", HEALTH_SERVICE_INVALID_MESSAGE)

    indicators = [_parse_indicator(row) for row in _rows(body.get("indicators"))]
    return HealthAssessment(
        health_score=health_score,
        health_level=health_level,
        indicators=tuple(indicator for indicator in indicators if indicator is not None),
        fault_rate=_with_unit(body.get("fault_rate"), "次/年"),
        average_downtime_hours=_with_unit(body.get("average_downtime_hours"), "小时/次"),
    )


def _parse_fault_prediction(payload: dict[str, Any]) -> FaultPrediction:
    """解析故障预测响应；故障类型/概率/时间窗口/退化趋势缺一按失败处理。"""

    body = _business_payload(payload)
    fault_type = _text(body.get("predicted_fault_type"))
    probability = _percent(body.get("probability"))
    expected_failure_time = _text(body.get("expected_failure_time"))
    degradation_trend = _text(body.get("degradation_trend"))
    if not (fault_type and probability and expected_failure_time and degradation_trend):
        raise PredictionServiceError("invalid", PREDICTION_SERVICE_INVALID_MESSAGE)

    maintenance_time = _text(body.get("maintenance_time"))
    return FaultPrediction(
        predicted_fault_type=fault_type,
        probability=probability,
        expected_failure_time=expected_failure_time,
        degradation_trend=degradation_trend,
        maintenance_time=maintenance_time or derive_maintenance_time(expected_failure_time),
        maintenance_time_derived=not maintenance_time,
    )


def derive_maintenance_time(expected_failure_time: str) -> str:
    """服务未返回维护时机时，按预计发生时间推导“在故障发生前完成维护”的时机。"""

    window = _text(expected_failure_time)
    day_range = _DAY_RANGE_PATTERN.search(window)
    if day_range:
        return f"建议在 {day_range.group('start')} 天内完成（预测故障发生在{window}）"
    month_range = _MONTH_RANGE_PATTERN.search(window)
    if month_range:
        return f"建议在 {month_range.group('start')} 个月内完成（预测故障发生在{window}）"
    months = _MONTH_PATTERN.search(window)
    if months:
        return f"建议在 {months.group('months')} 个月内完成（预测故障发生在{window}）"
    days = _DAY_PATTERN.search(window)
    if days:
        return f"建议在 {days.group('start')} 天内完成（预测故障发生在{window}）"
    return f"建议在{window}之前完成维护"


def _business_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """取出业务对象：``{data: {...}}`` / ``{result: {...}}`` / 顶层对象三种写法都接受。"""

    for key in ("data", "result"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            return inner
    return payload


def _rows(value: Any) -> list[Any]:
    """指标列表字段容错：数组直接使用，``{items: []}`` 取内部数组，其他情况视为空。"""

    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        return value["items"]
    return []


def _parse_indicator(raw: Any) -> HealthIndicator | None:
    """解析一项监测指标；没有指标名称的脏行丢弃，不进入报告。"""

    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("indicator_name"))
    if not name:
        return None
    return HealthIndicator(
        indicator_name=name,
        indicator_value=_text(raw.get("indicator_value")),
        indicator_unit=_text(raw.get("indicator_unit")),
        weight_ratio=_percent(raw.get("weight_ratio")),
        indicator_score=_deduction(raw.get("indicator_score")),
        criteria=_text(raw.get("criteria")),
    )


def _text(value: Any) -> str:
    """把服务字段归一为展示文本：None→空串，整数值浮点去掉 ``.0``。"""

    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _with_unit(value: Any, unit: str) -> str:
    """纯数值补上需求规定的单位；服务已经带单位的文本原样保留。"""

    text = _text(value)
    if not text:
        return ""
    return f"{text} {unit}" if _as_number(text) is not None else text


def _percent(value: Any) -> str:
    """概率/权重归一为百分比文本：0.67→67%，67→67%，"67%" 原样保留。"""

    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{_format_number(float(value) * 100)}%" if value <= 1 else f"{_format_number(float(value))}%"
    text = str(value).strip()
    if not text:
        return ""
    number = _as_number(text.rstrip("%"))
    if number is None:
        return text
    return f"{_format_number(number * 100)}%" if number <= 1 else f"{_format_number(number)}%"


def _deduction(value: Any) -> str:
    """指标所扣分数归一为带符号文本：8 / -8 / 0 → "-8 分" / "-8 分" / "0 分"。"""

    text = _text(value)
    if not text:
        return ""
    number = _as_number(text)
    if number is None:
        return text
    if number == 0:
        return "0 分"
    return f"-{_format_number(abs(number))} 分"


def _as_number(text: str) -> float | None:
    """把文本解析成数值；带全角或其它装饰字符时返回 None，交由调用方保留原文。"""

    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _format_number(value: float) -> str:
    """数值去掉无意义的尾随零：30.0→30，33.33→33.33。"""

    return f"{value:.10g}"
