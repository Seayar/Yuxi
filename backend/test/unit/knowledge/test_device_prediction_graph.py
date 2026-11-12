"""kg5 设备检索、设备 ID 解析与故障现象维护知识匹配单测（假图、假台账）。"""

from __future__ import annotations

import asyncio

import pytest

from yuxi.agents.toolkits.tugraph import TuGraphError
from yuxi.knowledge.device_prediction import graph as graph_module
from yuxi.knowledge.device_prediction.contracts import (
    GUIDANCE_NOT_MATCHED_NOTE,
    GUIDANCE_NO_DEVICE_TYPE_NOTE,
    GUIDANCE_QUERY_FAILED_NOTE,
    DeviceCandidate,
    PredictionQuery,
)

pytestmark = pytest.mark.unit

PHENOMENON_TOWER = {
    "id": "FP_TOWER_TILT",
    "名称": "杆塔倾斜故障",
    "故障码": "TOWER_TILT",
    "所需资源": "全站仪或RTK 1台、电子倾角仪1台\n扭矩扳手1套\n检修班组2～6人",
    "注意事项": "1. 先复核整基杆塔倾斜。\n2. 按风险等级办理停电、工作票。",
}

PHENOMENON_OVERHEAT = {
    "id": "FP_HIGH_TEMP_OVERHEAT",
    "名称": "高温过热故障",
    "故障码": "TR_THERMAL_FAULT_HIGH",
    "所需资源": "红外热像仪1台",
    "注意事项": "停电、验电、接地。",
}


class _FakeClient:
    """按查询形态返回固定记录，并记录查询参数。"""

    def __init__(
        self,
        *,
        devices=None,
        types=None,
        phenomena=None,
        global_phenomena=None,
        types_error=None,
        phenomena_error=None,
    ) -> None:
        self.devices = list(devices or [])
        self.types = list(types or [])
        self.phenomena = list(phenomena or [])
        self.global_phenomena = list(global_phenomena if global_phenomena is not None else phenomena or [])
        self.types_error = types_error
        self.phenomena_error = phenomena_error
        self.calls: list[dict] = []

    def run(self, cypher: str, **params):
        self.calls.append(params)
        if params.get("label") == "设备实例":
            return [{"n": record} for record in self.devices]
        if params.get("label") == "故障现象":
            if self.phenomena_error is not None:
                raise self.phenomena_error
            return [{"n": record} for record in self.global_phenomena]
        rel = params.get("rel")
        if rel == "具有现象":
            if self.phenomena_error is not None:
                raise self.phenomena_error
            return [{"b": record} for record in self.phenomena]
        if rel == "属于" and "device_ids" in params:
            if self.types_error is not None:
                raise self.types_error
            return self.types
        if rel == "属于":
            return [{"a": record} for record in self.devices]
        raise AssertionError(f"未预期的查询: {cypher}")


def _query(device_name=None, device_type=None, voltage_level=None, location_hint=None) -> PredictionQuery:
    return PredictionQuery(
        raw_text="测试",
        device_name=device_name,
        device_type=device_type,
        voltage_level=voltage_level,
        location_hint=location_hint,
    )


# ---------------------------------------------------------------------------
# 设备检索
# ---------------------------------------------------------------------------
def test_exact_name_ranks_first_and_duplicates_collapse():
    """同一设备被多个关键词命中时只保留最高分。"""

    client = _FakeClient(
        devices=[
            {"id": "D1", "名称": "3k18", "层级路径": "#3开闭所 > 3k18", "电压等级": "10kV", "台账ID列表": "L1、L2"},
            {"id": "D2", "名称": "3k18备用柜", "层级路径": "#3开闭所 > 3k18备用柜"},
        ]
    )

    candidates = graph_module.search_devices(client, _query(device_name="3k18"))

    assert [item.device_id for item in candidates] == ["D1", "D2"]
    assert candidates[0].score > candidates[1].score
    assert candidates[0].ledger_ids == ("L1", "L2")


def test_type_and_voltage_only_recall_by_type_edge():
    """只给设备类型时仍能召回设备，并按类型匹配加分。"""

    client = _FakeClient(
        devices=[
            {"id": "D1", "名称": "开关柜 #A", "层级路径": "变电设施 > #2变电站 > 开关柜 #A"},
        ],
        types=[{"a": {"id": "D1"}, "b": {"名称": "开关柜"}}],
    )

    candidates = graph_module.search_devices(client, _query(device_type="开关柜"))

    assert [candidate.device_id for candidate in candidates] == ["D1"]
    assert candidates[0].device_type == "开关柜"
    # 只按类型召回时基础分 1.0，类型一致再加 20.0
    assert candidates[0].score == pytest.approx(21.0)


def test_location_hint_and_voltage_add_weight():
    """位置线索命中层级路径、电压等级一致时加分。"""

    client = _FakeClient(
        devices=[
            {"id": "D1", "名称": "1号主变", "层级路径": "变电设施 > 35kV#1变电站 > 1号主变", "电压等级": "35kV"},
        ]
    )

    candidates = graph_module.search_devices(
        client, _query(device_name="1号主变", voltage_level="35kV", location_hint="35kV#1变电站")
    )

    assert candidates[0].score == pytest.approx(100.0 + 30.0 + 10.0)


def test_device_type_query_failure_keeps_candidates():
    """kg5 没有设备类型层或类型查询失败时，设备召回结果仍然可用。"""

    client = _FakeClient(
        devices=[{"id": "D1", "名称": "3k18"}],
        types_error=TuGraphError("unknown label"),
    )

    candidates = graph_module.search_devices(client, _query(device_name="3k18"))

    assert [candidate.device_id for candidate in candidates] == ["D1"]
    assert candidates[0].device_type is None


def test_empty_recall_returns_no_candidates():
    assert graph_module.search_devices(_FakeClient(), _query(device_name="不存在设备")) == []


# ---------------------------------------------------------------------------
# 设备 ID 解析
# ---------------------------------------------------------------------------
def test_ledger_id_prefers_node_ledger_ids_and_skips_placeholders():
    """设备 ID 先取 kg5 节点的台账 ID 列表，占位值跳过。"""

    candidate = DeviceCandidate("LGR#3k18", "3k18", ledger_ids=("补充数据", "bb1baf15"))

    assert asyncio.run(graph_module.resolve_device_id(candidate)) == ("bb1baf15", "台账ID列表")


def test_ledger_id_falls_back_to_exact_name_in_ledger(monkeypatch):
    """节点 ID 是占位值时按精确名称与祖先回查三区台账。"""

    from yuxi.knowledge.fault_trace import ledger as ledger_module

    class _Device:
        device_id = "be6c80285fdc6ab2e5d1dd722a117407"
        name = "主变1"

    class _Repository:
        async def find_device(self, name, *, ancestors):
            assert name == "主变1"
            assert ancestors == ["变电设施", "110kV变电站", "主变区域", "主变1间隔"]
            return _Device()

    monkeypatch.setattr(ledger_module, "ledger_repository", _Repository())
    candidate = DeviceCandidate(
        "LGR#SUP110ST1T1",
        "主变1",
        path="变电设施 > 110kV变电站 > 主变区域 > 主变1间隔 > 主变1",
        ledger_ids=("补充数据",),
    )

    assert asyncio.run(graph_module.resolve_device_id(candidate)) == (
        "be6c80285fdc6ab2e5d1dd722a117407",
        "台账接口",
    )


def test_ledger_lookup_rejects_non_exact_name(monkeypatch):
    """台账只接受精确同名行：子串匹配到的其他设备不会被当成目标设备。"""

    from yuxi.knowledge.fault_trace import ledger as ledger_module

    class _Device:
        device_id = "other-id"
        name = "主变1间隔"

    class _Repository:
        async def find_device(self, name, *, ancestors):
            return _Device()

    monkeypatch.setattr(ledger_module, "ledger_repository", _Repository())

    assert asyncio.run(graph_module.resolve_device_id(DeviceCandidate("LGR#x", "主变1"))) is None


def test_ledger_unavailable_or_missing_returns_none(monkeypatch):
    """台账不可用或查不到时返回 None，由工具层给出固定失败文案。"""

    from yuxi.knowledge.fault_trace import ledger as ledger_module

    class _Repository:
        async def find_device(self, name, *, ancestors):
            raise RuntimeError("minio down")

    monkeypatch.setattr(ledger_module, "ledger_repository", _Repository())
    assert asyncio.run(graph_module.resolve_device_id(DeviceCandidate("LGR#x", "杆塔"))) is None

    class _EmptyRepository:
        async def find_device(self, name, *, ancestors):
            return None

    monkeypatch.setattr(ledger_module, "ledger_repository", _EmptyRepository())
    assert asyncio.run(graph_module.resolve_device_id(DeviceCandidate("LGR#y", "不存在"))) is None


# ---------------------------------------------------------------------------
# 维护知识：设备类型 → 故障现象 → 故障原因 → 处置措施
# ---------------------------------------------------------------------------
def test_guidance_matches_phenomenon_by_fault_code():
    """故障码精确匹配优先，资源与注意事项取自现象属性。"""

    client = _FakeClient(phenomena=[PHENOMENON_TOWER])

    guidance = graph_module.lookup_maintenance_guidance(client, fault_type="TOWER_TILT", device_type="杆塔")

    assert guidance.fault_type == "杆塔倾斜故障"
    assert guidance.resources == ["全站仪或RTK 1台、电子倾角仪1台", "扭矩扳手1套", "检修班组2～6人"]
    assert guidance.cautions == ["1. 先复核整基杆塔倾斜。", "2. 按风险等级办理停电、工作票。"]
    assert guidance.notes == []
    # 候选范围按设备类型限定
    assert any(call.get("device_type") == "杆塔" for call in client.calls)


@pytest.mark.parametrize("fault_type", ["高温过热故障", "高温过热", "高温过热/局部放电"])
def test_guidance_matches_phenomenon_by_name_and_composite_parts(fault_type):
    """名称精确、名称包含与复合故障类型拆分都能命中同一现象。"""

    client = _FakeClient(phenomena=[PHENOMENON_OVERHEAT])

    guidance = graph_module.lookup_maintenance_guidance(client, fault_type=fault_type, device_type="变压器")

    assert guidance.fault_type == "高温过热故障"


def test_guidance_unmatched_fault_type_is_reported():
    """设备类型下没有匹配现象时返回带说明的空结果，不伪造内容。"""

    client = _FakeClient(phenomena=[PHENOMENON_OVERHEAT])

    guidance = graph_module.lookup_maintenance_guidance(client, fault_type="绝缘击穿", device_type="变压器")

    assert guidance.is_empty() is True
    assert guidance.notes == [GUIDANCE_NOT_MATCHED_NOTE]


def test_guidance_without_device_type_searches_all_phenomena():
    """设备没有类型（缺 属于 边）时退回全图现象匹配，并在说明中标注。"""

    client = _FakeClient(phenomena=[], global_phenomena=[PHENOMENON_TOWER])

    guidance = graph_module.lookup_maintenance_guidance(client, fault_type="杆塔倾斜故障", device_type="")

    assert guidance.fault_type == "杆塔倾斜故障"
    assert GUIDANCE_NO_DEVICE_TYPE_NOTE in guidance.notes
    assert any(call.get("label") == "故障现象" for call in client.calls)


def test_guidance_without_device_type_requires_exact_match():
    """没有设备类型时不做包含/二元组模糊匹配，避免跨设备类型误配；未命中也要披露类型缺失口径。"""

    client = _FakeClient(phenomena=[], global_phenomena=[PHENOMENON_OVERHEAT])

    guidance = graph_module.lookup_maintenance_guidance(client, fault_type="过热", device_type="")

    assert guidance.notes == [GUIDANCE_NOT_MATCHED_NOTE, GUIDANCE_NO_DEVICE_TYPE_NOTE]


def test_guidance_tie_break_prefers_more_specific_name():
    """同分时取名称更长的现象，不依赖图的返回顺序。"""

    generic = {"id": "FP_A", "名称": "过热", "所需资源": "", "注意事项": ""}
    specific = {"id": "FP_B", "名称": "电缆过热故障", "所需资源": "", "注意事项": ""}
    client = _FakeClient(phenomena=[generic, specific])

    guidance = graph_module.lookup_maintenance_guidance(client, fault_type="电缆过热", device_type="电缆")

    assert guidance.fault_type == "电缆过热故障"


def test_guidance_query_failure_degrades_with_note():
    """图不可用或标签不存在时降级为带失败说明的空结果，不阻断整份报告。"""

    client = _FakeClient(phenomena_error=TuGraphError("unknown label"))

    guidance = graph_module.lookup_maintenance_guidance(client, fault_type="杆塔倾斜故障", device_type="杆塔")

    assert guidance.is_empty() is True
    assert guidance.notes == [GUIDANCE_QUERY_FAILED_NOTE]


def test_guidance_empty_fault_type_is_not_matched():
    guidance = graph_module.lookup_maintenance_guidance(_FakeClient(), fault_type="  ", device_type="变压器")

    assert guidance.notes == [GUIDANCE_NOT_MATCHED_NOTE]
