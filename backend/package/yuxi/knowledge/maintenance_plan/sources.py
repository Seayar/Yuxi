"""工器具库存与人员数据（MinIO JSON）读取、匹配与抽样。

两个数据源由数据团队放到 MinIO，运行时只读；接口先按“可替换的仓储”预留：

- 工器具：默认 ``maintenance-plan/tools.json``，结构
  ``{"items":[{"name": "...", "quantity": 1}]}``；``quantity>0`` 判在库。
- 人员：默认 ``maintenance-plan/personnel.json``，结构
  ``{"班组":[{"班组名称": "...", "人员": ["姓名", ...]}]}``。

``maintenance-plan`` 与故障溯源的 ``fault-assets`` 是平级 bucket；模块也分别由
``maintenance_plan`` / ``fault_trace`` 两个平级包拥有，互不嵌套。

文件不存在、MinIO 不可用或字段不合法时不阻断方案生成：资源清单标注“库存数据未接入”，
人员安排退化为岗位与人数。人员池不足所需人数时返回 ``shortage``，由工具层决定停止。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, replace
from typing import Any

from yuxi.knowledge.maintenance_plan.contracts import (
    PERSONNEL_DATA_MISSING_NOTE,
    PERSONNEL_SHORTAGE_MESSAGE,
    TOOLS_DATA_MISSING_NOTE,
    DeviceCandidate,
    PersonnelAssignment,
    PersonnelRequirement,
    ResourceRequirement,
    TeamInfo,
)
from yuxi.utils import logger

CACHE_TTL_SECONDS = 300

_TRUE_VALUES = frozenset({"true", "1", "是", "在库", "可用", "已入库", "正常"})
_FALSE_VALUES = frozenset({"false", "0", "否", "不在库", "已出库", "已报废", "试验中", "停用"})


@dataclass(frozen=True)
class ToolRecord:
    """工器具 JSON 中的一条记录。"""

    name: str
    in_stock: bool | None
    status: str = ""
    location: str = ""
    quantity: int | None = None


@dataclass(frozen=True)
class PersonRecord:
    """人员 JSON 中的一条记录。"""

    name: str
    team: str = ""
    role: str = ""


class MaintenanceDataRepository:
    """MinIO 两个 JSON 的只读快照（进程内按 TTL 缓存）。"""

    _tools: list[ToolRecord] | None = None
    _tools_loaded_at: float = 0.0
    _personnel: list[PersonRecord] | None = None
    _personnel_loaded_at: float = 0.0
    _ledger: list[dict[str, Any]] | None = None
    _ledger_loaded_at: float = 0.0
    _lock = asyncio.Lock()

    @classmethod
    def reset(cls) -> None:
        """清空缓存（测试或数据更新后调用）。"""

        cls._tools = None
        cls._tools_loaded_at = 0.0
        cls._personnel = None
        cls._personnel_loaded_at = 0.0
        cls._ledger = None
        cls._ledger_loaded_at = 0.0

    async def load_tools(self, *, force: bool = False) -> list[ToolRecord] | None:
        """读取工器具 JSON；未配置/读取失败返回 ``None``。"""

        if (
            not force
            and type(self)._tools is not None
            and time.monotonic() - type(self)._tools_loaded_at < CACHE_TTL_SECONDS
        ):
            return type(self)._tools
        async with type(self)._lock:
            if (
                not force
                and type(self)._tools is not None
                and time.monotonic() - type(self)._tools_loaded_at < CACHE_TTL_SECONDS
            ):
                return type(self)._tools
            rows = await _read_rows(_tools_bucket(), _tools_object())
            if rows is None:
                return None
            records = [_parse_tool(row) for row in rows]
            type(self)._tools = [record for record in records if record is not None]
            type(self)._tools_loaded_at = time.monotonic()
            return type(self)._tools

    async def load_personnel(self, *, force: bool = False) -> list[PersonRecord] | None:
        """读取人员 JSON；未配置/读取失败返回 ``None``。"""

        if (
            not force
            and type(self)._personnel is not None
            and time.monotonic() - type(self)._personnel_loaded_at < CACHE_TTL_SECONDS
        ):
            return type(self)._personnel
        async with type(self)._lock:
            if (
                not force
                and type(self)._personnel is not None
                and time.monotonic() - type(self)._personnel_loaded_at < CACHE_TTL_SECONDS
            ):
                return type(self)._personnel
            rows = await _read_person_rows(_personnel_bucket(), _personnel_object())
            if rows is None:
                return None
            records = [_parse_person(row) for row in rows]
            type(self)._personnel = [record for record in records if record is not None]
            type(self)._personnel_loaded_at = time.monotonic()
            return type(self)._personnel

    async def load_ledger(self, *, force: bool = False) -> list[dict[str, Any]] | None:
        """读取故障台账 ``fault-assets/ledger/devices.json``；只用于补全设备台账 ID。"""

        if (
            not force
            and type(self)._ledger is not None
            and time.monotonic() - type(self)._ledger_loaded_at < CACHE_TTL_SECONDS
        ):
            return type(self)._ledger
        async with type(self)._lock:
            if (
                not force
                and type(self)._ledger is not None
                and time.monotonic() - type(self)._ledger_loaded_at < CACHE_TTL_SECONDS
            ):
                return type(self)._ledger
            payload = await _read_json(_ledger_bucket(), _ledger_object())
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                return None
            type(self)._ledger = [
                row for row in rows if isinstance(row, dict) and _first_text(row, "id") and _first_text(row, "name")
            ]
            type(self)._ledger_loaded_at = time.monotonic()
            return type(self)._ledger

    async def resolve_ledger_ids(self, device: DeviceCandidate) -> tuple[str, ...]:
        """补全设备台账 ID：kg4 的“补充数据”占位无法作为 Redis key 前缀时，按名称+层级回查台账。

        台账不可用或匹配不到时保留原 ID，不阻断方案；匹配结果只追加，不替换图上的真实 ID。
        """

        ids = tuple(device.ledger_ids)
        if ids and not any(_is_placeholder_id(item) for item in ids):
            return ids
        rows = await self.load_ledger()
        if not rows:
            return ids
        row = _match_ledger_row(rows, device.name, _path_segments(device.path))
        if row is None:
            logger.info(f"台账中未找到设备“{device.name}”的台账 ID，沿用图谱 ID：{ids}")
            return ids
        resolved = str(row.get("id") or "").strip()
        if not resolved:
            return ids
        merged = tuple(dict.fromkeys([*ids, resolved]))
        logger.info(f"设备“{device.name}”的台账 ID 由图谱占位补全为 {merged}")
        return merged


def _ledger_bucket() -> str:
    return os.getenv("FAULT_TRACE_LEDGER_BUCKET", "fault-assets")


def _ledger_object() -> str:
    return os.getenv("FAULT_TRACE_LEDGER_OBJECT", "ledger/devices.json")


def _is_placeholder_id(value: str) -> bool:
    return str(value or "").strip() in {"", "补充数据", "无", "null", "未知"}


def _path_segments(path: str) -> list[str]:
    """层级路径 → 上级名称；去掉设备自身。"""

    parts = [part.strip() for part in str(path or "").split(">") if part.strip()]
    return parts[:-1] if parts else []


def _match_ledger_row(rows: list[dict[str, Any]], name: str, ancestors: list[str]) -> dict[str, Any] | None:
    """按名称与层级从台账中选一行；同名时用祖先命中数消歧。"""

    needle = _normalize_name(name)
    if not needle:
        return None
    candidates = [row for row in rows if _normalize_name(row.get("name")) == needle]
    if not candidates:
        candidates = [
            row
            for row in rows
            if (row_name := _normalize_name(row.get("name"))) and (needle in row_name or row_name in needle)
        ]
    if not candidates:
        return None

    by_id = {str(row.get("id")): row for row in rows}

    def path_of(row: dict[str, Any]) -> list[str]:
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
        return names

    scored = [
        (sum(1 for ancestor in ancestors if ancestor in path_of(row)), -len(path_of(row)), row) for row in candidates
    ]
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def _normalize_name(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _tools_bucket() -> str:
    return os.getenv("MAINTENANCE_PLAN_TOOLS_BUCKET", "maintenance-plan")


def _tools_object() -> str:
    return os.getenv("MAINTENANCE_PLAN_TOOLS_OBJECT", "tools.json")


def _personnel_bucket() -> str:
    return os.getenv("MAINTENANCE_PLAN_PERSONNEL_BUCKET", "maintenance-plan")


def _personnel_object() -> str:
    return os.getenv("MAINTENANCE_PLAN_PERSONNEL_OBJECT", "personnel.json")


async def _read_json(bucket: str, object_name: str) -> Any:
    """下载并解析 JSON；任何失败都返回 ``None`` 并记录原因。"""

    try:
        from yuxi.storage.minio import get_minio_client

        client = get_minio_client()
        raw = await client.adownload_file(bucket, object_name)
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 外部数据不可用时降级，不阻断图谱方案
        logger.warning(f"读取处置方案外部数据失败（{bucket}/{object_name}）: {exc}")
        return None


async def _read_rows(bucket: str, object_name: str) -> list[dict[str, Any]] | None:
    """读取 ``{data: []}`` / ``{items: []}`` / 顶层数组的工器具数据。"""

    payload = await _read_json(bucket, object_name)
    if payload is None:
        return None
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if isinstance(payload, dict) and rows is None:
        rows = payload.get("items")
    if not isinstance(rows, list):
        logger.warning(f"处置方案外部数据不是数组（{bucket}/{object_name}）")
        return None
    return [row for row in rows if isinstance(row, dict)]


async def _read_person_rows(bucket: str, object_name: str) -> list[dict[str, Any]] | None:
    """读取人员数据，支持 ``{"班组":[{"班组名称":...,"人员":[...]}]}`` 的分组结构。"""

    payload = await _read_json(bucket, object_name)
    if payload is None:
        return None
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        logger.warning(f"处置方案人员数据不是对象或数组（{bucket}/{object_name}）")
        return None

    groups = payload.get("班组")
    if isinstance(groups, list):
        rows: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            team = _first_text(group, "班组名称", "名称", "班组")
            members = _first_value(group, "人员", "成员", "members")
            if not isinstance(members, list):
                continue
            for member in members:
                if isinstance(member, str) and member.strip():
                    rows.append({"姓名": member.strip(), "班组": team})
                elif isinstance(member, dict):
                    row = dict(member)
                    if team and not _first_text(row, "班组", "所属班组"):
                        row["班组"] = team
                    rows.append(row)
        return rows

    rows = payload.get("data") or payload.get("items") or payload.get("人员")
    if not isinstance(rows, list):
        logger.warning(f"处置方案人员数据缺少可用数组（{bucket}/{object_name}）")
        return None
    return [row for row in rows if isinstance(row, dict)]


def _parse_tool(row: dict[str, Any]) -> ToolRecord | None:
    name = _first_text(row, "名称", "工具名称", "物料名称", "name")
    if not name:
        return None
    quantity = _as_int(_first_value(row, "quantity", "数量", "库存数量"))
    status = _first_text(row, "状态", "status")
    in_stock = _parse_bool(_first_value(row, "是否在库", "在库", "in_stock"))
    if in_stock is None and status:
        in_stock = _status_in_stock(status)
    if in_stock is None and quantity is not None:
        in_stock = quantity > 0
    if not status and in_stock is not None:
        status = "在库" if in_stock else "不在库"
    return ToolRecord(
        name=name,
        in_stock=in_stock,
        status=status,
        location=_first_text(row, "存放地点", "存放位置", "location"),
        quantity=quantity,
    )


def _parse_person(row: dict[str, Any]) -> PersonRecord | None:
    name = _first_text(row, "姓名", "名称", "员工姓名", "name")
    if not name:
        return None
    return PersonRecord(
        name=name,
        team=_first_text(row, "班组", "所属班组", "team"),
        role=_first_text(row, "岗位角色", "角色", "职务", "role"),
    )


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _first_text(row: dict[str, Any], *keys: str) -> str:
    value = _first_value(row, *keys)
    return str(value).strip() if value is not None else ""


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def _status_in_stock(status: str) -> bool | None:
    text = str(status or "").strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return None


# ---------------------------------------------------------------------------
# 库存标注与人员抽样
# ---------------------------------------------------------------------------
def annotate_resources(
    resources: list[ResourceRequirement], tools: list[ToolRecord] | None
) -> tuple[list[ResourceRequirement], str]:
    """给资源清单附库存状态；``tools=None`` 时全部标注“库存数据未接入”。"""

    if not tools:
        return list(resources), TOOLS_DATA_MISSING_NOTE

    annotated: list[ResourceRequirement] = []
    for item in resources:
        record = _match_tool(item.name, tools)
        if record is None:
            annotated.append(replace(item, in_stock=None, stock_note="清单未匹配"))
            continue
        if record.in_stock is True:
            annotated.append(replace(item, in_stock=True, stock_note="在库"))
        elif record.in_stock is False:
            annotated.append(replace(item, in_stock=False, stock_note="不在库"))
        else:
            annotated.append(replace(item, in_stock=None, stock_note="状态未知"))
    return annotated, ""


def _match_tool(name: str, tools: list[ToolRecord]) -> ToolRecord | None:
    """先精确匹配，再按“资源名包含清单名”的最长匹配。"""

    wanted = _normalize(name)
    if not wanted:
        return None
    exact = next((record for record in tools if _normalize(record.name) == wanted), None)
    if exact is not None:
        return exact
    contained = [
        record
        for record in tools
        if (candidate := _normalize(record.name)) and (candidate in wanted or wanted in candidate)
    ]
    if not contained:
        return None
    return max(contained, key=lambda record: len(record.name))


def select_personnel(
    requirements: list[PersonnelRequirement],
    personnel: list[PersonRecord] | None,
    team: TeamInfo | None,
    *,
    rng: random.Random | None = None,
) -> PersonnelAssignment:
    """按人员需求合计人数从人员池随机抽样（不重复），优先责任班组与岗位角色。"""

    total = sum(item.count for item in requirements if item.count > 0)
    if total <= 0:
        return PersonnelAssignment(total=0)

    if personnel is None or not personnel:
        return PersonnelAssignment(total=total, note=PERSONNEL_DATA_MISSING_NOTE)

    pool = list(personnel)
    note = ""
    if team is not None and any(record.team for record in pool):
        team_pool = [record for record in pool if _normalize(record.team) == _normalize(team.name)]
        if len(team_pool) >= total:
            pool = team_pool
        elif team_pool:
            note = "责任班组人员不足，已从全体人员池抽取"

    if len(pool) < total:
        return PersonnelAssignment(total=total, note=PERSONNEL_SHORTAGE_MESSAGE, shortage=True)

    generator = rng or random.Random()
    remaining = list(pool)
    # 先按岗位角色把需求合计（同一角色可能来自多个需求节点，如通用+分支），再逐角色抽样；
    # 若逐节点写 roles，后写会覆盖前写，导致 named != total 并误杀整份合法方案。
    per_role: dict[str, int] = {}
    for requirement in sorted(requirements, key=lambda item: item.node_id):
        count = max(requirement.count, 0)
        if count:
            per_role[requirement.role] = per_role.get(requirement.role, 0) + count

    roles: dict[str, tuple[str, ...]] = {}
    for role, count in per_role.items():
        preferred = [record for record in remaining if _role_matches(record.role, role)]
        take = min(count, len(preferred))
        chosen = generator.sample(preferred, take) if take else []
        for record in chosen:
            remaining.remove(record)
        if take < count:
            extra = generator.sample(remaining, count - take)
            chosen.extend(extra)
            for record in extra:
                remaining.remove(record)
        roles[role] = tuple(record.name for record in chosen)

    return PersonnelAssignment(roles=roles, total=total, note=note)


def _role_matches(actual: str, wanted: str) -> bool:
    left = _normalize(actual)
    right = _normalize(wanted)
    if not left or not right:
        return False
    return left == right or left in right or right in left


def _normalize(text: str) -> str:
    return "".join(str(text or "").split()).casefold()


def format_personnel(assignment: PersonnelAssignment) -> list[str]:
    """把抽样结果格式化为“岗位角色：姓名、姓名”行；无名单时返回空列表。"""

    lines: list[str] = []
    for role, names in assignment.roles.items():
        if names:
            lines.append(f"{role}：{'、'.join(names)}")
    return lines
