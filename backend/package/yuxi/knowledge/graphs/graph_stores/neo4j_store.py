"""Neo4j 图谱存储实现。

保留 Yuxi 既有的多标签隔离模型：每个知识库使用
``:Chunk:MilvusKB:`kb_id``` / ``:Entity:MilvusKB:`kb_id``` 标签，
因此已有 Neo4j 图谱数据无需迁移。
"""

from __future__ import annotations

import json
from typing import Any

from yuxi.knowledge.graphs.graph_utils import (
    build_entity_records,
    build_graph_payload,
    build_triple_records,
    compute_triple_id,
    cypher_merge_chunk,
    cypher_merge_entity_mention,
    cypher_merge_relation,
    normalize_entity_name,
    normalize_graph_edge,
    normalize_graph_node,
)
from yuxi.storage.graph_connection import safe_graph_label
from yuxi.utils import logger


class Neo4jGraphStore:
    backend = "neo4j"

    def __init__(self, connection=None) -> None:
        self.connection = connection

    @property
    def driver(self):
        return self.connection.driver

    def _write(self, query):
        """优先走连接管理器的写事务；兼容仅暴露 driver 的测试替身。"""
        if hasattr(self.connection, "write"):
            return self.connection.write(query)
        with self.driver.session() as session:
            return session.execute_write(query)

    def _read(self, cypher: str, **kwargs) -> list[dict[str, Any]]:
        if hasattr(self.connection, "read"):
            return self.connection.read(cypher, **kwargs)
        with self.driver.session() as session:
            return [record.data() for record in session.run(cypher, **kwargs)]

    def ensure_ready(self, kb_id: str) -> None:
        """Neo4j 无强 Schema，无需预建标签。"""

    # ─── 写入 ───────────────────────────────────────────────

    def write_chunk_graph(
        self, kb_id: str, chunk, normalized_result: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """将单个 chunk 的抽取结果写入 Neo4j。"""
        label = safe_graph_label(kb_id)
        graph_payload = build_graph_payload(normalized_result)
        relation_extractor_type = graph_payload["metadata"].get("extractor_type", "unknown")
        entities = graph_payload["entities"]
        relations = graph_payload["relations"]
        entity_by_id = {entity["id"]: entity for entity in entities}
        entity_records = build_entity_records(kb_id, entities)
        entity_record_by_local_id = {
            entity["id"]: record for entity, record in zip(entities, entity_records, strict=True)
        }
        triple_records = build_triple_records(kb_id, relations, entity_record_by_local_id, graph_payload)
        content_preview = (chunk.content or "")[:300]

        # 预构建 Cypher 模板（同一 chunk 内复用）
        merge_chunk_cypher = cypher_merge_chunk(label)
        merge_entity_cypher = cypher_merge_entity_mention(label)
        merge_relation_cypher = cypher_merge_relation(label)

        def query(tx):
            # 1. MERGE Chunk 节点
            tx.run(
                merge_chunk_cypher,
                chunk_id=chunk.chunk_id,
                file_id=chunk.file_id,
                kb_id=kb_id,
                chunk_index=chunk.chunk_index,
                content_preview=content_preview,
                start_char_pos=chunk.start_char_pos,
                end_char_pos=chunk.end_char_pos,
            )

            # 2. MERGE Entity 节点 + Chunk→Entity (MENTIONS)
            for entity in entities:
                entity_record = entity_record_by_local_id[entity["id"]]
                tx.run(
                    merge_entity_cypher,
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    kb_id=kb_id,
                    entity_id=entity_record["entity_id"],
                    normalized_name=normalize_entity_name(entity["text"]),
                    entity_label=entity.get("label") or "Entity",
                    name=entity["text"],
                    attributes=json.dumps(entity.get("attributes") or [], ensure_ascii=False),
                )

            # 3. MERGE Entity→Entity (RELATION) 边
            for relation in relations:
                source = entity_by_id[relation["source"]]
                target = entity_by_id[relation["target"]]
                source_record = entity_record_by_local_id[relation["source"]]
                target_record = entity_record_by_local_id[relation["target"]]
                relation_type = relation.get("label") or "RELATED_TO"
                triple_id = compute_triple_id(
                    kb_id,
                    source_record["normalized_name"],
                    source_record["label"],
                    relation_type,
                    target_record["normalized_name"],
                    target_record["label"],
                )
                tx.run(
                    merge_relation_cypher,
                    kb_id=kb_id,
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    source_name=normalize_entity_name(source["text"]),
                    source_label=source.get("label") or "Entity",
                    target_name=normalize_entity_name(target["text"]),
                    target_label=target.get("label") or "Entity",
                    relation_type=relation_type,
                    triple_id=triple_id,
                    text=relation["text"],
                    extractor_type=relation_extractor_type,
                )

        self._write(query)
        return entity_records, triple_records

    # ─── 删除 ───────────────────────────────────────────────

    def delete_graph(self, kb_id: str) -> None:
        label = safe_graph_label(kb_id)

        def query(tx):
            tx.run(f"MATCH (n:MilvusKB:`{label}`) DETACH DELETE n")

        self._write(query)

    def delete_file_graph(
        self,
        kb_id: str,
        file_id: str,
        orphan_entity_ids: list[str] | None = None,
        orphan_triple_ids: list[str] | None = None,
    ) -> None:
        # Neo4j 按 chunk 存多条关系边，可在图内按 file_id 删除并重算孤儿；
        # 外部孤儿列表（含 orphan_triple_ids）由 TuGraph 实现使用，这里无需消费。
        del orphan_entity_ids, orphan_triple_ids
        label = safe_graph_label(kb_id)

        def query(tx):
            tx.run(
                f"""
                MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION {{kb_id: $kb_id, file_id: $file_id}}]->
                    (:Entity:MilvusKB:`{label}`)
                DELETE r
                """,
                kb_id=kb_id,
                file_id=file_id,
            )
            tx.run(
                f"""
                MATCH (:Chunk:MilvusKB:`{label}` {{kb_id: $kb_id, file_id: $file_id}})-[m:MENTIONS]->
                    (e:Entity:MilvusKB:`{label}`)
                DELETE m
                WITH DISTINCT e
                WHERE NOT ()-[:MENTIONS]->(e)
                DETACH DELETE e
                """,
                kb_id=kb_id,
                file_id=file_id,
            )
            tx.run(
                f"""
                MATCH (c:Chunk:MilvusKB:`{label}` {{kb_id: $kb_id, file_id: $file_id}})
                DETACH DELETE c
                """,
                kb_id=kb_id,
                file_id=file_id,
            )

        self._write(query)

    # ─── 查询 ───────────────────────────────────────────────

    def query_nodes(
        self,
        kb_id: str,
        *,
        keyword: str = "",
        max_depth: int = 1,
        max_nodes: int = 50,
        exclude_chunk: bool = False,
    ) -> dict[str, Any]:
        label = safe_graph_label(kb_id)
        try:
            return self._query_nodes_sync(kb_id, label, keyword, max_nodes, max_depth, exclude_chunk)
        except Exception as e:
            logger.error(f"Milvus graph query failed: {e}")
            return {"nodes": [], "edges": []}

    def _query_nodes_sync(
        self,
        kb_id: str,
        label: str,
        keyword: str,
        limit: int,
        max_depth: int,
        exclude_chunk: bool,
    ) -> dict[str, Any]:
        with self.driver.session() as session:
            query_params: dict[str, Any] = {
                "keyword": keyword,
                "limit": limit,
            }
            if max_depth > 0:
                max_depth = min(max_depth, 3)
                query_params["path_limit"] = max(limit, 1) * 10
            result = session.run(
                self._build_query(label, keyword, limit, max_depth, exclude_chunk),
                **query_params,
            )
            if max_depth <= 0:
                return self._process_query_result(result, limit, kb_id, exclude_chunk)
            record = result.single()
            if not record:
                return {"nodes": [], "edges": []}
            return self._process_subgraph_record(record, limit, kb_id)

    def query_seed_subgraph(self, kb_id: str, *, entity_ids: list[str], max_nodes: int) -> dict[str, Any]:
        if not entity_ids:
            return {"nodes": [], "edges": []}
        seed_entity_ids = list(dict.fromkeys(entity_ids))
        label = safe_graph_label(kb_id)
        cypher = f"""
        MATCH (seed:Entity:MilvusKB:`{label}`)
        WHERE seed.entity_id IN $entity_ids
        MATCH p = (seed)-[*1..2]-(n:MilvusKB:`{label}`)
        WITH p LIMIT $path_limit
        WITH collect(p) AS paths
        UNWIND paths AS node_path
        UNWIND nodes(node_path) AS node
        WITH paths, collect(DISTINCT node) AS graph_nodes
        UNWIND paths AS rel_path
        UNWIND relationships(rel_path) AS rel
        RETURN graph_nodes AS nodes, collect(DISTINCT rel) AS edges
        """
        try:
            with self.driver.session() as session:
                record = session.run(
                    cypher,
                    entity_ids=seed_entity_ids,
                    path_limit=max(max_nodes, 1) * 4,
                ).single()
            if not record:
                return {"nodes": [], "edges": []}
            return self._process_subgraph_record(record, max_nodes, kb_id)
        except Exception as e:
            logger.error(f"Milvus seed subgraph query failed: {e}")
            return {"nodes": [], "edges": []}

    def get_labels(self, kb_id: str) -> list[str]:
        label = safe_graph_label(kb_id)
        cypher = f"""
        MATCH (n:MilvusKB:`{label}`)
        UNWIND labels(n) AS node_label
        WITH DISTINCT node_label
        WHERE node_label <> 'MilvusKB' AND node_label <> $kb_id
        RETURN node_label
        ORDER BY node_label
        """
        try:
            records = self._read(cypher, kb_id=kb_id)
            return [record["node_label"] for record in records]
        except Exception as e:
            logger.error(f"Failed to get Milvus graph labels: {e}")
            return []

    def get_stats(self, kb_id: str) -> dict[str, Any]:
        label = safe_graph_label(kb_id)
        stats_cypher = f"""
        MATCH (n:MilvusKB:`{label}`)
        WITH count(n) AS node_count
        OPTIONAL MATCH (:MilvusKB:`{label}`)-[r]->(:MilvusKB:`{label}`)
        RETURN node_count, count(r) AS edge_count
        """
        label_cypher = f"""
        MATCH (n:Entity:MilvusKB:`{label}`)
        WITH n.label AS entity_label, count(*) AS instances
        RETURN entity_label, instances
        ORDER BY instances DESC
        """
        try:
            with self.driver.session() as session:
                stats = session.run(stats_cypher).single()
                label_stats = session.run(label_cypher)
                return {
                    "total_nodes": stats["node_count"] if stats else 0,
                    "total_edges": stats["edge_count"] if stats else 0,
                    "entity_types": [{"type": row["entity_label"], "count": row["instances"]} for row in label_stats],
                }
        except Exception as e:
            logger.error(f"Failed to get Milvus graph stats: {e}")
            return {"total_nodes": 0, "total_edges": 0, "entity_types": []}

    # ─── 内部工具 ───────────────────────────────────────────

    @staticmethod
    def _build_where(exclude_chunk: bool, keyword: str) -> str:
        clauses = []
        if exclude_chunk:
            clauses.append("NOT n:Chunk")
        if keyword and keyword != "*":
            clauses.append(
                "(toLower(coalesce(n.name, '')) CONTAINS toLower($keyword)"
                " OR toLower(coalesce(n.content_preview, '')) CONTAINS toLower($keyword)"
                " OR toLower(coalesce(n.chunk_id, '')) CONTAINS toLower($keyword))"
            )
        return "WHERE " + " AND ".join(clauses) if clauses else ""

    def _build_query(self, label: str, keyword: str, limit: int, max_depth: int, exclude_chunk: bool = False) -> str:
        where = self._build_where(exclude_chunk, keyword)

        if max_depth <= 0:
            return f"""
            MATCH (n:MilvusKB:`{label}`)
            {where}
            RETURN n AS h, null AS r, null AS t
            LIMIT $limit
            """

        path_node_filter = f"path_node:MilvusKB AND path_node:`{label}`"
        if exclude_chunk:
            path_node_filter += " AND NOT path_node:Chunk"

        return f"""
        MATCH (n:MilvusKB:`{label}`)
        {where}
        WITH n LIMIT $limit
        WITH collect(n) AS seeds
        UNWIND seeds AS seed
        OPTIONAL MATCH p = (seed)-[*1..{max_depth}]-(m:MilvusKB:`{label}`)
        WHERE all(path_node IN nodes(p) WHERE {path_node_filter})
        WITH seeds, p
        LIMIT $path_limit
        WITH seeds, collect(p) AS paths
        RETURN reduce(path_nodes = [], path IN paths | path_nodes + nodes(path)) + seeds AS nodes,
               reduce(path_edges = [], path IN paths | path_edges + relationships(path)) AS edges
        """

    def _process_query_result(self, result, limit: int, kb_id: str, exclude_chunk: bool = False) -> dict[str, Any]:
        nodes = []
        edges = []
        node_ids = set()
        edge_ids = set()

        for record in result:
            for key in ("h", "t"):
                raw_node = record.get(key)
                if raw_node is None:
                    continue
                node = normalize_graph_node(raw_node, kb_id)
                if not node or node["id"] in node_ids:
                    continue
                if exclude_chunk and node.get("type") == "Chunk":
                    continue
                nodes.append(node)
                node_ids.add(node["id"])
            raw_edge = record.get("r")
            if raw_edge is not None:
                edge = normalize_graph_edge(raw_edge)
                if edge and edge["id"] not in edge_ids:
                    edges.append(edge)
                    edge_ids.add(edge["id"])
            if len(nodes) >= limit:
                break

        return self._finalize_subgraph_result(nodes, edges, limit)

    def _process_subgraph_record(self, record: Any, limit: int, kb_id: str) -> dict[str, Any]:
        nodes = []
        edges = []
        node_ids = set()
        edge_ids = set()

        for raw_node in record.get("nodes") or []:
            node = normalize_graph_node(raw_node, kb_id)
            if not node or node["id"] in node_ids:
                continue
            nodes.append(node)
            node_ids.add(node["id"])
            if len(nodes) >= limit:
                break

        for raw_edge in record.get("edges") or []:
            edge = normalize_graph_edge(raw_edge)
            if not edge or edge["id"] in edge_ids:
                continue
            if edge["source_id"] not in node_ids or edge["target_id"] not in node_ids:
                continue
            edges.append(edge)
            edge_ids.add(edge["id"])

        return self._finalize_subgraph_result(nodes, edges, limit)

    @staticmethod
    def _finalize_subgraph_result(
        nodes: list[dict[str, Any]], edges: list[dict[str, Any]], limit: int
    ) -> dict[str, Any]:
        limit = max(0, limit)
        final_nodes = nodes[:limit]
        node_ids = {node["id"] for node in final_nodes}
        final_edges = [
            edge for edge in edges if edge.get("source_id") in node_ids and edge.get("target_id") in node_ids
        ]
        return {"nodes": final_nodes, "edges": final_edges[: limit * 2]}
