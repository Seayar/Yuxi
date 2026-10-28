"""故障图谱证据模型与拓扑追溯单元测试。"""

from __future__ import annotations

import pytest
from yuxi.knowledge.fault_trace.graph import (
    FaultGraphEvidence,
    collect_consequences,
    resolve_device_type,
    trace_topology_evidence,
)

pytestmark = pytest.mark.unit


def _node(node_id: str, name: str, label: str, **properties) -> dict:
    return {"id": node_id, "name": name, "type": label, "properties": {"label": label, **properties}}


def _edge(source: str, target: str, edge_type: str) -> dict:
    return {
        "id": f"{source}->{edge_type}->{target}",
        "source_id": source,
        "target_id": target,
        "type": edge_type,
        "properties": {},
    }


def _evidence() -> FaultGraphEvidence:
    nodes = [
        _node("dev_main", "3B-7Y", "device", 电压等级="10kV"),
        _node("dev_station", "35kV#3变电站", "device", 电压等级="35kV"),
        _node("dev_upstream", "110kV变电站", "device", 电压等级="110kV"),
        _node("dev_downstream", "10kV开闭所", "device", 电压等级="10kV"),
        _node("dev_line", "岭开线", "device", 设备分类="输电线路"),
        _node("type_line", "架空线路", "device_type"),
        _node("type_station", "变电站", "device_type"),
        _node("cause", "树木倒伏压线", "cause"),
        _node("conseq_short", "相间短路", "consequence"),
    ]
    edges = [
        _edge("dev_main", "type_line", "belongs_to_device_type"),
        _edge("dev_station", "type_station", "belongs_to_device_type"),
        # 业务口径：电气连接入边=上游，出边=下游；供电于不参与上下游拆分。
        _edge("dev_station", "dev_main", "electrically_connected_to"),
        _edge("dev_main", "dev_downstream", "electrically_connected_to"),
        _edge("dev_upstream", "dev_station", "supplies_power_to"),
        _edge("dev_line", "dev_upstream", "supplies_power_to"),
        _edge("cause", "conseq_short", "may_lead_to_consequence"),
    ]
    return FaultGraphEvidence(nodes=nodes, edges=edges)


def test_resolve_device_type_reads_belongs_to_edge():
    evidence = _evidence()

    assert resolve_device_type(evidence, ["dev_main"]) == "架空线路"
    assert resolve_device_type(evidence, ["dev_downstream"]) is None


def test_topology_splits_electrical_edges_by_direction():
    topology = trace_topology_evidence(_evidence(), ["dev_main"])

    assert [device["name"] for device in topology.upstream] == ["35kV#3变电站"]
    assert [device["name"] for device in topology.downstream] == ["10kV开闭所"]
    assert topology.paths == ["35kV#3变电站 → 3B-7Y", "3B-7Y → 10kV开闭所"]
    # 供电于不入影响范围：110kV变电站只在供电链路上。
    assert "110kV变电站" not in [device["name"] for device in topology.upstream]


def test_topology_warns_when_no_electrical_neighbors():
    evidence = FaultGraphEvidence(
        nodes=[_node("dev_main", "3B-7Y", "device")],
        edges=[],
    )

    topology = trace_topology_evidence(evidence, ["dev_main"])

    assert topology.upstream == []
    assert topology.downstream == []
    assert any("上游电气连接" in warning for warning in topology.warnings)
    assert any("下游电气连接" in warning for warning in topology.warnings)


def test_collect_consequences_deduplicates_nodes():
    evidence = _evidence()
    evidence.edges.append(_edge("cause", "conseq_short", "may_lead_to_consequence"))

    assert collect_consequences(evidence) == [{"consequence_id": "conseq_short", "name": "相间短路"}]
