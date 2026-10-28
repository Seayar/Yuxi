"""kg3 适配器的查询编排单元测试（替身客户端，不依赖真实 TuGraph）。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.fault_trace.graph_kg3 import (
    collect_kg3_evidence,
    find_devices,
    resolve_device_type,
)

pytestmark = pytest.mark.unit

DEVICE = {
    "名称": "3B-7Y",
    "id": "LGR#3b7y",
    "台账ID列表": "3b7y",
    "电压等级": "10kV",
    "设备分类": "中压线路",
}
TYPE = {"名称": "架空线路", "id": "DEV#架空线路"}
MODE = {"名称": "异物搭挂", "id": "FM-WR-01"}
PHENOMENON = {"名称": "树木倒伏枝干压覆导线", "id": "PH-04"}
CAUSE = {"名称": "通道内树木未及时砍伐或修剪，遇大风倒伏压线", "id": "CA-04"}
TRIGGER = {"名称": "泄漏电流超1mA", "id": "TC-R03", "表达式": "leakage_current > 1"}
MEASURE = {"名称": "修剪通道内树木", "id": "MM-03"}
CONSEQUENCE = {"名称": "相间短路", "id": "FC-01"}
STATION = {"名称": "35kV#3变电站", "id": "LGR#station", "电压等级": "35kV"}


class _FakeClient:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.queries: list[tuple[str, dict]] = []

    def run(self, cypher: str, **params):
        self.queries.append((cypher, params))
        return self._responder(cypher, params)


def _base_responder(cypher: str, params: dict):
    rel = params.get("rel")
    if "coalesce(n.名称, '') = $name" in cypher:
        return [{"n": DEVICE}] if params.get("name") == "3B-7Y" else []
    if "coalesce(t.名称, '') = $name" in cypher:
        return [{"t": TYPE}] if params.get("name") == "架空线路" else []
    if "CONTAINS $kw" in cypher and params.get("label") == "设备实例":
        return [{"n": DEVICE}, {"n": STATION}] if params.get("kw") in {"3B", "变电站"} else []
    if rel == "属于" and params.get("src") == "设备实例":
        return []
    if rel == "属于" and params.get("src") == "设备类型":
        return [{"b": {"名称": "架空线路", "id": "DEV#架空线路"}}]
    if rel == "具有故障模式":
        return [{"a": TYPE, "rel_type": "具有故障模式", "b": MODE}]
    if rel == "具有现象":
        return [{"a": MODE, "rel_type": "具有现象", "b": PHENOMENON}]
    if rel == "具有原因" and params.get("src") == "故障现象":
        return []
    if rel == "具有原因":
        return [{"a": MODE, "rel_type": "具有原因", "b": CAUSE}]
    if rel == "具有触发条件":
        return [{"a": CAUSE, "rel_type": "具有触发条件", "b": TRIGGER}]
    if rel == "具有措施":
        return [{"a": CAUSE, "rel_type": "具有措施", "b": MEASURE}]
    if rel == "可能导致":
        return [{"a": CAUSE, "rel_type": "可能导致", "b": CONSEQUENCE}]
    if rel == "电气连接":
        return [{"a": STATION, "rel_type": "电气连接", "b": DEVICE}]
    return []


def test_find_devices_prefers_exact_name():
    client = _FakeClient(_base_responder)

    devices = find_devices(client, ["3B-7Y"])

    assert [device["name"] for device in devices] == ["3B-7Y"]
    assert any("coalesce(n.名称, '') = $name" in cypher for cypher, _ in client.queries)


def test_find_devices_falls_back_to_contains():
    client = _FakeClient(_base_responder)

    devices = find_devices(client, ["3B"])

    assert {device["name"] for device in devices} == {"3B-7Y", "35kV#3变电站"}


def test_resolve_device_type_maps_device_class_when_edge_missing():
    client = _FakeClient(_base_responder)
    device = {
        "id": "LGR#3b7y",
        "name": "3B-7Y",
        "type": "device",
        "properties": DEVICE,
    }

    name, node = resolve_device_type(client, device)

    assert name == "架空线路"
    assert node == {
        "id": "DEV#架空线路",
        "name": "架空线路",
        "type": "device_type",
        "labels": ["设备类型"],
        "properties": {"名称": "架空线路", "id": "DEV#架空线路"},
    }


def test_resolve_device_type_returns_none_for_unmapped_station_class():
    client = _FakeClient(_base_responder)
    device = {
        "id": "LGR#station",
        "name": "35kV#3变电站",
        "type": "device",
        "properties": {"名称": "35kV#3变电站", "id": "LGR#station", "设备分类": "变电站"},
    }

    assert resolve_device_type(client, device) == (None, None)


def test_collect_kg3_evidence_marks_mode_level_cause_links():
    client = _FakeClient(_base_responder)
    device = {"id": "LGR#3b7y", "name": "3B-7Y", "type": "device", "properties": DEVICE}
    type_node = {"id": "DEV#架空线路", "name": "架空线路", "type": "device_type", "properties": {"名称": "架空线路"}}

    evidence = collect_kg3_evidence(client, devices=[device], device_type=type_node)

    assert evidence.cause_links == "fault_mode"
    assert [node["name"] for node in evidence.nodes_with_label("fault_mode")] == ["异物搭挂"]
    assert [node["name"] for node in evidence.nodes_with_label("phenomenon")] == ["树木倒伏枝干压覆导线"]
    assert [node["name"] for node in evidence.nodes_with_label("cause")] == [
        "通道内树木未及时砍伐或修剪，遇大风倒伏压线"
    ]
    assert [node["name"] for node in evidence.nodes_with_label("trigger")] == ["泄漏电流超1mA"]
    assert [node["name"] for node in evidence.nodes_with_label("consequence")] == ["相间短路"]
    edge_types = {edge["type"] for edge in evidence.edges}
    assert {
        "belongs_to_device_type",
        "has_fault_mode",
        "has_phenomenon",
        "has_cause",
        "has_trigger_condition",
        "has_measure",
        "may_lead_to_consequence",
        "electrically_connected_to",
    } <= edge_types


TRANSFORMER = {"名称": "变压器", "id": "DEV#变压器"}
TR_PHENOMENON = {"名称": "冒烟", "id": "PHEN-TR-SMOKE"}
OTHER_PHENOMENON = {"名称": "冒烟", "id": "PHEN-SW-SMOKE"}
TR_CAUSE = {"名称": "绕组匝间短路与局部放电", "id": "CAUSE-TR-C4"}
TR_TRIGGER = {
    "名称": "C4-1",
    "id": "COND-TR-C4-1",
    "表达式": "winding_local_temp > 145",
    "判据口径": "绕组局部温度超过145℃",
}


def _type_level_responder(cypher: str, params: dict):
    rel = params.get("rel")
    if rel == "具有故障模式":
        return []
    if rel == "具有现象":
        if "DEV#变压器" in (params.get("ids") or []):
            return [{"a": TRANSFORMER, "rel_type": "具有现象", "b": TR_PHENOMENON}]
        return []
    if rel == "具有原因":
        return [{"a": TR_PHENOMENON, "rel_type": "具有原因", "b": TR_CAUSE}]
    if rel == "具有触发条件":
        return [{"a": TR_CAUSE, "rel_type": "具有触发条件", "b": TR_TRIGGER}]
    return []


def test_collect_evidence_supports_type_level_phenomena_without_mode():
    """新数据没有故障模式层，设备类型直接挂现象；原因按现象 id 精确过滤。"""

    client = _FakeClient(_type_level_responder)
    device = {"id": "LGR#tr", "name": "#1主变压器", "type": "device", "properties": {"名称": "#1主变压器"}}
    type_node = {"id": "DEV#变压器", "name": "变压器", "type": "device_type", "properties": {"名称": "变压器"}}

    evidence = collect_kg3_evidence(client, devices=[device], device_type=type_node)

    assert evidence.nodes_with_label("fault_mode") == []
    assert [node["name"] for node in evidence.nodes_with_label("phenomenon")] == ["冒烟"]
    assert [node["name"] for node in evidence.nodes_with_label("cause")] == ["绕组匝间短路与局部放电"]
    assert [node["name"] for node in evidence.nodes_with_label("trigger")] == ["绕组局部温度超过145℃"]
    assert evidence.cause_links == "fault_phenomenon"
    cause_query = next(
        cypher
        for cypher, params in client.queries
        if params.get("rel") == "具有原因" and params.get("src") == "故障现象"
    )
    assert "coalesce(a.id, '') IN $ids" in cause_query
    assert "IN $names" not in cause_query


FOREIGN_TYPE = {"名称": "电流互感器", "id": "DEV#电流互感器"}


class _GraphFakeClient:
    """按 ``id`` / ``台账ID列表`` 参数真的过滤，而不是按 cypher 文本猜答案。

    ``belongs_to`` 是 ``{设备 id: 类型节点}``，``ledger_belongs_to`` 是
    ``{台账ID列表: 类型节点}``；查询里出现哪个参数就用哪个键过滤。
    """

    def __init__(self, *, belongs_to=None, ledger_belongs_to=None) -> None:
        self.belongs_to = belongs_to or {}
        self.ledger_belongs_to = ledger_belongs_to or {}
        self.queries: list[tuple[str, dict]] = []

    def run(self, cypher: str, **params):
        self.queries.append((cypher, params))
        if params.get("rel") != "属于":
            return []
        if "$device_id" in cypher and params.get("device_id") in self.belongs_to:
            return [{"b": self.belongs_to[params["device_id"]]}]
        if "$ledger_id" in cypher and params.get("ledger_id") in self.ledger_belongs_to:
            return [{"b": self.ledger_belongs_to[params["ledger_id"]]}]
        return []


def test_resolve_device_type_filters_other_devices_without_ledger_id():
    """真实过滤语义：目标设备没有台账 ID 时，他设备的“属于”边不会被返回。"""

    target = {"id": "LGR#target", "name": "目标设备", "type": "device", "properties": {"名称": "目标设备"}}
    client = _GraphFakeClient(
        # 只有“另一个无台账 ID 的设备”有类型边：查询若把空台账 ID 拼进条件就会命中它。
        belongs_to={"LGR#other": FOREIGN_TYPE},
        ledger_belongs_to={"": FOREIGN_TYPE},
    )

    assert resolve_device_type(client, target) == (None, None)
    # 参数总是成对传入；关键是 cypher 里不能出现台账条件，否则空值会命中他设备。
    assert all("台账ID列表" not in cypher for cypher, _params in client.queries)


def test_resolve_device_type_skips_edge_lookup_without_any_identifier():
    """设备既无 id 也无台账 ID 时没有可靠匹配键，不做等值查询。"""

    client = _GraphFakeClient()
    device = {"name": "无标识设备", "type": "device", "properties": {"设备分类": "中压线路"}}

    assert resolve_device_type(client, device) == (None, None)
    assert all(params.get("rel") != "属于" for _, params in client.queries)


def test_resolve_device_type_falls_back_to_ledger_id_edge():
    """设备 id 没有类型边时仍可按台账 ID 命中，并使用稳定排序。"""

    target = {"id": "LGR#x", "name": "x", "type": "device", "properties": {"台账ID列表": "3b7y"}}
    client = _GraphFakeClient(
        belongs_to={},
        ledger_belongs_to={"3b7y": {"名称": "架空线路", "id": "DEV#架空线路"}},
    )

    name, node = resolve_device_type(client, target)

    assert name == "架空线路"
    assert node["id"] == "DEV#架空线路"
    belongs_queries = [cypher for cypher, params in client.queries if params.get("rel") == "属于"]
    assert all("ORDER BY" in cypher for cypher in belongs_queries)
    # 空台账 ID 只在设备没有台账 ID 时才会被排除；这里有值，两个条件都应出现在同一条查询里。
    assert all("$device_id" in cypher and "$ledger_id" in cypher for cypher in belongs_queries)
