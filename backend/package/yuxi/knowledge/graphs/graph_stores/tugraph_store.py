"""TuGraph 图谱存储实现。

TuGraph 使用强 Schema，且每个点/边只能有一个标签，因此这里用固定的
``Chunk`` / ``Entity`` 点标签与 ``MENTIONS`` / ``RELATION`` 边标签，并用
``kb_id`` 属性隔离不同知识库（单一 ``default`` 子图），不再沿用 Neo4j 的
按知识库标签隔离模型。

同时规避 TuGraph 相对 openCypher 的差异：只读查询不使用多个 MATCH、模式谓词
（``exists((a)-[]->(b))``）、``all()``/``reduce()`` 与动态属性访问 ``n[expr]``；
子图查询改用 ``nodes(p)``/``relationships(p)`` 后在 Python 侧展开。
"""

from __future__ import annotations

import json
from typing import Any

from yuxi.knowledge.graphs.graph_utils import (
    build_entity_records,
    build_graph_payload,
    build_triple_records,
    cypher_tugraph_merge_chunk,
    cypher_tugraph_merge_entity_mention,
    cypher_tugraph_merge_relation,
    normalize_entity_name,
)
from yuxi.utils import logger

# 点/边标签的字段定义：(字段名, 类型, 是否可选)。主键字段放在最前。
_CHUNK_FIELDS = (
    ("chunk_id", "STRING", False),
    ("file_id", "STRING", True),
    ("kb_id", "STRING", True),
    ("chunk_index", "INT64", True),
    ("content_preview", "STRING", True),
    ("start_char_pos", "INT64", True),
    ("end_char_pos", "INT64", True),
)
_ENTITY_FIELDS = (
    ("entity_id", "STRING", False),
    ("kb_id", "STRING", True),
    ("normalized_name", "STRING", True),
    ("entity_label", "STRING", True),
    ("name", "STRING", True),
    ("attributes", "STRING", True),
)
_MENTIONS_FIELDS = (
    ("mention_id", "STRING", False),
    ("chunk_id", "STRING", True),
    ("entity_id", "STRING", True),
    ("file_id", "STRING", True),
    ("kb_id", "STRING", True),
)
_RELATION_FIELDS = (
    ("triple_id", "STRING", False),
    ("kb_id", "STRING", True),
    ("chunk_id", "STRING", True),
    ("file_id", "STRING", True),
    ("source_name", "STRING", True),
    ("target_name", "STRING", True),
    ("type", "STRING", True),
    ("text", "STRING", True),
    ("extractor_type", "STRING", True),
)


class TugraphGraphStore:
    backend = "tugraph"

    def __init__(self, connection) -> None:
        self.connection = connection
        self._schema_ready = False

    @property
    def driver(self):
        return self.connection.driver

    # ─── Schema 引导 ────────────────────────────────────────

    def ensure_ready(self, kb_id: str) -> None:
        """幂等创建 TuGraph 点/边标签，缺失时补齐。"""
        if self._schema_ready:
            return
        with self.connection.driver.session() as session:
            try:
                self._create_missing_labels(session)
            except Exception:
                # 并发首次写入时另一进程可能已建好标签（TuGraph 对已存在标签报 LabelExist），
                # 重查确认齐全后放行；只有标签仍缺失才向调用方失败。
                if not self._labels_present(session):
                    raise
        self._schema_ready = True

    def _create_missing_labels(self, session) -> None:
        vertex_labels, edge_labels = self._list_labels(session)
        if "Chunk" not in vertex_labels:
            session.run(self._create_vertex_label_cypher("Chunk", _CHUNK_FIELDS)).consume()
        if "Entity" not in vertex_labels:
            session.run(self._create_vertex_label_cypher("Entity", _ENTITY_FIELDS)).consume()
        if "MENTIONS" not in edge_labels:
            session.run(self._create_edge_label_cypher("MENTIONS", [["Chunk", "Entity"]], _MENTIONS_FIELDS)).consume()
        if "RELATION" not in edge_labels:
            session.run(self._create_edge_label_cypher("RELATION", [["Entity", "Entity"]], _RELATION_FIELDS)).consume()

    def _labels_present(self, session) -> bool:
        vertex_labels, edge_labels = self._list_labels(session)
        return {"Chunk", "Entity"} <= vertex_labels and {"MENTIONS", "RELATION"} <= edge_labels

    @staticmethod
    def _list_labels(session) -> tuple[set[str], set[str]]:
        # 4.5.x 的 db.edgeLabels() 返回 label 列；保留 edgeLabels 兜底以兼容旧版本。
        # 注意：neo4j.Record 的 `in` 按值迭代，不能用来判断键，须用 .get()。
        vertex_labels = {record["label"] for record in session.run("CALL db.vertexLabels()")}
        edge_labels = {
            (record.get("label") or record.get("edgeLabels")) for record in session.run("CALL db.edgeLabels()")
        }
        return vertex_labels, edge_labels

    @staticmethod
    def _create_vertex_label_cypher(label: str, fields: tuple[tuple[str, str, bool], ...]) -> str:
        """构造 ``db.createVertexLabel(label, primary_field, field, type, optional, ...)``。

        TuGraph 4.5.x 的 Bolt/GQL 解析器要求字段类型写为**带引号的字符串**
        （如 ``'STRING'``/``'INT64'``）；裸标识符（4.1.0 兼容形式）会触发
        ``visit(...) failed``。4.1.0 则是相反（带引号报 ``"STRING" has to be a variable``）。
        """
        primary_field = fields[0][0]
        parts = [f"'{label}'", f"'{primary_field}'"]
        for name, field_type, optional in fields:
            parts.extend([f"'{name}'", f"'{field_type}'", "true" if optional else "false"])
        return f"CALL db.createVertexLabel({', '.join(parts)})"

    @staticmethod
    def _create_edge_label_cypher(
        label: str, constraints: list[list[str]], fields: tuple[tuple[str, str, bool], ...]
    ) -> str:
        """构造 ``db.createEdgeLabel(label, constraints, field, type, optional, ...)``。"""
        parts = [f"'{label}'", f"'{json.dumps(constraints)}'"]
        for name, field_type, optional in fields:
            parts.extend([f"'{name}'", f"'{field_type}'", "true" if optional else "false"])
        return f"CALL db.createEdgeLabel({', '.join(parts)})"

    # ─── 写入 ───────────────────────────────────────────────

    def write_chunk_graph(
        self, kb_id: str, chunk, normalized_result: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """将单个 chunk 的抽取结果写入 TuGraph。"""
        self.ensure_ready(kb_id)
        graph_payload = build_graph_payload(normalized_result)
        entities = graph_payload["entities"]
        relations = graph_payload["relations"]
        entity_records = build_entity_records(kb_id, entities)
        entity_record_by_local_id = {
            entity["id"]: record for entity, record in zip(entities, entity_records, strict=True)
        }
        triple_records = build_triple_records(kb_id, relations, entity_record_by_local_id, graph_payload)
        normalized_name_by_entity_id = {record["entity_id"]: record["normalized_name"] for record in entity_records}
        content_preview = (chunk.content or "")[:300]

        def query(tx):
            tx.run(
                cypher_tugraph_merge_chunk(),
                chunk_id=chunk.chunk_id,
                file_id=chunk.file_id,
                kb_id=kb_id,
                chunk_index=chunk.chunk_index,
                content_preview=content_preview,
                start_char_pos=chunk.start_char_pos,
                end_char_pos=chunk.end_char_pos,
            )
            for entity in entities:
                entity_record = entity_record_by_local_id[entity["id"]]
                tx.run(
                    cypher_tugraph_merge_entity_mention(),
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    kb_id=kb_id,
                    entity_id=entity_record["entity_id"],
                    mention_id=f"{chunk.chunk_id}:{entity_record['entity_id']}",
                    normalized_name=normalize_entity_name(entity["text"]),
                    entity_label=entity.get("label") or "Entity",
                    name=entity["text"],
                    attributes=json.dumps(entity.get("attributes") or [], ensure_ascii=False),
                )
            for record in triple_records:
                tx.run(
                    cypher_tugraph_merge_relation(),
                    kb_id=kb_id,
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    triple_id=record["triple_id"],
                    source_entity_id=record["source_entity_id"],
                    target_entity_id=record["target_entity_id"],
                    source_name=normalized_name_by_entity_id[record["source_entity_id"]],
                    target_name=normalized_name_by_entity_id[record["target_entity_id"]],
                    relation_type=record["relation_type"],
                    text=record["text"],
                    extractor_type=record["extractor_type"],
                )

        self.connection.write(query)
        return entity_records, triple_records

    # ─── 删除 ───────────────────────────────────────────────

    def delete_graph(self, kb_id: str) -> None:
        def query(tx):
            tx.run("MATCH (n) WHERE n.kb_id = $kb_id DETACH DELETE n", kb_id=kb_id)

        self.connection.write(query)

    def delete_file_graph(
        self,
        kb_id: str,
        file_id: str,
        orphan_entity_ids: list[str] | None = None,
        orphan_triple_ids: list[str] | None = None,
    ) -> None:
        """按文件删除提及/Chunk，并按服务层给出的 PostgreSQL 孤儿清单清理边与实体。

        TuGraph 4.5.x 不支持模式谓词（``NOT (e)<-[:MENTIONS]-()``）与多 MATCH 子句，
        因此在图内“重新计算孤儿”不可行。RELATION 边以 ``triple_id`` 为主键、一条边代表
        整个知识库的该三元组，删除文件时是否删边取决于三元组是否仍被其它文件引用
        （``orphan_triple_ids``），不能按 ``file_id`` 误删；孤儿清单由 service 层基于
        PostgreSQL 计算。
        """
        orphan_entity_ids = orphan_entity_ids or []
        orphan_triple_ids = orphan_triple_ids or []

        def query(tx):
            tx.run(
                """
                MATCH (:Chunk)-[m:MENTIONS]->(:Entity)
                WHERE m.kb_id = $kb_id AND m.file_id = $file_id
                DELETE m
                """,
                kb_id=kb_id,
                file_id=file_id,
            )
            if orphan_triple_ids:
                tx.run(
                    """
                    MATCH (:Entity)-[r:RELATION]->(:Entity)
                    WHERE r.kb_id = $kb_id AND r.triple_id IN $triple_ids
                    DELETE r
                    """,
                    kb_id=kb_id,
                    triple_ids=orphan_triple_ids,
                )
            if orphan_entity_ids:
                tx.run(
                    """
                    MATCH (e:Entity)
                    WHERE e.kb_id = $kb_id AND e.entity_id IN $entity_ids
                    DETACH DELETE e
                    """,
                    kb_id=kb_id,
                    entity_ids=orphan_entity_ids,
                )
            tx.run(
                "MATCH (c:Chunk) WHERE c.kb_id = $kb_id AND c.file_id = $file_id DETACH DELETE c",
                kb_id=kb_id,
                file_id=file_id,
            )

        self.connection.write(query)

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
        try:
            return self._query_nodes_sync(kb_id, keyword, max_nodes, max_depth, exclude_chunk)
        except Exception as e:
            logger.error(f"TuGraph graph query failed: {e}")
            return {"nodes": [], "edges": []}

    def _query_nodes_sync(
        self,
        kb_id: str,
        keyword: str,
        limit: int,
        max_depth: int,
        exclude_chunk: bool,
    ) -> dict[str, Any]:
        # 与 Neo4j 的 toLower($keyword) 对齐：节点字段已小写，关键词也需小写后再比较。
        keyword = keyword.lower()
        with self.connection.driver.session() as session:
            if max_depth <= 0:
                # 参数名不能是 `limit`：TuGraph 解析器把它当作关键字并报 ParserException。
                params = {"keyword": keyword, "node_limit": limit, "kb_id": kb_id}
                result = session.run(self._build_node_query(exclude_chunk, keyword), **params)
                return self._process_node_records(result, limit, kb_id, exclude_chunk)
            depth = min(max_depth, 3)
            params = {"keyword": keyword, "kb_id": kb_id, "path_limit": max(limit, 1) * 10}
            result = session.run(
                self._build_path_query(depth, keyword, exclude_chunk),
                **params,
            )
            vid_set, euid_set = self._collect_path_identifiers(result)
            return self._fetch_subgraph_by_ids(session, vid_set, euid_set, limit, kb_id, exclude_chunk)

    @staticmethod
    def _collect_path_identifiers(result) -> tuple[set[int], set[str]]:
        """从路径查询结果收集顶点 id（int）与边 euid（str）。

        TuGraph 4.5.2 的 ``nodes(p)``/``relationships(p)`` 返回**列表的字符串表示**
        （如 ``"[0,1]"`` 与 ``"[0_1_0_0_0,1_2_1_0_0]"``），统一解析后再做二次抓取。
        """
        vid_set: set[int] = set()
        euid_set: set[str] = set()
        for record in result:
            for raw_val in (record.get("ns"), record.get("es")):
                for text in TugraphGraphStore._parse_string_list(raw_val):
                    if "_" in text:
                        euid_set.add(text)
                        continue
                    try:
                        vid_set.add(int(text))
                    except ValueError:
                        continue
        return vid_set, euid_set

    @staticmethod
    def _parse_string_list(raw) -> list[str]:
        """解析 TuGraph 以字符串返回的列表结果（如 ``labels(n)`` 返回 ``"[Entity]"``）。"""
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return [str(item) for item in raw]
        text = str(raw).strip()
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1].strip()
            return [part.strip().strip("\"'") for part in inner.split(",")] if inner else []
        return [text] if text else []

    def _fetch_subgraph_by_ids(
        self,
        session,
        vid_set: set[int],
        euid_set: set[str],
        limit: int,
        kb_id: str,
        exclude_chunk: bool,
    ) -> dict[str, Any]:
        nodes = []
        by_id: dict[int, dict[str, Any]] = {}
        if vid_set:
            for record in session.run(
                "MATCH (n) WHERE id(n) IN $ids RETURN id(n) AS nid, n, labels(n) AS ls",
                ids=sorted(vid_set),
            ):
                node = self._build_node_dict(record.get("n"), record.get("nid"), record.get("ls") or [], kb_id)
                if not node or node["id"] in by_id:
                    continue
                if exclude_chunk and node.get("type") == "Chunk":
                    continue
                by_id[node["id"]] = node
                nodes.append(node)
                if len(nodes) >= limit:
                    break

        edges = []
        seen_edge_ids = set()
        if euid_set and by_id:
            for record in session.run(
                """
                MATCH (a)-[r]->(b)
                WHERE euid(r) IN $euids
                RETURN id(a) AS src, id(b) AS dst, type(r) AS t, properties(r) AS props
                """,
                euids=sorted(euid_set),
            ):
                src, dst = record.get("src"), record.get("dst")
                if src not in by_id or dst not in by_id:
                    continue
                properties = self._coerce_props(record.get("props"))
                # RELATION 边的业务关系类型在 type 属性上；MENTIONS 无该属性，回落到标签名。
                edge_type = properties.get("type") or record.get("t")
                edge_id = f"{src}:{edge_type}:{dst}"
                if edge_id in seen_edge_ids:
                    continue
                seen_edge_ids.add(edge_id)
                edges.append(
                    {
                        "id": edge_id,
                        "source_id": src,
                        "target_id": dst,
                        "type": edge_type,
                        "properties": properties,
                        "normalized": {"type": edge_type, "direction": "directed"},
                    }
                )

        return self._finalize_subgraph_result(nodes, edges, limit)

    @staticmethod
    def _coerce_props(raw) -> dict[str, Any]:
        """把 TuGraph 返回的属性（可能是 dict、Pair 序列或 JSON 字符串）归一为 dict。"""
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (list, tuple)):
            try:
                return dict(raw)
            except (TypeError, ValueError):
                return {}
        if isinstance(raw, str):
            text = raw.strip()
            if text:
                try:
                    loaded = json.loads(text)
                    return loaded if isinstance(loaded, dict) else {}
                except (TypeError, ValueError):
                    pass
            return {}
        return {}

    def _process_node_records(self, result, limit: int, kb_id: str, exclude_chunk: bool = False) -> dict[str, Any]:
        nodes = []
        node_ids = set()
        for record in result:
            node = self._build_node_dict(record.get("n"), record.get("nid"), record.get("ls") or [], kb_id)
            if not node or node["id"] in node_ids:
                continue
            if exclude_chunk and node.get("type") == "Chunk":
                continue
            nodes.append(node)
            node_ids.add(node["id"])
            if len(nodes) >= limit:
                break
        return self._finalize_subgraph_result(nodes, [], limit)

    @staticmethod
    def _build_node_dict(raw_node, node_id, labels, kb_id: str) -> dict[str, Any] | None:
        """构造统一节点结构：显式 id 为 TuGraph 顶点 id，并按属性识别类型。"""
        properties: dict[str, Any]
        if isinstance(raw_node, dict):
            properties = dict(raw_node.get("properties") or raw_node)
            if not labels:
                labels = raw_node.get("labels") or []
        elif hasattr(raw_node, "items"):
            properties = dict(raw_node.items())
            if not labels:
                labels = list(raw_node.labels)
        else:
            return None
        labels = TugraphGraphStore._parse_string_list(labels)
        if node_id is None:
            node_id = properties.get("entity_id") or properties.get("chunk_id")
        if node_id is None:
            return None
        entity_type = (
            "Chunk" if "Chunk" in labels else (properties.get("entity_label") or properties.get("label") or "Entity")
        )
        name = properties.get("name") or properties.get("content_preview") or properties.get("chunk_id") or str(node_id)
        return {
            "id": node_id,
            "name": name,
            "original_id": node_id,
            "type": entity_type,
            # 保留 Chunk/Entity 标签（与 Neo4j 输出一致），仅过滤知识库标识。
            "labels": [label for label in labels if label not in {"MilvusKB", kb_id}],
            "properties": properties,
            "normalized": {"name": name, "type": entity_type, "source": "milvus"},
            "graph_type": "milvus",
        }

    @staticmethod
    def _build_node_filter(exclude_chunk: bool, keyword: str) -> str:
        clauses = ["n.kb_id = $kb_id"]
        if exclude_chunk:
            clauses.append("NOT n:Chunk")
        if keyword and keyword != "*":
            clauses.append(
                "(toLower(coalesce(n.name, '')) CONTAINS $keyword"
                " OR toLower(coalesce(n.content_preview, '')) CONTAINS $keyword"
                " OR toLower(coalesce(n.chunk_id, '')) CONTAINS $keyword)"
            )
        return " AND ".join(clauses)

    def _build_node_query(self, exclude_chunk: bool, keyword: str) -> str:
        return f"""
        MATCH (n)
        WHERE {self._build_node_filter(exclude_chunk, keyword)}
        RETURN id(n) AS nid, n, labels(n) AS ls
        LIMIT $node_limit
        """

    def _build_path_query(self, depth: int, keyword: str, exclude_chunk: bool) -> str:
        # 4.5.2 不允许单条只读语句出现“WITH..LIMIT + OPTIONAL MATCH”或第二个 MATCH 子句；
        # 用单个变长路径 MATCH（含零跳）返回起点与邻居。exclude_chunk 时限定从非 Chunk
        # 节点沿 RELATION 边扩展，与 Neo4j“路径不含 Chunk”的语义一致（边类型限定已实测可用）。
        if exclude_chunk:
            traversal = f"[:RELATION*0..{depth}]"
            seed_filter = "AND NOT n:Chunk"
        else:
            traversal = f"[*0..{depth}]"
            seed_filter = ""
        return f"""
        MATCH p = (n)-{traversal}-(m)
        WHERE n.kb_id = $kb_id {seed_filter}
          AND (toLower(coalesce(n.name, '')) CONTAINS $keyword
               OR toLower(coalesce(n.content_preview, '')) CONTAINS $keyword
               OR toLower(coalesce(n.chunk_id, '')) CONTAINS $keyword
               OR $keyword = '*')
        RETURN nodes(p) AS ns, relationships(p) AS es
        LIMIT $path_limit
        """

    def query_seed_subgraph(self, kb_id: str, *, entity_ids: list[str], max_nodes: int) -> dict[str, Any]:
        if not entity_ids:
            return {"nodes": [], "edges": []}
        seed_entity_ids = list(dict.fromkeys(entity_ids))
        # 只读查询不允许两个 MATCH 子句（4.5.2）；把种子过滤并入单个变长路径 MATCH。
        cypher = """
        MATCH p = (seed:Entity)-[*1..2]-(n)
        WHERE seed.kb_id = $kb_id AND seed.entity_id IN $entity_ids
        RETURN nodes(p) AS ns, relationships(p) AS es
        LIMIT $path_limit
        """
        try:
            with self.connection.driver.session() as session:
                result = session.run(
                    cypher,
                    kb_id=kb_id,
                    entity_ids=seed_entity_ids,
                    path_limit=max(max_nodes, 1) * 4,
                )
                vid_set, euid_set = self._collect_path_identifiers(result)
                return self._fetch_subgraph_by_ids(session, vid_set, euid_set, max_nodes, kb_id, False)
        except Exception as e:
            logger.error(f"TuGraph seed subgraph query failed: {e}")
            return {"nodes": [], "edges": []}

    def get_labels(self, kb_id: str) -> list[str]:
        cypher = """
        MATCH (n)
        WHERE n.kb_id = $kb_id
        UNWIND labels(n) AS node_label
        WITH DISTINCT node_label
        RETURN node_label
        ORDER BY node_label
        """
        try:
            records = self.connection.read(cypher, kb_id=kb_id)
            return [record["node_label"] for record in records]
        except Exception as e:
            logger.error(f"Failed to get TuGraph graph labels: {e}")
            return []

    def get_stats(self, kb_id: str) -> dict[str, Any]:
        # 4.5.2 不允许单条语句两个 MATCH；分别查询点与边统计。边数与 Neo4j 对齐，包含 MENTIONS 与 RELATION。
        node_cypher = """
        MATCH (n)
        WHERE n.kb_id = $kb_id
        RETURN count(n) AS node_count
        """
        edge_cypher = """
        MATCH (a)-[r]->(b)
        WHERE r.kb_id = $kb_id
        RETURN count(r) AS edge_count
        """
        label_cypher = """
        MATCH (e:Entity)
        WHERE e.kb_id = $kb_id
        WITH e.entity_label AS entity_label, count(*) AS instances
        RETURN entity_label, instances
        ORDER BY instances DESC
        """
        try:
            with self.connection.driver.session() as session:
                node_stats = session.run(node_cypher, kb_id=kb_id).single()
                edge_stats = session.run(edge_cypher, kb_id=kb_id).single()
                label_stats = session.run(label_cypher, kb_id=kb_id)
                return {
                    "total_nodes": node_stats["node_count"] if node_stats else 0,
                    "total_edges": edge_stats["edge_count"] if edge_stats else 0,
                    "entity_types": [{"type": row["entity_label"], "count": row["instances"]} for row in label_stats],
                }
        except Exception as e:
            logger.error(f"Failed to get TuGraph graph stats: {e}")
            return {"total_nodes": 0, "total_edges": 0, "entity_types": []}

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
