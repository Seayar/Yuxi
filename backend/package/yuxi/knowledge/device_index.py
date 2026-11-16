"""kg5 设备名索引：为设备召回提供归一化与近似匹配。

用户与意图识别给出的设备写法常与图谱名称不一致（电压前缀、序号写法、省略词、错字）。
索引把图谱里真实存在的设备名归一化后放进进程内缓存，供召回阶段做精确/包含/词元/近似
匹配；它只返回图谱里已存在的设备，不产生新的设备事实，也不参与授权。索引构建失败时
调用方回退现有的名称查询，不影响主链路。
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from yuxi.utils import logger

DEVICE_INDEX_TTL_SECONDS = float(os.getenv("DEVICE_NAME_INDEX_TTL_SECONDS", "600"))
DEVICE_INDEX_MAX_ENTRIES = int(os.getenv("DEVICE_NAME_INDEX_MAX_ENTRIES", "5000"))
DEVICE_INDEX_CANDIDATES = int(os.getenv("DEVICE_NAME_INDEX_CANDIDATES", "8"))
DEVICE_RECALL_TOKEN_LIMIT = int(os.getenv("DEVICE_NAME_TOKEN_RECALL_LIMIT", "3"))
# 兜底召回最多保留的记录数：泛词元可能命中上百条，交给模型/卡片的必须有界。
DEVICE_RECALL_MAX_RECORDS = int(os.getenv("DEVICE_RECALL_MAX_RECORDS", "24"))

# 归一化后仍保留中文与字母数字，去掉空白、标点与分隔符（“3B-7Y”与“3B 7Y”等价）。
_NON_WORD_PATTERN = re.compile(r"[\s\W_]+", re.UNICODE)
_ALNUM_RUN_PATTERN = re.compile(r"[a-z0-9]+")
_CHINESE_RUN_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
# 近似匹配只在长度接近时计算，避免拿长名称去模糊命中短词元。
_RATIO_MIN = 0.6
_RATIO_LENGTH_WINDOW = 6


def normalize_device_text(text: str | None) -> str:
    """归一化设备名：全角转半角、去空白与标点、忽略大小写。"""

    if not text:
        return ""
    return _NON_WORD_PATTERN.sub("", unicodedata.normalize("NFKC", str(text))).casefold()


def device_tokens(text: str | None) -> list[str]:
    """设备名匹配词元：字母数字段 + 中文二元组。

    单字词元（如“1”“线”）区分度太低，会把大量设备卷进召回，直接丢弃；整个查询只有一个
    字时才保留它。结果已归一化（小写、去标点），用于索引相似度计算。
    """

    normalized = normalize_device_text(text)
    if not normalized:
        return []
    tokens = list(_ALNUM_RUN_PATTERN.findall(normalized))
    for run in _CHINESE_RUN_PATTERN.findall(normalized):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    minimum = 1 if len(normalized) == 1 else 2
    return list(dict.fromkeys(token for token in tokens if len(token) >= minimum))


def device_recall_tokens(text: str | None) -> list[str]:
    """Cypher ``CONTAINS`` 用的词元：保留原始大小写。

    图查询的 ``CONTAINS`` 区分大小写，不能直接传归一化（小写）结果；``3B``、``SVG``
    这类带大小写的名称必须按原样匹配。
    """

    if not text:
        return []
    tokens: list[str] = []
    for run in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", str(text)):
        if run.isascii():
            if len(run) >= 2:
                tokens.append(run)
            continue
        if len(run) == 1:
            continue
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(tokens))[:DEVICE_RECALL_TOKEN_LIMIT]


def fallback_score(name: str, tokens: Sequence[str], *, index_rank: int | None = None) -> float:
    """兜底候选排序分：词元命中数优先，其次是设备名索引的相似度排名。

    兜底召回可能一次返回上百条泛词元命中，统一给 1 分会让真正的目标被名称排序挤出候选；
    命中词元越多、索引排名越靠前，分数越高。
    """

    hits = sum(1 for token in tokens if token and token in str(name or ""))
    score = 1.0 + hits * 2.0
    if index_rank is not None:
        score = max(score, 3.0 + max(0, DEVICE_INDEX_CANDIDATES - index_rank) * 0.1)
    return score


@dataclass(frozen=True)
class IndexedDevice:
    """索引中的一台设备：只保留召回需要的字段。"""

    device_id: str
    name: str
    path: str
    category: str
    normalized: str


@dataclass(frozen=True)
class DeviceNameIndex:
    """设备名索引；``devices`` 按归一化名称去重。"""

    devices: tuple[IndexedDevice, ...]
    built_at: float

    @classmethod
    def from_records(
        cls,
        records: Sequence[dict[str, Any]],
        *,
        name_props: Sequence[str],
        path_prop: str = "层级路径",
        category_prop: str = "设备分类",
    ) -> DeviceNameIndex:
        """把图谱设备节点转换成索引；没有名称与 id 的节点跳过。"""

        devices: list[IndexedDevice] = []
        seen: set[str] = set()
        for record in records:
            props = record.get("n") if isinstance(record, dict) else None
            if not isinstance(props, dict):
                props = record if isinstance(record, dict) else {}
            device_id = str(props.get("id") or "").strip()
            name = next(
                (str(props.get(prop) or "").strip() for prop in name_props if str(props.get(prop) or "").strip()),
                "",
            )
            if not device_id or not name:
                continue
            normalized = normalize_device_text(name)
            if not normalized or device_id in seen:
                continue
            seen.add(device_id)
            devices.append(
                IndexedDevice(
                    device_id=device_id,
                    name=name,
                    path=str(props.get(path_prop) or "").strip(),
                    category=str(props.get(category_prop) or "").strip(),
                    normalized=normalized,
                )
            )
        return cls(devices=tuple(devices), built_at=time.monotonic())

    def search(self, query: str, *, limit: int = DEVICE_INDEX_CANDIDATES) -> list[IndexedDevice]:
        """按归一化名称/词元/近似度召回设备；没有足够相似的结果时返回空列表。"""

        normalized = normalize_device_text(query)
        if not normalized or not self.devices:
            return []
        tokens = device_tokens(query)
        scored: list[tuple[float, IndexedDevice]] = []
        for device in self.devices:
            score = _name_similarity(normalized, tokens, device.normalized)
            if score >= 0.5:
                scored.append((score, device))
        scored.sort(key=lambda item: (-item[0], len(item[1].name), item[1].name))
        return [device for _score, device in scored[:limit]]


def _name_similarity(normalized: str, tokens: list[str], name: str) -> float:
    """名称相似度：精确 > 前缀 > 包含 > 词元覆盖 > 编辑相似。"""

    if not name:
        return 0.0
    if name == normalized:
        return 1.0
    if name.startswith(normalized) or normalized.startswith(name):
        return 0.9
    if normalized in name or name in normalized:
        return 0.8
    if tokens:
        matched = sum(1 for token in tokens if token in name)
        if matched:
            return 0.5 + 0.4 * (matched / len(tokens))
    if abs(len(name) - len(normalized)) <= _RATIO_LENGTH_WINDOW:
        ratio = SequenceMatcher(None, normalized, name).ratio()
        if ratio >= _RATIO_MIN:
            return ratio
    return 0.0


_CACHE: dict[str, DeviceNameIndex] = {}


def get_device_name_index(
    client: Any,
    *,
    graph_key: str,
    label: str,
    name_props: Sequence[str],
    path_prop: str = "层级路径",
    category_prop: str = "设备分类",
) -> DeviceNameIndex | None:
    """按图配置取或构建设备名索引；TTL 内复用，构建失败返回 None。"""

    now = time.monotonic()
    cached = _CACHE.get(graph_key)
    if cached is not None and now - cached.built_at < DEVICE_INDEX_TTL_SECONDS:
        return cached
    try:
        records = client.run(
            "MATCH (n) WHERE labels(n)[0] = $label RETURN n LIMIT $node_limit",
            label=label,
            node_limit=DEVICE_INDEX_MAX_ENTRIES,
        )
        index = DeviceNameIndex.from_records(
            records,
            name_props=name_props,
            path_prop=path_prop,
            category_prop=category_prop,
        )
    except Exception as exc:  # noqa: BLE001 —— 索引是可选召回增强，失败时回退现有查询
        logger.warning(f"设备名索引构建失败，回退名称查询: {exc}")
        return None
    if not index.devices:
        return None
    _CACHE[graph_key] = index
    logger.info(f"设备名索引已构建: graph={graph_key} devices={len(index.devices)}")
    return index


def graph_key(config: Any) -> str:
    """图配置 → 索引缓存键；配置缺失时返回空串，调用方跳过索引。"""

    uri = str(getattr(config, "uri", "") or "")
    name = str(getattr(config, "graph", "") or "")
    return f"{uri}|{name}" if uri or name else ""


def reset_device_name_index() -> None:
    """清空进程内索引缓存（测试与图配置切换时使用）。"""

    _CACHE.clear()
