"""故障设备台账（MinIO JSON）读取与设备 ID 解析。

台账由数据团队导出到 MinIO（默认 ``fault-assets/ledger/devices.json``），运行时只读。
kg3 设备名称与台账层级共同确定台账设备 ID，再用该 ID 作为 Redis 实时值 key 前缀。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from yuxi.storage.minio import get_minio_client
from yuxi.utils import logger

LEDGER_BUCKET = os.getenv("FAULT_TRACE_LEDGER_BUCKET", "fault-assets")
LEDGER_OBJECT = os.getenv("FAULT_TRACE_LEDGER_OBJECT", "ledger/devices.json")
LEDGER_CACHE_TTL_SECONDS = 300


@dataclass(frozen=True)
class LedgerDevice:
    """台账中的一个设备行；``device_id`` 是 Redis key 使用的设备 ID。"""

    device_id: str
    name: str
    device_type: str | None = None
    voltage_level: str | None = None
    path: tuple[str, ...] = ()
    attribute_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "device_type": self.device_type,
            "voltage_level": self.voltage_level,
            "path": list(self.path),
        }


class LedgerRepository:
    """MinIO 台账的只读快照与设备查找。"""

    _rows: list[dict[str, Any]] | None = None
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
            payload = json.loads(raw.decode("utf-8"))
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise ValueError("故障台账缺少 data 数组")
            type(self)._rows = [row for row in rows if isinstance(row, dict) and row.get("id")]
            type(self)._loaded_at = time.monotonic()
            return type(self)._rows

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

        candidates = [row for row in rows if _normalize_name(row.get("name")) == _normalize_name(clean)]
        if not candidates:
            # 空名称会让 ``"" in name`` 恒真并命中无关设备，必须先排除空查询与空行名。
            needle = _normalize_name(clean)
            if needle:
                candidates = [
                    row
                    for row in rows
                    if (name := _normalize_name(row.get("name"))) and (needle in name or name in needle)
                ]
        if not candidates:
            return None

        wanted = [str(item) for item in (ancestors or []) if str(item).strip()]
        scored: list[tuple[int, int, int, dict[str, Any]]] = []
        for row in candidates:
            path = path_of(row)
            path_text = " ".join(path)
            ancestor_hits = sum(1 for item in wanted if item in path_text)
            attributes = [item for item in (row.get("attributes") or []) if isinstance(item, dict)]
            scored.append((ancestor_hits, 1 if attributes else 0, len(path), row))
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        row = scored[0][3]
        attributes = [str(item.get("code")) for item in (row.get("attributes") or []) if item.get("code")]
        return LedgerDevice(
            device_id=str(row.get("id")),
            name=str(row.get("name") or clean),
            device_type=str(row.get("classifyName") or _type_name(row.get("type")) or "") or None,
            voltage_level=_voltage_from_path(path_of(row), str(row.get("name") or "")),
            path=path_of(row),
            attribute_codes=tuple(attributes),
        )


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
