"""假 TuGraph driver：记录收到的 Cypher 与参数，按预设图谱内容回答查询。

单测只使用本模块的假对象，不连接任何数据库、图谱或大模型。假 driver 覆盖 seed 用到的全部
查询形态：标签枚举、建标签、节点 MERGE、边 MERGE、按 (standard_id, 条款号) 查标准条款、
按标准整取条款、按 设备实例.名称 查候选设备。
"""

from __future__ import annotations

from typing import Any


class FakeResult:
    """查询结果替身：可迭代、可 single()、可 consume()。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None

    def consume(self) -> "FakeResult":
        return self


class FakeSession:
    def __init__(self, driver: "FakeDriver") -> None:
        self._driver = driver

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def run(self, cypher: str, **params: Any) -> FakeResult:
        self._driver.calls.append((cypher, params))
        return FakeResult(self._driver.respond(cypher, params))


class FakeDriver:
    """可配置的假 driver。

    - ``registered_vertex`` / ``registered_edge``：图上已注册的标签（用于验证“已注册跳过”）；
    - ``clauses``：``{standard_id: {条款号: clause_id}}``；
    - ``devices``：``{名称: [{"id":..., "path":...}]}``。
    """

    def __init__(
        self,
        *,
        registered_vertex: tuple[str, ...] = (),
        registered_edge: tuple[str, ...] = (),
        clauses: dict[str, dict[str, str]] | None = None,
        devices: dict[str, list[dict[str, str]]] | None = None,
    ) -> None:
        self.registered_vertex = set(registered_vertex)
        self.registered_edge = set(registered_edge)
        self.clauses = dict(clauses or {})
        self.devices = dict(devices or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def session(self) -> FakeSession:
        return FakeSession(self)

    def close(self) -> None:
        self.closed = True

    def respond(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "db.vertexLabels()" in cypher:
            return [{"label": label} for label in sorted(self.registered_vertex)]
        if "db.edgeLabels()" in cypher:
            return [{"label": label} for label in sorted(self.registered_edge)]
        if cypher.startswith("CALL db.createVertexLabel") or cypher.startswith("CALL db.createEdgeLabel"):
            return []
        if "RETURN n.id AS id, n.条款号 AS no" in cypher:
            rows = (self.clauses.get(params.get("sid")) or {}).items()
            return [{"id": clause_id, "no": no} for no, clause_id in rows]
        if "RETURN n.id AS id LIMIT 1" in cypher:
            clause_id = (self.clauses.get(params.get("sid")) or {}).get(params.get("no"))
            return [{"id": clause_id}] if clause_id else []
        if "RETURN n.id AS id, n.层级路径 AS path" in cypher:
            return list(self.devices.get(params.get("name")) or [])
        return []

    # -- 断言辅助 ---------------------------------------------------------
    def cyphers(self) -> list[str]:
        return [cypher for cypher, _ in self.calls]

    def created_vertex_labels(self) -> list[str]:
        return [c for c in self.cyphers() if c.startswith("CALL db.createVertexLabel")]

    def created_edge_labels(self) -> list[str]:
        return [c for c in self.cyphers() if c.startswith("CALL db.createEdgeLabel")]

    def merges(self, label: str) -> list[tuple[str, dict[str, Any]]]:
        """返回针对某标签的节点 MERGE 调用。"""

        return [call for call in self.calls if f"MERGE (n:{label} " in call[0]]

    def edges(self, rel: str) -> list[tuple[str, dict[str, Any]]]:
        """返回某关系类型的边 MERGE 调用。"""

        return [call for call in self.calls if f"MERGE (a)-[r:{rel}]->(b)" in call[0]]
