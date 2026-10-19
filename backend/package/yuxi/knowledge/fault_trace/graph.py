"""故障图谱证据模型与纯函数重组逻辑。

``FaultGraphEvidence`` 是图谱适配器（``graph_kg``）与装配逻辑（``answer``）共享的
证据形状；因果链与拓扑的方向/类型重组在客户端完成。图谱读取由 ``graph_kg`` 的
独立 TuGraph 适配器负责，本模块不依赖任何具体图数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yuxi.knowledge.fault_trace.contracts import (
    ENTITY_CAUSE,
    ENTITY_DEVICE,
    ENTITY_FAULT_MODE,
    REL_CAUSAL_TYPES,
    REL_FAULT_MODE_TYPE,
    REL_TOPOLOGY_TYPE,
)

TOPO_MAX_HOPS = 3


@dataclass
class FaultGraphEvidence:
    """一次故障溯源从图谱检索到的证据集合（已去重）。"""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)

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


def _entity_label(node: dict[str, Any]) -> str | None:
    """取节点实体的业务标签（device/fault_mode/cause/…）。

    Neo4j 后端把标签写到节点属性 ``label``，TuGraph 后端写到 ``entity_label``
    且 normalize 后的顶层 ``type`` 也携带该标签；统一优先取属性字段，
    再退回顶层 ``type``，避免后端字段差异泄漏到语义层。
    """

    properties = node.get("properties") or {}
    label = properties.get("label") or properties.get("entity_label")
    if label:
        return label
    node_type = node.get("type")
    if node_type and node_type != "Chunk":
        return node_type
    return None


def find_device_nodes(evidence: FaultGraphEvidence, *, device_names: list[str]) -> list[dict[str, Any]]:
    """按名称（包含匹配）找到设备实体节点，优先 label=device。"""

    matches: list[dict[str, Any]] = []
    for node in evidence.nodes:
        if node.get("type") == "Chunk":
            continue
        name = str(node.get("name") or "")
        if not name:
            continue
        if any(device in name or name in device for device in device_names if device):
            matches.append(node)
    device_matches = [node for node in matches if _entity_label(node) == ENTITY_DEVICE]
    return device_matches or matches


def _edge_neighbor(evidence: FaultGraphEvidence, edge: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    if str(edge.get("source_id")) == str(node_id):
        return evidence.node_by_id(str(edge.get("target_id") or ""))
    if str(edge.get("target_id")) == str(node_id):
        return evidence.node_by_id(str(edge.get("source_id") or ""))
    return None


def resolve_fault_modes(evidence: FaultGraphEvidence, device_node_ids: list[str]) -> list[dict[str, Any]]:
    """取与设备节点通过 has_fault_mode 相连的故障模式节点。"""

    mode_ids: set[str] = set()
    for edge in evidence.edges_with_type(REL_FAULT_MODE_TYPE):
        for node_id in device_node_ids:
            neighbor = _edge_neighbor(evidence, edge, node_id)
            if neighbor is not None and _entity_label(neighbor) == ENTITY_FAULT_MODE:
                mode_ids.add(str(neighbor["id"]))
    modes = [node for node in evidence.nodes if node.get("id") in mode_ids]
    # 无 has_fault_mode 边时，把图里全部故障模式作为候选（人工数据允许只建因果链）。
    if not modes:
        modes = evidence.nodes_with_label(ENTITY_FAULT_MODE)
    return modes


def build_reasoning_rows(evidence: FaultGraphEvidence, modes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按现象分组生成「现象 → 可能原因 → 处置建议」矩阵行（多对多）。

    行以现象为键：原因是该现象所属故障模式沿 has_cause 的原因节点，
    措施是原因沿 has_measure 的处置节点；同一原因可出现在多个现象下。
    """

    mode_ids = {str(node.get("id")) for node in modes}
    cause_by_mode: dict[str, list[str]] = {}
    measure_by_cause: dict[str, list[str]] = {}
    phenomenon_by_mode: dict[str, list[str]] = {}

    for edge in evidence.edges:
        edge_type = edge.get("type")
        if edge_type not in REL_CAUSAL_TYPES:
            continue
        source_id = str(edge.get("source_id") or "")
        target_id = str(edge.get("target_id") or "")
        if edge_type == "has_phenomenon" and source_id in mode_ids:
            phenomenon_by_mode.setdefault(source_id, []).append(target_id)
        elif edge_type == "has_cause" and source_id in mode_ids:
            cause_by_mode.setdefault(source_id, []).append(target_id)
        elif edge_type == "has_measure":
            measure_by_cause.setdefault(source_id, []).append(target_id)

    cause_ids = {cause for causes in cause_by_mode.values() for cause in causes}
    rows: dict[str, dict[str, Any]] = {}
    for mode_id in mode_ids:
        for phenomenon_id in phenomenon_by_mode.get(mode_id, []):
            phenomenon = evidence.node_name(phenomenon_id)
            if not phenomenon:
                continue
            causes = [
                evidence.node_name(cause_id)
                for cause_id in cause_by_mode.get(mode_id, [])
                if evidence.node_name(cause_id)
            ]
            measures: list[str] = []
            for cause_id in cause_by_mode.get(mode_id, []):
                measures.extend(
                    evidence.node_name(measure_id)
                    for measure_id in measure_by_cause.get(cause_id, [])
                    if evidence.node_name(measure_id)
                )
            row = rows.setdefault(
                phenomenon,
                {
                    "phenomenon": phenomenon,
                    "causes": [],
                    "measures": [],
                },
            )
            for cause in causes:
                if cause not in row["causes"]:
                    row["causes"].append(cause)
            for measure in measures:
                if measure not in row["measures"]:
                    row["measures"].append(measure)
    # 无 has_phenomenon 边时，用候选原因集合生成单行兜底，避免空矩阵。
    if not rows and cause_ids:
        row = rows.setdefault("故障现象", {"phenomenon": "故障现象", "causes": [], "measures": []})
        for cause_id in cause_ids:
            name = evidence.node_name(cause_id)
            if name and name not in row["causes"]:
                row["causes"].append(name)
            for measure_id in measure_by_cause.get(cause_id, []):
                measure = evidence.node_name(measure_id)
                if measure and measure not in row["measures"]:
                    row["measures"].append(measure)
    return list(rows.values())


def pick_located_cause(rows: list[dict[str, Any]], evidence: FaultGraphEvidence) -> tuple[str, list[str]]:
    """按“可解释现象最多的原因”确定性定位，返回 (定位原因, 其措施列表)。

    解释现象数相同的原因按在矩阵中先出现的顺序胜出，避免结果随集合序变化。
    措施的归属按图谱 ``has_measure`` 边从该原因节点直接可达的处置节点确定，
    不把同行其他原因的措施混入。
    """

    measure_by_cause_name: dict[str, list[str]] = {}
    for edge in evidence.edges:
        if edge.get("type") != "has_measure":
            continue
        cause_node = evidence.node_by_id(str(edge.get("source_id") or ""))
        if cause_node is None or _entity_label(cause_node) != ENTITY_CAUSE:
            continue
        cause_name = str(cause_node.get("name") or "")
        measure_name = evidence.node_name(str(edge.get("target_id") or ""))
        if cause_name and measure_name and measure_name not in measure_by_cause_name.setdefault(cause_name, []):
            measure_by_cause_name[cause_name].append(measure_name)

    cause_count: dict[str, int] = {}
    cause_order: dict[str, int] = {}
    next_order = 0
    for row in rows:
        for cause in row.get("causes") or []:
            if cause not in cause_count:
                cause_count[cause] = 0
                cause_order[cause] = next_order
                next_order += 1
            cause_count[cause] += 1
    if not cause_count:
        return "", []
    located_cause = max(cause_count.items(), key=lambda item: (item[1], -cause_order[item[0]]))[0]
    return located_cause, list(measure_by_cause_name.get(located_cause, []))


def trace_topology(evidence: FaultGraphEvidence, device_node_ids: list[str]) -> tuple[list[str], list[str], list[str]]:
    """沿 electrically_connected_to 追溯上游/下游设备与路径（最多 3 跳）。

    上游 = 边指向当前设备（电流来源方向），下游 = 边从当前设备流出。
    """

    def walk(start_id: str, *, outgoing: bool) -> tuple[list[str], list[str]]:
        visited: set[str] = set()
        frontier: list[tuple[str, int]] = [(start_id, 0)]
        names: list[str] = []
        paths: list[str] = []
        while frontier:
            current_id, depth = frontier.pop(0)
            if current_id in visited or depth >= TOPO_MAX_HOPS:
                continue
            visited.add(current_id)
            for edge in evidence.edges_with_type(REL_TOPOLOGY_TYPE):
                if outgoing and str(edge.get("source_id")) != current_id:
                    continue
                if not outgoing and str(edge.get("target_id")) != current_id:
                    continue
                neighbor_id = str(edge.get("target_id") if outgoing else edge.get("source_id") or "")
                neighbor = evidence.node_by_id(neighbor_id)
                if neighbor is None:
                    continue
                neighbor_name = str(neighbor.get("name") or "")
                if neighbor_id in visited:
                    continue
                if neighbor_name and neighbor_name not in names:
                    names.append(neighbor_name)
                if outgoing:
                    paths.append(f"{evidence.node_name(current_id) or '设备'} → {neighbor_name}")
                else:
                    paths.append(f"{neighbor_name} → {evidence.node_name(current_id) or '设备'}")
                frontier.append((neighbor_id, depth + 1))
        return names, paths

    upstream_names: list[str] = []
    downstream_names: list[str] = []
    upstream_paths: list[str] = []
    downstream_paths: list[str] = []
    for node_id in device_node_ids:
        up_names, up_paths = walk(node_id, outgoing=False)
        down_names, down_paths = walk(node_id, outgoing=True)
        for name in up_names:
            if name not in upstream_names:
                upstream_names.append(name)
        for name in down_names:
            if name not in downstream_names:
                downstream_names.append(name)
        for path in up_paths:
            if path not in upstream_paths:
                upstream_paths.append(path)
        for path in down_paths:
            if path not in downstream_paths:
                downstream_paths.append(path)
    return upstream_names, downstream_names, upstream_paths + downstream_paths
