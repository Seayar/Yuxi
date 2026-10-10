"""图谱存储后端工厂。"""

from __future__ import annotations

from yuxi.knowledge.graphs.graph_stores.base import GraphStore, run_graph_query_io
from yuxi.knowledge.graphs.graph_stores.neo4j_store import Neo4jGraphStore
from yuxi.knowledge.graphs.graph_stores.tugraph_store import TugraphGraphStore

__all__ = [
    "GraphStore",
    "Neo4jGraphStore",
    "TugraphGraphStore",
    "create_graph_store",
    "run_graph_query_io",
]


def create_graph_store(connection=None) -> GraphStore:
    """按连接后端创建对应的图存储实现。"""
    if connection is None:
        from yuxi.storage.graph_connection import get_shared_graph_connection

        connection = get_shared_graph_connection()
    if getattr(connection, "backend", "neo4j") == "tugraph":
        return TugraphGraphStore(connection)
    return Neo4jGraphStore(connection)
