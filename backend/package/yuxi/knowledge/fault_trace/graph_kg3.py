"""TuGraph 故障图谱适配器。

按故障图谱 schema 读取设备、设备类型、故障现象、故障原因、触发条件、处置措施与电气拓扑，
映射为 ``FaultGraphEvidence``（内部英文类型）。故障模式与故障后果已从图谱移除；运行时只走
``设备类型 -[具有现象]-> 故障现象 -[具有原因]-> 故障原因``，每个设备类型拥有自己的现象节点。
读接口只执行单条只读 MATCH；标签/关系名来自 ``contracts`` 白名单，变量全部参数化。
运行时图名由管理员配置决定，不在适配器内写死。
"""

from __future__ import annotations

import re
from typing import Any

from yuxi.knowledge.device_index import (
    DEVICE_RECALL_MAX_RECORDS,
    DEVICE_RECALL_TOKEN_LIMIT,
    device_recall_tokens,
    fallback_score,
    get_device_name_index,
)
from yuxi.knowledge.fault_trace.contracts import (
    DEVICE_CLASS_TO_TYPE,
    ENTITY_CAUSE,
    ENTITY_DEVICE,
    ENTITY_PHENOMENON,
    KG_DEVICE_LABEL,
    KG_DEVICE_NAME_PROPS,
    KG_DEVICE_TYPE_LABEL,
    KG_ENTITY_LABELS,
    LEDGER_ID_PLACEHOLDERS,
    KG_RELATION_TYPES,
    REL_DEVICE_TYPE,
    UNMAPPED_STATION_CLASSES,
)
from yuxi.knowledge.fault_trace.graph import FaultGraphEvidence

QUERY_LIMIT = 200


def find_devices(client: Any, names: list[str], *, graph_key: str = "") -> list[dict[str, Any]]:
    """按名称精确/包含匹配设备实例；都未命中时依次用词元与设备名索引扩大召回。

    用户与意图识别经常带电压等级前缀（``110kV银开线``）或序号口语写法（``1号主变``），
    故障图设备名是不带前缀的 ``银开线`` / ``#1主变…``，因此每个名称先展开成等价的
    查询写法，再执行精确/包含匹配；仍没有结果时用词元匹配与设备名索引兜底，避免
    “图谱里有这台设备但按名称没查到”。索引只返回图谱里已存在的设备，不造新设备。
    """

    evidence = FaultGraphEvidence()
    lookup_names = list(dict.fromkeys(item for name in names for item in _device_lookup_names(name)))

    exact: list[dict[str, Any]] = []
    for name in lookup_names:
        records = client.run(
            "MATCH (n) WHERE labels(n)[0] = $label AND coalesce(n.名称, '') = $name RETURN n LIMIT $node_limit",
            label=KG_DEVICE_LABEL,
            name=name,
            node_limit=QUERY_LIMIT,
        )
        exact.extend(_add_nodes(evidence, records, "n", KG_DEVICE_LABEL))
    if exact:
        return _dedupe(exact)

    contains: list[dict[str, Any]] = []
    for name in lookup_names:
        records = client.run(
            "MATCH (n) WHERE labels(n)[0] = $label "
            "AND (coalesce(n.名称, '') CONTAINS $kw OR coalesce(n.显示名, '') CONTAINS $kw) "
            "RETURN n LIMIT $node_limit",
            label=KG_DEVICE_LABEL,
            kw=name,
            node_limit=QUERY_LIMIT,
        )
        contains.extend(_add_nodes(evidence, records, "n", KG_DEVICE_LABEL))
    if contains:
        return _dedupe(contains)

    return _find_devices_by_fallback(client, names, graph_key=graph_key)


def _find_devices_by_fallback(client: Any, names: list[str], *, graph_key: str) -> list[dict[str, Any]]:
    """兜底召回：词元命中与设备名索引合并，按命中质量排序后截断。

    泛词元（“间隔”“线路”）可能命中上百条，统一给 1 分会让目标被名称排序挤出候选；
    这里按词元命中数与索引排名打分，并限制交给后续消歧/卡片的记录数。
    """

    tokens = device_recall_tokens(" ".join(names))
    token_records = _find_devices_by_tokens(client, tokens)
    index_records = _find_devices_by_index(client, names, graph_key=graph_key) if graph_key else []

    ranked: dict[str, tuple[float, dict[str, Any]]] = {}
    for record in token_records:
        device_id = str(record.get("id") or "")
        if not device_id:
            continue
        score = fallback_score(str(record.get("name") or ""), tokens)
        current = ranked.get(device_id)
        if current is None or score > current[0]:
            ranked[device_id] = (score, record)
    for rank, record in enumerate(index_records):
        device_id = str(record.get("id") or "")
        if not device_id:
            continue
        score = fallback_score(str(record.get("name") or ""), tokens, index_rank=rank)
        current = ranked.get(device_id)
        if current is None or score > current[0]:
            ranked[device_id] = (score, record)
    ordered = sorted(ranked.values(), key=lambda item: (-item[0], str(item[1].get("name") or "")))
    return [record for _score, record in ordered[:DEVICE_RECALL_MAX_RECORDS]]


def _find_devices_by_tokens(client: Any, tokens: list[str]) -> list[dict[str, Any]]:
    """按名称词元召回：名称里出现任一高区分度词元即视为候选。"""

    tokens = list(dict.fromkeys(token for token in tokens if len(token) >= 2))[:DEVICE_RECALL_TOKEN_LIMIT]
    if not tokens:
        return []
    evidence = FaultGraphEvidence()
    found: list[dict[str, Any]] = []
    for token in tokens:
        records = client.run(
            "MATCH (n) WHERE labels(n)[0] = $label "
            "AND (coalesce(n.名称, '') CONTAINS $kw OR coalesce(n.显示名, '') CONTAINS $kw) "
            "RETURN n LIMIT $node_limit",
            label=KG_DEVICE_LABEL,
            kw=token,
            node_limit=QUERY_LIMIT,
        )
        found.extend(_add_nodes(evidence, records, "n", KG_DEVICE_LABEL))
    return _dedupe(found)


def _find_devices_by_index(client: Any, names: list[str], *, graph_key: str) -> list[dict[str, Any]]:
    """按设备名索引召回：归一化/近似匹配后按 id 取回图谱节点，保留索引排序。"""

    index = get_device_name_index(
        client,
        graph_key=graph_key,
        label=KG_DEVICE_LABEL,
        name_props=KG_DEVICE_NAME_PROPS,
    )
    if index is None:
        return []
    ordered_ids: list[str] = []
    for name in names:
        ordered_ids.extend(entry.device_id for entry in index.search(name))
    ordered_ids = list(dict.fromkeys(ordered_ids))
    if not ordered_ids:
        return []
    records = client.run(
        "MATCH (n) WHERE labels(n)[0] = $label AND " + _in_condition("id") + " RETURN n LIMIT $node_limit",
        label=KG_DEVICE_LABEL,
        ids=ordered_ids,
        node_limit=QUERY_LIMIT,
    )
    evidence = FaultGraphEvidence()
    nodes = _add_nodes(evidence, records, "n", KG_DEVICE_LABEL)
    by_id = {str(node.get("id") or ""): node for node in nodes}
    return [by_id[device_id] for device_id in ordered_ids if device_id in by_id]


def _in_condition(prop: str) -> str:
    """生成 ``coalesce(n.属性, '') IN $ids`` 条件片段。"""

    return f"coalesce(n.{prop}, '') IN $ids"


# 电压等级前缀：用户常写“110kV银开线”，故障图设备名是“银开线”。
_VOLTAGE_PREFIX_PATTERN = re.compile(r"^\s*\d+(?:\.\d+)?\s*(?:[kK][vV]|千伏)\s*")
# 序号口语写法：“1号主变”对应故障图的“#1主变…/1#主变…/1主变…”。
_ORDINAL_NAME_PATTERN = re.compile(r"^\s*(?P<number>\d+)\s*号\s*(?P<rest>\S.*)$")


def _device_lookup_names(name: str) -> list[str]:
    """把一个设备名称展开成故障图里可能使用的查询写法。"""

    clean = str(name or "").strip()
    if not clean:
        return []
    candidates = [clean]
    without_voltage = _VOLTAGE_PREFIX_PATTERN.sub("", clean).strip()
    if without_voltage and without_voltage != clean:
        candidates.append(without_voltage)
    for base in list(candidates):
        match = _ORDINAL_NAME_PATTERN.match(base)
        if match:
            number = match.group("number")
            rest = match.group("rest").strip()
            candidates.extend((f"#{number}{rest}", f"{number}#{rest}", f"{number}{rest}"))
    return list(dict.fromkeys(item for item in candidates if item))


def resolve_device_type(client: Any, device: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """解析设备类型：设备 id 的 ``属于`` 边 → 台账分类映射 → 台账 ID 的 ``属于`` 边。

    ``属于`` 边按设备 id 匹配，多条不同类型边按 fail-closed 返回 ``(None, None)``：图谱里
    没有多类型事实（kg5 当前 0 台），出现多类型说明数据有问题，猜一个会把现象/原因算错。
    台账 ID 只在设备没有 id 时兜底，且跳过补充数据占位值、要求唯一类型。
    """

    device_evidence = FaultGraphEvidence(nodes=[device], edges=[])
    properties = device.get("properties") or {}
    device_id = str(device.get("id") or "")
    ledger_id = str(properties.get("台账ID列表") or "")

    if device_id:
        type_nodes = _belongs_to_types(
            client,
            device_evidence,
            condition="coalesce(a.id, '') = $device_id",
            device_id=device_id,
        )
        if type_nodes:
            return _unique_type(type_nodes)

    device_class = str(properties.get("设备分类") or properties.get("台账分类") or "").strip()
    if device_class in UNMAPPED_STATION_CLASSES:
        return None, None
    mapped = DEVICE_CLASS_TO_TYPE.get(device_class)
    if mapped:
        records = client.run(
            "MATCH (t) WHERE labels(t)[0] = $label AND coalesce(t.名称, '') = $name RETURN t LIMIT $node_limit",
            label=KG_DEVICE_TYPE_LABEL,
            name=mapped,
            node_limit=QUERY_LIMIT,
        )
        type_nodes = _add_nodes(device_evidence, records, "t", KG_DEVICE_TYPE_LABEL)
        if type_nodes:
            return _first_type(type_nodes)

    if not device_id and ledger_id and ledger_id not in LEDGER_ID_PLACEHOLDERS:
        type_nodes = _belongs_to_types(
            client,
            device_evidence,
            condition="coalesce(a.台账ID列表, '') = $ledger_id",
            ledger_id=ledger_id,
        )
        if type_nodes:
            return _unique_type(type_nodes)
    return None, None


def _unique_type(type_nodes: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None]:
    """类型节点唯一时才采用；多类型视为数据歧义，返回 ``(None, None)``。"""

    names = {str(node.get("name") or "") for node in type_nodes if str(node.get("name") or "")}
    if len(names) != 1:
        return None, None
    return _first_type(type_nodes)


def _belongs_to_types(
    client: Any,
    evidence: FaultGraphEvidence,
    *,
    condition: str,
    **params: Any,
) -> list[dict[str, Any]]:
    """按给定等值条件读取设备 ``属于`` 的设备类型节点。"""

    records = client.run(
        "MATCH (a)-[r]->(b) WHERE type(r) = $rel AND labels(a)[0] = $src AND labels(b)[0] = $dst "
        f"AND {condition} "
        "RETURN b LIMIT $node_limit",
        rel="属于",
        src=KG_DEVICE_LABEL,
        dst=KG_DEVICE_TYPE_LABEL,
        node_limit=QUERY_LIMIT,
        **params,
    )
    return _add_nodes(evidence, records, "b", KG_DEVICE_TYPE_LABEL)


def _first_type(type_nodes: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None]:
    """在一组类型节点中取名称/id 最小的一个；名称缺失时按无法确定处理。

    TuGraph 4.5.2 的 cypher 计划器不接受 ``ORDER BY coalesce(...)``，返回顺序本身也不稳定，
    排序只能在客户端完成。
    """

    first = min(type_nodes, key=lambda node: (str(node.get("name") or ""), str(node.get("id") or "")))
    name = str(first.get("name") or "").strip()
    return (name, first) if name else (None, None)


def device_ancestors(client: Any, device: dict[str, Any]) -> list[str]:
    """取设备在故障图里的上级设备名称（``位于`` 边），用于同名设备消歧。"""

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
    """采集因果链、触发条件、处置措施与电气拓扑。"""

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

    phenomenon_ids, phenomenon_names = _collect_phenomena(
        client,
        evidence,
        type_ids=type_ids,
        type_names=type_names,
    )
    cause_ids, cause_names = _collect_causes(
        client,
        evidence,
        phenomenon_ids=phenomenon_ids,
        phenomenon_names=phenomenon_names,
    )
    _collect_triggers(client, evidence, cause_ids=cause_ids, cause_names=cause_names)
    _collect_measures(client, evidence, cause_ids=cause_ids, cause_names=cause_names)
    _collect_topology(client, evidence, device_ids=device_ids)
    _collect_device_types(client, evidence)
    return evidence


def _collect_phenomena(
    client: Any,
    evidence: FaultGraphEvidence,
    *,
    type_ids: list[str],
    type_names: list[str],
) -> tuple[list[str], list[str]]:
    """只读当前设备类型自己的 ``具有现象`` 边；共享现象已拆成每类型独立节点。"""

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
) -> tuple[list[str], list[str]]:
    """只读当前设备类型现象节点的 ``具有原因`` 边；原因不再跨现象/设备类型共享。"""

    edges = _query_related(
        client,
        source_label="故障现象",
        relation="具有原因",
        target_label="故障原因",
        source_ids=phenomenon_ids,
        source_names=phenomenon_names,
    )
    _add_edges(evidence, edges, "故障现象", "故障原因")
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
    """把故障图节点映射为内部证据节点（id/name/type/properties）。"""

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
