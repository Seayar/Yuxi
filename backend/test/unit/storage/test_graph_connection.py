import pytest

from yuxi.storage import graph_connection
from yuxi.storage.graph_connection import (
    Neo4jConnectionManager,
    TugraphConnectionManager,
    close_shared_graph_connection,
    graph_backend_name,
    safe_graph_label,
)


@pytest.mark.parametrize("label", ["kb_test", "MilvusKB", "_internal_1"])
def test_safe_graph_label_accepts_valid_labels(label):
    assert safe_graph_label(label) == label


@pytest.mark.parametrize("label", ["", "1invalid", "has-dash", "has space", "中文"])
def test_safe_graph_label_rejects_invalid_labels(label):
    with pytest.raises(ValueError, match="非法图数据库标签"):
        safe_graph_label(label)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "neo4j"), ("", "neo4j"), ("NEO4J", "neo4j"), ("tugraph", "tugraph"), (" TuGraph ", "tugraph")],
)
def test_graph_backend_name_reads_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    else:
        monkeypatch.setenv("GRAPH_BACKEND", value)
    assert graph_backend_name() == expected


def test_graph_backend_name_rejects_unknown(monkeypatch):
    monkeypatch.setenv("GRAPH_BACKEND", "memgraph")
    with pytest.raises(ValueError, match="不支持的图数据库后端"):
        graph_backend_name()


def test_close_shared_graph_connection_closes_existing_manager(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(graph_connection, "_shared_graph_connection", connection)

    close_shared_graph_connection()

    assert connection.closed is True


def test_tugraph_connection_defaults(monkeypatch):
    monkeypatch.delenv("TUGRAPH_URI", raising=False)
    monkeypatch.delenv("TUGRAPH_USERNAME", raising=False)
    monkeypatch.delenv("TUGRAPH_PASSWORD", raising=False)
    manager = TugraphConnectionManager.__new__(TugraphConnectionManager)

    assert manager.backend == "tugraph"
    assert manager._uri() == "bolt://tugraph:7687"
    assert manager._auth() == ("admin", "73@TuGraph")


def test_neo4j_connection_defaults(monkeypatch):
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_USERNAME", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    manager = Neo4jConnectionManager.__new__(Neo4jConnectionManager)

    assert manager.backend == "neo4j"
    assert manager._uri() == "bolt://localhost:7687"
    assert manager._auth() == ("neo4j", "0123456789")
