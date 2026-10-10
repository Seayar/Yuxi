from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from yuxi.knowledge.graphs.extractors import normalize_extraction_result
from yuxi.knowledge.graphs.graph_stores import create_graph_store
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.storage.graph_connection import get_shared_graph_connection, graph_backend_name, safe_graph_label


@pytest.mark.integration
def test_delete_file_graph_preserves_shared_and_unrelated_entities():
    if graph_backend_name() != "neo4j":
        pytest.skip("该用例直接写入 Neo4j 多标签模型，仅在 neo4j 后端下运行")
    kb_id = f"pytest_delete_graph_{uuid4().hex}"
    label = safe_graph_label(kb_id)
    connection = get_shared_graph_connection()
    service = MilvusGraphService(graph_connection=connection)

    try:
        with connection.driver.session() as session:
            session.run(
                f"""
                CREATE (f1:Chunk:MilvusKB:`{label}` {{kb_id: $kb_id, file_id: 'f1'}}),
                       (f2:Chunk:MilvusKB:`{label}` {{kb_id: $kb_id, file_id: 'f2'}}),
                       (shared:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, name: 'shared'}}),
                       (f1_only:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, name: 'f1_only'}}),
                       (unrelated_orphan:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, name: 'unrelated_orphan'}}),
                       (f1)-[:MENTIONS {{kb_id: $kb_id, file_id: 'f1'}}]->(shared),
                       (f1)-[:MENTIONS {{kb_id: $kb_id, file_id: 'f1'}}]->(f1_only),
                       (f2)-[:MENTIONS {{kb_id: $kb_id, file_id: 'f2'}}]->(shared)
                """,
                kb_id=kb_id,
            ).consume()

        service.store.delete_file_graph(kb_id, "f1")

        with connection.driver.session() as session:
            remaining_entities = set(
                session.run(f"MATCH (e:Entity:MilvusKB:`{label}`) RETURN e.name AS name").value("name")
            )
            remaining_files = set(
                session.run(f"MATCH (c:Chunk:MilvusKB:`{label}`) RETURN c.file_id AS file_id").value("file_id")
            )

        assert remaining_entities == {"shared", "unrelated_orphan"}
        assert remaining_files == {"f2"}

        service.store.delete_file_graph(kb_id, "f2")

        with connection.driver.session() as session:
            remaining_entities = set(
                session.run(f"MATCH (e:Entity:MilvusKB:`{label}`) RETURN e.name AS name").value("name")
            )

        assert remaining_entities == {"unrelated_orphan"}
    finally:
        with connection.driver.session() as session:
            session.run(f"MATCH (n:MilvusKB:`{label}`) DETACH DELETE n").consume()


@pytest.mark.integration
def test_tugraph_delete_file_graph_keeps_triples_referenced_by_other_files():
    if graph_backend_name() != "tugraph":
        pytest.skip("该用例直接写入 TuGraph 单标签模型，仅在 tugraph 后端下运行")

    kb_id = f"pytest_tugraph_delete_{uuid4().hex}"
    connection = get_shared_graph_connection()
    store = create_graph_store(connection)

    def write_chunk(chunk_id, file_id, text, entities, relation):
        store.write_chunk_graph(
            kb_id,
            SimpleNamespace(
                chunk_id=chunk_id,
                file_id=file_id,
                chunk_index=0,
                content=text,
                start_char_pos=0,
                end_char_pos=len(text),
            ),
            normalize_extraction_result(
                {
                    "entities": [{"text": name, "label": label} for name, label in entities],
                    "relations": [
                        {
                            "source": {"text": relation[0], "label": relation[1]},
                            "target": {"text": relation[2], "label": relation[3]},
                            "text": relation[4],
                            "label": relation[5],
                        }
                    ],
                },
                "llm",
            ),
        )

    try:
        # f1、f2 抽到同一条 (张三)-[WORKS_AT]->(公司) 三元组
        for chunk_id, file_id in (("f1_c1", "f1"), ("f2_c1", "f2")):
            write_chunk(
                chunk_id,
                file_id,
                "张三任职于公司",
                [("张三", "Person"), ("公司", "Organization")],
                ("张三", "Person", "公司", "Organization", "任职于", "WORKS_AT"),
            )

        subgraph = store.query_nodes(kb_id, keyword="*", max_depth=1, max_nodes=50)
        relation_edges = [edge for edge in subgraph["edges"] if edge["type"] == "WORKS_AT"]
        assert len(relation_edges) == 1, "同一条三元组在 TuGraph 只存一条边"
        triple_id = relation_edges[0]["properties"]["triple_id"]

        # 先删除最后写入者 f2：边的 file_id 标记即 f2，f1 仍引用三元组，边必须保留；
        # 若按 file_id 删边（历史缺陷），这里会误删。
        store.delete_file_graph(kb_id, "f2", orphan_entity_ids=[], orphan_triple_ids=[])
        after_f2 = store.query_nodes(kb_id, keyword="*", max_depth=1, max_nodes=50)
        assert [edge for edge in after_f2["edges"] if edge["type"] == "WORKS_AT"], "仍被 f1 引用的三元组边被误删"
        assert {"张三", "公司"} <= {node["name"] for node in after_f2["nodes"]}
        assert "f2_c1" not in {node["properties"].get("chunk_id") for node in after_f2["nodes"]}

        # 删除 f1：三元组与实体都成孤儿，按 PostgreSQL 孤儿清单清理后全部消失
        entity_ids = [node["properties"]["entity_id"] for node in after_f2["nodes"] if node["type"] != "Chunk"]
        store.delete_file_graph(kb_id, "f1", orphan_entity_ids=entity_ids, orphan_triple_ids=[triple_id])
        # 用不吞异常的查询路径断言终态，避免查询失败被空结果掩盖。
        assert store._query_nodes_sync(kb_id, "*", 50, 1, False) == {"nodes": [], "edges": []}
    finally:
        store.delete_graph(kb_id)
