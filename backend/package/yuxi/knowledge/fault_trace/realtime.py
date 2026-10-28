"""故障溯源实时值读取（Redis，只读）。

key 约定：``{FAULT_TRACE_REDIS_PREFIX}{redis_device_id}:{attribute}``。只读、批量
``MGET``、单次读取 1 秒超时、最多读取 20 个属性。读取结果按状态区分：``ok``；
``missing``（key 不存在）；``invalid``（值非法或非有限数）；``unavailable``（连接失败/
超时）；``not_configured``（未配置地址）；``not_collected``（图像类属性）。
原因求值把除 ``ok`` 以外的状态一律视为未知，不当作 ``false``。
"""

from __future__ import annotations

import asyncio
import math
import os
from typing import Any

from yuxi.knowledge.fault_trace.contracts import (
    IMAGE_ATTRIBUTE_TYPES,
    MAX_REALTIME_ATTRIBUTES,
    REALTIME_ATTRIBUTE_TYPES,
    REALTIME_ATTRIBUTE_UNITS,
    RealtimeValue,
)
from yuxi.utils import logger

READ_TIMEOUT_SECONDS = 1.0
READ_ATTEMPTS = 2


def realtime_redis_url() -> str:
    """故障实时值 Redis 地址；未单独配置时复用应用 Redis。"""

    return (os.getenv("FAULT_TRACE_REDIS_URL") or "").strip() or (os.getenv("REDIS_URL") or "").strip()


def realtime_redis_db() -> int:
    try:
        return int(os.getenv("FAULT_TRACE_REDIS_DB", "0"))
    except ValueError:
        return 0


def realtime_redis_prefix() -> str:
    return os.getenv("FAULT_TRACE_REDIS_PREFIX", "")


async def read_realtime_values(device_id: str, attributes: list[str]) -> dict[str, RealtimeValue]:
    """读取设备实时属性；返回每个属性（含图像类）的读取状态。"""

    unique = list(dict.fromkeys(str(attribute) for attribute in attributes if attribute))
    values = {
        attribute: RealtimeValue(
            attribute,
            "not_collected",
            unit=REALTIME_ATTRIBUTE_UNITS.get(attribute),
        )
        for attribute in unique
    }
    targets = [attribute for attribute in unique if attribute not in IMAGE_ATTRIBUTE_TYPES][:MAX_REALTIME_ATTRIBUTES]
    if not targets:
        return values

    url = realtime_redis_url()
    if not url:
        return {
            attribute: RealtimeValue(attribute, "not_configured", unit=REALTIME_ATTRIBUTE_UNITS.get(attribute))
            for attribute in unique
        }

    raw_values = await _mget(url, device_id, targets)
    if raw_values is None:
        for attribute in targets:
            values[attribute] = RealtimeValue(
                attribute,
                "unavailable",
                unit=REALTIME_ATTRIBUTE_UNITS.get(attribute),
            )
        return values
    for attribute, raw in zip(targets, raw_values):
        if raw is None:
            values[attribute] = RealtimeValue(attribute, "missing", unit=REALTIME_ATTRIBUTE_UNITS.get(attribute))
            continue
        values[attribute] = _parse_value(attribute, raw)
    return values


async def _mget(url: str, device_id: str, attributes: list[str]) -> list[str | None] | None:
    """批量读取属性值；``None`` 表示读取失败，列表内的 ``None`` 表示 key 不存在。"""

    from redis.asyncio import Redis
    from redis.exceptions import RedisError

    prefix = realtime_redis_prefix()
    keys = [f"{prefix}{device_id}:{attribute}" for attribute in attributes]
    try:
        client = Redis.from_url(
            url,
            db=realtime_redis_db(),
            decode_responses=True,
            socket_timeout=READ_TIMEOUT_SECONDS,
            socket_connect_timeout=READ_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        logger.warning(f"故障实时值 Redis 地址无效: {exc}")
        return None
    try:
        for attempt in range(READ_ATTEMPTS):
            try:
                return list(await asyncio.wait_for(client.mget(keys), timeout=READ_TIMEOUT_SECONDS))
            except (TimeoutError, OSError, RedisError) as exc:
                if attempt == READ_ATTEMPTS - 1:
                    logger.warning(f"故障实时值读取失败: {exc}")
                    return None
        return None
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不能影响业务
            pass


def _parse_value(attribute: str, raw: Any) -> RealtimeValue:
    kind = REALTIME_ATTRIBUTE_TYPES.get(attribute)
    unit = REALTIME_ATTRIBUTE_UNITS.get(attribute)
    text = str(raw).strip()
    if kind == "float":
        return _number_value(attribute, text, unit)
    if kind == "bool":
        lowered = text.lower()
        if lowered in {"true", "false"}:
            return RealtimeValue(attribute, "ok", lowered == "true", raw=text)
        return RealtimeValue(attribute, "invalid", raw=text)
    # 未注册属性按数值/布尔/文本自动识别，供图谱自定义触发条件使用。
    try:
        float(text)
    except ValueError:
        lowered = text.lower()
        if lowered in {"true", "false"}:
            return RealtimeValue(attribute, "ok", lowered == "true", raw=text)
        return RealtimeValue(attribute, "ok", text, raw=text, unit=unit)
    return _number_value(attribute, text, unit)


def _number_value(attribute: str, text: str, unit: str | None) -> RealtimeValue:
    """解析数值属性；``nan``/``inf`` 属于非法值，不能参与判据比较。"""

    try:
        value = float(text)
    except ValueError:
        return RealtimeValue(attribute, "invalid", raw=text, unit=unit)
    if not math.isfinite(value):
        return RealtimeValue(attribute, "invalid", raw=text, unit=unit)
    return RealtimeValue(attribute, "ok", value, raw=text, unit=unit)
