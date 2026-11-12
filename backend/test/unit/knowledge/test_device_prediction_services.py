"""设备健康评估与 LSTM 故障预测服务客户端单测（假 HTTP，不连接外部服务）。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from yuxi.knowledge.device_prediction import services
from yuxi.knowledge.device_prediction.contracts import (
    HEALTH_SERVICE_INVALID_MESSAGE,
    HEALTH_SERVICE_NOT_CONFIGURED_MESSAGE,
    HEALTH_SERVICE_UNAVAILABLE_MESSAGE,
    HEALTH_SERVICE_UNKNOWN_DEVICE_MESSAGE,
    PREDICTION_SERVICE_INVALID_MESSAGE,
    PREDICTION_SERVICE_NOT_CONFIGURED_MESSAGE,
    PREDICTION_SERVICE_UNKNOWN_DEVICE_MESSAGE,
)

pytestmark = pytest.mark.unit

BASE_URLS = {
    "prediction_base_url": "http://prediction.example:8084",
    "health_base_url": "http://health.example:8085",
}

HEALTH_PAYLOAD = {
    "code": 0,
    "data": {
        "health_score": 72.0,
        "health_level": "注意",
        "fault_rate": "0.8 次/年",
        "average_downtime_hours": "6.5 小时/次",
        "indicators": [
            {
                "indicator_name": "绕组温度",
                "indicator_value": "85",
                "indicator_unit": "℃",
                "weight_ratio": 0.3,
                "indicator_score": 8,
                "criteria": "正常区间 ≤ 75℃；每超 5℃ 扣 2 分",
            },
            {
                "indicator_name": "顶层油温",
                "indicator_value": "68",
                "indicator_unit": "℃",
                "weight_ratio": 0.25,
                "indicator_score": 0,
                "criteria": "正常区间 ≤ 85℃",
            },
            {"indicator_value": "缺少指标名"},
        ],
    },
}

PREDICTION_PAYLOAD = {
    "predicted_fault_type": "触头过热 / 冷却效能下降",
    "probability": 0.67,
    "expected_failure_time": "未来 30–60 天",
    "degradation_trend": "负荷率连续 3 个月上升，近 30 天最高达 78%",
    "maintenance_time": "建议在 30 天内完成针对性检查维护",
}


class _FakeOption:
    key = "device_prediction_opts"

    def __init__(self, value: dict) -> None:
        self._value = value

    async def get(self) -> dict:
        return dict(self._value)


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else "not-json"

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError("expecting value", self.text, 0)
        return self._payload


def _install(monkeypatch, *, response=None, error=None, base_urls=None):
    """把 httpx 客户端替换成固定响应，返回记录到的请求 URL。"""

    calls: list[str] = []

    class _Client:
        def __init__(self, *, timeout=None) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info) -> bool:
            return False

        async def get(self, url: str):
            calls.append(url)
            if error is not None:
                raise error
            return response

    monkeypatch.setattr(services, "device_prediction_opts", _FakeOption(BASE_URLS if base_urls is None else base_urls))
    monkeypatch.setattr(services.httpx, "AsyncClient", _Client)
    return calls


def _raise(coro):
    with pytest.raises(services.PredictionServiceError) as info:
        asyncio.run(coro)
    return info.value


def test_health_assessment_is_parsed_and_normalized(monkeypatch):
    """嵌套 data 信封、百分数与扣分写法都被归一为报告可直接展示的文本。"""

    _install(monkeypatch, response=_FakeResponse(payload=HEALTH_PAYLOAD))

    health = asyncio.run(services.fetch_health_assessment("bb1baf15"))

    assert health.health_score == "72"
    assert health.health_level == "注意"
    assert health.fault_rate == "0.8 次/年"
    assert health.average_downtime_hours == "6.5 小时/次"
    assert [item.indicator_name for item in health.indicators] == ["绕组温度", "顶层油温"]
    assert health.indicators[0].weight_ratio == "30%"
    assert health.indicators[0].indicator_score == "-8 分"
    assert health.indicators[1].indicator_score == "0 分"


def test_fault_prediction_is_parsed_and_device_id_is_url_encoded(monkeypatch):
    """设备 ID 里的 ``#`` 等字符必须编码，不能把 URL 截断成片段。"""

    calls = _install(monkeypatch, response=_FakeResponse(payload=PREDICTION_PAYLOAD))

    prediction = asyncio.run(services.fetch_fault_prediction("LGR#3k18"))

    assert calls == ["http://prediction.example:8084/api/prediction/predict/LGR%233k18"]
    assert prediction.predicted_fault_type == "触头过热 / 冷却效能下降"
    assert prediction.probability == "67%"
    assert prediction.maintenance_time == "建议在 30 天内完成针对性检查维护"
    assert prediction.maintenance_time_derived is False


def test_health_endpoint_path_and_base_url(monkeypatch):
    """健康评估走自己的服务地址与路径。"""

    calls = _install(monkeypatch, response=_FakeResponse(payload=HEALTH_PAYLOAD))

    asyncio.run(services.fetch_health_assessment("bb1baf15"))

    assert calls == ["http://health.example:8085/api/device-health/bb1baf15"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"health_level": "注意"}, HEALTH_SERVICE_INVALID_MESSAGE),
        ({"health_score": 72}, HEALTH_SERVICE_INVALID_MESSAGE),
    ],
)
def test_health_missing_required_field_is_invalid(monkeypatch, payload, message):
    """缺健康评分或健康等级按服务失败处理，不产出半份报告。"""

    _install(monkeypatch, response=_FakeResponse(payload=payload))

    error = _raise(services.fetch_health_assessment("bb1baf15"))

    assert error.code == "invalid"
    assert error.message == message


@pytest.mark.parametrize("field", ["predicted_fault_type", "probability", "expected_failure_time", "degradation_trend"])
def test_prediction_missing_required_field_is_invalid(monkeypatch, field):
    """故障预测四个核心字段缺一即失败。"""

    payload = dict(PREDICTION_PAYLOAD)
    payload.pop(field)
    _install(monkeypatch, response=_FakeResponse(payload=payload))

    error = _raise(services.fetch_fault_prediction("bb1baf15"))

    assert error.message == PREDICTION_SERVICE_INVALID_MESSAGE


def test_missing_maintenance_time_is_derived_and_marked(monkeypatch):
    """服务未返回维护时机时按预计发生时间推导，并标记为推导值。"""

    payload = dict(PREDICTION_PAYLOAD)
    payload.pop("maintenance_time")
    _install(monkeypatch, response=_FakeResponse(payload=payload))

    prediction = asyncio.run(services.fetch_fault_prediction("bb1baf15"))

    assert prediction.maintenance_time_derived is True
    assert prediction.maintenance_time == "建议在 30 天内完成（预测故障发生在未来 30–60 天）"


def test_not_configured_base_url_is_reported(monkeypatch):
    """服务地址为空时返回“未配置”，与不可达区分。"""

    _install(monkeypatch, response=_FakeResponse(payload=HEALTH_PAYLOAD), base_urls={})

    health_error = _raise(services.fetch_health_assessment("bb1baf15"))
    prediction_error = _raise(services.fetch_fault_prediction("bb1baf15"))

    assert health_error.code == "not_configured"
    assert health_error.message == HEALTH_SERVICE_NOT_CONFIGURED_MESSAGE
    assert prediction_error.message == PREDICTION_SERVICE_NOT_CONFIGURED_MESSAGE


@pytest.mark.parametrize(
    ("error", "message"),
    [(httpx.ConnectError("boom"), HEALTH_SERVICE_UNAVAILABLE_MESSAGE)],
)
def test_unreachable_service_is_reported(monkeypatch, error, message):
    """连接失败返回“暂不可用”，不伪装成没有异常。"""

    _install(monkeypatch, error=error)

    assert _raise(services.fetch_health_assessment("bb1baf15")).message == message


def test_non_200_and_non_json_are_reported(monkeypatch):
    """非 200 与响应不是 JSON 都按失败处理。"""

    _install(monkeypatch, response=_FakeResponse(status_code=500, payload=HEALTH_PAYLOAD))
    assert _raise(services.fetch_health_assessment("bb1baf15")).message == HEALTH_SERVICE_UNAVAILABLE_MESSAGE

    _install(monkeypatch, response=_FakeResponse(payload=None))
    assert _raise(services.fetch_fault_prediction("bb1baf15")).message == PREDICTION_SERVICE_INVALID_MESSAGE

    # 合法 JSON 但不是对象（数组）同样按数据不完整处理，不能当成空报告继续装配。
    _install(monkeypatch, response=_FakeResponse(payload=[]))
    assert _raise(services.fetch_health_assessment("bb1baf15")).message == HEALTH_SERVICE_INVALID_MESSAGE


def test_unknown_device_is_reported_separately(monkeypatch):
    """服务返回 404（实测“未知设备 ID”）属于设备口径问题，不能报成服务不可用。"""

    _install(monkeypatch, response=_FakeResponse(status_code=404, payload={"detail": "未知设备 ID: x"}))

    health_error = _raise(services.fetch_health_assessment("bb1baf15"))
    prediction_error = _raise(services.fetch_fault_prediction("bb1baf15"))

    assert health_error.code == "unknown_device"
    assert health_error.message == HEALTH_SERVICE_UNKNOWN_DEVICE_MESSAGE
    assert prediction_error.code == "unknown_device"
    assert prediction_error.message == PREDICTION_SERVICE_UNKNOWN_DEVICE_MESSAGE


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ("未来 30–60 天", "建议在 30 天内完成（预测故障发生在未来 30–60 天）"),
        ("未来 3–6 个月", "建议在 3 个月内完成（预测故障发生在未来 3–6 个月）"),
        ("未来 12 个月以上", "建议在 12 个月内完成（预测故障发生在未来 12 个月以上）"),
        ("60 天", "建议在 60 天内完成（预测故障发生在60 天）"),
        ("近期", "建议在近期之前完成维护"),
    ],
)
def test_maintenance_time_derivation(window, expected):
    """维护时机推导覆盖天/月区间、单值与无法解析三种写法，区间取早界。"""

    assert services.derive_maintenance_time(window) == expected


def test_numeric_reliability_fields_get_required_units(monkeypatch):
    """服务返回纯数值时按需求口径补单位；已带单位的文本保持原样。"""

    payload = {
        "device_id": "05ec3443017349ff886e84f61ddd5fa3",
        "health_score": 71.2,
        "health_level": "一般",
        "fault_rate": 0.25,
        "average_downtime_hours": 6.0,
        "indicators": [],
    }
    _install(monkeypatch, response=_FakeResponse(payload=payload))

    health = asyncio.run(services.fetch_health_assessment("05ec3443017349ff886e84f61ddd5fa3"))

    assert health.health_score == "71.2"
    assert health.health_level == "一般"
    assert health.fault_rate == "0.25 次/年"
    assert health.average_downtime_hours == "6 小时/次"

    payload["fault_rate"] = "0.8 次/年"
    payload["average_downtime_hours"] = "6.5 小时/次"
    _install(monkeypatch, response=_FakeResponse(payload=payload))

    health = asyncio.run(services.fetch_health_assessment("05ec3443017349ff886e84f61ddd5fa3"))

    assert health.fault_rate == "0.8 次/年"
    assert health.average_downtime_hours == "6.5 小时/次"
