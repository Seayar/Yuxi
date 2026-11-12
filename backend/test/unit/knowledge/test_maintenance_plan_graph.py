"""kg4 图检索单元测试：全部使用假客户端，不连接图谱/数据库/大模型。"""

from __future__ import annotations

import pytest
from yuxi.agents.toolkits.tugraph import TuGraphError
from yuxi.knowledge.maintenance_plan.contracts import (
    BranchSelection,
    DeviceCandidate,
    PlanQuery,
    Scenario,
)
from yuxi.knowledge.maintenance_plan.graph_plan import (
    collect_scene_evidence,
    find_scenes,
    plan_graph_ready,
    rank_scenes,
    search_devices,
)

pytestmark = pytest.mark.unit


DEVICE_3K18 = {
    "id": "LGR#3k18",
    "名称": "3k18",
    "台账ID列表": "L1、L2",
    "层级路径": "变电设施 > 35kV#3变电站 > 10kV电压等级区域 > #3开闭所 > 3k18",
    "电压等级": "10kV",
    "台账分类": "辅柜",
    "设备分类": "开关柜",
}
DEVICE_UNTYPED = {
    "id": "LGR#area",
    "名称": "3k18区域",
    "台账ID列表": "L9",
    "层级路径": "变电设施 > 35kV#3变电站 > 3k18区域",
    "电压等级": "10kV",
}
SCENE_SWITCHGEAR = {
    "id": "SCENE_A2",
    "名称": "开关柜整柜更换（柜体锈蚀、五防失效）",
    "说明": "馈线一_912 柜体锈蚀、五防失效，需整柜更换的 A 级检修方案。",
    "适用检修等级": "A级",
    "分支判定维度": "布置位置",
    "分支1条件": "站内开关室",
    "分支2条件": "箱变内（如 #30箱变01出线柜）",
}


class FakeClient:
    """按参数分派 kg4 查询；只实现本测试用到的语句形态。"""

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict]] = []

    def run(self, cypher: str, **params):
        self.queries.append((cypher, params))
        if "CONTAINS $kw" in cypher:
            return [{"n": DEVICE_3K18}, {"n": DEVICE_UNTYPED}]
        rel = params.get("rel")
        if rel == "属于":
            if "IN $device_ids" in cypher:
                return [{"a": {"id": "LGR#3k18"}, "b": {"名称": "开关柜"}}]
            return [{"a": DEVICE_3K18}]
        if rel == "包含作业":
            return [{"b": SCENE_SWITCHGEAR}]
        if rel == "位于":
            return [{"b": {"名称": "#3开闭所", "电压等级": "10kV"}}]
        if rel == "管辖":
            return [{"a": {"名称": "1号站电力巡护班组", "组织类型": "班组", "来源": "达梦"}, "b": {"名称": "#3开闭所"}}]
        if rel == "包含步骤":
            return [
                {"b": {"id": "S2", "步骤序号": 2, "名称": "第二步", "操作内容": "拆柜"}},
                {"b": {"id": "S1", "步骤序号": 1, "名称": "第一步", "操作内容": "停电验电"}},
            ]
        if rel == "需要安全措施":
            return [
                {"b": {"id": "SAFE1", "名称": "个人防护", "具体内容": "安全帽", "是否通用": "是", "适用分支值": ""}},
                {"b": {"id": "SAFE2", "名称": "分支1措施", "具体内容": "站内围栏", "是否通用": "否", "适用分支值": "分支1"}},
                {"b": {"id": "SAFE3", "名称": "分支2措施", "具体内容": "箱变围栏", "是否通用": "否", "适用分支值": "分支2"}},
            ]
        if rel == "需要资源":
            return [
                {"b": {"id": "RES1", "名称": "吊车", "数量": 2, "计量单位": "台", "是否通用": "是", "适用分支值": ""}},
                {"b": {"id": "RES2", "名称": "绝缘隔板", "数量": 0, "数量要求": "4 块／1 套", "是否通用": "否", "适用分支值": "分支1"}},
                {"b": {"id": "RES3", "名称": "箱变专用工具", "数量": 1, "计量单位": "套", "是否通用": "否", "适用分支值": "分支2"}},
            ]
        if rel == "需要人员":
            return [
                {"b": {"id": "PER1", "岗位角色": "工作负责人", "人数": 1, "是否通用": "是", "适用分支值": ""}},
                {"b": {"id": "PER2", "岗位角色": "检修工", "人数": 3, "是否通用": "是", "适用分支值": ""}},
                {"b": {"id": "PER3", "岗位角色": "专责监护人", "人数": 2, "是否通用": "否", "适用分支值": "分支1"}},
                {"b": {"id": "PER4", "岗位角色": "箱变监护", "人数": 1, "是否通用": "否", "适用分支值": "分支2"}},
            ]
        return []


def test_search_devices_resolves_type_and_drops_untyped_nodes():
    client = FakeClient()
    candidates = search_devices(client, PlanQuery(raw_text="3k18 开关柜整柜更换", device_name="3k18"))

    assert [item.name for item in candidates] == ["3k18"]
    assert candidates[0].device_type == "开关柜"
    assert candidates[0].ledger_ids == ("L1", "L2")


def test_search_devices_by_device_type_uses_a_key_result():
    client = FakeClient()
    candidates = search_devices(client, PlanQuery(raw_text="10kV 开关柜整柜更换", device_type="开关柜"))

    assert [item.name for item in candidates] == ["3k18"]
    assert candidates[0].device_type == "开关柜"


def test_search_devices_falls_back_to_line_class_when_type_edge_missing():
    class LineClient:
        def run(self, cypher, **params):
            if "CONTAINS $kw" in cypher:
                return [
                    {
                        "n": {
                            "id": "LGR#3B-6K",
                            "名称": "3B-6K",
                            "台账ID列表": "1001feeder35000012",
                            "层级路径": "变电设施 > 35kV#1变电站 > 3B-6K",
                            "台账分类": "中压线路",
                        }
                    }
                ]
            return []

    candidates = search_devices(LineClient(), PlanQuery(raw_text="3B-6K 带电作业", device_name="3B-6K"))

    assert [item.name for item in candidates] == ["3B-6K"]
    assert candidates[0].device_type == "架空线路"


def test_find_scenes_reads_has_scene_relation():
    device = DeviceCandidate("LGR#3k18", "3k18", "开关柜", "10kV", "#3开闭所 > 3k18", ledger_ids=("L1",))
    scenes = find_scenes(FakeClient(), device)

    assert len(scenes) == 1
    assert scenes[0].scene_id == "SCENE_A2"
    assert scenes[0].branch_dimension == "布置位置"
    assert scenes[0].branch1_condition == "站内开关室"


def test_rank_scenes_prefers_name_match_and_filters_level():
    scenes = [
        Scenario("A", "开关柜整柜更换（柜体锈蚀、五防失效）", level="A级", branch_dimension="布置位置"),
        Scenario("B", "开关柜柜内检修（活门挡板处理）", level="B级", branch_dimension="柜型结构"),
    ]
    ranked = rank_scenes(scenes, PlanQuery(raw_text="开关柜整柜更换，A级检修"))

    assert ranked[0].scene_id == "A"
    assert ranked[0].score > ranked[1].score
    assert all(scene.level == "A级" for scene in rank_scenes(scenes, PlanQuery(raw_text="整柜更换", maintenance_level="A")))


def test_collect_scene_evidence_filters_branch_and_sorts_steps():
    device = DeviceCandidate(
        "LGR#3k18",
        "3k18",
        "开关柜",
        "10kV",
        "#3开闭所 > 3k18",
        ledger_ids=("L1", "L2"),
    )
    scenario = Scenario(
        "SCENE_A2",
        "开关柜整柜更换",
        level="A级",
        branch_dimension="布置位置",
        branch1_condition="站内开关室",
        branch2_condition="箱变内",
    )
    branch = BranchSelection("布置位置", "站内开关室", "分支1", source="redis", ledger_id="L1")

    evidence = collect_scene_evidence(FakeClient(), PlanQuery(), device, scenario, branch)

    assert [step.sequence for step in evidence.steps] == [1, 2]
    assert [step.content for step in evidence.steps] == ["停电验电", "拆柜"]
    assert [item.node_id for item in evidence.safety] == ["SAFE1", "SAFE2"]
    assert [item.node_id for item in evidence.resources] == ["RES1", "RES2"]
    assert evidence.resources[1].quantity == 0
    assert evidence.resources[1].quantity_requirement == "4 块／1 套"
    assert sum(item.count for item in evidence.personnel) == 6
    assert [item.node_id for item in evidence.personnel] == ["PER1", "PER2", "PER3"]
    assert evidence.team is not None
    assert evidence.team.name == "1号站电力巡护班组"
    assert evidence.device.voltage_level == "10kV"


def test_collect_scene_evidence_without_branch_keeps_only_common_items():
    device = DeviceCandidate("LGR#3k18", "3k18", "开关柜", "10kV", "#3开闭所 > 3k18", ledger_ids=("L1",))
    scenario = Scenario("SCENE_A2", "开关柜整柜更换", level="A级", branch_dimension="布置位置")

    evidence = collect_scene_evidence(FakeClient(), PlanQuery(), device, scenario, None)

    assert [item.node_id for item in evidence.safety] == ["SAFE1"]
    assert [item.node_id for item in evidence.resources] == ["RES1"]
    assert sum(item.count for item in evidence.personnel) == 4


def test_plan_graph_ready_returns_false_on_query_error():
    class BrokenClient:
        def run(self, cypher, **params):
            raise TuGraphError("boom")

    assert plan_graph_ready(BrokenClient()) is False
