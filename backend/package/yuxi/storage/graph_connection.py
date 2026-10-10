"""图数据库连接与读写的通用层。

Yuxi 的知识图谱可以写入 Neo4j 或 TuGraph。两者都通过 Bolt 协议访问，
因此连接生命周期、读写简写和标签校验可以共享；差异集中在连接参数、
Schema 引导与 Cypher 方言（见 ``yuxi.knowledge.graphs.graph_stores``）。

后端由 ``GRAPH_BACKEND`` 环境变量选择，默认 ``neo4j``；未识别的取值会显式
失败，而不是静默退回某个后端。
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from typing import Any

from neo4j import GraphDatabase as GD

from yuxi.utils import logger

_SAFE_GRAPH_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GRAPH_BACKEND_ENV = "GRAPH_BACKEND"
SUPPORTED_GRAPH_BACKENDS = ("neo4j", "tugraph")


def graph_backend_name() -> str:
    """返回当前生效的图数据库后端名称，非法取值直接失败。"""
    value = (os.environ.get(GRAPH_BACKEND_ENV) or "neo4j").strip().lower()
    if value not in SUPPORTED_GRAPH_BACKENDS:
        raise ValueError(
            f"不支持的图数据库后端 {GRAPH_BACKEND_ENV}={value}，可选：{', '.join(SUPPORTED_GRAPH_BACKENDS)}"
        )
    return value


def safe_graph_label(value: str) -> str:
    """校验可安全拼入 Cypher 的标签/标识符，避免注入。"""
    if not _SAFE_GRAPH_LABEL_RE.match(value or ""):
        raise ValueError(f"非法图数据库标签: {value}")
    return value


class GraphConnectionManager:
    """Neo4j 与 TuGraph 共享的连接管理器基类。"""

    backend = "graph"

    def __init__(self) -> None:
        self.driver = None
        self.status = "closed"
        self._connect()

    def _connect(self) -> None:
        if self.driver and self._is_connected():
            return
        try:
            self.driver = GD.driver(self._uri(), auth=self._auth(), **self._driver_kwargs())
            with self.driver.session() as session:
                session.run("RETURN 1")
            self._after_connect()
            self.status = "open"
            logger.info(f"Successfully connected to graph backend: {self.backend}")
        except Exception as e:
            logger.error(f"Failed to connect to graph backend {self.backend}: {e}")
            raise

    def _is_connected(self) -> bool:
        if not self.driver:
            return False
        try:
            with self.driver.session() as session:
                session.run("RETURN 1")
            return True
        except Exception:
            return False

    def _uri(self) -> str:
        raise NotImplementedError

    def _auth(self) -> tuple[str, str]:
        raise NotImplementedError

    def _driver_kwargs(self) -> dict[str, Any]:
        """创建驱动时的额外参数（例如 TuGraph 的默认子图）。"""
        return {}

    def _after_connect(self) -> None:
        """连接建立后的后端专属引导（例如 TuGraph 的 Schema 准备）。"""

    def write(self, query: Callable) -> Any:
        """在一个会话中执行写操作函数 ``query(session)``。"""
        with self.driver.session() as session:
            return query(session)

    def read(self, cypher: str, **kwargs) -> list[dict[str, Any]]:
        """执行只读 Cypher 查询并返回结果列表。"""
        with self.driver.session() as session:
            result = session.run(cypher, **kwargs)
            return [record.data() for record in result]

    def is_running(self) -> bool:
        return self.status == "open" or self.status == "processing"

    def close(self) -> None:
        if self.driver:
            self.driver.close()
            self.driver = None
            self.status = "closed"


class Neo4jConnectionManager(GraphConnectionManager):
    """Neo4j 后端连接管理器。"""

    backend = "neo4j"

    def _uri(self) -> str:
        return os.environ.get("NEO4J_URI", "bolt://localhost:7687")

    def _auth(self) -> tuple[str, str]:
        return (
            os.environ.get("NEO4J_USERNAME", "neo4j"),
            os.environ.get("NEO4J_PASSWORD", "0123456789"),
        )

    def write(self, query: Callable) -> Any:
        """保留 Neo4j 托管事务的重试语义。"""
        with self.driver.session() as session:
            return session.execute_write(query)


class TugraphConnectionManager(GraphConnectionManager):
    """TuGraph 后端连接管理器。

    TuGraph 兼容 Bolt 协议，可通过官方 Neo4j 驱动访问；默认 Bolt 端口 7687、
    默认账号 ``admin`` / ``73@TuGraph``。TuGraph 使用强 Schema，且每个点/边只能有
    一个标签，因此 Schema 引导与 Cypher 由 ``TugraphGraphStore`` 负责。
    """

    backend = "tugraph"

    def _uri(self) -> str:
        return os.environ.get("TUGRAPH_URI", "bolt://tugraph:7687")

    def _auth(self) -> tuple[str, str]:
        return (
            os.environ.get("TUGRAPH_USERNAME", "admin"),
            os.environ.get("TUGRAPH_PASSWORD", "73@TuGraph"),
        )

    def _driver_kwargs(self) -> dict[str, Any]:
        # TuGraph 的 Bolt 要求 Run 消息显式携带 db（子图）名；未指定时 neo4j 5.x
        # 驱动会省略该字段并触发 "Missing 'db' item"。这里固定默认子图。
        return {"database": os.environ.get("TUGRAPH_GRAPH", "default")}


_shared_graph_connection: GraphConnectionManager | None = None
_shared_graph_connection_lock = threading.Lock()


def _create_graph_connection() -> GraphConnectionManager:
    backend = graph_backend_name()
    if backend == "tugraph":
        return TugraphConnectionManager()
    return Neo4jConnectionManager()


def get_shared_graph_connection() -> GraphConnectionManager:
    """返回当前后端共享的连接管理器，按需创建。"""
    global _shared_graph_connection
    if _shared_graph_connection is None or not _shared_graph_connection.driver:
        with _shared_graph_connection_lock:
            if _shared_graph_connection is None or not _shared_graph_connection.driver:
                _shared_graph_connection = _create_graph_connection()
    return _shared_graph_connection


def close_shared_graph_connection() -> None:
    with _shared_graph_connection_lock:
        if _shared_graph_connection is not None:
            _shared_graph_connection.close()
