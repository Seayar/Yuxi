"""跨智能体共用的 TuGraph 图只读连接客户端。

故障溯源与处置方案共用这里的连接生命周期；各模块自己拥有查询语句。
TuGraph 只读限制（单语句最多一个 MATCH、中文标签、``LIMIT $node_limit`` 参数名）由
调用方在查询里消化。连接参数来自管理员配置 ``fault_trace_opts``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from neo4j import GraphDatabase, Query

from yuxi.utils import logger

CONNECT_TIMEOUT_SECONDS = float(os.getenv("TUGRAPH_CONNECT_TIMEOUT", "5"))
# 单条查询的事务超时（毫秒），由 bolt 事务配置下发给服务端。
QUERY_TIMEOUT_SECONDS = float(os.getenv("TUGRAPH_QUERY_TIMEOUT", "10"))


class TuGraphError(Exception):
    """图连接或查询失败。"""


@dataclass(frozen=True)
class TuGraphConfig:
    """外部图数据库连接配置。"""

    uri: str
    graph: str
    username: str
    password: str


class TuGraphClient:
    """连接外部 TuGraph 图的只读客户端（同步方法，Boost 驱动）。"""

    def __init__(self, config: TuGraphConfig) -> None:
        self._config = config
        self._driver = None

    def ensure_connected(self) -> None:
        if self._driver is not None:
            try:
                with self._driver.session() as session:
                    session.run(_timed("RETURN 1"))
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"图连接失效，重新连接: {exc}")
                self.close()
        try:
            self._driver = GraphDatabase.driver(
                self._config.uri,
                auth=(self._config.username, self._config.password),
                database=self._config.graph,
                # 服务不可达时必须快速失败：调用发生在 async 流程的线程里，不能无限挂起。
                connection_timeout=CONNECT_TIMEOUT_SECONDS,
                connection_acquisition_timeout=CONNECT_TIMEOUT_SECONDS,
            )
            with self._driver.session() as session:
                session.run(_timed("RETURN 1"))
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise TuGraphError(f"无法连接图数据库（{self._config.uri}/{self._config.graph}）: {exc}") from exc

    def run(self, cypher: str, **params) -> list[dict[str, Any]]:
        """执行一条只读查询并返回记录列表。

        事务超时通过驱动 ``Query`` 对象下发（``Session.run`` 的关键字参数只会并入 Cypher
        参数，不设置超时）；服务端是否执行该上限取决于 TuGraph 对 bolt 事务配置的支持。
        """

        self.ensure_connected()
        try:
            with self._driver.session() as session:
                return [record.data() for record in session.run(_timed(cypher), params)]
        except Exception as exc:  # noqa: BLE001
            raise TuGraphError(f"图查询失败: {exc}") from exc

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            finally:
                self._driver = None


def _timed(cypher: str) -> Query:
    """把只读查询包装成带事务超时的 ``Query``。"""

    return Query(cypher, timeout=QUERY_TIMEOUT_SECONDS)
