"""故障设备台账（MinIO JSON）读取与设备 ID 解析。

台账由数据团队导出到 MinIO（默认 ``fault-assets/ledger/devices.json``），运行时只读。
故障图设备名称与台账层级共同确定台账设备 ID，再用该 ID 作为 Redis 实时值 key 前缀。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any

import json_repair

from yuxi.knowledge.device_index import device_tokens, normalize_device_text
from yuxi.storage.minio import get_minio_client
from yuxi.utils import logger

LEDGER_BUCKET = os.getenv("FAULT_TRACE_LEDGER_BUCKET", "fault-assets")
LEDGER_OBJECT = os.getenv("FAULT_TRACE_LEDGER_OBJECT", "ledger/devices.json")
LEDGER_CACHE_TTL_SECONDS = 300
_VOLTAGE_TEXT_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*(?:[kK][vV]|千伏)", re.IGNORECASE)
_GENERIC_PATH_NODES = frozenset({"变电设施", "输电设施"})
_PATH_ANCHOR_PATTERN = re.compile(r"变电站|开闭所|箱变|线路")


@dataclass(frozen=True)
class LedgerDevice:
    """台账中的一个设备行；``device_id`` 是 Redis key 使用的设备 ID。"""

    device_id: str
    name: str
    device_type: str | None = None
    voltage_level: str | None = None
    path: tuple[str, ...] = ()
    attribute_codes: tuple[str, ...] = ()
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "device_type": self.device_type,
            "voltage_level": self.voltage_level,
            "path": list(self.path),
        }


@dataclass(frozen=True)
class _LedgerSearchRecord:
    """台账搜索缓存行：避免每次请求重复计算层级路径与归一化名称。"""

    device: LedgerDevice
    normalized_name: str
    normalized_path: str


class LedgerRepository:
    """MinIO 台账的只读快照与设备查找。"""

    _rows: list[dict[str, Any]] | None = None
    _index: list[_LedgerSearchRecord] | None = None
    _loaded_at: float = 0.0
    _lock = asyncio.Lock()

    async def load(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not force and self._rows is not None and time.monotonic() - self._loaded_at < LEDGER_CACHE_TTL_SECONDS:
            return self._rows
        async with self._lock:
            if not force and self._rows is not None and time.monotonic() - self._loaded_at < LEDGER_CACHE_TTL_SECONDS:
                return self._rows
            client = get_minio_client()
            raw = await client.adownload_file(LEDGER_BUCKET, LEDGER_OBJECT)
            payload = _load_ledger_json(raw)
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise ValueError("故障台账缺少 data 数组")
            type(self)._rows = [row for row in rows if isinstance(row, dict) and row.get("id")]
            type(self)._index = _build_search_index(type(self)._rows)
            type(self)._loaded_at = time.monotonic()
            return type(self)._rows

    async def search_devices(
        self,
        query: str,
        *,
        device_type: str | None = None,
        voltage_level: str | None = None,
        location_hint: str | None = None,
        limit: int = 24,
        minimum_score: float = 0.0,
    ) -> list[LedgerDevice]:
        """按名称近似、类型、电压与位置检索台账候选。

        名称质量决定主排序；类型、电压与层级路径只加权，不把用户的明确名称输入
        静默替换成别的设备。台账不可用时返回空列表，由图谱侧继续或工具给出澄清。
        """

        try:
            rows = await self.load()
        except Exception as exc:  # noqa: BLE001 - 台账是召回增强，图谱仍可独立工作
            logger.warning(f"读取故障台账失败: {exc}")
            return []
        if type(self)._index is None:
            type(self)._index = _build_search_index(rows)
        index = type(self)._index
        if limit <= 0:
            return []

        wanted_type = _normalize_name(device_type)
        wanted_voltage = _normalize_name(voltage_level)
        wanted_location = normalize_device_text(location_hint)
        scored: list[tuple[float, int, LedgerDevice]] = []
        for rank, record in enumerate(index):
            score = _ledger_match_score(
                query,
                record,
                device_type=wanted_type,
                voltage_level=wanted_voltage,
                location_hint=wanted_location,
            )
            if score > 0 and score >= minimum_score:
                scored.append((score, rank, record.device))
        scored.sort(key=lambda item: (-item[0], item[1], item[2].name, item[2].device_id))
        return [replace(device, score=score) for score, _rank, device in scored[:limit]]

    @classmethod
    def reset(cls) -> None:
        """清空台账快照与搜索缓存，供测试和显式刷新使用。"""

        cls._rows = None
        cls._index = None
        cls._loaded_at = 0.0

    async def find_device(self, name: str, *, ancestors: list[str] | None = None) -> LedgerDevice | None:
        clean = str(name or "").strip()
        if not clean:
            return None
        try:
            rows = await self.load()
        except Exception as exc:  # noqa: BLE001 - 台账不可用时由调用方回退图谱 ID
            logger.warning(f"读取故障台账失败: {exc}")
            return None
        by_id = {str(row.get("id")): row for row in rows}
        path_cache: dict[str, tuple[str, ...]] = {}

        def path_of(row: dict[str, Any]) -> tuple[str, ...]:
            row_id = str(row.get("id"))
            if row_id in path_cache:
                return path_cache[row_id]
            names: list[str] = []
            cursor = row
            seen: set[str] = set()
            while cursor is not None:
                parent_id = str(cursor.get("pid") or "")
                if not parent_id or parent_id in seen or parent_id not in by_id:
                    break
                seen.add(parent_id)
                cursor = by_id[parent_id]
                names.append(str(cursor.get("name") or ""))
            path = tuple(reversed(names))
            path_cache[row_id] = path
            return path

        wanted = [str(item) for item in (ancestors or []) if str(item).strip()]
        exact = [row for row in rows if _normalize_name(row.get("name")) == _normalize_name(clean)]
        if exact:
            return match_ledger_candidate(
                name=clean,
                path=" > ".join(wanted),
                candidates=[_ledger_device_from_row(row, path=path_of(row)) for row in exact],
            )

        # 名称包含只在唯一命中时使用；同名/同前缀数据不再静默取第一条。
        needle = _normalize_name(clean)
        contained = [
            row
            for row in rows
            if needle and (row_name := _normalize_name(row.get("name"))) and (needle in row_name or row_name in needle)
        ]
        if len(contained) != 1:
            return None
        return _ledger_device_from_row(contained[0], path=path_of(contained[0]))


def _ledger_device_from_row(row: dict[str, Any], *, path: tuple[str, ...]) -> LedgerDevice:
    """台账原始行 → 业务设备快照。"""

    attributes = [str(item.get("code")) for item in (row.get("attributes") or []) if item.get("code")]
    return LedgerDevice(
        device_id=str(row.get("id")),
        name=str(row.get("name") or ""),
        device_type=str(row.get("classifyName") or _type_name(row.get("type")) or "") or None,
        voltage_level=_voltage_from_path(path, str(row.get("name") or "")),
        path=path,
        attribute_codes=tuple(attributes),
    )


def _build_search_index(rows: list[dict[str, Any]]) -> list[_LedgerSearchRecord]:
    """把台账行转换为可重复使用的搜索记录；空名称行不参与候选召回。"""

    by_id = {str(row.get("id")): row for row in rows}
    records: list[_LedgerSearchRecord] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        device_id = str(row.get("id") or "").strip()
        if not name or not device_id:
            continue
        path = _row_path(row, by_id=by_id)
        records.append(
            _LedgerSearchRecord(
                device=LedgerDevice(
                    device_id=device_id,
                    name=name,
                    device_type=str(row.get("classifyName") or _type_name(row.get("type")) or "") or None,
                    voltage_level=_voltage_from_path(path, name),
                    path=path,
                    attribute_codes=tuple(
                        str(item.get("code")) for item in (row.get("attributes") or []) if item.get("code")
                    ),
                ),
                normalized_name=normalize_device_text(name),
                normalized_path=normalize_device_text(" ".join(path)),
            )
        )
    return records


def _load_ledger_json(raw: bytes) -> Any:
    """解析台账 JSON；数据团队历史导出存在轻微语法瑕疵时使用显式修复。"""

    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f"故障台账 JSON 存在格式瑕疵，尝试修复解析: {exc}")
        return json_repair.loads(text)


def _row_path(row: dict[str, Any], *, by_id: dict[str, dict[str, Any]]) -> tuple[str, ...]:
    """按 ``pid`` 向上拼出祖先名称，从根到近。"""

    names: list[str] = []
    seen: set[str] = set()
    cursor = row
    while cursor is not None:
        parent_id = str(cursor.get("pid") or "")
        if not parent_id or parent_id in seen or parent_id not in by_id:
            break
        seen.add(parent_id)
        cursor = by_id[parent_id]
        name = str(cursor.get("name") or "").strip()
        if name:
            names.append(name)
    return tuple(reversed(names))


def _ledger_match_score(
    query: str,
    record: _LedgerSearchRecord,
    *,
    device_type: str,
    voltage_level: str,
    location_hint: str,
) -> float:
    """台账候选打分：名称质量为主，类型/电压/位置提供有限加权。"""

    wanted = normalize_device_text(query)
    name = record.normalized_name
    score = 0.0
    if wanted and name:
        if name == wanted:
            score = 100.0
        elif name.startswith(wanted) or wanted.startswith(name):
            score = 85.0
        elif wanted in name or name in wanted:
            score = 70.0
        else:
            tokens = device_tokens(query)
            if tokens:
                matched = sum(1 for token in tokens if token in name)
                if matched:
                    score = 35.0 + 25.0 * matched / len(tokens)
            wanted_core = _name_core(query)
            name_core = _name_core(record.device.name)
            if score == 0 and abs(len(name_core) - len(wanted_core)) <= max(6, len(wanted_core) // 2):
                ratio = SequenceMatcher(None, wanted_core, name_core).ratio()
                if ratio >= 0.55:
                    score = ratio * 70.0

    candidate_type = normalize_device_text(record.device.device_type)
    type_match = bool(
        device_type and candidate_type and (device_type in candidate_type or candidate_type in device_type)
    )
    if device_type and not wanted and not type_match:
        return 0.0
    if type_match:
        score += 20.0
    if voltage_level and normalize_device_text(record.device.voltage_level) == voltage_level:
        score += 10.0
    if location_hint and location_hint in record.normalized_path:
        score += 15.0
    return score


def _name_core(value: str) -> str:
    """去掉电压等级后的名称核心，避免 ``10kV`` 公共字符造成近似误召回。"""

    return normalize_device_text(_VOLTAGE_TEXT_PATTERN.sub("", str(value or "")))


def match_ledger_candidate(
    *,
    name: str,
    path: str,
    ledger_ids: tuple[str, ...] = (),
    candidates: list[LedgerDevice] | tuple[LedgerDevice, ...],
) -> LedgerDevice | None:
    """按名称、业务 ID 与层级为图谱设备选择唯一台账行；歧义时不猜。"""

    wanted_name = normalize_device_text(name)
    wanted_ids = {str(item).strip() for item in ledger_ids if str(item).strip()}
    same_name = [item for item in candidates if normalize_device_text(item.name) == wanted_name]
    if not same_name:
        return None

    for candidate in same_name:
        if candidate.device_id in wanted_ids:
            return candidate
    graph_path = normalize_device_text(path)
    scored = [(_ledger_path_score(candidate.path, graph_path, wanted_name), candidate) for candidate in same_name]
    best_hits = max(score for score, _candidate in scored)
    if best_hits <= 0:
        return None
    best = [candidate for score, candidate in scored if score == best_hits]
    return best[0] if len(best) == 1 else None


def _ledger_path_score(path: tuple[str, ...], graph_path: str, name: str) -> int:
    """计算台账路径与图谱路径的区分性重叠；泛化根节点不计分。"""

    anchors = [node for node in path if _PATH_ANCHOR_PATTERN.search(str(node or ""))]
    if anchors:
        matched_anchors = sum(1 for node in anchors if normalize_device_text(node) in graph_path)
        return matched_anchors if matched_anchors == len(anchors) else 0
    specific_nodes = [node for node in path if normalize_device_text(node) and node not in _GENERIC_PATH_NODES]
    if specific_nodes:
        return sum(1 for node in specific_nodes if normalize_device_text(node) in graph_path)
    return 1 if name and name in graph_path else 0


def _normalize_name(value: Any) -> str:
    return "".join(str(value or "").split()).lower()


def _type_name(value: Any) -> str:
    """台账 ``type`` 可能是对象、字符串或 null，脏行不能让整次溯源失败。"""

    if isinstance(value, dict):
        return str(value.get("name") or "")
    if isinstance(value, str):
        return value
    return ""


def _voltage_from_path(path: tuple[str, ...], name: str) -> str | None:
    for text in (*reversed(path), name):
        match = re.search(r"(\d+(?:\.\d+)?)\s*[kK][vV]", text)
        if match:
            return f"{match.group(1)}kV"
    return None


ledger_repository = LedgerRepository()
