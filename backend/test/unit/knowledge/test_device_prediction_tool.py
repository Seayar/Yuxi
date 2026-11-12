"""设备预测维护工具编排单元测试（假图、假预测服务、假台账）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from yuxi.agents.toolkits.tugraph import TuGraphError
from yuxi.knowledge.device_prediction import graph as graph_module
from yuxi.knowledge.device_prediction import tool as tool_module
from yuxi.knowledge.device_prediction.contracts import (
    CHOICE_ABORTED_MESSAGE,
    DEVICE_CHOICE_SOURCE,
    DEVICE_ID_UNRESOLVED_MESSAGE,
    HEALTH_SERVICE_UNAVAILABLE_MESSAGE,
    NO_DEVICE_MESSAGE,
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
    instances: list["_FakeClient"] = []

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

    async def fake_intent(text, *, model_call, previous=None):
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
        lambda client, query, limit=8: candidates if candidates is not None else [DEVICE],
    )
    monkeypatch.setattr(graph_module, "resolve_device_id", fake_resolve)
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
    # 只读 kg5：连接图名取 device_prediction_graph，绝不能误用故障溯源的 graph（kg3）字段。
    assert [client.config.graph for client in _FakeClient.instances] == ["kg5", "kg5"]


def test_graph_name_falls_back_to_kg5_when_option_key_missing(monkeypatch):
    """系统配置没存 device_prediction_graph 时回退 kg5，而不是回退 graph 字段的 kg3。"""

    option = dict(KG_OPTION)
    option.pop("device_prediction_graph")
    _install(monkeypatch)
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(option))
    monkeypatch.setattr(graph_module, "search_devices", lambda client, query, limit=8: [])

    assert _run(monkeypatch) == NO_DEVICE_MESSAGE
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


def test_unknown_device_returns_fixed_message(monkeypatch):
    _install(monkeypatch, candidates=[])

    assert _run(monkeypatch) == NO_DEVICE_MESSAGE


def test_graph_query_failure_returns_fixed_message_and_closes_client(monkeypatch):
    """kg5 不可用（图不存在/连接失败）时返回固定文案，连接必须关闭。"""

    _install(monkeypatch)

    def failing(client, query, limit=8):
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


def test_free_text_choice_retries_with_previous_query(monkeypatch):
    """自由输入时把补充文字交给意图解析，并带上上一轮结果重新检索。"""

    other = DeviceCandidate("LGR#tr2", "2号主变", device_type="变压器", voltage_level="110kV")
    recorder = _InterruptRecorder([{"device_prediction_device": {"text": "2号主变"}}])
    _install(monkeypatch, candidates=[DEVICE, other])
    monkeypatch.setattr(tool_module, "interrupt", recorder)
    seen: list[tuple] = []

    async def fake_intent(text, *, model_call, previous=None):
        seen.append((text, previous))
        return PredictionQuery(raw_text=text, device_name="1号主变", device_type="变压器")

    monkeypatch.setattr(tool_module, "extract_prediction_intent", fake_intent)

    _run(monkeypatch)

    assert seen[-1][0] == "2号主变"
    assert seen[-1][1] is not None
