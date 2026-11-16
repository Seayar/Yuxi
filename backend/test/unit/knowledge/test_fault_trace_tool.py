"""故障溯源工具单元测试：故障图四步编排、设备校验、实时判据、多现象归属与失败分支。"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from yuxi.agents import thinking as thinking_module
from yuxi.knowledge.fault_trace import tool as tool_module
from yuxi.knowledge.fault_trace import workflow as workflow_module
from yuxi.knowledge.fault_trace.contracts import (
    DEVICE_AMBIGUOUS_MESSAGE,
    DEVICE_NOT_FOUND_MESSAGE,
    FAULT_GRAPH_NOT_CONFIGURED_MESSAGE,
    FAULT_GRAPH_UNAVAILABLE_MESSAGE,
    FAULT_TRACE_AGENT_SLUG,
    IMAGE_NOT_FOUND_MESSAGE,
    INTENT_CANCELLED_MESSAGE,
    NO_FAULT_GRAPH_MESSAGE,
    FaultTraceQuery,
    RealtimeValue,
)
from yuxi.knowledge.fault_trace.graph import FaultGraphEvidence
from yuxi.agents.toolkits.tugraph import TuGraphConfig, TuGraphError
from yuxi.knowledge.fault_trace.images import DeviceImage
from yuxi.knowledge.fault_trace.ledger import LedgerDevice
from yuxi.knowledge.fault_trace.tool import _resolve_kg_config
from yuxi.services.input_message_service import build_chat_input_message
from yuxi.services.yolo_service import YoloDetectResult

pytestmark = pytest.mark.unit

FAULT_FN = tool_module.trace_fault.coroutine


class _FakeOption:
    def __init__(self, value: dict) -> None:
        self._value = value

    async def get(self) -> dict:
        return self._value


def _kg_option() -> dict:
    return {
        "server_url": "bolt://tugraph:7688",
        "graph": "kg5",
        "username": "kg-user",
        "password": "test-password",
    }


DEVICE = {
    "id": "LGR#3b7y",
    "name": "3B-7Y",
    "type": "device",
    "labels": ["设备实例"],
    "properties": {"名称": "3B-7Y", "台账ID列表": "3b7y", "电压等级": "10kV", "设备分类": "中压线路"},
}
TYPE = {
    "id": "DEV#架空线路",
    "name": "架空线路",
    "type": "device_type",
    "labels": ["设备类型"],
    "properties": {"名称": "架空线路"},
}
STATION = {
    "id": "LGR#station",
    "name": "35kV#3变电站",
    "type": "device",
    "labels": ["设备实例"],
    "properties": {"名称": "35kV#3变电站", "电压等级": "35kV", "设备分类": "变电站"},
}
PHENOMENON = {
    "id": "PH-04",
    "name": "树木倒伏枝干压覆导线",
    "type": "phenomenon",
    "properties": {"名称": "树木倒伏枝干压覆导线"},
}
CAUSE = {
    "id": "CA-04",
    "name": "通道内树木未及时砍伐或修剪，遇大风倒伏压线",
    "type": "cause",
    "properties": {"名称": "通道内树木未及时砍伐或修剪，遇大风倒伏压线"},
}
TRIGGER = {
    "id": "TC-R03",
    "name": "泄漏电流超1mA",
    "type": "trigger",
    "properties": {"名称": "泄漏电流超1mA", "表达式": "leakage_current > 1"},
}
MEASURE = {
    "id": "MM-03",
    "name": "修剪通道内树木",
    "type": "measure",
    "properties": {"名称": "修剪通道内树木", "说明": "1. 修剪树木\n2. 清理现场"},
}


def _edge(source: str, target: str, edge_type: str) -> dict:
    return {
        "id": f"{source}->{edge_type}->{target}",
        "source_id": source,
        "target_id": target,
        "type": edge_type,
        "properties": {},
    }


def _evidence(*, with_chain: bool = True) -> FaultGraphEvidence:
    edges = [
        _edge(DEVICE["id"], TYPE["id"], "belongs_to_device_type"),
        _edge(STATION["id"], DEVICE["id"], "electrically_connected_to"),
    ]
    if not with_chain:
        return FaultGraphEvidence(nodes=[DEVICE, TYPE], edges=edges)
    edges += [
        _edge(TYPE["id"], PHENOMENON["id"], "has_phenomenon"),
        _edge(PHENOMENON["id"], CAUSE["id"], "has_cause"),
        _edge(CAUSE["id"], TRIGGER["id"], "has_trigger_condition"),
        _edge(CAUSE["id"], MEASURE["id"], "has_measure"),
    ]
    return FaultGraphEvidence(
        nodes=[DEVICE, TYPE, STATION, PHENOMENON, CAUSE, TRIGGER, MEASURE],
        edges=edges,
    )


def _patch_success(monkeypatch, *, values: dict | None = None):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(_kg_option()))

    def fake_find(_client, _names):
        return [DEVICE]

    def fake_resolve(_client, _device):
        return "架空线路", TYPE

    def fake_collect(_client, *, devices, device_type):
        return _evidence()

    async def fake_values(_device_id, _attributes):
        return values or {"leakage_current": RealtimeValue("leakage_current", "ok", 1.2, raw="1.2")}

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(
            device_name="3B-7Y",
            voltage_level="10kV",
            phenomena=["树木倒伏枝干压覆导线"],
        )

    async def fake_ledger(_name, *, ancestors=None):
        del ancestors
        return None

    async def fake_image(_name):
        return None

    async def fake_detect(**_kwargs):
        return YoloDetectResult(fault=False, fault_type=None, fault_name=None)

    monkeypatch.setattr(workflow_module, "find_devices", fake_find)
    monkeypatch.setattr(workflow_module, "resolve_device_type", fake_resolve)
    monkeypatch.setattr(workflow_module, "collect_kg3_evidence", fake_collect)
    monkeypatch.setattr(workflow_module, "read_realtime_values", fake_values)
    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)
    monkeypatch.setattr(workflow_module, "resolve_device_image", fake_image)
    monkeypatch.setattr(workflow_module, "detect_fault_image", fake_detect)
    monkeypatch.setattr(workflow_module.ledger_repository, "find_device", fake_ledger)


def _runtime() -> SimpleNamespace:
    return SimpleNamespace(
        context=SimpleNamespace(model=None, skills=["fault-trace"]),
        tool_call_id="t1",
    )


@pytest.mark.asyncio
async def test_resolve_kg_config_reads_all_fields(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(_kg_option()))

    config = await _resolve_kg_config()

    assert config == TuGraphConfig(
        uri="bolt://tugraph:7688",
        graph="kg5",
        username="kg-user",
        password="test-password",
    )


@pytest.mark.asyncio
async def test_resolve_kg_config_returns_none_when_incomplete(monkeypatch):
    for value in ({}, {"server_url": "bolt://x:7688"}, {"server_url": "bolt://x:7688", "graph": "kg3"}):
        monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(value))
        assert await _resolve_kg_config() is None


def test_tool_schema_hides_confirmation_from_model():
    """模型不能通过工具参数自行确认意图；确认结论只来自确认卡片。"""

    assert "confirmed_intent" not in tool_module.FaultTraceInput.model_fields
    assert "image_result" not in tool_module.FaultTraceInput.model_fields
    # 即便调用参数里塞了这两个键，schema 校验也会丢弃它们，服务端函数拿不到。
    parsed = tool_module.FaultTraceInput.model_validate(
        {"query_text": "3B-7Y 故障", "confirmed_intent": True, "image_result": {"fault": True}}
    )
    assert parsed.model_dump() == {"query_text": "3B-7Y 故障"}


@pytest.mark.asyncio
async def test_trace_fault_returns_not_configured_when_kg_missing(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption({}))

    result = await FAULT_FN(query_text="1号主变冷却器风扇停转", runtime=_runtime())

    assert result == FAULT_GRAPH_NOT_CONFIGURED_MESSAGE


@pytest.mark.asyncio
async def test_trace_fault_emits_four_steps_with_fault_graph_data(monkeypatch):
    _patch_success(monkeypatch)

    result = await FAULT_FN(
        query_text="3B-7Y 通道内树木倒伏压覆导线",
        confirmed_intent=True,
        runtime=_runtime(),
    )

    steps = result["thinking_steps"]
    assert [step["step_id"] for step in steps] == [
        "parse_fault",
        "match_causal_chain",
        "trace_topology",
        "generate_report",
    ]
    assert [step["step_index"] for step in steps] == [1, 2, 3, 4]
    assert [step["title"] for step in steps] == [
        "解析故障描述",
        "匹配因果链",
        "拓扑关系追溯",
        "生成溯源报告",
    ]
    assert {step["state"] for step in steps} == {"completed"}
    assert {step["agent"] for step in steps} == {FAULT_TRACE_AGENT_SLUG}

    intent = steps[0]["data"]
    assert intent["device"]["device_id"] == "3b7y"
    assert intent["device"]["device_type"] == "架空线路"
    assert intent["summary"]

    causal = steps[1]["data"]
    assert causal["device_type"] == "架空线路"
    assert "fault_modes" not in causal
    assert causal["causes"][0]["status"] == "established"
    assert causal["causes"][0]["true_count"] == 1
    assert causal["triggers"][0]["result"] == "true"
    assert causal["missing_data"] == []

    topology = steps[2]["data"]
    assert [device["name"] for device in topology["upstream"]] == ["35kV#3变电站"]
    assert topology["consequences"] == []

    final = steps[3]["data"]
    assert final["verdict"] == "established"
    assert final["located_cause"] == {"cause_id": "CA-04", "name": CAUSE["name"]}
    assert final["report"]["location"]["phenomena"] == ["树木倒伏枝干压覆导线"]
    assert final["report"]["meta"]["warnings"] == []
    assert final["report"]["schema_version"] == "fault-trace-report/v1"
    assert final["report"]["suggestions"] == [
        {"cause_id": "CA-04", "cause_name": CAUSE["name"], "measures": ["1. 修剪树木\n2. 清理现场"]}
    ]
    assert "<th>故障现象</th><th>可能原因</th>" in final["answer_markdown"]
    assert "1. 修剪树木<br>2. 清理现场" in final["answer_markdown"]
    assert "**④ 影响范围**" in final["answer_markdown"]
    assert "**⑤ 处置建议**" in final["answer_markdown"]


@pytest.mark.asyncio
async def test_trace_fault_prefers_ledger_device_id(monkeypatch):
    _patch_success(monkeypatch)
    called: list[str] = []

    async def fake_ledger(_name, *, ancestors=None):
        del ancestors
        return LedgerDevice(device_id="f531", name="#1主变压器", voltage_level="35kV")

    async def fake_values(device_id, _attributes):
        called.append(device_id)
        return {"leakage_current": RealtimeValue("leakage_current", "ok", 1.2, raw="1.2")}

    monkeypatch.setattr(workflow_module.ledger_repository, "find_device", fake_ledger)
    monkeypatch.setattr(workflow_module, "read_realtime_values", fake_values)

    result = await FAULT_FN(query_text="3B-7Y 故障", confirmed_intent=True, runtime=_runtime())

    assert called == ["f531"]
    intent = result["thinking_steps"][0]["data"]
    assert intent["device"]["device_id"] == "f531"
    assert intent["device"]["ledger_id"] == "f531"


def _evidence_type_level() -> FaultGraphEvidence:
    """设备类型直接挂现象，现象挂原因；没有故障模式层。"""

    edges = [
        _edge(DEVICE["id"], TYPE["id"], "belongs_to_device_type"),
        _edge(TYPE["id"], PHENOMENON["id"], "has_phenomenon"),
        _edge(PHENOMENON["id"], CAUSE["id"], "has_cause"),
        _edge(CAUSE["id"], TRIGGER["id"], "has_trigger_condition"),
        _edge(STATION["id"], DEVICE["id"], "electrically_connected_to"),
    ]
    return FaultGraphEvidence(
        nodes=[DEVICE, TYPE, PHENOMENON, CAUSE, TRIGGER, STATION],
        edges=edges,
        cause_links="fault_phenomenon",
    )


def _evidence_multi_phenomenon() -> FaultGraphEvidence:
    """同一设备类型挂两个现象；两个原因都成立，只有命中的“燃烧起火”进入定位与推理表。"""

    smoke = {
        "id": "PH-SMOKE",
        "name": "冒烟异常",
        "type": "phenomenon",
        "labels": ["故障现象"],
        "properties": {"名称": "冒烟异常"},
    }
    fire = {
        "id": "PH-FIRE",
        "name": "燃烧起火",
        "type": "phenomenon",
        "labels": ["故障现象"],
        "properties": {"名称": "燃烧起火"},
    }
    smoke_cause = {
        "id": "CAUSE-SMOKE",
        "name": "绕组匝间短路与局部放电",
        "type": "cause",
        "labels": ["故障原因"],
        "properties": {"名称": "绕组匝间短路与局部放电"},
    }
    fire_cause = {
        "id": "CAUSE-FIRE",
        "name": "冷却（散热）系统失效",
        "type": "cause",
        "labels": ["故障原因"],
        "properties": {"名称": "冷却（散热）系统失效"},
    }
    smoke_trigger = {
        "id": "TC-SMOKE",
        "name": "绕组局部温度超过145℃",
        "type": "trigger",
        "labels": ["触发条件"],
        "properties": {"名称": "绕组局部温度超过145℃", "表达式": "winding_local_temp > 145"},
    }
    fire_trigger = {
        "id": "TC-FIRE",
        "name": "风机电流为0A",
        "type": "trigger",
        "labels": ["触发条件"],
        "properties": {"名称": "风机电流为0A", "表达式": "cooling_fan_current == 0"},
    }
    edges = [
        _edge(DEVICE["id"], TYPE["id"], "belongs_to_device_type"),
        _edge(TYPE["id"], smoke["id"], "has_phenomenon"),
        _edge(TYPE["id"], fire["id"], "has_phenomenon"),
        _edge(smoke["id"], smoke_cause["id"], "has_cause"),
        _edge(fire["id"], fire_cause["id"], "has_cause"),
        _edge(smoke_cause["id"], smoke_trigger["id"], "has_trigger_condition"),
        _edge(fire_cause["id"], fire_trigger["id"], "has_trigger_condition"),
        _edge(STATION["id"], DEVICE["id"], "electrically_connected_to"),
    ]
    return FaultGraphEvidence(
        nodes=[DEVICE, TYPE, STATION, smoke, fire, smoke_cause, fire_cause, smoke_trigger, fire_trigger],
        edges=edges,
        cause_links="fault_phenomenon",
    )


@pytest.mark.asyncio
async def test_trace_fault_scopes_location_to_matched_phenomenon(monkeypatch):
    """多现象设备类型只把命中现象的原因写入定位，推理表也只保留命中现象。"""

    _patch_success(monkeypatch)
    evidence = _evidence_multi_phenomenon()
    monkeypatch.setattr(workflow_module, "collect_kg3_evidence", lambda *_args, **_kwargs: evidence)

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=["燃烧起火"])

    async def fake_values(_device_id, _attributes):
        return {
            "winding_local_temp": RealtimeValue("winding_local_temp", "ok", 180.0, raw="180"),
            "cooling_fan_current": RealtimeValue("cooling_fan_current", "ok", 0.0, raw="0"),
        }

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)
    monkeypatch.setattr(workflow_module, "read_realtime_values", fake_values)

    result = await FAULT_FN(query_text="3B-7Y 燃烧起火", confirmed_intent=True, runtime=_runtime())

    report = result["report"]
    assert report["summary"]["phenomenon"] == "燃烧起火"
    assert report["location"]["phenomena"] == ["燃烧起火"]
    assert report["location"]["cause_name"] == "冷却（散热）系统失效"
    assert [cause["name"] for cause in report["location"]["causes"]] == ["冷却（散热）系统失效"]
    assert [row["phenomenon"] for row in report["reasoning"]] == ["燃烧起火"]
    location_markdown = result["answer_markdown"].split("**③ 原因定位**")[1].split("**④ 影响范围**")[0]
    assert "燃烧起火" in location_markdown
    assert "冒烟异常" not in location_markdown
    reasoning_markdown = result["answer_markdown"].split("**② 推理过程**")[1].split("**③ 原因定位**")[0]
    assert "冒烟异常" not in reasoning_markdown


@pytest.mark.asyncio
async def test_trace_fault_reasoning_keeps_only_yolo_phenomenon(monkeypatch):
    """图片类别命中其中一个现象时，推理表不再列出该设备类型的其它现象。"""

    _patch_success(monkeypatch)
    monkeypatch.setattr(
        workflow_module,
        "collect_kg3_evidence",
        lambda *_args, **_kwargs: _evidence_multi_phenomenon(),
    )

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=[])

    async def fake_image(_name):
        return DeviceImage(
            bucket="fault-assets",
            object_key="device-images/3B-7Y.jpg",
            content_type="image/jpeg",
            data=b"jpeg",
        )

    async def fake_detect(**_kwargs):
        return YoloDetectResult(fault=True, fault_type="fire", fault_name="燃烧起火")

    async def fake_values(_device_id, _attributes):
        return {"cooling_fan_current": RealtimeValue("cooling_fan_current", "ok", 0.0, raw="0")}

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)
    monkeypatch.setattr(workflow_module, "resolve_device_image", fake_image)
    monkeypatch.setattr(workflow_module, "detect_fault_image", fake_detect)
    monkeypatch.setattr(workflow_module, "read_realtime_values", fake_values)

    result = await FAULT_FN(query_text="3B-7Y 通道异常", confirmed_intent=True, runtime=_runtime())

    report = result["report"]
    assert [row["phenomenon"] for row in report["reasoning"]] == ["燃烧起火"]
    assert report["location"]["phenomena"] == ["燃烧起火"]
    reasoning_markdown = result["answer_markdown"].split("**② 推理过程**")[1].split("**③ 原因定位**")[0]
    assert "燃烧起火" in reasoning_markdown
    assert "冒烟异常" not in reasoning_markdown


@pytest.mark.asyncio
async def test_trace_fault_reasoning_prefers_yolo_over_text(monkeypatch):
    """文字现象与图片类别不一致时，推理表只跟随图片类别。"""

    _patch_success(monkeypatch)
    monkeypatch.setattr(
        workflow_module,
        "collect_kg3_evidence",
        lambda *_args, **_kwargs: _evidence_multi_phenomenon(),
    )

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=["冒烟异常"])

    async def fake_image(_name):
        return DeviceImage(
            bucket="fault-assets",
            object_key="device-images/3B-7Y.jpg",
            content_type="image/jpeg",
            data=b"jpeg",
        )

    async def fake_detect(**_kwargs):
        return YoloDetectResult(fault=True, fault_type="fire", fault_name="燃烧起火")

    async def fake_values(_device_id, _attributes):
        return {"cooling_fan_current": RealtimeValue("cooling_fan_current", "ok", 0.0, raw="0")}

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)
    monkeypatch.setattr(workflow_module, "resolve_device_image", fake_image)
    monkeypatch.setattr(workflow_module, "detect_fault_image", fake_detect)
    monkeypatch.setattr(workflow_module, "read_realtime_values", fake_values)

    result = await FAULT_FN(query_text="3B-7Y 冒烟异常", confirmed_intent=True, runtime=_runtime())

    report = result["report"]
    assert [row["phenomenon"] for row in report["reasoning"]] == ["燃烧起火"]
    assert report["location"]["phenomena"] == ["燃烧起火"]
    assert report["location"]["cause_name"] == "冷却（散热）系统失效"


def _evidence_matched_unknown_other_established() -> FaultGraphEvidence:
    """命中的“燃烧起火”原因缺少实时值（unknown），未命中的“冒烟异常”原因成立。"""

    smoke = {
        "id": "PH-SMOKE",
        "name": "冒烟异常",
        "type": "phenomenon",
        "labels": ["故障现象"],
        "properties": {"名称": "冒烟异常"},
    }
    fire = {
        "id": "PH-FIRE",
        "name": "燃烧起火",
        "type": "phenomenon",
        "labels": ["故障现象"],
        "properties": {"名称": "燃烧起火"},
    }
    smoke_cause = {
        "id": "CAUSE-SMOKE",
        "name": "冒烟原因",
        "type": "cause",
        "labels": ["故障原因"],
        "properties": {"名称": "冒烟原因"},
    }
    fire_cause = {
        "id": "CAUSE-FIRE",
        "name": "火因待确认",
        "type": "cause",
        "labels": ["故障原因"],
        "properties": {"名称": "火因待确认"},
    }
    smoke_trigger = {
        "id": "TC-SMOKE",
        "name": "风机电流为0A",
        "type": "trigger",
        "labels": ["触发条件"],
        "properties": {"名称": "风机电流为0A", "表达式": "cooling_fan_current == 0"},
    }
    fire_trigger = {
        "id": "TC-FIRE",
        "name": "绕组局部温度超过145℃",
        "type": "trigger",
        "labels": ["触发条件"],
        "properties": {"名称": "绕组局部温度超过145℃", "表达式": "winding_local_temp > 145"},
    }
    edges = [
        _edge(DEVICE["id"], TYPE["id"], "belongs_to_device_type"),
        _edge(TYPE["id"], smoke["id"], "has_phenomenon"),
        _edge(TYPE["id"], fire["id"], "has_phenomenon"),
        _edge(smoke["id"], smoke_cause["id"], "has_cause"),
        _edge(fire["id"], fire_cause["id"], "has_cause"),
        _edge(smoke_cause["id"], smoke_trigger["id"], "has_trigger_condition"),
        _edge(fire_cause["id"], fire_trigger["id"], "has_trigger_condition"),
        _edge(STATION["id"], DEVICE["id"], "electrically_connected_to"),
    ]
    return FaultGraphEvidence(
        nodes=[DEVICE, TYPE, STATION, smoke, fire, smoke_cause, fire_cause, smoke_trigger, fire_trigger],
        edges=edges,
        cause_links="fault_phenomenon",
    )


@pytest.mark.asyncio
async def test_trace_fault_scopes_verdict_and_evidence_to_matched_phenomenon(monkeypatch):
    """命中现象无成立原因时，③ 的 verdict/证据/文案不能混入未命中现象的成立原因。"""

    _patch_success(monkeypatch)
    monkeypatch.setattr(
        workflow_module,
        "collect_kg3_evidence",
        lambda *_args, **_kwargs: _evidence_matched_unknown_other_established(),
    )

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=["燃烧起火"])

    async def fake_values(_device_id, _attributes):
        return {"cooling_fan_current": RealtimeValue("cooling_fan_current", "ok", 0.0, raw="0")}

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)
    monkeypatch.setattr(workflow_module, "read_realtime_values", fake_values)

    result = await FAULT_FN(query_text="3B-7Y 燃烧起火", confirmed_intent=True, runtime=_runtime())

    report = result["report"]
    assert report["summary"]["phenomenon"] == "燃烧起火"
    assert report["location"]["causes"] == []
    assert report["location"]["verdict"] == "unknown"
    assert report["location"]["phenomena"] == []
    assert [item["trigger_id"] for item in report["location"]["evidence"]] == ["TC-FIRE"]
    location_markdown = result["answer_markdown"].split("**③ 原因定位**")[1].split("**④ 影响范围**")[0]
    assert "冒烟原因" not in location_markdown
    assert "全部候选原因的实时判据均不成立" not in location_markdown


def _evidence_matched_without_causes() -> FaultGraphEvidence:
    """命中的“燃烧起火”没有 has_cause 边；未命中的“冒烟异常”原因成立。"""

    smoke = {
        "id": "PH-SMOKE",
        "name": "冒烟异常",
        "type": "phenomenon",
        "labels": ["故障现象"],
        "properties": {"名称": "冒烟异常"},
    }
    fire = {
        "id": "PH-FIRE",
        "name": "燃烧起火",
        "type": "phenomenon",
        "labels": ["故障现象"],
        "properties": {"名称": "燃烧起火"},
    }
    smoke_cause = {
        "id": "CAUSE-SMOKE",
        "name": "冒烟原因",
        "type": "cause",
        "labels": ["故障原因"],
        "properties": {"名称": "冒烟原因"},
    }
    smoke_trigger = {
        "id": "TC-SMOKE",
        "name": "风机电流为0A",
        "type": "trigger",
        "labels": ["触发条件"],
        "properties": {"名称": "风机电流为0A", "表达式": "cooling_fan_current == 0"},
    }
    edges = [
        _edge(DEVICE["id"], TYPE["id"], "belongs_to_device_type"),
        _edge(TYPE["id"], smoke["id"], "has_phenomenon"),
        _edge(TYPE["id"], fire["id"], "has_phenomenon"),
        _edge(smoke["id"], smoke_cause["id"], "has_cause"),
        _edge(smoke_cause["id"], smoke_trigger["id"], "has_trigger_condition"),
        _edge(STATION["id"], DEVICE["id"], "electrically_connected_to"),
    ]
    return FaultGraphEvidence(
        nodes=[DEVICE, TYPE, STATION, smoke, fire, smoke_cause, smoke_trigger],
        edges=edges,
        cause_links="fault_phenomenon",
    )


@pytest.mark.asyncio
async def test_trace_fault_matched_phenomenon_without_causes_keeps_empty_scope(monkeypatch):
    """命中现象没有原因边时范围保持为空，不能回退到全部原因或其它现象的证据。"""

    _patch_success(monkeypatch)
    monkeypatch.setattr(
        workflow_module,
        "collect_kg3_evidence",
        lambda *_args, **_kwargs: _evidence_matched_without_causes(),
    )

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=["燃烧起火"])

    async def fake_values(_device_id, _attributes):
        return {"cooling_fan_current": RealtimeValue("cooling_fan_current", "ok", 0.0, raw="0")}

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)
    monkeypatch.setattr(workflow_module, "read_realtime_values", fake_values)

    result = await FAULT_FN(query_text="3B-7Y 燃烧起火", confirmed_intent=True, runtime=_runtime())

    location = result["report"]["location"]
    assert location["verdict"] == "none"
    assert location["causes"] == []
    assert location["phenomena"] == []
    assert location["evidence"] == []
    location_markdown = result["answer_markdown"].split("**③ 原因定位**")[1].split("**④ 影响范围**")[0]
    assert "命中的故障现象下没有可判定的原因" in location_markdown
    assert "图谱未返回可判定的故障原因" not in location_markdown


@pytest.mark.asyncio
async def test_trace_fault_falls_back_to_phenomena_of_located_causes_when_no_match(monkeypatch):
    """没有现象命中时按成立原因反推现象，不把没有成立原因的现象写进定位。"""

    _patch_success(monkeypatch)
    monkeypatch.setattr(
        workflow_module,
        "collect_kg3_evidence",
        lambda *_args, **_kwargs: _evidence_multi_phenomenon(),
    )

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=["异物搭挂"])

    async def fake_values(_device_id, _attributes):
        return {"winding_local_temp": RealtimeValue("winding_local_temp", "ok", 180.0, raw="180")}

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)
    monkeypatch.setattr(workflow_module, "read_realtime_values", fake_values)

    result = await FAULT_FN(query_text="3B-7Y 异物搭挂", confirmed_intent=True, runtime=_runtime())

    causal = next(step for step in result["thinking_steps"] if step["step_id"] == "match_causal_chain")
    assert any("未匹配到用户描述中的故障现象" in warning for warning in causal["data"]["warnings"])
    assert result["report"]["location"]["phenomena"] == ["冒烟异常"]
    # 没有命中现象时没有可收敛的依据，推理表保留该设备类型的全部现象。
    assert [row["phenomenon"] for row in result["report"]["reasoning"]] == ["冒烟异常", "燃烧起火"]


@pytest.mark.asyncio
async def test_trace_fault_supports_type_level_phenomena_without_mode(monkeypatch):
    _patch_success(monkeypatch)
    monkeypatch.setattr(workflow_module, "collect_kg3_evidence", lambda *_args, **_kwargs: _evidence_type_level())

    result = await FAULT_FN(query_text="3B-7Y 故障", confirmed_intent=True, runtime=_runtime())

    steps = {step["step_id"]: step for step in result["thinking_steps"]}
    assert "fault_modes" not in steps["match_causal_chain"]["data"]
    assert steps["match_causal_chain"]["data"]["causes"][0]["status"] == "established"
    assert steps["generate_report"]["data"]["verdict"] == "established"


@pytest.mark.asyncio
async def test_trace_fault_asks_for_device_when_not_found(monkeypatch):
    _patch_success(monkeypatch)

    def fake_find(_client, _names):
        return []

    monkeypatch.setattr(workflow_module, "find_devices", fake_find)

    result = await FAULT_FN(query_text="未知设备故障", confirmed_intent=True, runtime=_runtime())

    assert result["clarification"] == DEVICE_NOT_FOUND_MESSAGE
    steps = result["thinking_steps"]
    assert [step["step_id"] for step in steps] == ["parse_fault"]
    assert steps[0]["state"] == "waiting"
    assert steps[0]["data"]["reason"] == "device_not_found"


@pytest.mark.asyncio
async def test_trace_fault_returns_candidates_when_ambiguous(monkeypatch):
    _patch_success(monkeypatch)
    second = {
        **DEVICE,
        "id": "LGR#other",
        "properties": {**DEVICE["properties"], "台账ID列表": "other"},
    }

    def fake_find(_client, _names):
        return [DEVICE, second]

    monkeypatch.setattr(workflow_module, "find_devices", fake_find)

    result = await FAULT_FN(query_text="3B-7Y 故障", confirmed_intent=True, runtime=_runtime())

    assert result["clarification"] == DEVICE_AMBIGUOUS_MESSAGE
    assert len(result["candidates"]) == 2
    steps = result["thinking_steps"]
    assert steps[0]["data"]["reason"] == "device_ambiguous"
    assert steps[0]["data"]["candidates"][0]["device_id"] == "3b7y"


@pytest.mark.asyncio
async def test_trace_fault_maps_graph_error_without_leaking_address(monkeypatch):
    _patch_success(monkeypatch)
    events: list[dict] = []
    monkeypatch.setattr(thinking_module, "get_stream_writer", lambda: events.append)

    def fake_find(_client, _names):
        raise TuGraphError("boom at bolt://192.168.138.10:7688")

    monkeypatch.setattr(workflow_module, "find_devices", fake_find)

    result = await FAULT_FN(query_text="3B-7Y 故障", confirmed_intent=True, runtime=_runtime())

    assert result == FAULT_GRAPH_UNAVAILABLE_MESSAGE
    failed = next(
        event["thinking"]
        for event in events
        if event["thinking"]["step_id"] == "parse_fault" and event["thinking"]["state"] == "failed"
    )
    assert failed["error"] == {"code": "graph_unavailable", "message": FAULT_GRAPH_UNAVAILABLE_MESSAGE}
    assert "192.168" not in str(failed)


@pytest.mark.asyncio
async def test_trace_fault_fails_causal_chain_without_evidence(monkeypatch):
    _patch_success(monkeypatch)
    events: list[dict] = []
    monkeypatch.setattr(thinking_module, "get_stream_writer", lambda: events.append)

    def fake_collect(_client, *, devices, device_type):
        return _evidence(with_chain=False)

    monkeypatch.setattr(workflow_module, "collect_kg3_evidence", fake_collect)

    result = await FAULT_FN(query_text="3B-7Y 故障", confirmed_intent=True, runtime=_runtime())

    assert result == NO_FAULT_GRAPH_MESSAGE
    states = [event["thinking"]["state"] for event in events if event["thinking"]["step_id"] == "match_causal_chain"]
    assert states == ["started", "failed"]


@pytest.mark.asyncio
async def test_trace_fault_keeps_waiting_on_clarification(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(_kg_option()))

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(
            device_type="变压器",
            phenomena=["过热"],
            clarification_required=True,
            clarification_message="未能识别故障设备，请明确设备名称后重新提交。",
        )

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)

    result = await FAULT_FN(query_text="变压器过热", confirmed_intent=True, runtime=_runtime())

    assert result["clarification"] == "未能识别故障设备，请明确设备名称后重新提交。"
    steps = result["thinking_steps"]
    assert [step["step_id"] for step in steps] == ["parse_fault"]
    assert steps[0]["state"] == "waiting"
    assert steps[0]["data"]["reason"] == "needs_clarification"


@pytest.mark.asyncio
async def test_trace_fault_marks_unknown_when_realtime_unavailable(monkeypatch):
    _patch_success(
        monkeypatch,
        values={"leakage_current": RealtimeValue("leakage_current", "unavailable")},
    )

    result = await FAULT_FN(query_text="3B-7Y 故障", confirmed_intent=True, runtime=_runtime())

    steps = {step["step_id"]: step for step in result["thinking_steps"]}
    assert steps["match_causal_chain"]["data"]["causes"][0]["status"] == "unknown"
    assert steps["match_causal_chain"]["data"]["missing_data"] == ["leakage_current"]
    assert "实时数据服务暂时不可用" in " ".join(steps["match_causal_chain"]["data"]["warnings"])
    assert steps["generate_report"]["data"]["verdict"] == "unknown"


@pytest.mark.asyncio
async def test_trace_fault_merges_realtime_values_across_ledger_ids(monkeypatch):
    _patch_success(monkeypatch)
    device = {
        **DEVICE,
        "properties": {**DEVICE["properties"], "台账ID列表": "old-id、3b7y"},
    }
    calls: list[str] = []

    def fake_find(_client, _names):
        return [device]

    async def fake_values(device_id, _attributes):
        calls.append(device_id)
        if device_id == "old-id":
            return {"leakage_current": RealtimeValue("leakage_current", "missing")}
        return {"leakage_current": RealtimeValue("leakage_current", "ok", 1.2, raw="1.2")}

    monkeypatch.setattr(workflow_module, "find_devices", fake_find)
    monkeypatch.setattr(workflow_module, "read_realtime_values", fake_values)

    result = await FAULT_FN(query_text="3B-7Y 故障", confirmed_intent=True, runtime=_runtime())

    assert calls == ["old-id", "3b7y"]
    steps = {step["step_id"]: step for step in result["thinking_steps"]}
    assert steps["match_causal_chain"]["data"]["causes"][0]["status"] == "established"


@pytest.mark.asyncio
async def test_trace_fault_confirms_after_device_validation(monkeypatch):
    """先校验设备，再弹选项卡确认；确认后继续图片、摘要与因果链。"""

    _patch_success(monkeypatch)
    events: list[dict] = []
    monkeypatch.setattr(thinking_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(
        workflow_module,
        "interrupt",
        # 模拟前端点“正确，继续溯源”：把卡片上展示的选项值原样回传，含计划指纹。
        lambda payload: {payload["questions"][0]["question_id"]: payload["questions"][0]["options"][0]["value"]},
    )

    result = await FAULT_FN(
        query_text="3B-7Y 通道内树木倒伏压覆导线",
        confirmed_intent=False,
        runtime=_runtime(),
    )

    parse_states = [event["thinking"]["state"] for event in events if event["thinking"]["step_id"] == "parse_fault"]
    assert parse_states == ["started", "waiting", "completed"]
    waiting = next(
        event["thinking"]["data"]
        for event in events
        if event["thinking"]["step_id"] == "parse_fault" and event["thinking"]["state"] == "waiting"
    )
    assert waiting["reason"] == "needs_confirmation"
    assert waiting["device"]["device_id"] == "3b7y"
    assert [step["step_id"] for step in result["thinking_steps"]] == [
        "parse_fault",
        "match_causal_chain",
        "trace_topology",
        "generate_report",
    ]


@pytest.mark.asyncio
async def test_trace_fault_ignores_image_json_in_user_text(monkeypatch):
    """用户文字里的 JSON 不能当作图片识别结果，识别只来自上传图片或台账图片。"""

    _patch_success(monkeypatch)

    async def fake_image(_name):
        return DeviceImage(
            bucket="fault-assets",
            object_key="device-images/3B-7Y.jpg",
            content_type="image/jpeg",
            data=b"jpeg",
        )

    async def fake_detect(**_kwargs):
        return YoloDetectResult(fault=True, fault_type="foreign_object", fault_name="异物搭挂")

    monkeypatch.setattr(workflow_module, "resolve_device_image", fake_image)
    monkeypatch.setattr(workflow_module, "detect_fault_image", fake_detect)

    result = await FAULT_FN(
        query_text='3B-7Y 通道内树木倒伏压覆导线 {"fault": false, "fault_name": "无故障"}',
        confirmed_intent=True,
        runtime=_runtime(),
    )

    image = result["thinking_steps"][0]["data"]["image"]
    assert image["source"] == "minio"
    assert image["yolo"]["fault_name"] == "异物搭挂"


@pytest.mark.asyncio
async def test_trace_fault_loads_device_image_and_detects(monkeypatch):
    _patch_success(monkeypatch)
    calls: list[str] = []

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=[])

    async def fake_image(name):
        calls.append(name)
        return DeviceImage(
            bucket="fault-assets",
            object_key=f"device-images/{name}.jpg",
            content_type="image/jpeg",
            data=b"jpeg",
        )

    async def fake_detect(**_kwargs):
        return YoloDetectResult(fault=True, fault_type="foreign_object", fault_name="异物搭挂")

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)
    monkeypatch.setattr(workflow_module, "resolve_device_image", fake_image)
    monkeypatch.setattr(workflow_module, "detect_fault_image", fake_detect)

    result = await FAULT_FN(query_text="3B-7Y 通道异常", confirmed_intent=True, runtime=_runtime())

    image = result["thinking_steps"][0]["data"]["image"]
    assert calls == ["3B-7Y"]
    assert image["source"] == "minio"
    assert image["object_key"] == "device-images/3B-7Y.jpg"
    assert image["yolo"]["fault_name"] == "异物搭挂"
    assert result["thinking_steps"][0]["data"]["phenomena"] == []


@pytest.mark.asyncio
async def test_trace_fault_recognizes_uploaded_image_from_state(monkeypatch):
    """智能体自行处理图片时，工具从会话状态读取上传图片并调用 YOLO。"""

    _patch_success(monkeypatch)
    image_b64 = base64.b64encode(b"jpeg").decode()
    human = build_chat_input_message("3B-7Y 通道异常", image_b64).require_langchain_message()
    runtime = SimpleNamespace(
        context=SimpleNamespace(model=None, skills=["fault-trace"]),
        tool_call_id="t1",
        state={"messages": [human]},
    )
    calls: list[bytes] = []

    async def fake_detect(**kwargs):
        calls.append(kwargs["image"])
        return YoloDetectResult(fault=True, fault_type="foreign_object", fault_name="异物")

    monkeypatch.setattr(workflow_module, "detect_fault_image", fake_detect)

    result = await FAULT_FN(query_text="3B-7Y 通道异常", confirmed_intent=True, runtime=runtime)

    image = result["thinking_steps"][0]["data"]["image"]
    assert calls == [b"jpeg"]
    assert image["source"] == "user_upload"
    assert image["yolo"]["fault_name"] == "异物"


@pytest.mark.asyncio
async def test_trace_fault_clarifies_when_no_image_and_no_phenomenon(monkeypatch):
    _patch_success(monkeypatch)

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=[])

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)

    result = await FAULT_FN(query_text="3B-7Y 通道异常", confirmed_intent=True, runtime=_runtime())

    assert result["clarification"] == IMAGE_NOT_FOUND_MESSAGE
    steps = result["thinking_steps"]
    assert [step["step_id"] for step in steps] == ["parse_fault"]
    assert steps[0]["data"]["reason"] == "phenomenon_missing"


@pytest.mark.asyncio
async def test_trace_fault_uses_model_summary_and_reasoning(monkeypatch):
    _patch_success(monkeypatch)

    async def fake_model(prompt: str) -> str:
        if "故障现象简述" in prompt:
            return "3B-7Y 通道内树木倒伏压覆导线，现场可见放电痕迹。"
        return f"泄漏电流实测1.2mA超过判据，定位{CAUSE['name']}。"

    monkeypatch.setattr(workflow_module, "_context_model_call", lambda _context: fake_model)

    result = await FAULT_FN(
        query_text="3B-7Y 通道内树木倒伏压覆导线",
        confirmed_intent=True,
        runtime=_runtime(),
    )

    report = result["report"]
    assert report["summary"]["text"] == "3B-7Y 通道内树木倒伏压覆导线，现场可见放电痕迹。"
    assert report["location"]["reasoning"].startswith("泄漏电流实测1.2mA")
    assert result["thinking_steps"][0]["data"]["summary"] == report["summary"]["text"]


@pytest.mark.asyncio
async def test_trace_fault_continues_when_replay_reidentifies_differently(monkeypatch):
    """用户确认后重放识别出别的现象时也继续溯源，不返回“不一致”文案。"""

    _patch_success(monkeypatch)

    def confirming(payload):
        return {payload["questions"][0]["question_id"]: payload["questions"][0]["options"][0]["value"]}

    monkeypatch.setattr(workflow_module, "interrupt", confirming)

    first = await FAULT_FN(query_text="3B-7Y 通道内树木倒伏压覆导线", runtime=_runtime())
    assert "report" in first

    async def changed_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=["异物搭挂"])

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", changed_extract)

    second = await FAULT_FN(query_text="3B-7Y 通道异常", runtime=_runtime())

    assert "report" in second
    assert second["report"]["location"]["verdict"] in {"established", "rejected", "unknown"}


@pytest.mark.asyncio
async def test_trace_fault_confirmation_shows_identified_type_with_graph_classification(monkeypatch):
    """模型识别设备类型与图谱归类不同时并列展示，溯源证据仍按图谱归类读取。"""

    _patch_success(monkeypatch)
    asked: list[dict] = []

    async def fake_extract(*_args, **_kwargs):
        return FaultTraceQuery(
            device_name="3B-7Y",
            device_type="输电线路",
            voltage_level="10kV",
            phenomena=["树木倒伏枝干压覆导线"],
        )

    def confirming(payload):
        asked.append(payload)
        return {payload["questions"][0]["question_id"]: payload["questions"][0]["options"][0]["value"]}

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", fake_extract)
    monkeypatch.setattr(workflow_module, "interrupt", confirming)

    result = await FAULT_FN(query_text="3B-7Y 通道内树木倒伏压覆导线", runtime=_runtime())

    question = asked[0]["questions"][0]["question"]
    assert "设备类型=输电线路（图谱归类：架空线路）" in question
    # 证据按图谱归类取：报告元数据使用图谱解析出的设备类型，而不是模型文本。
    assert result["report"]["meta"]["device_type"] == "架空线路"


@pytest.mark.asyncio
async def test_trace_fault_cancel_on_correction_card_stops(monkeypatch):
    """关闭“补充修正”卡片按取消结束，不能把取消哨兵当成用户补充重新识别。"""

    _patch_success(monkeypatch)
    payloads: list[dict] = []
    intent_calls: list[int] = []
    events: list[dict] = []

    def answering(payload):
        payloads.append(payload)
        return "correct" if len(payloads) == 1 else "reject"

    async def counting_extract(*_args, **_kwargs):
        intent_calls.append(1)
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=["树木倒伏枝干压覆导线"])

    monkeypatch.setattr(thinking_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", counting_extract)
    monkeypatch.setattr(workflow_module, "interrupt", answering)

    result = await FAULT_FN(query_text="3B-7Y 通道内树木倒伏枝干压覆导线", runtime=_runtime())

    assert result == INTENT_CANCELLED_MESSAGE
    assert [payload["source"] for payload in payloads] == [
        "fault_trace_intent_confirmation",
        "fault_trace_intent_correction",
    ]
    assert len(intent_calls) == 1
    # 取消要给解析步骤一个终态，历史回显不能一直停在 waiting。
    parse_states = [event["thinking"]["state"] for event in events if event["thinking"]["step_id"] == "parse_fault"]
    assert parse_states[-1] == "failed"


@pytest.mark.asyncio
async def test_trace_fault_confirmation_value_is_plain_confirm(monkeypatch):
    """确认卡片的“正确”选项就是普通确认值，不再携带需要比对的信息。"""

    _patch_success(monkeypatch)
    asked: list[dict] = []

    def confirming(payload):
        asked.append(payload)
        return {payload["questions"][0]["question_id"]: payload["questions"][0]["options"][0]["value"]}

    monkeypatch.setattr(workflow_module, "interrupt", confirming)

    result = await FAULT_FN(query_text="3B-7Y 通道内树木倒伏压覆导线", runtime=_runtime())

    assert "report" in result
    assert asked[0]["questions"][0]["options"][0]["value"] == "confirm"
