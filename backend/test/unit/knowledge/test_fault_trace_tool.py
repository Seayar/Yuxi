"""故障溯源工具单元测试：输入解析、kg 配置解析与编排分支。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from yuxi.knowledge.fault_trace import tool as tool_module
from yuxi.knowledge.fault_trace.contracts import (
    FAULT_GRAPH_NOT_CONFIGURED_MESSAGE,
    KG_GRAPH_UNAVAILABLE_MESSAGE,
    FaultTraceQuery,
)
from yuxi.knowledge.fault_trace.graph import FaultGraphEvidence
from yuxi.knowledge.fault_trace.graph_kg import KgGraphConfig
from yuxi.knowledge.fault_trace.tool import (
    _parse_image_result,
    _resolve_kg_config,
)

pytestmark = pytest.mark.unit

FAULT_FN = tool_module.trace_fault.coroutine  # 异步工具的可调用体在 StructuredTool.coroutine


class _FakeOption:
    def __init__(self, value: dict) -> None:
        self._value = value

    async def get(self) -> dict:
        return self._value


def _kg_option() -> dict:
    return {
        "server_url": "bolt://tugraph-kg:7688",
        "graph": "kg",
        "username": "kg-user",
        "password": "test-password",
    }


@pytest.mark.asyncio
async def test_resolve_kg_config_reads_all_fields(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(_kg_option()))

    config = await _resolve_kg_config()

    assert config == KgGraphConfig(
        uri="bolt://tugraph-kg:7688",
        graph="kg",
        username="kg-user",
        password="test-password",
    )


@pytest.mark.asyncio
async def test_resolve_kg_config_returns_none_when_incomplete(monkeypatch):
    for value in ({}, {"server_url": "bolt://x:7688"}, {"server_url": "bolt://x:7688", "graph": "kg"}):
        monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(value))
        assert await _resolve_kg_config() is None


def test_parse_image_result_accepts_detect_payload():
    payload = _parse_image_result('{"fault": true, "fault_type": "fire", "fault_name": "冒烟/着火"}')
    assert payload["fault_type"] == "fire"


def test_parse_image_result_rejects_invalid():
    assert _parse_image_result(None) is None
    assert _parse_image_result("not json") is None
    assert _parse_image_result("[1, 2]") is None
    assert _parse_image_result('{"fault": "yes"}') is None


@pytest.mark.asyncio
async def test_trace_fault_returns_not_configured_when_kg_missing(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption({}))
    context = SimpleNamespace(model=None, _fault_trace_result=None, skills=["fault-trace"])
    runtime = SimpleNamespace(context=context, tool_call_id="t1")

    result = await FAULT_FN(
        query_text="1号主变冷却器风扇停转",
        runtime=runtime,
    )

    assert result == FAULT_GRAPH_NOT_CONFIGURED_MESSAGE


@pytest.mark.asyncio
async def test_trace_fault_maps_kg_error_to_unavailable(monkeypatch):
    """kg 连接/查询失败必须投影为「图谱不可用」，不能伪装成未配置或未检索到。"""

    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(_kg_option()))

    async def fake_kg_collect(client, query):
        raise tool_module.KgGraphError("boom")

    monkeypatch.setattr(tool_module, "_collect_kg_evidence", fake_kg_collect)
    context = SimpleNamespace(model=None, skills=["fault-trace"])
    runtime = SimpleNamespace(context=context, tool_call_id="t1")

    result = await FAULT_FN(
        query_text="1号主变冷却器风扇停转",
        confirmed_intent=True,
        runtime=runtime,
    )

    assert result == KG_GRAPH_UNAVAILABLE_MESSAGE
    assert context._fault_trace_results["1号主变冷却器风扇停转"] == result


@pytest.mark.asyncio
async def test_trace_fault_caches_per_query_and_replays_plain_no_result(monkeypatch):
    """缓存按故障描述区分：同一描述重放复用结果，不同描述必须重新查询。"""

    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(_kg_option()))
    calls = []

    async def _empty_evidence(client, query):
        calls.append(query)
        return FaultGraphEvidence(nodes=[], edges=[])

    monkeypatch.setattr(tool_module, "_collect_kg_evidence", _empty_evidence)
    context = SimpleNamespace(model=None, skills=["fault-trace"])
    runtime = SimpleNamespace(context=context, tool_call_id="t1")

    first = await FAULT_FN(query_text="1号主变冷却器风扇停转", confirmed_intent=True, runtime=runtime)
    replayed = await FAULT_FN(query_text="1号主变冷却器风扇停转", confirmed_intent=True, runtime=runtime)
    other = await FAULT_FN(query_text="2号主变冷却器风扇停转", confirmed_intent=True, runtime=runtime)

    assert "未在故障知识图谱中检索到" in first
    assert replayed == first
    # 重放命中缓存只查一次图；不同描述不共享缓存，各自查询
    assert len(calls) == 2
    assert other == first


@pytest.mark.asyncio
async def test_trace_fault_returns_clarification_when_device_missing(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(_kg_option()))

    async def fake_extract(query_text, *, image_result, model_call=None, previous=None, feedback=None):
        return FaultTraceQuery(
            device_type="变压器",
            phenomena=["过热"],
            clarification_required=True,
            clarification_message="未能识别故障设备，请明确设备名称后重新提交。",
        )

    monkeypatch.setattr(tool_module, "extract_fault_trace_intent", fake_extract)
    context = SimpleNamespace(model=None, _fault_trace_result=None, skills=["fault-trace"])
    runtime = SimpleNamespace(context=context, tool_call_id="t1")

    result = await FAULT_FN(
        query_text="变压器过热",
        confirmed_intent=True,
        runtime=runtime,
    )

    assert result["clarification"] == "未能识别故障设备，请明确设备名称后重新提交。"
