"""独立 TuGraph ``kg`` 图的故障溯源适配器。

方案 A：故障溯源数据由人工录入到外部 TuGraph 图 ``kg``（中文标签，独立于 Yuxi
自管的 default 图）。本模块直接连接该图，按中文标签/关系查询因果链与电气拓扑，
把结果映射为 ``FaultGraphEvidence``；装配逻辑（``answer``/``graph``）只消费内部
英文类型，不感知双语差异。

TuGraph 实测行为（4.5.2）：只读查询一个语句最多一个 MATCH；中文标签在 pattern
中只有注册后可用且不能加反引号；``record.data()`` 返回的节点是属性字典，关系
用 ``type(r)`` 单独投影；反引号属性访问返回空，中文属性用裸标识符。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from neo4j import GraphDatabase

from yuxi.knowledge.fault_trace.contracts import (
    KG_DEVICE_LABEL,
    KG_DEVICE_NAME_PROPS,
    KG_ENTITY_LABELS,
    KG_RELATION_TYPES,
)
from yuxi.knowledge.fault_trace.graph import FaultGraphEvidence
from yuxi.utils import logger

KG_QUERY_LIMIT = 200


class KgGraphError(Exception):
    """kg 图连接或查询失败。"""


@dataclass(frozen=True)
class KgGraphConfig:
    """外部故障图谱连接配置。"""

    uri: str
    graph: str
    username: str
    password: str


class KgGraphClient:
    """连接独立 ``kg`` 图的只读客户端（同步方法，Boost 驱动）。

    连接参数来自管理员配置 ``fault_trace_opts``；未配置时抛 ``KgGraphError``，
    由调用方投影为“故障溯源知识库未配置”。
    """

    def __init__(self, config: KgGraphConfig) -> None:
        self._config = config
        self._driver = None

    def ensure_connected(self) -> None:
        if self._driver is not None:
            try:
                with self._driver.session() as session:
                    session.run("RETURN 1")
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"kg 图连接失效，重新连接: {exc}")
                self.close()
        try:
            self._driver = GraphDatabase.driver(
                self._config.uri,
                auth=(self._config.username, self._config.password),
                database=self._config.graph,
            )
            with self._driver.session() as session:
                session.run("RETURN 1")
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise KgGraphError(f"无法连接故障溯源知识图谱（{self._config.uri}/{self._config.graph}）: {exc}") from exc

    def run(self, cypher: str, **params) -> list[dict[str, Any]]:
        """执行一条只读查询并返回记录列表。"""
        self.ensure_connected()
        try:
            with self._driver.session() as session:
                return [record.data() for record in session.run(cypher, **params)]
        except Exception as exc:  # noqa: BLE001
            raise KgGraphError(f"故障图谱查询失败: {exc}") from exc

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            finally:
                self._driver = None


def collect_kg_fault_evidence(
    client: KgGraphClient,
    *,
    device_names: list[str],
    device_type: str | None,
    phenomena: list[str],
    image_fault_name: str | None,
) -> FaultGraphEvidence:
    """按中文标签查询 kg 图并组装为内部证据集合。

    - 设备实例节点按 名称/显示名/name 关键词匹配；
    - 故障现象关键词反向匹配故障模式（SRS：以故障现象为起点沿 has_phenomenon 反查）；
    - 设备经 ``存在故障模式`` 边挂到故障模式；
    - 因果链（表现为/起因/处理/诱发条件）与电气拓扑（电连接）逐跳收集。
    """

    evidence = FaultGraphEvidence()
    node_ids: set[str] = set()
    edge_ids: set[str] = set()

    def add_node(raw: Any, expected_label: str) -> dict[str, Any] | None:
        mapped = _map_node(raw, expected_label=expected_label)
        if mapped is None:
            return None
        existing = evidence.node_by_id(mapped["id"])
        if existing is not None:
            return existing
        node_ids.add(mapped["id"])
        evidence.nodes.append(mapped)
        return mapped

    def add_edges(records: list[dict[str, Any]], source_label: str, target_label: str) -> None:
        for record in records:
            source = add_node(record.get("a"), source_label)
            target = add_node(record.get("b"), target_label)
            edge_type = _map_relation(record.get("rel_type"))
            if source is None or target is None or not edge_type:
                continue
            edge_id = f"{source['id']}->{edge_type}->{target['id']}"
            if edge_id in edge_ids:
                continue
            edge_ids.add(edge_id)
            evidence.edges.append(
                {
                    "id": edge_id,
                    "source_id": source["id"],
                    "target_id": target["id"],
                    "type": edge_type,
                    "properties": {},
                }
            )

    # 1) 设备实例：按设备名/类型关键词匹配
    device_ids: list[str] = []
    device_names_seen: list[str] = []
    for keyword in [*device_names, device_type]:
        clean = str(keyword or "").strip()
        if not clean:
            continue
        records = _query_nodes_by_keyword(client, label=KG_DEVICE_LABEL, keyword=clean)
        for record in records:
            mapped = add_node(record.get("n"), KG_DEVICE_LABEL)
            if mapped and mapped["id"] not in device_ids:
                device_ids.append(mapped["id"])
                device_names_seen.append(mapped["name"])

    # 2) 故障现象关键词 → 反向匹配故障模式（表现为）
    for keyword in [*phenomena, image_fault_name]:
        clean = str(keyword or "").strip()
        if not clean:
            continue
        records = _query_phenomenon_modes(client, keyword=clean)
        add_edges(records, "故障模式", "故障现象")

    # 3) 设备 →[存在故障模式]→ 故障模式
    if device_ids:
        records = _query_relations(
            client,
            source_label=KG_DEVICE_LABEL,
            relation="存在故障模式",
            target_label="故障模式",
            source_ids=device_ids[:50],
            source_names=device_names_seen[:50],
        )
        add_edges(records, KG_DEVICE_LABEL, "故障模式")

    # 4) 因果链：故障模式 →[表现为]→ 现象 / [起因]→ 原因 / [诱发条件]→ 诱发因素
    mode_nodes = [node for node in evidence.nodes if node.get("type") == "fault_mode"]
    mode_ids = [node["id"] for node in mode_nodes]
    mode_names = [node["name"] for node in mode_nodes]
    for relation, target_label in (("表现为", "故障现象"), ("起因", "故障原因"), ("诱发条件", "诱发因素")):
        records = _query_relations(
            client,
            source_label="故障模式",
            relation=relation,
            target_label=target_label,
            source_ids=mode_ids[:50],
            source_names=mode_names[:50],
        )
        add_edges(records, "故障模式", target_label)

    # 5) 处置措施：故障原因 →[处理]→ 处置措施
    cause_nodes = [node for node in evidence.nodes if node.get("type") == "cause"]
    cause_ids = [node["id"] for node in cause_nodes]
    cause_names = [node["name"] for node in cause_nodes]
    if cause_ids or cause_names:
        records = _query_relations(
            client,
            source_label="故障原因",
            relation="处理",
            target_label="处置措施",
            source_ids=cause_ids[:50],
            source_names=cause_names[:50],
        )
        add_edges(records, "故障原因", "处置措施")

    # 6) 电气拓扑：设备实例 →[电连接]→ 设备实例
    topo_records = _query_topology(client, device_ids=device_ids[:50], device_names=device_names_seen[:50])
    add_edges(topo_records, KG_DEVICE_LABEL, KG_DEVICE_LABEL)

    return evidence


def _query_nodes_by_keyword(client: KgGraphClient, *, label: str, keyword: str) -> list[dict[str, Any]]:
    conditions = " OR ".join(f"coalesce(n.{prop}, '') CONTAINS $kw" for prop in KG_DEVICE_NAME_PROPS)
    return client.run(
        f"MATCH (n) WHERE labels(n)[0] = $label AND ({conditions}) RETURN n LIMIT $node_limit",
        label=label,
        kw=keyword,
        node_limit=KG_QUERY_LIMIT,
    )


def _query_phenomenon_modes(client: KgGraphClient, *, keyword: str) -> list[dict[str, Any]]:
    """以故障现象关键词反向匹配故障模式（现象 →[表现为]← 模式）。"""

    return client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src AND type(r) = $rel AND labels(b)[0] = $dst "
        "AND (coalesce(b.名称, '') CONTAINS $kw OR coalesce(b.显示名, '') CONTAINS $kw) "
        "RETURN a, type(r) AS rel_type, b LIMIT $node_limit",
        src="故障模式",
        rel="表现为",
        dst="故障现象",
        kw=keyword,
        node_limit=KG_QUERY_LIMIT,
    )


def _query_relations(
    client: KgGraphClient,
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
    clauses = []
    if ids:
        clauses.append("coalesce(a.id, '') IN $ids")
    if names:
        clauses.extend(f"coalesce(a.{prop}, '') IN $names" for prop in KG_DEVICE_NAME_PROPS)
    source_match = " OR ".join(clauses)
    return client.run(
        f"MATCH (a)-[r]->(b) WHERE labels(a)[0] = $src "
        f"AND type(r) = $rel AND labels(b)[0] = $dst "
        f"AND ({source_match}) RETURN a, type(r) AS rel_type, b LIMIT $node_limit",
        src=source_label,
        rel=relation,
        dst=target_label,
        ids=ids,
        names=names,
        node_limit=KG_QUERY_LIMIT,
    )


def _query_topology(
    client: KgGraphClient,
    *,
    device_ids: list[str],
    device_names: list[str],
) -> list[dict[str, Any]]:
    ids = [str(item) for item in (device_ids or []) if item]
    names = [str(item) for item in (device_names or []) if item]
    if not ids and not names:
        return []
    clauses = []
    if ids:
        clauses.append("coalesce(a.id, '') IN $ids")
        clauses.append("coalesce(b.id, '') IN $ids")
    if names:
        clauses.extend(f"coalesce(a.{prop}, '') IN $names" for prop in KG_DEVICE_NAME_PROPS)
        clauses.extend(f"coalesce(b.{prop}, '') IN $names" for prop in KG_DEVICE_NAME_PROPS)
    endpoint_match = " OR ".join(clauses)
    return client.run(
        "MATCH (a)-[r]->(b) WHERE labels(a)[0] = $label AND labels(b)[0] = $label "
        f"AND type(r) = $rel AND ({endpoint_match}) "
        "RETURN a, type(r) AS rel_type, b LIMIT $node_limit",
        label=KG_DEVICE_LABEL,
        rel="电连接",
        ids=ids,
        names=names,
        node_limit=KG_QUERY_LIMIT,
    )


def _map_node(raw: Any, *, expected_label: str) -> dict[str, Any] | None:
    """把 kg 节点映射为内部证据节点（id/name/type/properties）。

    ``record.data()`` 返回的节点是属性字典；标签由查询上下文（``expected_label``）
    给出，因为 TuGraph 的 ``labels(n)`` 返回字符串而非列表。
    """

    if raw is None:
        return None
    if hasattr(raw, "items"):
        props = dict(raw.items())
    elif isinstance(raw, dict):
        props = dict(raw)
    else:
        return None
    entity_type = KG_ENTITY_LABELS.get(expected_label)
    if entity_type is None:
        return None
    name = ""
    for prop in (*KG_DEVICE_NAME_PROPS, "措施名称", "条款号"):
        value = props.get(prop)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    node_id = props.get("id") or props.get("实例ID") or props.get("measure_id") or name
    if not node_id:
        return None
    return {
        "id": str(node_id),
        "name": name or str(node_id),
        "type": entity_type,
        "labels": [expected_label],
        "properties": props,
        "normalized": {"name": name or str(node_id), "type": entity_type, "source": "kg"},
    }


def _map_relation(raw_type: Any) -> str | None:
    """把 kg 中文关系类型映射为内部英文类型。"""

    if not isinstance(raw_type, str):
        return None
    return KG_RELATION_TYPES.get(raw_type)
