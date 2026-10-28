"""实时值读取（Redis）单元测试：状态映射与图像属性隔离。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.fault_trace import realtime

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_read_realtime_values_maps_ok_missing_and_invalid(monkeypatch):
    async def fake_mget(url, device_id, attributes):
        assert device_id == "dev-1"
        assert attributes == ["leakage_current", "uv_photon_rate", "line_protection_trip", "crossarm_temp_rise"]
        return ["1.2", None, "maybe", "bad"]

    monkeypatch.setattr(realtime, "_mget", fake_mget)
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6380/0")

    values = await realtime.read_realtime_values(
        "dev-1",
        ["leakage_current", "uv_photon_rate", "line_protection_trip", "crossarm_temp_rise"],
    )

    assert (values["leakage_current"].status, values["leakage_current"].value) == ("ok", 1.2)
    assert values["uv_photon_rate"].status == "missing"
    assert values["line_protection_trip"].status == "invalid"
    assert values["crossarm_temp_rise"].status == "invalid"


@pytest.mark.asyncio
async def test_image_attributes_are_not_read_from_redis(monkeypatch):
    called = False

    async def fake_mget(url, device_id, attributes):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(realtime, "_mget", fake_mget)
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6380/0")

    values = await realtime.read_realtime_values("dev-1", ["image_tree_fall_detected"])

    assert values["image_tree_fall_detected"].status == "not_collected"
    assert called is False


@pytest.mark.asyncio
async def test_unconfigured_url_reports_not_configured_for_image_attributes(monkeypatch):
    """没有实时值地址时，图像类属性与其他属性一样是 not_configured，而不是 not_collected。"""

    monkeypatch.delenv("FAULT_TRACE_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    values = await realtime.read_realtime_values("dev-1", ["image_tree_fall_detected", "leakage_current"])

    assert values["image_tree_fall_detected"].status == "not_configured"
    assert values["leakage_current"].status == "not_configured"


@pytest.mark.asyncio
async def test_unregistered_attributes_are_read_and_parsed(monkeypatch):
    async def fake_mget(url, device_id, attributes):
        assert device_id == "dev-1"
        return ["180", "true"]

    monkeypatch.setattr(realtime, "_mget", fake_mget)
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6380/0")

    values = await realtime.read_realtime_values("dev-1", ["winding_local_temp", "cooling_fan_running"])

    assert (values["winding_local_temp"].status, values["winding_local_temp"].value) == ("ok", 180.0)
    assert (values["cooling_fan_running"].status, values["cooling_fan_running"].value) == ("ok", True)


@pytest.mark.asyncio
async def test_missing_redis_url_is_not_configured(monkeypatch):
    monkeypatch.delenv("FAULT_TRACE_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    values = await realtime.read_realtime_values("dev-1", ["leakage_current"])

    assert values["leakage_current"].status == "not_configured"


@pytest.mark.asyncio
async def test_read_failure_is_unavailable_not_missing(monkeypatch):
    """连接失败或超时不能被当成 key 缺失，否则告警永不触发。"""

    async def fake_mget(url, device_id, attributes):
        return None

    monkeypatch.setattr(realtime, "_mget", fake_mget)
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6380/0")

    values = await realtime.read_realtime_values("dev-1", ["leakage_current"])

    assert values["leakage_current"].status == "unavailable"


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "NaN", "Infinity"])
def test_non_finite_numbers_are_invalid(raw):
    """非有限数参与比较会得到错误的成立/不成立，必须判为非法值。"""

    assert realtime._parse_value("leakage_current", raw).status == "invalid"


def _fake_redis(monkeypatch, *, error: Exception | None = None, delay: float = 0.0):
    """把真实 redis 客户端替换成只抛异常或阻塞的替身。"""

    import redis.asyncio

    class FakeRedis:
        @classmethod
        def from_url(cls, *_args, **_kwargs):
            return cls()

        async def mget(self, _keys):
            if delay:
                import asyncio

                await asyncio.sleep(delay)
            raise error

        async def aclose(self):
            return None

    monkeypatch.setattr(redis.asyncio, "Redis", FakeRedis)


@pytest.mark.asyncio
async def test_connection_refused_maps_to_unavailable(monkeypatch):
    """连接被拒是服务不可用，不能报成 key 缺失。"""

    from redis.exceptions import ConnectionError as RedisConnectionError

    _fake_redis(monkeypatch, error=RedisConnectionError("connection refused"))
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6380/0")

    values = await realtime.read_realtime_values("dev-1", ["leakage_current"])

    assert values["leakage_current"].status == "unavailable"


@pytest.mark.asyncio
async def test_read_timeout_maps_to_unavailable(monkeypatch):
    """客户端超时同样属于读取失败，重试耗尽后返回 unavailable。"""

    _fake_redis(monkeypatch, error=TimeoutError("read timeout"))
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6380/0")

    values = await realtime.read_realtime_values("dev-1", ["leakage_current"])

    assert values["leakage_current"].status == "unavailable"


@pytest.mark.asyncio
async def test_hanging_read_is_cut_by_wait_for(monkeypatch):
    """服务端不响应时由 ``wait_for`` 截断，而不是无限等待。"""

    monkeypatch.setattr(realtime, "READ_TIMEOUT_SECONDS", 0.01)
    _fake_redis(monkeypatch, error=TimeoutError("never"), delay=1.0)
    monkeypatch.setenv("FAULT_TRACE_REDIS_URL", "redis://example:6380/0")

    values = await realtime.read_realtime_values("dev-1", ["leakage_current"])

    assert values["leakage_current"].status == "unavailable"
