"""故障图谱证据模型与拓扑追溯。

``FaultGraphEvidence`` 是 kg3 适配器（``graph_kg3``）与报告装配（``answer``）共享的
证据形状；因果链与拓扑的方向/类型重组在这里完成，本模块不依赖具体图数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yuxi.knowledge.fault_trace.contracts import (
    ENTITY_CONSEQUENCE,
    ENTITY_DEVICE_TYPE,
    REL_CONSEQUENCE_TYPE,
    REL_DEVICE_TYPE,
    REL_ELECTRICAL_TYPE,
)


@dataclass
class FaultGraphEvidence:
    """一次故障溯源从 kg3 检索到的证据集合（已去重）。"""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    # 原因挂载层级：fault_phenomenon（现象→原因）或 fault_mode（模式→原因，数据缺口）
    cause_links: str = "fault_phenomenon"

    def node_by_id(self, node_id: str) -> dict[str, Any] | None:
        for node in self.nodes:
            if str(node.get("id")) == str(node_id):
                return node
        return None

    def node_name(self, node_id: str) -> str:
        node = self.node_by_id(node_id)
        return str(node.get("name") or "") if node else ""

    def nodes_with_label(self, label: str) -> list[dict[str, Any]]:
        return [node for node in self.nodes if _entity_label(node) == label]

    def edges_with_type(self, edge_type: str) -> list[dict[str, Any]]:
        return [edge for edge in self.edges if edge.get("type") == edge_type]


@dataclass
class FaultTopology:
    """一次拓扑追溯的结构化结果；上游/下游是设备对象，paths 是完整链路。"""

    upstream: list[dict[str, Any]] = field(default_factory=list)
    downstream: list[dict[str, Any]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    consequences: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _entity_label(node: dict[str, Any]) -> str | None:
    """取节点实体的业务标签；优先属性字段，其次顶层类型。"""

    properties = node.get("properties") or {}
    label = properties.get("label") or properties.get("entity_label")
    if label:
        return str(label)
    node_type = node.get("type")
    if node_type and node_type != "Chunk":
        return str(node_type)
    return None


def resolve_device_type(evidence: FaultGraphEvidence, device_node_ids: list[str]) -> str | None:
    """取设备在图谱里的设备类型名称（设备实例 -[属于]-> 设备类型）。"""

    for node_id in device_node_ids:
        name = _device_type_name(evidence, str(node_id))
        if name:
            return name
    return None


def _device_type_name(evidence: FaultGraphEvidence, device_id: str) -> str | None:
    for edge in evidence.edges_with_type(REL_DEVICE_TYPE):
        if str(edge.get("source_id")) != device_id:
            continue
        node = evidence.node_by_id(str(edge.get("target_id") or ""))
        if node is not None and _entity_label(node) == ENTITY_DEVICE_TYPE:
            name = str(node.get("name") or "").strip()
            if name:
                return name
    return None


def collect_consequences(evidence: FaultGraphEvidence) -> list[dict[str, Any]]:
    """取故障原因可能的后果（故障原因 -[可能导致]-> 故障后果），按 id 去重。"""

    consequences: list[dict[str, Any]] = []
    seen: set[str] = set()
    for edge in evidence.edges_with_type(REL_CONSEQUENCE_TYPE):
        node = evidence.node_by_id(str(edge.get("target_id") or ""))
        if node is None or _entity_label(node) != ENTITY_CONSEQUENCE:
            continue
        node_id = str(node.get("id"))
        if node_id in seen:
            continue
        seen.add(node_id)
        consequences.append({"consequence_id": node_id, "name": str(node.get("name") or "")})
    return consequences


def trace_topology_evidence(evidence: FaultGraphEvidence, device_node_ids: list[str]) -> FaultTopology:
    """按业务口径取直接上下游：出边为下游，入边为上游，边类型为 ``电气连接``。"""

    upstream: list[dict[str, Any]] = []
    downstream: list[dict[str, Any]] = []
    paths: list[str] = []
    seen_upstream: set[str] = set()
    seen_downstream: set[str] = set()
    for device_id in device_node_ids:
        device_id = str(device_id)
        self_name = evidence.node_name(device_id) or device_id
        for edge in evidence.edges_with_type(REL_ELECTRICAL_TYPE):
            source_id = str(edge.get("source_id") or "")
            target_id = str(edge.get("target_id") or "")
            if source_id == device_id and target_id != device_id:
                node = evidence.node_by_id(target_id)
                if node is None or target_id in seen_downstream:
                    continue
                seen_downstream.add(target_id)
                downstream.append(_topology_device(evidence, node))
                paths.append(f"{self_name} → {evidence.node_name(target_id) or target_id}")
            elif target_id == device_id and source_id != device_id:
                node = evidence.node_by_id(source_id)
                if node is None or source_id in seen_upstream:
                    continue
                seen_upstream.add(source_id)
                upstream.append(_topology_device(evidence, node))
                paths.append(f"{evidence.node_name(source_id) or source_id} → {self_name}")
    paths = list(dict.fromkeys(paths))
    device_names = [evidence.node_name(node_id) for node_id in device_node_ids]
    label = "、".join(name for name in device_names if name) or "该设备"
    warnings: list[str] = []
    if not upstream:
        warnings.append(f"未发现 {label} 的上游电气连接设备")
    if not downstream:
        warnings.append(f"未发现 {label} 的下游电气连接设备")
    return FaultTopology(
        upstream=upstream,
        downstream=downstream,
        paths=paths,
        consequences=collect_consequences(evidence),
        warnings=warnings,
    )


def _topology_device(evidence: FaultGraphEvidence, node: dict[str, Any]) -> dict[str, Any]:
    """把证据节点投影为思考步骤消费的设备对象；只暴露用户可理解的字段。"""

    properties = node.get("properties") or {}
    node_id = str(node.get("id") or "")
    device_type = _device_type_name(evidence, node_id) or _device_class(properties)
    return {
        "device_id": node_id,
        "name": str(node.get("name") or ""),
        "device_type": device_type,
        "voltage_level": str(properties.get("电压等级") or "") or None,
    }


def _device_class(properties: dict[str, Any]) -> str | None:
    value = properties.get("设备分类") or properties.get("台账分类")
    return str(value) if value else None
