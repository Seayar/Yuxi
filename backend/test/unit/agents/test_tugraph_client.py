"""TuGraph 客户端：连接失败快速失败、查询事务超时与错误包装。"""

from __future__ import annotations

import pytest
from neo4j import Query

from yuxi.agents.toolkits.tugraph import (
    QUERY_TIMEOUT_SECONDS,
    TuGraphClient,
    TuGraphConfig,
    TuGraphError,
)

pytestmark = pytest.mark.unit

CONFIG = TuGraphConfig(uri="bolt://tugraph:7688", graph="kg3", username="admin", password="secret")


class _Record:
    def __init__(self, data: dict) -> None:
        self._data = data

    def data(self) -> dict:
        return self._data


class _Session:
    def __init__(self, driver: _Driver) -> None:
        self._driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def run(self, query, parameters=None):
        self._driver.calls.append((query, parameters))
        if self._driver.error is not None:
            raise self._driver.error
        return [_Record({"name": "3B-7Y"})]


class _Driver:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[object, object]] = []
        self.closed = False

    def session(self) -> _Session:
        return _Session(self)

    def close(self) -> None:
        self.closed = True


def _client(monkeypatch, driver: _Driver) -> TuGraphClient:
    monkeypatch.setattr("yuxi.agents.toolkits.tugraph.GraphDatabase.driver", lambda *a, **k: driver)
    client = TuGraphClient(CONFIG)
    client.ensure_connected()
    return client


def test_query_carries_transaction_timeout(monkeypatch):
    """超时必须是驱动的事务配置：``Session.run`` 的关键字参数只会并入 Cypher 参数。"""

    driver = _Driver()
    client = _client(monkeypatch, driver)
    driver.calls.clear()

    records = client.run("MATCH (n) RETURN n", node_limit=10)

    assert records == [{"name": "3B-7Y"}]
    query, parameters = driver.calls[-1]
    assert isinstance(query, Query)
    assert query.text == "MATCH (n) RETURN n"
    assert query.timeout == QUERY_TIMEOUT_SECONDS
    assert parameters == {"node_limit": 10}


def test_query_failure_is_wrapped(monkeypatch):
    driver = _Driver()
    client = _client(monkeypatch, driver)
    driver.error = RuntimeError("boom")

    with pytest.raises(TuGraphError):
        client.run("MATCH (n) RETURN n")


def test_connection_failure_fails_fast(monkeypatch):
    def broken_driver(*args, **kwargs):
        raise OSError("unreachable")

    monkeypatch.setattr("yuxi.agents.toolkits.tugraph.GraphDatabase.driver", broken_driver)

    with pytest.raises(TuGraphError):
        TuGraphClient(CONFIG).ensure_connected()
