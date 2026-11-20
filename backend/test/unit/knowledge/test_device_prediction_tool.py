"""设备预测维护工具编排单元测试（假图、假预测服务、假台账）。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from yuxi.agents.toolkits.tugraph import TuGraphError
from yuxi.knowledge.device_prediction import graph as graph_module
from yuxi.knowledge.device_prediction import tool as tool_module
from yuxi.knowledge.fault_trace.ledger import LedgerDevice
from yuxi.knowledge.device_prediction.contracts import (
    CHOICE_ABORTED_MESSAGE,
    CHOICE_CANCELLED_MESSAGE,
    DEVICE_CHOICE_SOURCE,
    DEVICE_ID_UNRESOLVED_MESSAGE,
    HEALTH_SERVICE_UNAVAILABLE_MESSAGE,
    NO_PERMISSION_MESSAGE,
    PREDICTION_GRAPH_NOT_CONFIGURED_MESSAGE,
    PREDICTION_SERVICE_UNAVAILABLE_MESSAGE,
    REPORT_VERIFICATION_FAILED_MESSAGE,
    DeviceCandidate,
    FaultPrediction,
    HealthAssessment,
    HealthIndicator,
    MaintenanceGuidance,
    PredictionQuery,
)
from yuxi.knowledge.device_prediction.services import PredictionServiceError
from yuxi.knowledge.device_selection import SELECTED_DEVICE_UNAVAILABLE_MESSAGE, SelectedDeviceId

pytestmark = pytest.mark.unit

PREDICT = getattr(tool_module.predict_device, "coroutine", None) or tool_module.predict_device
KG_OPTION = {
    "server_url": "bolt://fake:7688",
    "graph": "kg3",
    "maintenance_plan_graph": "kg4",
    "device_prediction_graph": "kg5",
    "username": "u",
    "password": "p",
}


@pytest.fixture(autouse=True)
def empty_ledger_candidates(monkeypatch):
    async def search(*args, **kwargs):
        del args, kwargs
        return []

    monkeypatch.setattr(tool_module.ledger_repository, "search_devices", search)


DEVICE = DeviceCandidate(
    "LGR#tr1",
    "1号主变",
    device_type="变压器",
    voltage_level="110kV",
    path="变电设施 > 35kV#1变电站 > 1号主变",
    ledger_ids=("bb1baf15",),
)
HEALTH = HealthAssessment(
    health_score="72",
    health_level="注意",
    indicators=(
        HealthIndicator(
            indicator_name="绕组温度",
            indicator_value="85",
            indicator_unit="℃",
            weight_ratio="30%",
            indicator_score="-8 分",
            criteria="正常区间 ≤ 75℃",
        ),
    ),
    fault_rate="0.8 次/年",
    average_downtime_hours="6.5 小时/次",
)
PREDICTION = FaultPrediction(
    predicted_fault_type="触头过热",
    probability="67%",
    expected_failure_time="未来 30–60 天",
    degradation_trend="负荷率连续 3 个月上升",
    maintenance_time="建议在 30 天内完成针对性检查维护",
    image_url="![预测图](/api/data/image?objectName=prediction-charts%2Ftransformer.png)",
)
GUIDANCE = MaintenanceGuidance(
    fault_type="触头过热",
    resources=["红外热像仪"],
    cautions=["停电范围提前报备"],
)


class _FakeOption:
    def __init__(self, value: dict) -> None:
        self._value = value

    async def get(self) -> dict:
        return dict(self._value)


class _FakeClient:
    instances: list[_FakeClient] = []

    def __init__(self, config) -> None:
        self.config = config
        self.closed = False
        _FakeClient.instances.append(self)

    def close(self) -> None:
        self.closed = True


class _InterruptRecorder:
    def __init__(self, values: list | None = None) -> None:
        self.values = list(values or [])
        self.payloads: list[dict] = []

    def __call__(self, payload: dict):
        self.payloads.append(payload)
        return self.values.pop(0) if self.values else None


class _Emitter:
    """思考过程发射器替身：只记录，不依赖 LangGraph 流。"""

    def __init__(self, *args, **kwargs) -> None:
        self.events: list[tuple[str, str]] = []

    def emit(self, step_id: str, state: str = "completed", **kwargs) -> None:
        self.events.append((step_id, state))

    def snapshot(self) -> list[dict]:
        return []


def _install(
    monkeypatch,
    *,
    candidates=None,
    device_id="auto",
    health=None,
    prediction=None,
    guidance=None,
):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(KG_OPTION))
    monkeypatch.setattr(tool_module, "TuGraphClient", _FakeClient)
    _FakeClient.instances = []

    async def fake_intent(text, *, model_call, previous=None, device_override=None):
        del device_override
        return PredictionQuery(raw_text=text, device_name="1号主变", device_type="变压器")

    async def fake_resolve(device):
        if device_id != "auto":
            return device_id
        return (device.ledger_ids[0], "台账ID列表") if device.ledger_ids else None

    async def fake_health(device_id_value):
        return health if health is not None else HEALTH

    async def fake_prediction(device_id_value):
        return prediction if prediction is not None else PREDICTION

    monkeypatch.setattr(tool_module, "extract_prediction_intent", fake_intent)
    monkeypatch.setattr(tool_module, "fetch_health_assessment", fake_health)
    monkeypatch.setattr(tool_module, "fetch_fault_prediction", fake_prediction)
    monkeypatch.setattr(
        graph_module,
        "search_devices",
        lambda client, query, limit=24, graph_key="", ledger_candidates=(): (
            candidates if candidates is not None else [DEVICE]
        ),
    )
    monkeypatch.setattr(graph_module, "resolve_device_id", fake_resolve)

    def confirm_first_option(payload):
        question = payload["questions"][0]
        if not question["options"]:
            return None
        return {question["question_id"]: question["options"][0]["value"]}

    monkeypatch.setattr(tool_module, "interrupt", confirm_first_option)
    calls: list[dict] = []

    def fake_guidance(client, *, fault_type, device_type="", limit=20):
        calls.append({"fault_type": fault_type, "device_type": device_type})
        return guidance if guidance is not None else GUIDANCE

    monkeypatch.setattr(graph_module, "lookup_maintenance_guidance", fake_guidance)
    monkeypatch.setattr(graph_module, "guidance_calls", calls, raising=False)


def _runtime():
    return SimpleNamespace(context=SimpleNamespace())


def _run(monkeypatch, *, query="评估1号主变的健康状态", runtime=None):
    return asyncio.run(PREDICT(query, runtime=runtime or _runtime()))


# ---------------------------------------------------------------------------
# 主路径
# ---------------------------------------------------------------------------
def test_full_flow_returns_verified_report(monkeypatch):
    """设备 ID 来自台账、两个服务都被调用、报告通过校验并带四步思考过程。"""

    _install(monkeypatch)

    result = _run(monkeypatch)

    assert result["answer_source"] == "device_prediction"
    assert result["answer_verified"] is True
    assert result["report"]["device_id"] == "bb1baf15"
    assert result["report"]["health_score"] == "72"
    assert result["report"]["fault_type"] == "触头过热"
    assert result["report"]["image_url"] == PREDICTION.image_url
    prediction_section = result["answer_markdown"].split("## 二、故障预测")[1].split("## 三、可靠性与风险评估")[0]
    prediction_lines = prediction_section.splitlines()
    trend_line = prediction_lines.index("- 退化趋势：负荷率连续 3 个月上升")
    assert prediction_lines[trend_line + 1] == ""
    assert prediction_lines[trend_line + 2] == PREDICTION.image_url
    assert "## 四、维护计划建议" in result["answer_markdown"]
    assert [step["title"] for step in result["thinking_steps"]] == [
        "加载设备档案",
        "健康评估计算",
        "趋势预测",
        "生成维护建议",
    ]
    assert all(step["state"] == "completed" for step in result["thinking_steps"])
    # 维护知识按故障类型 + 设备类型匹配
    assert getattr(graph_module, "guidance_calls")[0] == {"fault_type": "触头过热", "device_type": "变压器"}
    # 两次图连接（设备检索 + 维护知识匹配）都在用完后关闭
    assert len(_FakeClient.instances) == 2
    assert all(client.closed for client in _FakeClient.instances)
    # 只读配置的设备预测维护图：连接图名取 device_prediction_graph，绝不能误用其他图名字段。
    assert [client.config.graph for client in _FakeClient.instances] == ["kg5", "kg5"]


def test_graph_name_falls_back_to_default_when_option_key_missing(monkeypatch):
    """系统配置没存 device_prediction_graph 时回退默认图名，而不是回退 graph 字段。"""

    option = dict(KG_OPTION)
    option.pop("device_prediction_graph")
    _install(monkeypatch)
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(option))
    monkeypatch.setattr(
        graph_module,
        "search_devices",
        lambda client, query, limit=24, graph_key="", ledger_candidates=(): [],
    )

    assert _run(monkeypatch) == CHOICE_ABORTED_MESSAGE
    assert _FakeClient.instances[0].config.graph == "kg5"


def test_unexpected_service_error_propagates_not_masked(monkeypatch):
    """非服务类异常必须原样抛出：不能被 gather(return_exceptions=True) 静默吞成“暂不可用”文案。"""

    _install(monkeypatch)

    async def crashing(device_id_value):
        raise ValueError("boom")

    monkeypatch.setattr(tool_module, "fetch_health_assessment", crashing)

    with pytest.raises(ValueError):
        _run(monkeypatch)


def test_result_is_cached_per_question(monkeypatch):
    """同一轮内重复问同一问题直接返回缓存；换个问题必须重新检索与调用服务。"""

    calls: list[str] = []

    async def fake_health(device_id_value):
        calls.append(device_id_value)
        return HEALTH

    _install(monkeypatch)
    monkeypatch.setattr(tool_module, "fetch_health_assessment", fake_health)
    context = SimpleNamespace()
    runtime = SimpleNamespace(context=context)

    first = asyncio.run(PREDICT("评估1号主变的健康状态", runtime=runtime))
    second = asyncio.run(PREDICT("评估1号主变的健康状态", runtime=runtime))
    third = asyncio.run(PREDICT("评估2号主变的健康状态", runtime=runtime))

    assert first is second
    assert third is not first
    assert calls == ["bb1baf15", "bb1baf15"]


# ---------------------------------------------------------------------------
# 固定文案边界
# ---------------------------------------------------------------------------
def test_missing_kg_config_returns_fixed_message(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption({}))

    assert _run(monkeypatch) == PREDICTION_GRAPH_NOT_CONFIGURED_MESSAGE


def test_empty_query_clarifies(monkeypatch):
    _install(monkeypatch)

    assert asyncio.run(PREDICT("   ", runtime=_runtime())) == "未能识别完整的设备信息，请明确目标设备"


def test_missing_permission_context_returns_fixed_message(monkeypatch):
    _install(monkeypatch)

    assert asyncio.run(PREDICT("评估1号主变的健康状态", runtime=SimpleNamespace(context=None))) == NO_PERMISSION_MESSAGE


def test_unknown_device_opens_free_text_card_and_then_aborts(monkeypatch):
    """零候选不能直接终止，应让用户填写新设备名；持续为空才按轮次预算终止。"""

    recorder = _InterruptRecorder()
    _install(monkeypatch, candidates=[])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    assert _run(monkeypatch) == CHOICE_ABORTED_MESSAGE
    assert len(recorder.payloads) == 4
    assert recorder.payloads[0]["questions"][0]["options"] == []


def test_missing_device_fields_also_open_free_text_card(monkeypatch):
    """设备字段完全缺失时同样进入设备卡片，而不是直接返回普通澄清。"""

    recorder = _InterruptRecorder()
    _install(monkeypatch, candidates=[])

    async def unclear(text, *, model_call, previous=None, device_override=None):
        del model_call, previous, device_override
        return PredictionQuery(
            raw_text=text,
            clarification_required=True,
            clarification_message="未能识别设备",
        )

    monkeypatch.setattr(tool_module, "extract_prediction_intent", unclear)
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    assert _run(monkeypatch, query="帮我看看这个设备") == CHOICE_ABORTED_MESSAGE
    assert recorder.payloads[0]["questions"][0]["options"] == []
    assert "填写正确的设备名称" in recorder.payloads[0]["questions"][0]["question"]


def test_missing_device_fields_still_use_raw_text_for_ledger_recall(monkeypatch):
    """设备字段缺失时，台账按完整原文召回并映射图谱后进入卡片。"""

    _install(monkeypatch)
    captured: dict[str, Any] = {}

    async def unclear(text, *, model_call, previous=None, device_override=None):
        del model_call, previous, device_override
        return PredictionQuery(raw_text=text, clarification_required=True, clarification_message="未能识别设备")

    async def ledger_search(text, **kwargs):
        captured["text"] = text
        captured["kwargs"] = kwargs
        return [LedgerDevice("ledger-line", "银开线", "输电线路", "110kV", ("输电设施",), score=92.0)]

    def graph_search(client, query, *, graph_key="", ledger_candidates=(), limit=24):
        del client, query, graph_key, limit
        captured["ledger_count"] = len(ledger_candidates)
        return [DEVICE]

    recorder = _InterruptRecorder([{"device_prediction_device": DEVICE.device_id}])
    monkeypatch.setattr(tool_module, "extract_prediction_intent", unclear)
    monkeypatch.setattr(tool_module.ledger_repository, "search_devices", ledger_search)
    monkeypatch.setattr(graph_module, "search_devices", graph_search)
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    result = _run(monkeypatch, query="评估110kV银开线未来故障风险")

    assert result["answer_verified"] is True
    assert captured["text"] == "评估110kV银开线未来故障风险"
    assert captured["kwargs"]["minimum_score"] == 60.0
    assert captured["ledger_count"] == 1


def test_graph_query_failure_returns_fixed_message_and_closes_client(monkeypatch):
    """设备预测维护图不可用（图不存在/连接失败）时返回固定文案，连接必须关闭。"""

    _install(monkeypatch)

    def failing(client, query, limit=24, graph_key="", ledger_candidates=()):
        raise TuGraphError("database does not exist")

    monkeypatch.setattr(graph_module, "search_devices", failing)

    assert _run(monkeypatch) == PREDICTION_GRAPH_NOT_CONFIGURED_MESSAGE
    assert len(_FakeClient.instances) == 1
    assert _FakeClient.instances[0].closed is True


def test_unresolved_ledger_id_returns_fixed_message(monkeypatch):
    """图谱与台账都拿不到业务设备 ID 时不调用预测服务。"""

    _install(monkeypatch, device_id=None)

    assert _run(monkeypatch) == DEVICE_ID_UNRESOLVED_MESSAGE


def test_health_service_failure_skips_prediction_step(monkeypatch):
    """健康评估失败时返回固定文案，故障预测步骤标记 skipped。"""

    _install(monkeypatch)

    async def failing(device_id_value):
        raise PredictionServiceError("unavailable", HEALTH_SERVICE_UNAVAILABLE_MESSAGE)

    monkeypatch.setattr(tool_module, "fetch_health_assessment", failing)
    emitters: list[_Emitter] = []

    class _RecordingEmitter(_Emitter):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__()
            emitters.append(self)

    monkeypatch.setattr(tool_module, "ThinkingEmitter", _RecordingEmitter)

    assert _run(monkeypatch) == HEALTH_SERVICE_UNAVAILABLE_MESSAGE
    assert emitters[0].events == [
        ("load_device", "started"),
        ("load_device", "completed"),
        ("health_assessment", "started"),
        ("fault_prediction", "started"),
        ("health_assessment", "failed"),
        ("fault_prediction", "skipped"),
    ]


def test_prediction_service_failure_returns_fixed_message(monkeypatch):
    _install(monkeypatch)

    async def failing(device_id_value):
        raise PredictionServiceError("unavailable", PREDICTION_SERVICE_UNAVAILABLE_MESSAGE)

    monkeypatch.setattr(tool_module, "fetch_fault_prediction", failing)

    assert _run(monkeypatch) == PREDICTION_SERVICE_UNAVAILABLE_MESSAGE


def test_unverified_report_is_not_returned(monkeypatch):
    """装配结果未通过证据校验时只返回固定文案。"""

    _install(monkeypatch)
    monkeypatch.setattr(
        tool_module,
        "produce_prediction_answer",
        lambda *args, **kwargs: {"answer_source": "device_prediction", "answer_verified": False},
    )

    assert _run(monkeypatch) == REPORT_VERIFICATION_FAILED_MESSAGE


# ---------------------------------------------------------------------------
# 设备选择中断
# ---------------------------------------------------------------------------
def test_single_candidate_still_requires_user_confirmation(monkeypatch):
    """唯一候选仍弹设备确认卡片，由用户确认后才执行。"""

    recorder = _InterruptRecorder([{"device_prediction_device": DEVICE.device_id}])
    _install(monkeypatch, candidates=[DEVICE])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    result = _run(monkeypatch)

    assert result["answer_verified"] is True
    assert len(recorder.payloads) == 1
    assert recorder.payloads[0]["questions"][0]["options"][0]["value"] == DEVICE.device_id


def test_ambiguous_device_choice_uses_selected_candidate(monkeypatch):
    """候选同分时发选择卡片，用户选中哪个就用哪个。"""

    other = DeviceCandidate("LGR#tr2", "2号主变", device_type="变压器", voltage_level="110kV", ledger_ids=("bb2baf15",))
    recorder = _InterruptRecorder([{"device_prediction_device": {"value": "LGR#tr2"}}])
    _install(monkeypatch, candidates=[DEVICE, other])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    result = _run(monkeypatch)

    assert result["answer_verified"] is True
    assert recorder.payloads[0]["source"] == DEVICE_CHOICE_SOURCE
    assert [option["value"] for option in recorder.payloads[0]["questions"][0]["options"]] == ["LGR#tr1", "LGR#tr2"]
    assert result["report"]["device_id"] == "bb2baf15"


def test_choice_budget_exhausted_returns_fixed_message(monkeypatch):
    """用户始终不选择时按轮次预算返回固定文案。"""

    other = DeviceCandidate("LGR#tr2", "2号主变", device_type="变压器", voltage_level="110kV")
    recorder = _InterruptRecorder()
    _install(monkeypatch, candidates=[DEVICE, other])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    assert _run(monkeypatch) == CHOICE_ABORTED_MESSAGE
    assert len(recorder.payloads) == 4


def test_free_text_choice_replaces_previous_device(monkeypatch):
    """自由输入时把新设备交给意图解析，旧设备不能作为兜底再次命中。"""

    other = DeviceCandidate("LGR#tr2", "2号主变", device_type="变压器", voltage_level="110kV")
    recorder = _InterruptRecorder([{"device_prediction_device": {"text": "2号主变"}}])
    _install(monkeypatch, candidates=[DEVICE, other])
    monkeypatch.setattr(tool_module, "interrupt", recorder)
    seen: list[tuple] = []

    async def fake_intent(text, *, model_call, previous=None, device_override=None):
        seen.append((text, previous, device_override))
        return PredictionQuery(raw_text=text, device_name=device_override or "1号主变", device_type="变压器")

    monkeypatch.setattr(tool_module, "extract_prediction_intent", fake_intent)

    _run(monkeypatch)

    assert seen[-1][0] == "2号主变"
    assert seen[-1][1] is not None
    assert seen[-1][2] == "2号主变"


def test_device_card_close_cancels(monkeypatch):
    """关闭设备卡片是取消：不能把 reject 当补充设备名再识别一次。"""

    other = DeviceCandidate("LGR#tr2", "2号主变", device_type="变压器", voltage_level="110kV")
    _install(monkeypatch, candidates=[DEVICE, other])
    monkeypatch.setattr(tool_module, "interrupt", lambda payload: "reject")
    seen: list[tuple] = []

    async def fake_intent(text, *, model_call, previous=None, device_override=None):
        del device_override
        seen.append((text, previous))
        return PredictionQuery(raw_text=text, device_name="1号主变")

    monkeypatch.setattr(tool_module, "extract_prediction_intent", fake_intent)

    assert _run(monkeypatch) == CHOICE_CANCELLED_MESSAGE
    # 只有最初的意图识别；关闭卡片不再触发一次“补充设备名”的重新识别。
    assert len(seen) == 1


def test_model_selection_marks_recommended_candidate(monkeypatch):
    """模型在候选内选中设备只决定推荐位，用户仍需在卡片上确认。"""

    other = DeviceCandidate("LGR#tr2", "2号主变", device_type="变压器", voltage_level="110kV")
    monkeypatch.setattr(
        graph_module,
        "search_devices",
        lambda client, query, limit=24, graph_key="", ledger_candidates=(): [DEVICE, other],
    )
    recorder = _InterruptRecorder([{"device_prediction_device": other.device_id}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    async def choosing(_prompt):
        return json.dumps({"device_id": other.device_id})

    located = asyncio.run(
        tool_module._locate_device(
            object(),
            PredictionQuery(raw_text="2号主变健康状态"),
            model_call=choosing,
            emitter=_Emitter(),
            graph_key="",
        )
    )

    assert located[1] is other
    options = recorder.payloads[0]["questions"][0]["options"]
    assert options[0]["value"] == other.device_id
    assert options[0]["label"].endswith("（推荐）")


def test_pick_device_marks_stale_candidate_id_as_selection_not_free_text(monkeypatch):
    recorder = _InterruptRecorder([{"device_prediction_device": "LGR#stale-selected"}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    picked = tool_module._pick_device(
        [DEVICE],
        PredictionQuery(raw_text="评估1号主变健康状态", device_name="1号主变"),
        [0],
    )

    assert picked == SelectedDeviceId("LGR#stale-selected")
    assert len(recorder.payloads) == 1


def test_locate_device_reloads_selected_id_when_candidate_set_changes(monkeypatch):
    selected = DeviceCandidate(
        "LGR#selected",
        "2号主变",
        device_type="变压器",
        voltage_level="110kV",
        path="变电设施 > 35kV#1变电站 > 2号主变",
        ledger_ids=("selected-ledger",),
    )
    recorder = _InterruptRecorder([{"device_prediction_device": selected.device_id}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)
    monkeypatch.setattr(graph_module, "search_devices", lambda *args, **kwargs: [DEVICE])
    monkeypatch.setattr(graph_module, "find_device_by_id", lambda *args, **kwargs: selected)
    emitter = _Emitter()

    query, located = asyncio.run(
        tool_module._locate_device(
            object(),
            PredictionQuery(raw_text="评估2号主变健康状态", device_name="2号主变"),
            model_call=None,
            emitter=emitter,
            graph_key="",
        )
    )

    assert located is selected
    assert query.raw_text == "评估2号主变健康状态"
    assert len(recorder.payloads) == 1
    assert ("load_device", "waiting") not in emitter.events


def test_locate_device_stale_id_missing_from_graph_fails_without_reprompt(monkeypatch):
    recorder = _InterruptRecorder([{"device_prediction_device": "LGR#missing"}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)
    monkeypatch.setattr(graph_module, "search_devices", lambda *args, **kwargs: [DEVICE])
    monkeypatch.setattr(graph_module, "find_device_by_id", lambda *args, **kwargs: None)

    result = asyncio.run(
        tool_module._locate_device(
            object(),
            PredictionQuery(raw_text="评估1号主变健康状态", device_name="1号主变"),
            model_call=None,
            emitter=_Emitter(),
            graph_key="",
        )
    )

    assert result == SELECTED_DEVICE_UNAVAILABLE_MESSAGE
    assert len(recorder.payloads) == 1
