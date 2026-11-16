"""工器具库存、人员班组匹配与台账补全单元测试（不连接 MinIO）。"""

from __future__ import annotations

import asyncio

from yuxi.knowledge.maintenance_plan import sources as sources_module
from yuxi.knowledge.maintenance_plan.contracts import (
    PERSONNEL_DATA_MISSING_NOTE,
    PERSONNEL_TEAM_MISSING_NOTE,
    DeviceCandidate,
    PersonnelRequirement,
    ResourceRequirement,
)
from yuxi.knowledge.maintenance_plan.sources import (
    LEADER_MISSING_TEXT,
    MaintenanceDataRepository,
    TeamRecord,
    ToolRecord,
    _match_ledger_row,
    _parse_tool,
    _read_teams,
    annotate_resources,
    assign_personnel,
    format_personnel,
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


TEAMS = [
    TeamRecord("1号站监控调度班组", ("35kV#1变电站",), ("王伟", "任敏")),
    TeamRecord("电力巡护班组", ("岭开线",), ("曹世杰", "黄洪飞")),
]


def test_assign_personnel_matches_nearest_path_node():
    requirements = [
        PersonnelRequirement("P1", "工作负责人", 1),
        PersonnelRequirement("P2", "检修工", 3),
    ]

    assignment = assign_personnel(requirements, TEAMS, "变电设施 > 35kV#1变电站 > 10kV电压等级区域 > 主变1")

    assert assignment.team == "1号站监控调度班组"
    assert assignment.leader == "王伟"
    assert assignment.total == 4
    assert assignment.note == ""


def test_assign_personnel_prefers_deeper_path_node():
    """多个责任区域都能匹配时，取层级路径上更近的节点对应班组。"""

    teams = [
        TeamRecord("根区域班组", ("变电设施",), ("甲",)),
        TeamRecord("1号站监控调度班组", ("35kV#1变电站",), ("王伟",)),
    ]

    assignment = assign_personnel([], teams, "变电设施 > 35kV#1变电站 > 主变1")

    assert assignment.team == "1号站监控调度班组"
    assert assignment.leader == "王伟"


def test_assign_personnel_falls_back_to_patrol_team():
    assignment = assign_personnel(
        [PersonnelRequirement("P1", "检修工", 2)], TEAMS, "变电设施 > 35kV#9变电站 > 未知设备"
    )

    assert assignment.team == "电力巡护班组"
    assert assignment.leader == "曹世杰"
    assert assignment.total == 2
    assert assignment.note == ""


def test_assign_personnel_keeps_requirement_total_for_small_team():
    """班组人数小于所需人数时不再报“人员数据不足”，所需人数仍按人员需求合计。"""

    assignment = assign_personnel(
        [PersonnelRequirement("P1", "检修工", 16)], TEAMS, "变电设施 > 35kV#1变电站 > 主变1"
    )

    assert assignment.total == 16
    assert assignment.leader == "王伟"
    assert assignment.note == ""


def test_assign_personnel_without_minio_data_returns_fallback_team():
    assignment = assign_personnel(
        [PersonnelRequirement("P1", "检修工", 2)], None, "变电设施 > 35kV#1变电站 > 主变1"
    )

    assert assignment.team == "电力巡护班组"
    assert assignment.leader == ""
    assert assignment.total == 2
    assert assignment.note == PERSONNEL_DATA_MISSING_NOTE
    assert format_personnel(assignment) == [f"工作负责人：{LEADER_MISSING_TEXT}"]


def test_assign_personnel_without_usable_members_marks_note():
    teams = [TeamRecord("1号站监控调度班组", ("35kV#1变电站",), ())]

    assignment = assign_personnel(
        [PersonnelRequirement("P1", "检修工", 4)], teams, "变电设施 > 35kV#1变电站 > 主变1"
    )

    assert assignment.team == "1号站监控调度班组"
    assert assignment.leader == ""
    assert assignment.note == PERSONNEL_TEAM_MISSING_NOTE
    assert format_personnel(assignment) == [f"工作负责人：{LEADER_MISSING_TEXT}"]


def test_assign_personnel_falls_back_when_patrol_team_absent():
    teams = [TeamRecord("1号站监控调度班组", ("35kV#1变电站",), ("王伟",))]

    assignment = assign_personnel([], teams, "变电设施 > 35kV#9变电站 > 未知设备")

    assert assignment.team == "电力巡护班组"
    assert assignment.leader == ""
    assert assignment.note == PERSONNEL_TEAM_MISSING_NOTE


def test_read_teams_parses_region_and_members(monkeypatch):
    async def fake_read_json(bucket, object_name):
        return {
            "班组": [
                {"班组名称": "1号站监控调度班组", "责任区域": ["35kV#1变电站"], "人员": ["王伟", "任敏"]},
                {"班组名称": "", "责任区域": [], "人员": []},
            ]
        }

    monkeypatch.setattr(sources_module, "_read_json", fake_read_json)
    teams = asyncio.run(_read_teams("b", "o"))

    assert teams == [TeamRecord("1号站监控调度班组", ("35kV#1变电站",), ("王伟", "任敏"))]


def test_read_teams_rejects_payload_without_team_array(monkeypatch):
    async def fake_read_json(bucket, object_name):
        return {"data": [{"姓名": "张三"}]}

    monkeypatch.setattr(sources_module, "_read_json", fake_read_json)

    assert asyncio.run(_read_teams("b", "o")) is None


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
