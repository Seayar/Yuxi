"""故障溯源工具单元测试：kg3 四步编排、设备校验、实时判据与失败分支。"""

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
    INTENT_PLAN_CHANGED_MESSAGE,
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
        "graph": "kg3",
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
MODE = {
    "id": "FM-WR-01",
    "name": "异物搭挂",
    "type": "fault_mode",
    "properties": {"名称": "异物搭挂"},
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
CONSEQUENCE = {
    "id": "FC-01",
    "name": "相间短路",
    "type": "consequence",
    "properties": {"名称": "相间短路"},
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
        return FaultGraphEvidence(nodes=[DEVICE, TYPE], edges=edges, cause_links="fault_mode")
    edges += [
        _edge(TYPE["id"], MODE["id"], "has_fault_mode"),
        _edge(MODE["id"], PHENOMENON["id"], "has_phenomenon"),
        _edge(MODE["id"], CAUSE["id"], "has_cause"),
        _edge(CAUSE["id"], TRIGGER["id"], "has_trigger_condition"),
        _edge(CAUSE["id"], MEASURE["id"], "has_measure"),
        _edge(CAUSE["id"], CONSEQUENCE["id"], "may_lead_to_consequence"),
    ]
    return FaultGraphEvidence(
        nodes=[DEVICE, TYPE, STATION, MODE, PHENOMENON, CAUSE, TRIGGER, MEASURE, CONSEQUENCE],
        edges=edges,
        cause_links="fault_mode",
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
        graph="kg3",
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


@pytest.mark.asyncio
async def test_trace_fault_returns_not_configured_when_kg_missing(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption({}))

    result = await FAULT_FN(query_text="1号主变冷却器风扇停转", runtime=_runtime())

    assert result == FAULT_GRAPH_NOT_CONFIGURED_MESSAGE


@pytest.mark.asyncio
async def test_trace_fault_emits_four_steps_with_kg3_data(monkeypatch):
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
    assert causal["fault_modes"] == [{"mode_id": "FM-WR-01", "name": "异物搭挂"}]
    assert causal["causes"][0]["status"] == "established"
    assert causal["causes"][0]["true_count"] == 1
    assert causal["triggers"][0]["result"] == "true"
    assert causal["missing_data"] == []

    topology = steps[2]["data"]
    assert [device["name"] for device in topology["upstream"]] == ["35kV#3变电站"]
    assert topology["consequences"] == [{"consequence_id": "FC-01", "name": "相间短路"}]

    final = steps[3]["data"]
    assert final["verdict"] == "established"
    assert final["located_cause"] == {"cause_id": "CA-04", "name": CAUSE["name"]}
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
    """没有故障模式层：设备类型直接挂现象，现象挂原因。"""

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


@pytest.mark.asyncio
async def test_trace_fault_supports_type_level_phenomena_without_mode(monkeypatch):
    _patch_success(monkeypatch)
    monkeypatch.setattr(workflow_module, "collect_kg3_evidence", lambda *_args, **_kwargs: _evidence_type_level())

    result = await FAULT_FN(query_text="3B-7Y 故障", confirmed_intent=True, runtime=_runtime())

    steps = {step["step_id"]: step for step in result["thinking_steps"]}
    assert steps["match_causal_chain"]["data"]["fault_modes"] == []
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
        lambda payload: {"fault_trace_intent_confirm": "confirm"},
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
async def test_trace_fault_rejects_plan_changed_after_confirmation(monkeypatch):
    """恢复后重放识别出的计划与用户确认过的计划不一致时，不执行新计划。

    真实链路里 ``interrupt`` 在 resume 时返回 checkpoint 中保存的选项值；这里用一个
    记录首次选项值的假 ``interrupt`` 复现同一行为：用户确认的是第一份计划，重放重算
    出第二份计划时载荷里的指纹对不上，工具必须返回结构化失败。
    """

    _patch_success(monkeypatch)
    confirmed: list[str] = []

    def confirm_once(payload):
        options = payload["questions"][0]["options"]
        value = options[0]["value"]
        if not confirmed:
            confirmed.append(value)
        return {payload["questions"][0]["question_id"]: confirmed[0]}

    monkeypatch.setattr(workflow_module, "interrupt", confirm_once)

    first = await FAULT_FN(query_text="3B-7Y 通道内树木倒伏压覆导线", runtime=_runtime())
    assert "report" in first

    async def changed_extract(*_args, **_kwargs):
        return FaultTraceQuery(device_name="3B-7Y", voltage_level="10kV", phenomena=["异物搭挂"])

    monkeypatch.setattr(workflow_module, "extract_fault_trace_intent", changed_extract)

    result = await FAULT_FN(query_text="3B-7Y 通道异常", runtime=_runtime())

    assert result == INTENT_PLAN_CHANGED_MESSAGE


@pytest.mark.asyncio
async def test_trace_fault_confirmation_token_survives_replay(monkeypatch):
    """同一份计划被确认两次（首轮与 resume 重放）时按令牌放行，不重复询问。"""

    _patch_success(monkeypatch)
    asked: list[dict] = []

    def confirming(payload):
        asked.append(payload)
        return {payload["questions"][0]["question_id"]: payload["questions"][0]["options"][0]["value"]}

    monkeypatch.setattr(workflow_module, "interrupt", confirming)

    first = await FAULT_FN(query_text="3B-7Y 通道内树木倒伏压覆导线", runtime=_runtime())
    second = await FAULT_FN(query_text="3B-7Y 通道内树木倒伏压覆导线", runtime=_runtime())

    assert "report" in first and "report" in second
    assert len(asked) == 2
    assert asked[0]["questions"][0]["options"][0]["value"].startswith("confirm#")
