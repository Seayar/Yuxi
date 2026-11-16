"""处置方案分支判定：只读 Redis key ``{台账ID}:{分支判定维度}``。

精确维度没有 key 时，再扫描该设备的中文维度 key，按值匹配场景的两个分支条件，兼容
``带电范围`` / ``停电范围`` 这类维度别名。key 约定与故障溯源实时值一致：
``{FAULT_TRACE_REDIS_PREFIX}{台账ID}:{维度}``，value 为场景的 ``分支1条件`` 或
``分支2条件``。读取失败不默认任何分支：状态区分
``not_required / not_configured / missing / unavailable / invalid / ok``，由工具层决定停止并返回
固定失败文案，不让人工选择或默认分支。Redis 地址复用故障溯源配置，避免同一批设备 ID 维护两份连接。
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
SCAN_COUNT = 100


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
    """读取并解析分支值；值必须与场景的两个分支条件之一精确匹配。

    先查 ``{台账ID}:{分支判定维度}``；没有命中时再扫该设备的其他中文维度 key，按**值**
    收敛到场景的两个条件。这样数据侧把维度写成 ``带电范围``、场景写 ``停电范围`` 时仍能
    自动判定，且不会因为维度名不同就退回人工选择。
    """

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

    db = realtime_redis_db()
    prefix = realtime_redis_prefix()
    values = await _mget(url, keys, db=db, prefix=prefix)
    if values is None:
        return BranchLookup("unavailable", reason="分支 Redis 读取失败")

    pairs = [
        (str(raw).strip(), key.partition(":")[0])
        for key, raw in zip(keys, values)
        if raw is not None and str(raw).strip()
    ]
    matches, invalid = _collect_matches(pairs, condition1, condition2)
    if not matches:
        ledger_ids = [ledger_id for ledger_id in device.ledger_ids if ledger_id]
        scanned = await _scan_values(url, ledger_ids, db=db, prefix=prefix)
        if scanned is None:
            return BranchLookup("unavailable", reason="分支 Redis 扫描失败")
        scanned_matches, scanned_invalid = _collect_matches(scanned, condition1, condition2)
        matches.extend(scanned_matches)
        invalid.extend(scanned_invalid)
    return _result_from_matches(matches, invalid)


def _collect_matches(
    pairs: list[tuple[str, str]], condition1: str, condition2: str
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    """把 ``(值, 台账ID)`` 收敛为分支匹配与非法值。"""

    matches: list[tuple[str, str, str]] = []
    invalid: list[tuple[str, str]] = []
    for text, ledger_id in pairs:
        branch = _match_condition(text, condition1, condition2)
        if branch is None:
            invalid.append((text, ledger_id))
        else:
            matches.append((branch, text, ledger_id))
    return matches, invalid


def _result_from_matches(matches: list[tuple[str, str, str]], invalid: list[tuple[str, str]]) -> BranchLookup:
    """把分支匹配收敛为最终状态；多个台账 ID 给出不同分支时判非法。"""

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


async def _scan_values(url: str, ledger_ids: list[str], *, db: int, prefix: str) -> list[tuple[str, str]] | None:
    """扫描设备的中文维度 key，返回 ``(值, 台账ID)``；``None`` 表示读取失败。

    只扫带中文维度名的 key，避免把故障溯源的英文实时属性值当成分支值。
    """

    from redis.asyncio import Redis
    from redis.exceptions import RedisError

    if not ledger_ids:
        return []
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
        pairs: list[tuple[str, str]] = []
        for ledger_id in ledger_ids:
            cursor = 0
            while True:
                cursor, keys = await asyncio.wait_for(
                    client.scan(cursor, match=f"{prefix}{ledger_id}:*", count=SCAN_COUNT),
                    timeout=READ_TIMEOUT_SECONDS,
                )
                if keys:
                    values = await asyncio.wait_for(client.mget(keys), timeout=READ_TIMEOUT_SECONDS)
                    for key, raw in zip(keys, values):
                        dimension = key.rsplit(":", 1)[-1]
                        if not any(ord(char) > 127 for char in dimension):
                            continue
                        text = str(raw or "").strip()
                        if text:
                            pairs.append((text, ledger_id))
                if cursor == 0:
                    break
        return pairs
    except (TimeoutError, OSError, RedisError) as exc:
        from yuxi.utils import logger

        logger.warning(f"分支判定 Redis 扫描失败: {exc}")
        return None
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不能影响业务
            pass
