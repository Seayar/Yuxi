"""图谱构建相关的纯函数工具集。

将数据变换逻辑从 MilvusGraphService 中抽离，
使 service 类专注于 I/O 和业务编排。
"""

from __future__ import annotations

from typing import Any

from yuxi.utils import hashstr


def normalize_entity_name(text: str) -> str:
    """统一实体名称：去首尾空白、小写化、压缩内部连续空白。"""
    return " ".join(text.strip().lower().split())


def compute_entity_id(kb_id: str, normalized_name: str, label: str) -> str:
    return hashstr(f"{kb_id}:{normalized_name}:{label}", length=32)


def compute_triple_id(
    kb_id: str,
    source_normalized_name: str,
    source_label: str,
    relation_type: str,
    target_normalized_name: str,
    target_label: str,
) -> str:
    return hashstr(
        f"{kb_id}:{source_normalized_name}:{source_label}:{relation_type}:{target_normalized_name}:{target_label}",
        length=32,
    )


def graph_entity_collection_name(kb_id: str) -> str:
    return f"{kb_id}_entity"


def graph_triple_collection_name(kb_id: str) -> str:
    return f"{kb_id}_triple"


def build_graph_payload(normalized_result: dict[str, Any]) -> dict[str, Any]:
    """将抽取器产出的标准化结果转换为 Neo4j 写入所需的图结构。

    返回的 entities 已完成去重合并：同名同 label 的实体只保留一份，
    属性（attributes）取并集。
    """
    entities: list[dict[str, Any]] = []
    entity_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    def add_entity(entity: dict[str, Any]) -> str:
        key = (normalize_entity_name(entity["text"]), entity.get("label") or "Entity")
        existing = entity_by_key.get(key)
        if existing is not None:
            known_attributes = {(attr["text"], attr["label"]) for attr in existing.get("attributes") or []}
            for attribute in entity.get("attributes") or []:
                attribute_key = (attribute["text"], attribute["label"])
                if attribute_key not in known_attributes:
                    existing.setdefault("attributes", []).append(attribute)
                    known_attributes.add(attribute_key)
            return existing["id"]

        graph_entity = {
            "id": f"e{len(entities) + 1}",
            "text": entity["text"],
            "label": entity.get("label") or "Entity",
            "attributes": list(entity.get("attributes") or []),
        }
        entities.append(graph_entity)
        entity_by_key[key] = graph_entity
        return graph_entity["id"]

    for entity in normalized_result["entities"]:
        add_entity(entity)

    relations = []
    for relation in normalized_result["relations"]:
        relations.append(
            {
                "source": add_entity(relation["source"]),
                "target": add_entity(relation["target"]),
                "text": relation["text"],
                "label": relation.get("label") or "RELATED_TO",
            }
        )

    return {"entities": entities, "relations": relations, "metadata": normalized_result["metadata"]}


# ─── Cypher 模板 ────────────────────────────────────────────────
# 将大段 Cypher 字符串集中管理，提升 write_chunk_graph 的可读性。


def cypher_merge_chunk(db_label: str) -> str:
    """MERGE Chunk 节点并写入元数据。"""
    return f"""
    MERGE (c:Chunk:MilvusKB:`{db_label}` {{chunk_id: $chunk_id}})
    SET c.file_id = $file_id,
        c.kb_id = $kb_id,
        c.chunk_index = $chunk_index,
        c.content_preview = $content_preview,
        c.start_char_pos = $start_char_pos,
        c.end_char_pos = $end_char_pos
    """


def cypher_merge_entity_mention(db_label: str) -> str:
    """MERGE Entity 节点并创建 Chunk → Entity 的 MENTIONS 关系。"""
    return f"""
    MATCH (c:Chunk:MilvusKB:`{db_label}` {{chunk_id: $chunk_id}})
    MERGE (e:Entity:MilvusKB:`{db_label}` {{
        kb_id: $kb_id,
        normalized_name: $normalized_name,
        label: $entity_label
    }})
    SET e.entity_id = $entity_id,
        e.name = $name,
        e.attributes = $attributes
    MERGE (c)-[m:MENTIONS {{chunk_id: $chunk_id, file_id: $file_id, kb_id: $kb_id}}]->(e)
    """


def cypher_merge_relation(db_label: str) -> str:
    """MERGE 两个 Entity 之间的 RELATION 边。"""
    return f"""
    MATCH (source:Entity:MilvusKB:`{db_label}` {{
        kb_id: $kb_id,
        normalized_name: $source_name,
        label: $source_label
    }})
    MATCH (target:Entity:MilvusKB:`{db_label}` {{
        kb_id: $kb_id,
        normalized_name: $target_name,
        label: $target_label
    }})
    MERGE (source)-[r:RELATION {{
        kb_id: $kb_id,
        chunk_id: $chunk_id,
        source_name: $source_name,
        target_name: $target_name,
        type: $relation_type
    }}]->(target)
    SET r.triple_id = $triple_id,
        r.text = $text,
        r.file_id = $file_id,
        r.extractor_type = $extractor_type
    """


# ─── 后端无关的记录构造与结果归一化 ─────────────────────────────


def build_entity_records(kb_id: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按实体属性构造图谱写入记录与向量索引载荷。"""
    records = []
    for entity in entities:
        label = entity.get("label") or "Entity"
        normalized_name = normalize_entity_name(entity["text"])
        entity_id = compute_entity_id(kb_id, normalized_name, label)
        records.append(
            {
                "entity_id": entity_id,
                "kb_id": kb_id,
                "normalized_name": normalized_name,
                "label": label,
                "name": entity["text"],
                "attributes": entity.get("attributes") or [],
                "content": normalized_name,
            }
        )
    return records


def build_triple_records(
    kb_id: str,
    relations: list[dict[str, Any]],
    entity_record_by_local_id: dict[str, dict[str, Any]],
    graph_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """按关系构造三元组写入记录与向量索引载荷，按 triple_id 去重。"""
    records = []
    seen_triple_ids: set[str] = set()
    extractor_type = graph_payload["metadata"].get("extractor_type", "unknown")
    for relation in relations:
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
        if triple_id in seen_triple_ids:
            continue
        seen_triple_ids.add(triple_id)
        content = f"{source_record['normalized_name']} → {relation_type} → {target_record['normalized_name']}"
        records.append(
            {
                "triple_id": triple_id,
                "kb_id": kb_id,
                "source_entity_id": source_record["entity_id"],
                "target_entity_id": target_record["entity_id"],
                "relation_type": relation_type,
                "content": content,
                "text": relation["text"],
                "extractor_type": extractor_type,
            }
        )
    return records


def normalize_graph_node(raw_node: Any, kb_id: str | None = None) -> dict[str, Any]:
    """将驱动返回的点归一化为统一的子图节点结构。

    兼容 Neo4j 驱动对象（``element_id``/``labels``/``items``）与 TuGraph
    返回的字典形态，避免把后端差异泄漏到上层。
    """
    if hasattr(raw_node, "labels") and hasattr(raw_node, "items"):
        properties = dict(raw_node.items())
        labels = list(raw_node.labels)
        node_id = getattr(raw_node, "element_id", None)
    elif isinstance(raw_node, dict):
        labels = raw_node.get("labels", [])
        properties = raw_node.get("properties") or {
            key: value for key, value in raw_node.items() if key not in {"id", "labels"}
        }
        node_id = raw_node.get("id") or raw_node.get("element_id")
    else:
        return {}

    if not node_id:
        node_id = properties.get("entity_id") or properties.get("chunk_id") or properties.get("name")
    if not node_id:
        return {}

    db_label = properties.get("kb_id") or kb_id
    filtered_labels = [label for label in labels if label not in {"MilvusKB", db_label}]
    entity_type = "Chunk" if "Chunk" in labels else properties.get("label", "Entity")
    name = properties.get("name") or properties.get("content_preview") or properties.get("chunk_id") or "Unknown"
    return {
        "id": node_id,
        "name": name,
        "original_id": node_id,
        "type": entity_type,
        "labels": filtered_labels,
        "properties": properties,
        "normalized": {
            "name": name,
            "type": entity_type,
            "source": "milvus",
        },
        "graph_type": "milvus",
    }


def normalize_graph_edge(raw_edge: Any) -> dict[str, Any]:
    """将驱动返回的边归一化为统一的子图边结构。"""
    if hasattr(raw_edge, "type") and hasattr(raw_edge, "items"):
        properties = dict(raw_edge.items())
        edge_type = properties.get("type") or raw_edge.type
        source_id = getattr(getattr(raw_edge, "start_node", None), "element_id", None)
        target_id = getattr(getattr(raw_edge, "end_node", None), "element_id", None)
        edge_id = getattr(raw_edge, "element_id", None) or properties.get("triple_id")
        if not source_id:
            source_id = properties.get("source_name")
        if not target_id:
            target_id = properties.get("target_name")
    elif isinstance(raw_edge, dict):
        properties = raw_edge.get("properties", {})
        edge_type = raw_edge.get("type")
        source_id = raw_edge.get("source_id")
        target_id = raw_edge.get("target_id")
        edge_id = raw_edge.get("id") or properties.get("triple_id")
    else:
        return {}

    if not edge_id:
        edge_id = f"{source_id}->{edge_type}->{target_id}"
    return {
        "id": edge_id,
        "source_id": source_id,
        "target_id": target_id,
        "type": edge_type,
        "properties": properties,
        "normalized": {
            "type": edge_type,
            "direction": "directed",
        },
    }


# ─── TuGraph Cypher 模板（单标签 + kb_id 属性隔离） ──────────────
# TuGraph 每个点/边只能有一个标签，且使用强 Schema，因此不能沿用
# Neo4j 的 ``:Chunk:MilvusKB:`kb_id``` 多标签隔离；改用固定标签
# ``Chunk``/``Entity`` 并把 ``kb_id`` 作为属性过滤。


def cypher_tugraph_merge_chunk() -> str:
    """MERGE Chunk 节点并写入元数据。"""
    return """
    MERGE (c:Chunk {chunk_id: $chunk_id})
    SET c.kb_id = $kb_id,
        c.file_id = $file_id,
        c.chunk_index = $chunk_index,
        c.content_preview = $content_preview,
        c.start_char_pos = $start_char_pos,
        c.end_char_pos = $end_char_pos
    """


def cypher_tugraph_merge_entity_mention() -> str:
    """MERGE Entity 节点并创建 Chunk → Entity 的 MENTIONS 关系。

    MERGE 属性图只支持单属性主键，故 MENTIONS 用 chunk 级唯一键
    ``mention_id``，其余字段用逗号 SET 写入；全部用 MERGE 可同时补齐
    Chunk，无需 MATCH。
    """
    return """
    MERGE (c:Chunk {chunk_id: $chunk_id})
    MERGE (e:Entity {entity_id: $entity_id})
    SET e.kb_id = $kb_id,
        e.normalized_name = $normalized_name,
        e.entity_label = $entity_label,
        e.name = $name,
        e.attributes = $attributes
    MERGE (c)-[m:MENTIONS {mention_id: $mention_id}]->(e)
    SET m.chunk_id = $chunk_id,
        m.file_id = $file_id,
        m.kb_id = $kb_id,
        m.entity_id = $entity_id
    """


def cypher_tugraph_merge_relation() -> str:
    """MERGE 两个 Entity 之间的 RELATION 边。

    以 ``triple_id`` 为单属性主键：一条边代表整个知识库的该三元组，
    多个 chunk 重复抽取时幂等收敛；删除文件时由 service 依据 PostgreSQL
    孤儿三元组列表决定是否删除该边。
    """
    return """
    MATCH (source:Entity {entity_id: $source_entity_id})
    MATCH (target:Entity {entity_id: $target_entity_id})
    MERGE (source)-[r:RELATION {triple_id: $triple_id}]->(target)
    SET r.kb_id = $kb_id,
        r.chunk_id = $chunk_id,
        r.file_id = $file_id,
        r.source_name = $source_name,
        r.target_name = $target_name,
        r.type = $relation_type,
        r.text = $text,
        r.extractor_type = $extractor_type
    """
