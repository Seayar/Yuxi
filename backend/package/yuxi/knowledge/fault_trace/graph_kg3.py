"""TuGraph ``kg3`` 故障图谱适配器。

按冻结的 kg3 schema 读取设备、设备类型、故障模式、故障现象、故障原因、触发条件、
处置措施、故障后果与电气拓扑，映射为 ``FaultGraphEvidence``（内部英文类型）。
读接口只执行单条只读 MATCH；标签/关系名来自 ``contracts`` 白名单，变量全部参数化。
"""

from __future__ import annotations

from typing import Any

from yuxi.knowledge.fault_trace.contracts import (
    DEVICE_CLASS_TO_TYPE,
    ENTITY_CAUSE,
    ENTITY_DEVICE,
    ENTITY_FAULT_MODE,
    ENTITY_PHENOMENON,
    KG_DEVICE_LABEL,
    KG_DEVICE_NAME_PROPS,
    KG_DEVICE_TYPE_LABEL,
    KG_ENTITY_LABELS,
    KG_RELATION_TYPES,
    REL_DEVICE_TYPE,
    UNMAPPED_STATION_CLASSES,
)
from yuxi.knowledge.fault_trace.graph import FaultGraphEvidence

QUERY_LIMIT = 200


def find_devices(client: Any, names: list[str]) -> list[dict[str, Any]]:
    """按名称精确匹配设备实例；没有精确命中时退回包含匹配。"""

    evidence = FaultGraphEvidence()
    exact: list[dict[str, Any]] = []
    for name in names:
        clean = str(name or "").strip()
        if not clean:
            continue
        records = client.run(
            "MATCH (n) WHERE labels(n)[0] = $label AND coalesce(n.名称, '') = $name RETURN n LIMIT $node_limit",
            label=KG_DEVICE_LABEL,
            name=clean,
            node_limit=QUERY_LIMIT,
        )
        exact.extend(_add_nodes(evidence, records, "n", KG_DEVICE_LABEL))
    if exact:
        return _dedupe(exact)
    contains: list[dict[str, Any]] = []
    for name in names:
        clean = str(name or "").strip()
        if not clean:
            continue
        records = client.run(
            "MATCH (n) WHERE labels(n)[0] = $label "
            "AND (coalesce(n.名称, '') CONTAINS $kw OR coalesce(n.显示名, '') CONTAINS $kw) "
            "RETURN n LIMIT $node_limit",
            label=KG_DEVICE_LABEL,
            kw=clean,
            node_limit=QUERY_LIMIT,
        )
        contains.extend(_add_nodes(evidence, records, "n", KG_DEVICE_LABEL))
    return _dedupe(contains)


def resolve_device_type(client: Any, device: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """解析设备类型：优先 ``属于`` 边，其次台账分类映射；无法确定时返回 (None, None)。"""

    device_evidence = FaultGraphEvidence(nodes=[device], edges=[])
    device_id = str(device.get("id") or "")
    ledger_id = str((device.get("properties") or {}).get("台账ID列表") or "")
    # 空值不能参与等值匹配：``coalesce(缺失, '') = ''`` 会命中所有缺该属性的设备，
    # 把别的设备的类型串到当前设备上。
    matchers: list[str] = []
    if device_id:
        matchers.append("coalesce(a.id, '') = $device_id")
    if ledger_id:
        matchers.append("coalesce(a.台账ID列表, '') = $ledger_id")
    if matchers:
        records = client.run(
            "MATCH (a)-[r]->(b) WHERE type(r) = $rel AND labels(a)[0] = $src AND labels(b)[0] = $dst "
            f"AND ({' OR '.join(matchers)}) "
            "RETURN b LIMIT $node_limit",
            rel="属于",
            src=KG_DEVICE_LABEL,
            dst=KG_DEVICE_TYPE_LABEL,
            device_id=device_id,
            ledger_id=ledger_id,
            node_limit=QUERY_LIMIT,
        )
        type_nodes = _add_nodes(device_evidence, records, "b", KG_DEVICE_TYPE_LABEL)
        if type_nodes:
            # 排序在客户端完成：TuGraph 4.5.2 的 cypher 计划器不接受 ``ORDER BY coalesce(...)``
            # （CypherException），而返回顺序本身不稳定。按名称、id 排序保证首项确定。
            first = min(type_nodes, key=lambda node: (str(node.get("name") or ""), str(node.get("id") or "")))
            return str(first.get("name") or "") or None, first

    properties = device.get("properties") or {}
    device_class = str(properties.get("设备分类") or properties.get("台账分类") or "").strip()
    if device_class in UNMAPPED_STATION_CLASSES:
        return None, None
    mapped = DEVICE_CLASS_TO_TYPE.get(device_class)
    if not mapped:
        return None, None
    records = client.run(
        "MATCH (t) WHERE labels(t)[0] = $label AND coalesce(t.名称, '') = $name RETURN t LIMIT $node_limit",
        label=KG_DEVICE_TYPE_LABEL,
        name=mapped,
        node_limit=QUERY_LIMIT,
    )
    type_nodes = _add_nodes(device_evidence, records, "t", KG_DEVICE_TYPE_LABEL)
    if type_nodes:
        return str(type_nodes[0].get("name") or "") or None, type_nodes[0]
    return None, None


def device_ancestors(client: Any, device: dict[str, Any]) -> list[str]:
    """取设备在 kg3 里的上级设备名称（``位于`` 边），用于同名设备消歧。"""

    device_id = str(device.get("id") or "")
    if not device_id:
        return []
    try:
        records = client.run(
            "MATCH (a)-[r]->(b) WHERE type(r) = $rel AND labels(a)[0] = $label "
            "AND coalesce(a.id, '') = $device_id RETURN b.名称 AS name LIMIT $node_limit",
            rel="位于",
            label=KG_DEVICE_LABEL,
            device_id=device_id,
            node_limit=QUERY_LIMIT,
        )
    except Exception:  # noqa: BLE001 - 消歧失败时按原候选处理
        return []
    return [str(record.get("name")) for record in records if record.get("name")]


def collect_kg3_evidence(
    client: Any,
    *,
    devices: list[dict[str, Any]],
    device_type: dict[str, Any] | None,
) -> FaultGraphEvidence:
    """采集因果链、触发条件、处置措施、故障后果与电气拓扑。"""

    evidence = FaultGraphEvidence()
    device_ids: list[str] = []
    for device in devices:
        stored = evidence.node_by_id(str(device.get("id")))
        if stored is None:
            evidence.nodes.append(device)
        device_ids.append(str(device.get("id") or ""))
    type_ids: list[str] = []
    type_names: list[str] = []
    if device_type is not None:
        stored = evidence.node_by_id(str(device_type.get("id")))
        if stored is None:
            evidence.nodes.append(device_type)
        type_ids.append(str(device_type.get("id") or ""))
        type_names.append(str(device_type.get("name") or ""))
        for device_id in device_ids:
            if device_id:
                evidence.edges.append(
                    {
                        "id": f"{device_id}->{REL_DEVICE_TYPE}->{device_type.get('id')}",
                        "source_id": device_id,
                        "target_id": str(device_type.get("id") or ""),
                        "type": REL_DEVICE_TYPE,
                        "properties": {},
                    }
                )

    mode_ids, mode_names = _collect_modes(client, evidence, type_ids=type_ids, type_names=type_names)
    phenomenon_ids, phenomenon_names = _collect_phenomena(
        client,
        evidence,
        mode_ids=mode_ids,
        mode_names=mode_names,
        type_ids=type_ids,
        type_names=type_names,
    )

    cause_ids, cause_names = _collect_causes(
        client,
        evidence,
        phenomenon_ids=phenomenon_ids,
        phenomenon_names=phenomenon_names,
        mode_ids=mode_ids,
        mode_names=mode_names,
    )
    _collect_triggers(client, evidence, cause_ids=cause_ids, cause_names=cause_names)
    _collect_measures(client, evidence, cause_ids=cause_ids, cause_names=cause_names)
    _collect_consequences(client, evidence, cause_ids=cause_ids, cause_names=cause_names)
    _collect_topology(client, evidence, device_ids=device_ids)
    _collect_device_types(client, evidence)
    return evidence


def _collect_modes(
    client: Any,
    evidence: FaultGraphEvidence,
    *,
    type_ids: list[str],
    type_names: list[str],
) -> tuple[list[str], list[str]]:
    edges = _query_related(
        client,
        source_label=KG_DEVICE_TYPE_LABEL,
        relation="具有故障模式",
        target_label="故障模式",
        source_ids=type_ids,
        source_names=type_names,
    )
    _add_edges(evidence, edges, KG_DEVICE_TYPE_LABEL, "故障模式")
    modes = evidence.nodes_with_label(ENTITY_FAULT_MODE)
    return [str(node["id"]) for node in modes], [str(node["name"]) for node in modes]


def _collect_phenomena(
    client: Any,
    evidence: FaultGraphEvidence,
    *,
    mode_ids: list[str],
    mode_names: list[str],
    type_ids: list[str],
    type_names: list[str],
) -> tuple[list[str], list[str]]:
    """优先 ``故障模式→故障现象``；新数据没有模式层时回退 ``设备类型→故障现象``。"""

    edges = _query_related(
        client,
        source_label="故障模式",
        relation="具有现象",
        target_label="故障现象",
        source_ids=mode_ids,
        source_names=mode_names,
    )
    _add_edges(evidence, edges, "故障模式", "故障现象")
    if not evidence.nodes_with_label(ENTITY_PHENOMENON):
        edges = _query_related(
            client,
            source_label=KG_DEVICE_TYPE_LABEL,
            relation="具有现象",
            target_label="故障现象",
            source_ids=type_ids,
            source_names=type_names,
        )
        _add_edges(evidence, edges, KG_DEVICE_TYPE_LABEL, "故障现象")
    nodes = evidence.nodes_with_label(ENTITY_PHENOMENON)
    return [str(node["id"]) for node in nodes], [str(node["name"]) for node in nodes]


def _collect_causes(
    client: Any,
    evidence: FaultGraphEvidence,
    *,
    phenomenon_ids: list[str],
    phenomenon_names: list[str],
    mode_ids: list[str],
    mode_names: list[str],
) -> tuple[list[str], list[str]]:
    """优先取 ``故障现象→故障原因``；数据只有模式级原因时标记 ``cause_links``。"""

    edges = _query_related(
        client,
        source_label="故障现象",
        relation="具有原因",
        target_label="故障原因",
        source_ids=phenomenon_ids,
        source_names=phenomenon_names,
    )
    if edges:
        _add_edges(evidence, edges, "故障现象", "故障原因")
        evidence.cause_links = "fault_phenomenon"
    else:
        edges = _query_related(
            client,
            source_label="故障模式",
            relation="具有原因",
            target_label="故障原因",
            source_ids=mode_ids,
            source_names=mode_names,
        )
        _add_edges(evidence, edges, "故障模式", "故障原因")
        evidence.cause_links = "fault_mode"
    causes = evidence.nodes_with_label(ENTITY_CAUSE)
    return [str(node["id"]) for node in causes], [str(node["name"]) for node in causes]


def _collect_triggers(
    client: Any,
    evidence: FaultGraphEvidence,
    *,
    cause_ids: list[str],
    cause_names: list[str],
) -> None:
    edges = _query_related(
        client,
        source_label="故障原因",
        relation="具有触发条件",
        target_label="触发条件",
        source_ids=cause_ids,
        source_names=cause_names,
    )
    _add_edges(evidence, edges, "故障原因", "触发条件")


def _collect_measures(
    client: Any,
    evidence: FaultGraphEvidence,
    *,
    cause_ids: list[str],
    cause_names: list[str],
) -> None:
    edges = _query_related(
        client,
        source_label="故障原因",
        relation="具有措施",
        target_label="处置措施",
        source_ids=cause_ids,
        source_names=cause_names,
    )
    _add_edges(evidence, edges, "故障原因", "处置措施")


def _collect_consequences(
    client: Any,
    evidence: FaultGraphEvidence,
    *,
    cause_ids: list[str],
    cause_names: list[str],
) -> None:
    edges = _query_related(
        client,
        source_label="故障原因",
        relation="可能导致",
        target_label="故障后果",
        source_ids=cause_ids,
        source_names=cause_names,
    )
    _add_edges(evidence, edges, "故障原因", "故障后果")


def _collect_topology(client: Any, evidence: FaultGraphEvidence, *, device_ids: list[str]) -> None:
    """按业务口径读取设备的一跳 ``电气连接``：出边为下游、入边为上游。"""

    ids = [device_id for device_id in device_ids if device_id]
    if not ids:
        return
    records = client.run(
        "MATCH (a)-[r]->(b) WHERE type(r) = $rel AND labels(a)[0] = $label "
        "AND labels(b)[0] = $label AND (coalesce(a.id, '') IN $ids OR coalesce(b.id, '') IN $ids) "
        "RETURN a, b LIMIT $node_limit",
        rel="电气连接",
        label=KG_DEVICE_LABEL,
        ids=ids,
        node_limit=QUERY_LIMIT,
    )
    for record in records:
        source = _map_node(record.get("a"), expected_label=KG_DEVICE_LABEL)
        target = _map_node(record.get("b"), expected_label=KG_DEVICE_LABEL)
        if source is None or target is None:
            continue
        _store_node(evidence, source)
        _store_node(evidence, target)
        _store_edge(evidence, source["id"], target["id"], "电气连接")


def _collect_device_types(client: Any, evidence: FaultGraphEvidence) -> None:
    """为设备（含拓扑邻居）补全 ``属于`` 设备类型，供步骤 2/3 展示。"""

    devices = evidence.nodes_with_label(ENTITY_DEVICE)
    ids = [str(node["id"]) for node in devices]
    names = [str(node["name"]) for node in devices]
    if not ids and not names:
        return
    edges = _query_related(
        client,
        source_label=KG_DEVICE_LABEL,
        relation="属于",
        target_label=KG_DEVICE_TYPE_LABEL,
        source_ids=ids,
        source_names=names,
    )
    _add_edges(evidence, edges, KG_DEVICE_LABEL, KG_DEVICE_TYPE_LABEL)


def _query_related(
    client: Any,
    *,
    source_label: str,
    relation: str,
    target_label: str,
    source_ids: list[str],
    source_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    ids = [str(item) for item in (source_ids or []) if item]
    names = [str(item) for item in (source_names or []) if item]
    if not ids and not names:
        return []
    # 有 id 时只按 id 匹配：同一名称（如各设备类型的“冒烟”现象）会跨类型重名，
    # 名称兜底只用于没有 id 的历史数据。
    if ids:
        source_match = "coalesce(a.id, '') IN $ids"
    else:
        source_match = " OR ".join(f"coalesce(a.{prop}, '') IN $names" for prop in KG_DEVICE_NAME_PROPS)
    return client.run(
        f"MATCH (a)-[r]->(b) WHERE type(r) = $rel AND labels(a)[0] = $src AND labels(b)[0] = $dst "
        f"AND ({source_match}) RETURN a, type(r) AS rel_type, b LIMIT $node_limit",
        rel=relation,
        src=source_label,
        dst=target_label,
        ids=ids,
        names=names,
        node_limit=QUERY_LIMIT,
    )


def _add_nodes(
    evidence: FaultGraphEvidence,
    records: list[dict[str, Any]],
    key: str,
    expected_label: str,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for record in records:
        mapped = _map_node(record.get(key), expected_label=expected_label)
        if mapped is None:
            continue
        _store_node(evidence, mapped)
        nodes.append(mapped)
    return nodes


def _add_edges(
    evidence: FaultGraphEvidence,
    records: list[dict[str, Any]],
    source_label: str,
    target_label: str,
) -> None:
    for record in records:
        source = _map_node(record.get("a"), expected_label=source_label)
        target = _map_node(record.get("b"), expected_label=target_label)
        if source is None or target is None:
            continue
        _store_node(evidence, source)
        _store_node(evidence, target)
        _store_edge(evidence, source["id"], target["id"], str(record.get("rel_type") or ""))


def _store_node(evidence: FaultGraphEvidence, node: dict[str, Any]) -> None:
    if evidence.node_by_id(str(node["id"])) is None:
        evidence.nodes.append(node)


def _store_edge(evidence: FaultGraphEvidence, source_id: str, target_id: str, raw_relation: str) -> None:
    edge_type = KG_RELATION_TYPES.get(raw_relation)
    if not edge_type:
        return
    edge_id = f"{source_id}->{edge_type}->{target_id}"
    if any(str(edge.get("id")) == edge_id for edge in evidence.edges):
        return
    evidence.edges.append(
        {
            "id": edge_id,
            "source_id": str(source_id),
            "target_id": str(target_id),
            "type": edge_type,
            "properties": {},
        }
    )


def _map_node(raw: Any, *, expected_label: str) -> dict[str, Any] | None:
    """把 kg3 节点映射为内部证据节点（id/name/type/properties）。"""

    if raw is None:
        return None
    if hasattr(raw, "items"):
        properties = dict(raw.items())
    elif isinstance(raw, dict):
        properties = dict(raw)
    else:
        return None
    entity_type = KG_ENTITY_LABELS.get(expected_label)
    if entity_type is None:
        return None
    name_props = (*KG_DEVICE_NAME_PROPS, "措施名称", "条款号")
    if expected_label == "触发条件":
        # 新数据把判据写成 C1-1 之类的短名，展示时优先用可读的判据口径。
        name_props = ("判据口径", "说明", *name_props)
    name = ""
    for prop in name_props:
        value = properties.get(prop)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    node_id = properties.get("id") or properties.get("实例ID") or properties.get("measure_id") or name
    if not node_id:
        return None
    return {
        "id": str(node_id),
        "name": name or str(node_id),
        "type": entity_type,
        "labels": [expected_label],
        "properties": properties,
    }


def _dedupe(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in nodes:
        node_id = str(node.get("id"))
        if node_id in seen:
            continue
        seen.add(node_id)
        result.append(node)
    return result
