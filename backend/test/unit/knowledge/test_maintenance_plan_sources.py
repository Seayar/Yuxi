"""工器具库存与人员抽样单元测试（不连接 MinIO）。"""

from __future__ import annotations

import asyncio
import random

from yuxi.knowledge.maintenance_plan.contracts import (
    PERSONNEL_DATA_MISSING_NOTE,
    DeviceCandidate,
    PersonnelRequirement,
    ResourceRequirement,
    TeamInfo,
)
from yuxi.knowledge.maintenance_plan import sources as sources_module
from yuxi.knowledge.maintenance_plan.sources import (
    MaintenanceDataRepository,
    PersonRecord,
    ToolRecord,
    _match_ledger_row,
    _parse_tool,
    _read_person_rows,
    annotate_resources,
    format_personnel,
    select_personnel,
)


def test_annotate_resources_exact_and_containment_match():
    resources = [
        ResourceRequirement("R1", "汽车吊车（25t）", 1, unit="台"),
        ResourceRequirement("R2", "绝缘隔板", 0, quantity_requirement="4 块"),
        ResourceRequirement("R3", "未登记工具", 1, unit="把"),
    ]
    tools = [
        ToolRecord("吊车", True, location="A 库"),
        ToolRecord("绝缘隔板", False),
    ]

    annotated, note = annotate_resources(resources, tools)

    assert note == ""
    assert annotated[0].in_stock is True
    assert annotated[0].stock_note == "在库"
    assert annotated[1].in_stock is False
    assert annotated[1].stock_note == "不在库"
    assert annotated[2].in_stock is None
    assert annotated[2].stock_note == "清单未匹配"


def test_annotate_resources_without_data_keeps_items_and_adds_note():
    resources = [ResourceRequirement("R1", "吊车", 1, unit="台")]

    annotated, note = annotate_resources(resources, None)

    assert annotated[0].stock_note == ""
    assert "未接入" in note


def test_select_personnel_prefers_team_and_roles():
    personnel = [
        PersonRecord("张三", "1号班组", "工作负责人"),
        PersonRecord("李四", "1号班组", "检修工"),
        PersonRecord("王五", "1号班组", "检修工"),
        PersonRecord("赵六", "2号班组", "检修工"),
    ]
    requirements = [
        PersonnelRequirement("P1", "工作负责人", 1, common=True),
        PersonnelRequirement("P2", "检修工", 2, common=True),
    ]

    assignment = select_personnel(
        requirements, personnel, TeamInfo("1号班组", "#3开闭所"), rng=random.Random(0)
    )

    assert assignment.total == 3
    assert assignment.shortage is False
    names = [name for group in assignment.roles.values() for name in group]
    assert "赵六" not in names
    assert assignment.roles["工作负责人"] == ("张三",)


def test_select_personnel_shortage_returns_explicit_flag():
    personnel = [PersonRecord("张三", "1号班组", "检修工")]
    requirements = [PersonnelRequirement("P1", "检修工", 5, common=True)]

    assignment = select_personnel(requirements, personnel, TeamInfo("1号班组", "#3开闭所"))

    assert assignment.shortage is True
    assert assignment.total == 5


def test_select_personnel_without_data_returns_requirement_only():
    requirements = [PersonnelRequirement("P1", "检修工", 2, common=True)]

    assignment = select_personnel(requirements, None, None)

    assert assignment.total == 2
    assert assignment.shortage is False
    assert assignment.note == PERSONNEL_DATA_MISSING_NOTE
    assert format_personnel(assignment) == []


def test_default_buckets_are_peers_of_fault_assets(monkeypatch):
    for key in (
        "MAINTENANCE_PLAN_TOOLS_BUCKET",
        "MAINTENANCE_PLAN_PERSONNEL_BUCKET",
        "MAINTENANCE_PLAN_TOOLS_OBJECT",
        "MAINTENANCE_PLAN_PERSONNEL_OBJECT",
    ):
        monkeypatch.delenv(key, raising=False)

    assert sources_module._tools_bucket() == "maintenance-plan"
    assert sources_module._personnel_bucket() == "maintenance-plan"
    assert sources_module._tools_object() == "tools.json"
    assert sources_module._personnel_object() == "personnel.json"


def test_parse_tool_uses_quantity_as_stock_flag():
    available = _parse_tool({"name": "吊车", "quantity": 3})
    unavailable = _parse_tool({"name": "隔板", "quantity": 0})
    explicit = _parse_tool({"name": "绝缘靴", "quantity": 0, "是否在库": "是"})

    assert available is not None and available.in_stock is True
    assert unavailable is not None and unavailable.in_stock is False
    assert explicit is not None and explicit.in_stock is True


def test_read_person_rows_flattens_team_groups(monkeypatch):
    async def fake_read_json(bucket, object_name):
        return {
            "班组": [
                {"班组名称": "1号班组", "人员": ["张三", "李四"]},
                {"班组名称": "2号班组", "人员": [{"姓名": "王五"}]},
            ]
        }

    monkeypatch.setattr(sources_module, "_read_json", fake_read_json)
    rows = asyncio.run(_read_person_rows("b", "o"))

    assert rows == [
        {"姓名": "张三", "班组": "1号班组"},
        {"姓名": "李四", "班组": "1号班组"},
        {"姓名": "王五", "班组": "2号班组"},
    ]


LEDGER_ROWS = [
    {"id": "led-1", "name": "主变1", "pid": "p1"},
    {"id": "p1", "name": "35kV#1变电站", "pid": None},
    {"id": "led-2", "name": "主变1", "pid": "p2"},
    {"id": "p2", "name": "35kV#2变电站", "pid": None},
]


def test_match_ledger_row_uses_ancestors_to_disambiguate():
    row = _match_ledger_row(LEDGER_ROWS, "主变1", ["35kV#2变电站"])

    assert row is not None
    assert row["id"] == "led-2"


def test_resolve_ledger_ids_fills_placeholder():
    device = DeviceCandidate(
        "LGR#x",
        "主变1",
        "变压器",
        "110kV",
        "变电设施 > 35kV#2变电站 > 主变1",
        ledger_ids=("补充数据",),
    )
    repository = MaintenanceDataRepository()

    async def fake_load_ledger(*, force=False):
        return LEDGER_ROWS

    repository.load_ledger = fake_load_ledger
    ids = asyncio.run(repository.resolve_ledger_ids(device))

    assert ids == ("补充数据", "led-2")


def test_resolve_ledger_ids_keeps_real_ids_without_loading_ledger():
    device = DeviceCandidate("LGR#x", "3k18", "开关柜", "10kV", "#3开闭所 > 3k18", ledger_ids=("real-id",))
    repository = MaintenanceDataRepository()

    async def fail_load(*args, **kwargs):
        raise AssertionError("真实台账 ID 不应触发台账下载")

    repository.load_ledger = fail_load
    ids = asyncio.run(repository.resolve_ledger_ids(device))

    assert ids == ("real-id",)


def test_select_personnel_aggregates_same_role_across_requirement_nodes():
    """同一岗位角色来自多个需求节点（通用+分支）时先合计再抽样，不允许后写覆盖前写。"""

    requirements = [
        PersonnelRequirement("P1", "检修工", 2, common=True),
        PersonnelRequirement("P2", "检修工", 1, common=False, applicable_branch="分支1"),
    ]
    pool = [PersonRecord(f"员工{i}", "1号班组", "检修工") for i in range(1, 6)]

    assignment = select_personnel(requirements, pool, None, rng=random.Random(7))

    assert assignment.total == 3
    assert len(assignment.roles) == 1
    assert len(assignment.roles["检修工"]) == 3
    assert sum(len(names) for names in assignment.roles.values()) == assignment.total
