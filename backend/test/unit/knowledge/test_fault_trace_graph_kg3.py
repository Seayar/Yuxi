"""故障图谱适配器的查询编排单元测试（替身客户端，不依赖真实 TuGraph）。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.device_index import reset_device_name_index
from yuxi.knowledge.fault_trace.ledger import LedgerDevice
from yuxi.knowledge.fault_trace.graph_kg3 import (
    collect_kg3_evidence,
    find_device_by_id,
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
PHENOMENON = {"名称": "树木倒伏枝干压覆导线", "id": "PH-04"}
CAUSE = {"名称": "通道内树木未及时砍伐或修剪，遇大风倒伏压线", "id": "CA-04"}
TRIGGER = {"名称": "泄漏电流超1mA", "id": "TC-R03", "表达式": "leakage_current > 1"}
MEASURE = {"名称": "修剪通道内树木", "id": "MM-03"}
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
    if rel == "具有现象":
        return [{"a": TYPE, "rel_type": "具有现象", "b": PHENOMENON}]
    if rel == "具有原因":
        return [{"a": PHENOMENON, "rel_type": "具有原因", "b": CAUSE}]
    if rel == "具有触发条件":
        return [{"a": CAUSE, "rel_type": "具有触发条件", "b": TRIGGER}]
    if rel == "具有措施":
        return [{"a": CAUSE, "rel_type": "具有措施", "b": MEASURE}]
    if rel == "电气连接":
        return [{"a": STATION, "rel_type": "电气连接", "b": DEVICE}]
    return []


def test_find_devices_prefers_exact_name():
    client = _FakeClient(_base_responder)

    devices = find_devices(client, ["3B-7Y"])

    assert [device["name"] for device in devices] == ["3B-7Y"]
    assert any("coalesce(n.名称, '') = $name" in cypher for cypher, _ in client.queries)


def test_find_device_by_id_returns_selected_graph_node():
    def responder(cypher: str, params: dict):
        if "coalesce(n.id, '') = $device_id" in cypher:
            return [{"n": DEVICE}] if params.get("device_id") == DEVICE["id"] else []
        return []

    device = find_device_by_id(_FakeClient(responder), DEVICE["id"])

    assert device is not None
    assert device["id"] == DEVICE["id"]
    assert device["name"] == "3B-7Y"


def test_find_devices_falls_back_to_contains():
    client = _FakeClient(_base_responder)

    devices = find_devices(client, ["3B"])

    assert {device["name"] for device in devices} == {"3B-7Y", "35kV#3变电站"}


def test_find_devices_strips_voltage_prefix():
    """用户与意图识别常带电压前缀：故障图设备名不带前缀，查询要先展开。"""

    def responder(cypher: str, params: dict):
        if "coalesce(n.名称, '') = $name" in cypher:
            return [{"n": {**DEVICE, "名称": "银开线"}}] if params.get("name") == "银开线" else []
        return _base_responder(cypher, params)

    devices = find_devices(_FakeClient(responder), ["110kV银开线"])

    assert [device["name"] for device in devices] == ["银开线"]


def test_find_devices_expands_ordinal_name():
    """“1号主变”要能命中故障图的 ``#1主变…`` 写法。"""

    def responder(cypher: str, params: dict):
        if "CONTAINS $kw" in cypher and params.get("label") == "设备实例":
            return [{"n": {**DEVICE, "名称": "#1主变间隔"}}] if params.get("kw") == "#1主变" else []
        return _base_responder(cypher, params)

    devices = find_devices(_FakeClient(responder), ["1号主变"])

    assert [device["name"] for device in devices] == ["#1主变间隔"]


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


def test_collect_kg3_evidence_reads_device_type_phenomenon_cause_chain():
    """适配器只读 设备类型→现象→原因→触发条件/措施，不查询已移除的模式/后果。"""

    client = _FakeClient(_base_responder)
    device = {"id": "LGR#3b7y", "name": "3B-7Y", "type": "device", "properties": DEVICE}
    type_node = {"id": "DEV#架空线路", "name": "架空线路", "type": "device_type", "properties": {"名称": "架空线路"}}

    evidence = collect_kg3_evidence(client, devices=[device], device_type=type_node)

    assert evidence.cause_links == "fault_phenomenon"
    assert evidence.nodes_with_label("fault_mode") == []
    assert [node["name"] for node in evidence.nodes_with_label("phenomenon")] == ["树木倒伏枝干压覆导线"]
    assert [node["name"] for node in evidence.nodes_with_label("cause")] == [
        "通道内树木未及时砍伐或修剪，遇大风倒伏压线"
    ]
    assert [node["name"] for node in evidence.nodes_with_label("trigger")] == ["泄漏电流超1mA"]
    assert evidence.nodes_with_label("consequence") == []
    edge_types = {edge["type"] for edge in evidence.edges}
    assert {
        "belongs_to_device_type",
        "has_phenomenon",
        "has_cause",
        "has_trigger_condition",
        "has_measure",
        "electrically_connected_to",
    } <= edge_types
    assert "has_fault_mode" not in edge_types
    assert "may_lead_to_consequence" not in edge_types
    assert not any(params.get("rel") in {"具有故障模式", "可能导致"} for _, params in client.queries)


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
    if rel == "具有现象":
        if "DEV#变压器" in (params.get("ids") or []):
            return [{"a": TRANSFORMER, "rel_type": "具有现象", "b": TR_PHENOMENON}]
        return []
    if rel == "具有原因":
        return [{"a": TR_PHENOMENON, "rel_type": "具有原因", "b": TR_CAUSE}]
    if rel == "具有触发条件":
        return [{"a": TR_CAUSE, "rel_type": "具有触发条件", "b": TR_TRIGGER}]
    return []


def test_collect_evidence_reads_type_level_phenomena_and_causes():
    """现象直接挂设备类型；原因按当前类型现象节点 id 精确过滤。"""

    client = _FakeClient(_type_level_responder)
    device = {"id": "LGR#tr", "name": "#1主变压器", "type": "device", "properties": {"名称": "#1主变压器"}}
    type_node = {"id": "DEV#变压器", "name": "变压器", "type": "device_type", "properties": {"名称": "变压器"}}

    evidence = collect_kg3_evidence(client, devices=[device], device_type=type_node)

    assert evidence.nodes_with_label("fault_mode") == []
    assert [node["name"] for node in evidence.nodes_with_label("phenomenon")] == ["冒烟"]
    assert [node["name"] for node in evidence.nodes_with_label("cause")] == ["绕组匝间短路与局部放电"]
    assert [node["name"] for node in evidence.nodes_with_label("trigger")] == ["绕组局部温度超过145℃"]
    assert evidence.cause_links == "fault_phenomenon"
    phenomenon_params = [params for _, params in client.queries if params.get("rel") == "具有现象"]
    assert phenomenon_params[0]["ids"] == ["DEV#变压器"]
    cause_params = [
        params for _, params in client.queries if params.get("rel") == "具有原因" and params.get("src") == "故障现象"
    ]
    assert cause_params[0]["ids"] == ["PHEN-TR-SMOKE"]
    assert OTHER_PHENOMENON["id"] not in cause_params[0]["ids"]
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


def test_find_devices_falls_back_to_name_index():
    """名称/包含/词元都没命中时用设备名索引召回；索引只返回图谱里已存在的设备。"""

    reset_device_name_index()
    kiosk = {"名称": "#1开闭所", "id": "LGR#kbs1", "设备分类": "开闭所", "层级路径": "变电设施 > #1开闭所"}

    class _IndexClient:
        def run(self, cypher, **params):
            if "= $name" in cypher or "CONTAINS $kw" in cypher:
                return []
            if params.get("ids") or params.get("device_ids"):
                return [{"n": kiosk}]
            if "RETURN n LIMIT" in cypher:
                return [{"n": kiosk}]  # 设备名索引构建
            return []

    devices = find_devices(_IndexClient(), ["一号开闭所"], graph_key="test-ft-index")

    assert [device["name"] for device in devices] == ["#1开闭所"]


def test_exact_hit_keeps_similar_ledger_candidate():
    """精确设备存在时，台账近似设备也要进入故障设备确认候选。"""

    similar = {
        "名称": "3B-7Y备用线路",
        "id": "LGR#3b7y-backup",
        "设备分类": "中压线路",
        "层级路径": "输电设施 > 3B-7Y备用线路",
    }

    def responder(cypher: str, params: dict):
        if "names" in params:
            return [{"n": similar}]
        if "= $name" in cypher or "CONTAINS $kw" in cypher:
            return [{"n": DEVICE}]
        return []

    devices = find_devices(
        _FakeClient(responder),
        ["3B-7Y"],
        ledger_candidates=[
            LedgerDevice(
                device_id="ledger-backup",
                name="3B-7Y备用线路",
                device_type="中压线路",
                voltage_level="10kV",
                path=("输电设施",),
                score=90.0,
            )
        ],
    )

    assert [device["name"] for device in devices] == ["3B-7Y", "3B-7Y备用线路"]
    assert devices[1]["properties"]["台账ID列表"] == "ledger-backup"


def test_ledger_candidate_can_recover_device_from_raw_text():
    """故障意图字段缺失时，台账仍可按原始描述召回并映射图谱设备。"""

    line = {
        "名称": "银开线",
        "id": "LGR#SUPLINEYK",
        "设备分类": "输电线路",
        "层级路径": "输电设施 > 银开线",
    }

    def responder(cypher: str, params: dict):
        if "names" in params:
            return [{"n": line}]
        return []

    devices = find_devices(
        _FakeClient(responder),
        ["110kV银开线故障"],
        ledger_candidates=[
            LedgerDevice(
                "ledger-line",
                "银开线",
                "输电线路",
                "110kV",
                ("输电设施",),
                score=92.0,
            )
        ],
    )

    assert [device["name"] for device in devices] == ["银开线"]
    assert devices[0]["properties"]["台账ID列表"] == "ledger-line"


def test_find_devices_returns_empty_without_graph_key_when_all_stages_miss():
    """没有图配置时不构建索引；所有召回阶段都未命中就返回空，由调用方给固定文案。"""

    class _EmptyClient:
        def run(self, cypher, **params):
            return []

    assert find_devices(_EmptyClient(), ["完全无关的设备"]) == []


def test_resolve_device_type_prefers_device_id_over_shared_ledger():
    """设备 id 命中类型边时忽略台账 ID：补充数据设备共用占位台账 ID，不能串到别的类型。"""

    target = {
        "id": "LGR#SUPLINEYK",
        "name": "银开线",
        "type": "device",
        "properties": {"台账ID列表": "补充数据", "设备分类": "输电线路"},
    }
    client = _GraphFakeClient(
        belongs_to={"LGR#SUPLINEYK": {"名称": "架空线路", "id": "DEV#架空线路"}},
        # 同占位台账 ID 下还有别的设备；按台账 ID 等值查询会把这些类型边一起返回。
        ledger_belongs_to={"补充数据": {"名称": "变压器", "id": "DEV#变压器"}},
    )

    name, node = resolve_device_type(client, target)

    assert name == "架空线路"
    assert node["id"] == "DEV#架空线路"
    belongs_queries = [cypher for cypher, params in client.queries if params.get("rel") == "属于"]
    assert len(belongs_queries) == 1
    assert "$ledger_id" not in belongs_queries[0]


def test_resolve_device_type_rejects_ambiguous_ledger_fallback():
    """没有 id 的设备：台账占位值命中的类型不唯一（或本身是占位值）时不猜类型。"""

    class _AmbiguousLedgerClient:
        def __init__(self) -> None:
            self.queries: list[tuple[str, dict]] = []

        def run(self, cypher: str, **params):
            self.queries.append((cypher, params))
            return []

    device = {"name": "x", "type": "device", "properties": {"台账ID列表": "补充数据"}}
    client = _AmbiguousLedgerClient()

    assert resolve_device_type(client, device) == (None, None)
    # 占位台账 ID 不作为匹配键，直接跳过“属于”查询。
    assert all(params.get("rel") != "属于" for _, params in client.queries)


def test_resolve_device_type_rejects_multiple_types():
    """设备 id 属于多条不同类型边时按数据歧义处理，不取名称最小者。"""

    class _MultiTypeClient:
        def __init__(self) -> None:
            self.queries: list[tuple[str, dict]] = []

        def run(self, cypher: str, **params):
            self.queries.append((cypher, params))
            if params.get("rel") != "属于":
                return []
            return [
                {"b": {"名称": "箱式变电站", "id": "DEV#箱式变电站"}},
                {"b": {"名称": "架空线路", "id": "DEV#架空线路"}},
            ]

    device = {"id": "LGR#x", "name": "x", "type": "device", "properties": {}}

    assert resolve_device_type(_MultiTypeClient(), device) == (None, None)


def test_resolve_device_type_falls_back_to_ledger_id_edge():
    """设备没有 id、也没有台账分类映射时仍可按唯一台账 ID 命中。"""

    target = {"name": "x", "type": "device", "properties": {"台账ID列表": "3b7y"}}
    client = _GraphFakeClient(
        belongs_to={},
        ledger_belongs_to={"3b7y": {"名称": "架空线路", "id": "DEV#架空线路"}},
    )

    name, node = resolve_device_type(client, target)

    assert name == "架空线路"
    assert node["id"] == "DEV#架空线路"
    belongs_queries = [(cypher, params) for cypher, params in client.queries if params.get("rel") == "属于"]
    assert [params.get("ledger_id") for _cypher, params in belongs_queries] == ["3b7y"]
    # TuGraph 4.5.2 的 cypher 计划器不接受 ORDER BY coalesce(...)，排序必须在客户端做。
    assert all("ORDER BY" not in cypher for cypher, _params in belongs_queries)


def test_resolve_device_type_skips_ledger_fallback_when_device_has_id():
    """有 id 的设备只按 id 查类型：台账 ID 是共享占位值，不能再作为匹配键兜底。"""

    target = {"id": "LGR#x", "name": "x", "type": "device", "properties": {"台账ID列表": "3b7y"}}
    client = _GraphFakeClient(
        belongs_to={},
        ledger_belongs_to={"3b7y": {"名称": "架空线路", "id": "DEV#架空线路"}},
    )

    assert resolve_device_type(client, target) == (None, None)
    assert all("台账ID列表" not in cypher for cypher, _params in client.queries)
