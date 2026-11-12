"""kg4 分支判定：只读 Redis key ``{台账ID}:{分支判定维度}``。

key 约定与故障溯源实时值一致：``{FAULT_TRACE_REDIS_PREFIX}{台账ID}:{维度}``，value 为场景的
``分支1条件`` 或 ``分支2条件``。读取失败不默认任何分支：状态区分
``not_required / not_configured / missing / unavailable / invalid / ok``，由工具层决定是
提示用户确认分支还是停止。Redis 地址复用故障溯源配置，避免同一批设备 ID 维护两份连接。
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass

from yuxi.knowledge.maintenance_plan.contracts import (
    BRANCH_1,
    BRANCH_2,
    DeviceCandidate,
    Scenario,
)

READ_TIMEOUT_SECONDS = 1.0
READ_ATTEMPTS = 2


@dataclass(frozen=True)
class BranchLookup:
    """一次分支判定的读取结果。"""

    state: str
    value: str = ""
    ledger_id: str = ""
    branch: str = ""
    reason: str = ""


def branch_keys(device: DeviceCandidate, scenario: Scenario) -> list[str]:
    """生成候选 key（不含 Redis 前缀）：每个台账 ID 一个 ``{台账ID}:{维度}``。"""

    dimension = str(scenario.branch_dimension or "").strip()
    if not dimension:
        return []
    return [f"{ledger_id}:{dimension}" for ledger_id in device.ledger_ids if ledger_id]


async def lookup_branch(device: DeviceCandidate, scenario: Scenario) -> BranchLookup:
    """读取并解析分支值；值必须与场景的两个分支条件之一精确匹配。"""

    dimension = str(scenario.branch_dimension or "").strip()
    condition1 = str(scenario.branch1_condition or "").strip()
    condition2 = str(scenario.branch2_condition or "").strip()
    if not dimension or not condition1 or not condition2:
        return BranchLookup("not_required")

    keys = branch_keys(device, scenario)
    if not keys:
        return BranchLookup("missing", reason="设备没有台账ID列表")

    from yuxi.knowledge.fault_trace.realtime import realtime_redis_db, realtime_redis_prefix, realtime_redis_url

    url = realtime_redis_url()
    if not url:
        return BranchLookup("not_configured", reason="未配置分支 Redis 地址")

    values = await _mget(url, keys, db=realtime_redis_db(), prefix=realtime_redis_prefix())
    if values is None:
        return BranchLookup("unavailable", reason="分支 Redis 读取失败")

    matches: list[tuple[str, str, str]] = []
    invalid: list[tuple[str, str]] = []
    for key, raw in zip(keys, values):
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        branch = _match_condition(text, condition1, condition2)
        if branch is None:
            invalid.append((text, key.partition(":")[0]))
            continue
        matches.append((branch, text, key.partition(":")[0]))

    if not matches:
        if invalid:
            return BranchLookup(
                "invalid",
                value=invalid[0][0],
                ledger_id=invalid[0][1],
                reason="值与两个分支条件都不匹配",
            )
        return BranchLookup("missing", reason="没有可用的分支 key/value")
    branches = {item[0] for item in matches}
    if len(branches) > 1:
        return BranchLookup(
            "invalid", value=matches[0][1], ledger_id=matches[0][2], reason="多个台账 ID 给出了不同分支"
        )
    branch, value, ledger_id = matches[0]
    return BranchLookup("ok", value=value, ledger_id=ledger_id, branch=branch)


def _match_condition(value: str, condition1: str, condition2: str) -> str | None:
    """把 Redis 原始值收敛到 分支1/分支2；两个条件归一后相同则视为非法。"""

    normalized = _normalize(value)
    first = _normalize(condition1)
    second = _normalize(condition2)
    if first == second:
        return None
    if normalized == first:
        return BRANCH_1
    if normalized == second:
        return BRANCH_2
    return None


def _normalize(text: str) -> str:
    """全角转半角、去空白、忽略大小写。"""

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or ""))).casefold()


async def _mget(url: str, keys: list[str], *, db: int, prefix: str) -> list[str | None] | None:
    """批量读取；``None`` 表示读取失败，列表内的 ``None`` 表示 key 不存在。"""

    from redis.asyncio import Redis
    from redis.exceptions import RedisError

    full_keys = [f"{prefix}{key}" for key in keys]
    try:
        client = Redis.from_url(
            url,
            db=db,
            decode_responses=True,
            socket_timeout=READ_TIMEOUT_SECONDS,
            socket_connect_timeout=READ_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        from yuxi.utils import logger

        logger.warning(f"分支判定 Redis 地址无效: {exc}")
        return None
    try:
        for attempt in range(READ_ATTEMPTS):
            try:
                return list(await asyncio.wait_for(client.mget(full_keys), timeout=READ_TIMEOUT_SECONDS))
            except (TimeoutError, OSError, RedisError) as exc:
                if attempt == READ_ATTEMPTS - 1:
                    from yuxi.utils import logger

                    logger.warning(f"分支判定 Redis 读取失败: {exc}")
                    return None
        return None
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不能影响业务
            pass
