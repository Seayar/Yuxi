"""故障图谱证据重组与 kg 适配器查询编排单元测试。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.fault_trace.graph import (
    FaultGraphEvidence,
    build_reasoning_rows,
    find_device_nodes,
    pick_located_cause,
    resolve_fault_modes,
    trace_topology,
)
from yuxi.knowledge.fault_trace.graph_kg import (
    _map_node,
    _map_relation,
    collect_kg_fault_evidence,
)

pytestmark = pytest.mark.unit


def _node(node_id: str, name: str, label: str) -> dict:
    return {
        "id": node_id,
        "name": name,
        "type": "Entity",
        "properties": {"label": label, "entity_id": node_id, "name": name},
    }


def _edge(edge_id: str, source: str, target: str, edge_type: str) -> dict:
    return {"id": edge_id, "source_id": source, "target_id": target, "type": edge_type, "properties": {}}


def _transformer_evidence() -> FaultGraphEvidence:
    """主变冷却器示例：一个故障模式、两个现象、两个原因、两类措施、上下游拓扑。"""

    nodes = [
        _node("dev_main", "1号主变", "device"),
        _node("dev_up", "110kV进线", "device"),
        _node("dev_down", "10kV开闭所", "device"),
        _node("mode_cool", "冷却系统故障", "fault_mode"),
        _node("ph_fan", "冷却器风扇无运转声音", "phenomenon"),
        _node("ph_oil", "顶层油温升高", "phenomenon"),
        _node("cause_motor", "冷却器风扇电机停转", "cause"),
        _node("cause_dust", "散热片积尘严重", "cause"),
        _node("ms_check", "检查风扇电机电源与控制回路", "measure"),
        _node("ms_clean", "清洗散热片积尘", "measure"),
        _node("trig", "冷却器风扇运行电流 = 0A 且顶层油温 > 85℃ → 冷却器风扇电机停转", "trigger"),
    ]
    edges = [
        _edge("e1", "dev_main", "mode_cool", "has_fault_mode"),
        _edge("e2", "mode_cool", "ph_fan", "has_phenomenon"),
        _edge("e3", "mode_cool", "ph_oil", "has_phenomenon"),
        _edge("e4", "mode_cool", "cause_motor", "has_cause"),
        _edge("e5", "mode_cool", "cause_dust", "has_cause"),
        _edge("e6", "cause_motor", "ms_check", "has_measure"),
        _edge("e7", "cause_dust", "ms_clean", "has_measure"),
        _edge("e8", "mode_cool", "trig", "has_trigger_condition"),
        _edge("e9", "dev_up", "dev_main", "electrically_connected_to"),
        _edge("e10", "dev_main", "dev_down", "electrically_connected_to"),
    ]
    return FaultGraphEvidence(nodes=nodes, edges=edges)


def test_find_device_nodes_returns_device_label_first():
    evidence = _transformer_evidence()

    devices = find_device_nodes(evidence, device_names=["1号主变"])

    assert [node["name"] for node in devices] == ["1号主变"]


def test_causal_rows_group_by_phenomenon():
    evidence = _transformer_evidence()
    modes = resolve_fault_modes(evidence, ["dev_main"])

    rows = build_reasoning_rows(evidence, modes)

    assert {row["phenomenon"] for row in rows} == {"冷却器风扇无运转声音", "顶层油温升高"}
    row = rows[0]
    assert set(row["causes"]) == {"冷却器风扇电机停转", "散热片积尘严重"}
    assert set(row["measures"]) == {"检查风扇电机电源与控制回路", "清洗散热片积尘"}


def test_pick_located_cause_prefers_most_explained_then_first_seen():
    evidence = _transformer_evidence()
    modes = resolve_fault_modes(evidence, ["dev_main"])
    rows = build_reasoning_rows(evidence, modes)

    located, measures = pick_located_cause(rows, evidence)

    assert located == "冷却器风扇电机停转"  # 与散热片积尘解释现象数相同，先出现者胜
    assert measures == ["检查风扇电机电源与控制回路"]


def test_topology_traces_upstream_and_downstream():
    evidence = _transformer_evidence()

    upstream, downstream, paths = trace_topology(evidence, ["dev_main"])

    assert upstream == ["110kV进线"]
    assert downstream == ["10kV开闭所"]
    assert any("1号主变" in path and "10kV开闭所" in path for path in paths)


def test_topology_rejects_reverse_direction():
    """only incoming edges count as upstream; outgoing as downstream."""
    nodes = [
        _node("a", "设备A", "device"),
        _node("b", "设备B", "device"),
    ]
    evidence = FaultGraphEvidence(
        nodes=nodes,
        edges=[_edge("e1", "b", "a", "electrically_connected_to")],
    )

    upstream, downstream, _ = trace_topology(evidence, ["a"])

    assert upstream == ["设备B"]
    assert downstream == []


class _FakeKgClient:
    """替身 kg 客户端：按查询特征返回预置记录。"""

    def __init__(self, responder) -> None:
        self._responder = responder
        self.queries: list[tuple[str, dict]] = []

    def run(self, cypher: str, **params):
        self.queries.append((cypher, params))
        return self._responder(cypher, params)


def test_map_node_maps_kg_labels_and_names():
    node = _map_node({"名称": "1号主变", "id": "LGR#1"}, expected_label="设备实例")
    assert node == {
        "id": "LGR#1",
        "name": "1号主变",
        "type": "device",
        "labels": ["设备实例"],
        "properties": {"名称": "1号主变", "id": "LGR#1"},
        "normalized": {"name": "1号主变", "type": "device", "source": "kg"},
    }
    assert _map_node({"名称": "冷却系统故障"}, expected_label="故障模式")["type"] == "fault_mode"
    assert _map_node({"名称": "未知"}, expected_label="未知标签") is None
    assert _map_node(None, expected_label="故障模式") is None


def test_map_relation_maps_chinese_types():
    assert _map_relation("存在故障模式") == "has_fault_mode"
    assert _map_relation("表现为") == "has_phenomenon"
    assert _map_relation("起因") == "has_cause"
    assert _map_relation("处理") == "has_measure"
    assert _map_relation("诱发条件") == "has_trigger_condition"
    assert _map_relation("电连接") == "electrically_connected_to"
    assert _map_relation("未注册关系") is None


def test_collect_kg_evidence_orchestrates_queries():
    """kg 编排：设备→模式→因果链→措施→拓扑，多路去重合并。"""

    device = {"名称": "1号主变", "id": "LGR#1"}
    mode = {"名称": "冷却系统故障"}
    phenomenon = {"名称": "冷却器风扇无运转声音"}
    cause = {"名称": "冷却器风扇电机停转"}
    measure = {"名称": "检查风扇电机电源与控制回路"}
    trigger = {"名称": "触发规则"}
    up_device = {"名称": "110kV进线", "id": "LGR#up"}

    def responder(cypher: str, params: dict):
        if params.get("label") == "设备实例" and "CONTAINS" in cypher:
            return [{"n": device}]
        if params.get("rel") == "表现为" and params.get("src") == "故障模式" and "CONTAINS" in cypher:
            return [{"a": mode, "rel_type": "表现为", "b": phenomenon}]
        if params.get("rel") == "存在故障模式":
            return [{"a": device, "rel_type": "存在故障模式", "b": mode}]
        if params.get("rel") == "起因":
            return [{"a": mode, "rel_type": "起因", "b": cause}]
        if params.get("rel") == "诱发条件":
            return [{"a": mode, "rel_type": "诱发条件", "b": trigger}]
        if params.get("rel") == "处理":
            return [{"a": cause, "rel_type": "处理", "b": measure}]
        if params.get("rel") == "电连接":
            return [{"a": up_device, "rel_type": "电连接", "b": device}]
        if params.get("rel") == "表现为":  # 模式→现象
            return [{"a": mode, "rel_type": "表现为", "b": phenomenon}]
        return []

    client = _FakeKgClient(responder)
    evidence = collect_kg_fault_evidence(
        client,
        device_names=["1号主变"],
        device_type=None,
        phenomena=["停转"],
        image_fault_name=None,
    )

    names = {(node["type"], node["name"]) for node in evidence.nodes}
    assert ("device", "1号主变") in names
    assert ("fault_mode", "冷却系统故障") in names
    assert ("phenomenon", "冷却器风扇无运转声音") in names
    assert ("cause", "冷却器风扇电机停转") in names
    assert ("measure", "检查风扇电机电源与控制回路") in names
    assert ("device", "110kV进线") in names

    edge_types = {edge["type"] for edge in evidence.edges}
    assert {
        "has_fault_mode",
        "has_phenomenon",
        "has_cause",
        "has_measure",
        "has_trigger_condition",
        "electrically_connected_to",
    } <= edge_types

    # 去重：同一节点只出现一次
    ids = [node["id"] for node in evidence.nodes]
    assert len(ids) == len(set(ids))


def test_collect_kg_evidence_without_devices_returns_empty():
    client = _FakeKgClient(lambda cypher, params: [])

    evidence = collect_kg_fault_evidence(
        client,
        device_names=[],
        device_type=None,
        phenomena=[],
        image_fault_name=None,
    )

    assert evidence.nodes == []
    assert evidence.edges == []
