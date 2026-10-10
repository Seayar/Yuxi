from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.knowledge.graphs.extractors import normalize_extraction_result
from yuxi.knowledge.graphs.graph_stores import Neo4jGraphStore, TugraphGraphStore, create_graph_store


class _EmptyResult:
    def consume(self):
        return self

    def single(self):
        return None

    def __iter__(self):
        return iter(())


class _ResultStub:
    def __init__(self, records=(), single=None):
        self._records = list(records)
        self._single = single

    def consume(self):
        return self

    def single(self):
        return self._single

    def __iter__(self):
        return iter(self._records)


class RecordingSession:
    """记录所有 Cypher，并按需返回点/边标签与预设查询结果。"""

    def __init__(self, *, vertex_labels=(), edge_labels=(), results=None):
        self.vertex_labels = set(vertex_labels)
        self.edge_labels = set(edge_labels)
        self.results = dict(results or {})
        self.statements: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cypher, **kwargs):
        self.statements.append((cypher, kwargs))
        normalized = " ".join(cypher.split())
        if normalized == "CALL db.vertexLabels()":
            return [{"label": label} for label in sorted(self.vertex_labels)]
        if normalized == "CALL db.edgeLabels()":
            return [{"edgeLabels": label} for label in sorted(self.edge_labels)]
        for marker, result in self.results.items():
            if marker in normalized:
                return result
        return _EmptyResult()


class RecordingConnection:
    backend = "tugraph"

    def __init__(self, session: RecordingSession):
        self._session = session
        self.driver = SimpleNamespace(session=lambda: self._session)

    def write(self, query):
        return query(self._session)

    def read(self, cypher, **kwargs):
        return list(self._session.run(cypher, **kwargs))


def _create_statements(session: RecordingSession) -> list[str]:
    return [c for c, _ in session.statements if "createVertexLabel" in c or "createEdgeLabel" in c]


def test_create_graph_store_selects_backend():
    tugraph = create_graph_store(RecordingConnection(RecordingSession()))
    assert isinstance(tugraph, TugraphGraphStore)
    assert tugraph.backend == "tugraph"

    neo4j = create_graph_store(SimpleNamespace(backend="neo4j"))
    assert isinstance(neo4j, Neo4jGraphStore)


def test_ensure_ready_creates_schema_once_and_is_idempotent():
    session = RecordingSession()
    store = TugraphGraphStore(RecordingConnection(session))

    store.ensure_ready("kb_1")

    creates = _create_statements(session)
    assert any("db.createVertexLabel('Chunk', 'chunk_id'" in c for c in creates)
    assert any("db.createVertexLabel('Entity', 'entity_id'" in c for c in creates)
    assert any("db.createEdgeLabel('MENTIONS'" in c for c in creates)
    assert any("db.createEdgeLabel('RELATION'" in c for c in creates)
    # Schema 是全局的，不应把知识库写进标签定义。
    assert all("kb_1" not in c for c in creates)

    session.statements.clear()
    store.ensure_ready("kb_2")
    assert session.statements == []


def test_ensure_ready_skips_existing_labels():
    session = RecordingSession(vertex_labels=["Chunk", "Entity"], edge_labels=["MENTIONS", "RELATION"])
    store = TugraphGraphStore(RecordingConnection(session))

    store.ensure_ready("kb_1")

    assert _create_statements(session) == []


def test_write_chunk_graph_uses_single_label_and_kb_property():
    session = RecordingSession()
    store = TugraphGraphStore(RecordingConnection(session))
    chunk = SimpleNamespace(
        chunk_id="chunk_1",
        file_id="file_1",
        chunk_index=1,
        content="张三任职于公司",
        start_char_pos=0,
        end_char_pos=8,
    )

    entities, triples = store.write_chunk_graph(
        "kb_test",
        chunk,
        normalize_extraction_result(
            {
                "relations": [
                    {
                        "source": {"text": "张三", "label": "Person"},
                        "target": {"text": "公司", "label": "Organization"},
                        "text": "任职于",
                        "label": "WORKS_AT",
                    }
                ],
            },
            "llm",
        ),
    )

    assert [entity["name"] for entity in entities] == ["张三", "公司"]
    assert triples[0]["relation_type"] == "WORKS_AT"

    statements = [c for c, _ in session.statements]
    assert all("MilvusKB" not in cypher for cypher in statements)
    assert any("MERGE (c:Chunk {chunk_id: $chunk_id})" in c for c in statements)
    assert any("MERGE (e:Entity {entity_id: $entity_id})" in c for c in statements)
    assert any("MERGE (source)-[r:RELATION {triple_id: $triple_id}]->(target)" in c for c in statements)

    relation_call = next(kwargs for cypher, kwargs in session.statements if "MERGE (source)-[r:RELATION" in cypher)
    assert relation_call["kb_id"] == "kb_test"
    assert relation_call["source_name"] == "张三"
    assert relation_call["target_name"] == "公司"


@pytest.mark.parametrize("max_depth", [0, 1, 2, 3, 5])
def test_query_nodes_avoids_unsupported_constructs(max_depth):
    session = RecordingSession(vertex_labels=["Chunk", "Entity"], edge_labels=["MENTIONS", "RELATION"])
    store = TugraphGraphStore(RecordingConnection(session))

    store.query_nodes("kb_1", keyword="张三", max_depth=max_depth, max_nodes=10)

    cyphers = [c for c, _ in session.statements]
    assert cyphers, "应至少发起一次查询"
    for cypher in cyphers:
        # 4.5.2 只读查询可用：参数、toLower、coalesce、单 MATCH；仍不可用：模式谓词、reduce、多 MATCH。
        assert "MilvusKB" not in cypher
        assert "reduce(" not in cypher
        assert "exists((" not in cypher
        assert "n.kb_id = $kb_id" in cypher
        # 单条语句不能出现第二个 MATCH / OPTIONAL MATCH。
        assert cypher.count("MATCH ") == 1
    if max_depth > 0:
        assert any(f"[*0..{min(max_depth, 3)}]" in cypher for cypher in cyphers)
        assert any("nodes(p) AS ns" in cypher for cypher in cyphers)


def test_query_nodes_depth_zero_avoids_reserved_limit_param():
    session = RecordingSession(vertex_labels=["Chunk", "Entity"], edge_labels=["MENTIONS", "RELATION"])
    store = TugraphGraphStore(RecordingConnection(session))

    store.query_nodes("kb_1", keyword="张三", max_depth=0, max_nodes=10)

    cypher, kwargs = next((c, k) for c, k in session.statements if "MATCH (n)" in c)
    # TuGraph 解析器把 `limit` 当作关键字，参数名必须避开。
    assert "LIMIT $node_limit" in cypher
    assert "limit" not in kwargs
    assert kwargs["node_limit"] == 10


def test_query_nodes_lowercases_keyword_like_neo4j():
    session = RecordingSession(vertex_labels=["Chunk", "Entity"], edge_labels=["MENTIONS", "RELATION"])
    store = TugraphGraphStore(RecordingConnection(session))

    store.query_nodes("kb_1", keyword="ZhangSan", max_depth=0, max_nodes=10)

    _, kwargs = next((c, k) for c, k in session.statements if "MATCH (n)" in c)
    assert kwargs["keyword"] == "zhangsan"


def test_get_stats_edge_count_includes_mentions():
    session = RecordingSession()
    store = TugraphGraphStore(RecordingConnection(session))

    store.get_stats("kb_1")

    edge_cypher = next(c for c, _ in session.statements if "count(r) AS edge_count" in c)
    # 与 Neo4j 对齐：边数统计覆盖 MENTIONS 与 RELATION，不能只匹配 Entity→Entity。
    assert "(a)-[r]->(b)" in edge_cypher


def test_delete_graph_scopes_by_kb_property():
    session = RecordingSession()
    store = TugraphGraphStore(RecordingConnection(session))

    store.delete_graph("kb_1")

    cypher, kwargs = session.statements[-1]
    assert "DETACH DELETE n" in cypher
    assert "n.kb_id = $kb_id" in cypher
    assert kwargs["kb_id"] == "kb_1"


def test_delete_file_graph_scopes_and_cleans_orphans():
    session = RecordingSession()
    store = TugraphGraphStore(RecordingConnection(session))

    store.delete_file_graph("kb_1", "file_1", orphan_entity_ids=["o1", "o2"], orphan_triple_ids=["t1", "t2"])

    cyphers = [c for c, _ in session.statements]
    assert len(cyphers) == 4
    assert "DELETE m" in cyphers[0] and "$file_id" in cyphers[0]
    # RELATION 边按 PostgreSQL 孤儿三元组删除；按 file_id 会误删仍被其它文件引用的边。
    orphan_triple_call = next(kwargs for c, kwargs in session.statements if "triple_id IN $triple_ids" in c)
    assert set(orphan_triple_call["triple_ids"]) == {"t1", "t2"}
    assert "r.file_id" not in cyphers[1]
    # 孤儿实体由调用方（PostgreSQL）给出，按 entity_id IN 精确删除，不用模式谓词。
    orphan_call = next(kwargs for c, kwargs in session.statements if "entity_id IN $entity_ids" in c)
    assert set(orphan_call["entity_ids"]) == {"o1", "o2"}
    assert "NOT (" not in cyphers[2] and "OPTIONAL MATCH" not in cyphers[2]
    assert "DETACH DELETE c" in cyphers[3]


def test_delete_file_graph_without_orphans_skips_entity_cleanup():
    session = RecordingSession()
    store = TugraphGraphStore(RecordingConnection(session))

    store.delete_file_graph("kb_1", "file_1")

    cyphers = [c for c, _ in session.statements]
    # 无孤儿列表时只删提及与 chunk，共 2 条；不按 file_id 删关系边。
    assert len(cyphers) == 2
    assert all("DELETE r" not in c for c in cyphers)
    assert all("DELETE e" not in c for c in cyphers)


def test_get_stats_maps_rows():
    session = RecordingSession(
        results={
            "RETURN count(n) AS node_count": _ResultStub(single={"node_count": 3}),
            "RETURN count(r) AS edge_count": _ResultStub(single={"edge_count": 2}),
            "e.entity_label AS entity_label": _ResultStub(records=[{"entity_label": "Person", "instances": 2}]),
        }
    )
    store = TugraphGraphStore(RecordingConnection(session))

    assert store.get_stats("kb_1") == {
        "total_nodes": 3,
        "total_edges": 2,
        "entity_types": [{"type": "Person", "count": 2}],
    }


def test_get_labels_returns_node_labels():
    session = RecordingSession(
        results={
            "UNWIND labels(n) AS node_label": _ResultStub(records=[{"node_label": "Entity"}, {"node_label": "Chunk"}])
        }
    )
    store = TugraphGraphStore(RecordingConnection(session))

    assert store.get_labels("kb_1") == ["Entity", "Chunk"]


def test_query_nodes_exclude_chunk_restricts_traversal_to_relations():
    session = RecordingSession(vertex_labels=["Chunk", "Entity"], edge_labels=["MENTIONS", "RELATION"])
    store = TugraphGraphStore(RecordingConnection(session))

    store.query_nodes("kb_1", keyword="张三", max_depth=2, max_nodes=10, exclude_chunk=True)

    path_cypher = next(c for c, _ in session.statements if "nodes(p) AS ns" in c)
    # 与 Neo4j 一致：排除 Chunk 时不能穿过 Chunk 扩展，种子也必须是实体。
    assert "[:RELATION*0..2]" in path_cypher
    assert "NOT n:Chunk" in path_cypher


def test_collect_path_identifiers_parses_string_lists():
    records = [
        {"ns": "[0,1]", "es": "[0_1_0_0_0,1_2_1_0_0]"},
        {"ns": [2], "es": ["3_4_0_0_0"]},
        {"ns": None, "es": None},
    ]

    vid_set, euid_set = TugraphGraphStore._collect_path_identifiers(records)

    assert vid_set == {0, 1, 2}
    assert euid_set == {"0_1_0_0_0", "1_2_1_0_0", "3_4_0_0_0"}


def test_fetch_subgraph_by_ids_uses_business_edge_type_and_coerces_props():
    session = RecordingSession(
        results={
            "id(n) IN $ids": _ResultStub(
                records=[
                    {"nid": 0, "n": {"entity_id": "e0", "name": "张三", "entity_label": "Person"}, "ls": "[Entity]"},
                    {"nid": 1, "n": {"entity_id": "e1", "name": "公司", "entity_label": "Org"}, "ls": ["Entity"]},
                ]
            ),
            "euid(r) IN $euids": _ResultStub(
                records=[
                    {"src": 0, "dst": 1, "t": "RELATION", "props": '{"type": "WORKS_AT", "triple_id": "t1"}'},
                    {"src": 0, "dst": 1, "t": "MENTIONS", "props": None},
                    {"src": 0, "dst": 9, "t": "RELATION", "props": "{}"},
                ]
            ),
        }
    )
    store = TugraphGraphStore(RecordingConnection(session))

    result = store._fetch_subgraph_by_ids(session, {0, 1}, {"0_1_0_0_0", "0_1_1_0_0"}, 50, "kb_1", False)

    # RELATION 边展示业务关系类型（不能是标签名 RELATION）；MENTIONS 回落标签名。
    edge_types = {edge["type"] for edge in result["edges"]}
    assert edge_types == {"WORKS_AT", "MENTIONS"}
    relation_edge = next(edge for edge in result["edges"] if edge["type"] == "WORKS_AT")
    assert relation_edge["id"] == "0:WORKS_AT:1"
    assert relation_edge["properties"]["triple_id"] == "t1"
    # labels(n) 在 Bolt 上可能是字符串形式，节点 labels 必须解析成列表（与 Neo4j 一致）。
    assert all(node["labels"] == ["Entity"] for node in result["nodes"])


def test_query_seed_subgraph_fetches_attributes_by_ids():
    session = RecordingSession(
        results={
            "-[*1..2]-": _ResultStub(records=[{"ns": "[0]", "es": "[]"}]),
            "id(n) IN $ids": _ResultStub(
                records=[
                    {"nid": 0, "n": {"entity_id": "e0", "name": "张三", "entity_label": "Person"}, "ls": ["Entity"]}
                ]
            ),
            "euid(r) IN $euids": _ResultStub(records=[]),
        }
    )
    store = TugraphGraphStore(RecordingConnection(session))

    result = store.query_seed_subgraph("kb_1", entity_ids=["e0"], max_nodes=10)

    assert [node["name"] for node in result["nodes"]] == ["张三"]


def test_write_chunk_graph_uses_entity_records_for_relation_names():
    session = RecordingSession()
    store = TugraphGraphStore(RecordingConnection(session))
    chunk = SimpleNamespace(
        chunk_id="chunk_1",
        file_id="file_1",
        chunk_index=0,
        content="A → B 任职于 T",
        start_char_pos=0,
        end_char_pos=9,
    )

    store.write_chunk_graph(
        "kb_test",
        chunk,
        normalize_extraction_result(
            {
                "relations": [
                    {
                        "source": {"text": "A → B"},
                        "target": {"text": "T", "label": "Organization"},
                        "text": "任职于",
                        "label": "WORKS_AT",
                    }
                ],
            },
            "llm",
        ),
    )

    relation_call = next(kwargs for cypher, kwargs in session.statements if "MERGE (source)-[r:RELATION" in cypher)
    # 不能从展示字符串 "a → b → WORKS_AT → t" 里 split 取名字。
    assert relation_call["source_name"] == "a → b"
    assert relation_call["target_name"] == "t"


class _ConcurrentLabelSession:
    """首次列表中 Chunk 缺失且创建失败，重查时标签已被其它进程建好。"""

    def __init__(self):
        self.list_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, cypher, **kwargs):
        normalized = " ".join(cypher.split())
        if normalized == "CALL db.vertexLabels()":
            self.list_calls += 1
            labels = ["Entity"] if self.list_calls == 1 else ["Chunk", "Entity"]
            return [{"label": label} for label in labels]
        if normalized == "CALL db.edgeLabels()":
            return [{"label": label} for label in ["MENTIONS", "RELATION"]]
        if "createVertexLabel" in normalized or "createEdgeLabel" in normalized:
            raise RuntimeError("Vertex label [Chunk] already exists.")
        return _EmptyResult()


def test_ensure_ready_tolerates_concurrent_label_creation():
    session = _ConcurrentLabelSession()
    store = TugraphGraphStore(RecordingConnection(session))

    store.ensure_ready("kb_1")

    assert session.list_calls == 2


class _FailingLabelSession(_ConcurrentLabelSession):
    """创建持续失败且标签始终缺失时，ensure_ready 必须显式失败。"""

    def run(self, cypher, **kwargs):
        normalized = " ".join(cypher.split())
        if normalized == "CALL db.vertexLabels()":
            self.list_calls += 1
            return [{"label": "Entity"}]
        if normalized == "CALL db.edgeLabels()":
            return [{"label": label} for label in ["MENTIONS", "RELATION"]]
        if "createVertexLabel" in normalized or "createEdgeLabel" in normalized:
            raise RuntimeError("boom")
        return _EmptyResult()


def test_ensure_ready_fails_when_labels_still_missing():
    session = _FailingLabelSession()
    store = TugraphGraphStore(RecordingConnection(session))

    with pytest.raises(RuntimeError):
        store.ensure_ready("kb_1")
