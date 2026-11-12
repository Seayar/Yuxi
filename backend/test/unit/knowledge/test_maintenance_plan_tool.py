"""kg4 处置方案工具编排单元测试（假图、假 Redis、假 MinIO）。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from yuxi.knowledge.maintenance_plan import graph_plan as graph_plan_module
from yuxi.knowledge.maintenance_plan import tool as tool_module
from yuxi.knowledge.maintenance_plan.branch import BranchLookup
from yuxi.knowledge.maintenance_plan.contracts import (
    BRANCH_CHOICE_SOURCE,
    CHOICE_ABORTED_MESSAGE,
    NO_DEVICE_MESSAGE,
    NO_SCENE_MESSAGE,
    PERSONNEL_SHORTAGE_MESSAGE,
    PLAN_GRAPH_NOT_CONFIGURED_MESSAGE,
    PLAN_VERIFICATION_FAILED_MESSAGE,
    SCENE_CHOICE_SOURCE,
    BranchSelection,
    DeviceCandidate,
    PersonnelRequirement,
    PlanQuery,
    ResourceRequirement,
    SafetyMeasure,
    Scenario,
    ScenarioEvidence,
    Step,
    TeamInfo,
)
from yuxi.knowledge.maintenance_plan.sources import PersonRecord, ToolRecord

pytestmark = pytest.mark.unit

GENERATE = getattr(tool_module.generate_maintenance_plan, "coroutine", None) or tool_module.generate_maintenance_plan

KG_OPTION = {
    "server_url": "bolt://fake:7688",
    "graph": "kg3",
    "maintenance_plan_graph": "kg4",
    "username": "u",
    "password": "p",
}


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
        if not self.values:
            return None
        return self.values.pop(0)


class _Emitter:
    """思考过程发射器替身：只记录，不依赖 LangGraph 流。"""

    def __init__(self) -> None:
        self.events: list[tuple[tuple, dict]] = []

    def emit(self, *args, **kwargs) -> None:
        self.events.append((args, kwargs))


class _FakeRepository:
    def __init__(self, tools=None, personnel=None) -> None:
        self._tools = tools
        self._personnel = personnel

    async def resolve_ledger_ids(self, device):
        return device.ledger_ids

    async def load_tools(self):
        return self._tools

    async def load_personnel(self):
        return self._personnel


DEVICE = DeviceCandidate("LGR#3k18", "3k18", "开关柜", "10kV", "#3开闭所 > 3k18", ledger_ids=("L1",))
SCENE = Scenario(
    "SCENE_A2",
    "开关柜整柜更换",
    level="A级",
    branch_dimension="布置位置",
    branch1_condition="站内开关室",
    branch2_condition="箱变内",
)
EVIDENCE = ScenarioEvidence(
    device=DEVICE,
    scenario=SCENE,
    branch=BranchSelection("布置位置", "站内开关室", "分支1", source="redis", ledger_id="L1"),
    steps=[Step("S1", 1, "停电、验电")],
    safety=[SafetyMeasure("SAFE1", "个人防护", "安全帽", common=True)],
    resources=[ResourceRequirement("RES1", "吊车", 1, unit="台", common=True)],
    personnel=[PersonnelRequirement("PER1", "工作负责人", 1, common=True)],
    team=TeamInfo("1号班组", "#3开闭所"),
)


def _install(monkeypatch, *, branch=None, evidence=None, tools=None, personnel=None, scenes=None, candidates=None):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(KG_OPTION))
    monkeypatch.setattr(tool_module, "TuGraphClient", _FakeClient)

    async def fake_intent(text, *, model_call, previous=None, feedback=None):
        return PlanQuery(device_name="3k18")

    monkeypatch.setattr(tool_module, "extract_plan_intent", fake_intent)
    monkeypatch.setattr(
        graph_plan_module, "search_devices", lambda client, query, limit=8: candidates if candidates is not None else [DEVICE]
    )
    monkeypatch.setattr(
        graph_plan_module, "find_scenes", lambda client, device: scenes if scenes is not None else [SCENE]
    )
    monkeypatch.setattr(graph_plan_module, "rank_scenes", lambda items, query: list(items))
    monkeypatch.setattr(
        graph_plan_module,
        "collect_scene_evidence",
        lambda client, query, device, scene, branch: evidence if evidence is not None else EVIDENCE,
    )

    async def fake_lookup(device, scene):
        return branch if branch is not None else BranchLookup("ok", value="站内开关室", ledger_id="L1", branch="分支1")

    monkeypatch.setattr(tool_module, "lookup_branch", fake_lookup)
    monkeypatch.setattr(
        tool_module,
        "MaintenanceDataRepository",
        lambda: _FakeRepository(tools if tools is not None else [ToolRecord("吊车", True)], personnel if personnel is not None else [PersonRecord("张三", "1号班组", "工作负责人")]),
    )


def _context():
    return SimpleNamespace(context=SimpleNamespace())


def test_returns_fixed_message_without_graph_config(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption({}))
    monkeypatch.setattr(tool_module, "extract_plan_intent", lambda *a, **k: None)
    monkeypatch.setattr(tool_module, "TuGraphClient", _FakeClient)

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result == PLAN_GRAPH_NOT_CONFIGURED_MESSAGE


def test_full_flow_returns_verified_plan(monkeypatch):
    _install(monkeypatch)

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result["answer_source"] == "maintenance_plan"
    assert result["answer_verified"] is True
    assert "执行分支：布置位置 = 站内开关室（分支1）" in result["answer_markdown"]
    assert "吊车 1 台【在库】" in result["answer_markdown"]
    titles = [step["title"] for step in result["thinking_steps"]]
    assert titles == ["解析任务需求", "检索知识图谱", "组装方案模板", "生成标准化方案"]
    assert all(step["state"] == "completed" for step in result["thinking_steps"])
    # 处置方案必须连 kg4（maintenance_plan_graph），不能误用故障溯源的 kg3 图名。
    assert _FakeClient.instances[-1].config.graph == "kg4"


def test_device_not_found_returns_fixed_message(monkeypatch):
    _install(monkeypatch, candidates=[])

    result = asyncio.run(GENERATE("不存在设备", runtime=_context()))

    assert result == NO_DEVICE_MESSAGE


def test_scene_not_found_returns_fixed_message(monkeypatch):
    _install(monkeypatch, scenes=[])

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result == NO_SCENE_MESSAGE


def test_personnel_shortage_returns_fixed_message(monkeypatch):
    evidence = ScenarioEvidence(
        device=DEVICE,
        scenario=SCENE,
        branch=BranchSelection("布置位置", "站内开关室", "分支1", source="redis", ledger_id="L1"),
        steps=[Step("S1", 1, "停电、验电")],
        safety=[],
        resources=[],
        personnel=[PersonnelRequirement("PER1", "检修工", 5, common=True)],
        team=TeamInfo("1号班组", "#3开闭所"),
    )
    _install(monkeypatch, evidence=evidence, personnel=[PersonRecord("张三", "1号班组", "检修工")])

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result == PERSONNEL_SHORTAGE_MESSAGE


def test_branch_falls_back_to_user_choice(monkeypatch):
    recorder = _InterruptRecorder([{"maintenance_plan_branch": {"value": "分支1"}}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    async def missing(device, scene):
        return BranchLookup("missing", reason="没有可用 key")

    monkeypatch.setattr(tool_module, "lookup_branch", missing)
    emitter = _Emitter()

    result = asyncio.run(tool_module._resolve_branch(DEVICE, SCENE, [0], emitter=emitter))

    assert isinstance(result, BranchSelection)
    assert result.branch == "分支1"
    assert result.source == "user"
    assert recorder.payloads[0]["source"] == BRANCH_CHOICE_SOURCE
    assert len(recorder.payloads[0]["questions"][0]["options"]) == 2
    # 等待用户选择期间思考步骤必须是 waiting，而不是伪造 completed。
    assert any(args[:2] == ("retrieve_graph", "waiting") for args, kwargs in emitter.events)


def test_verification_failure_returns_fixed_message_and_failed_thinking(monkeypatch):
    """produce 载荷未通过校验时：不返回未校验报告，思考步骤标记 generate_plan failed。"""

    _install(monkeypatch)
    emitter = _Emitter()
    monkeypatch.setattr(tool_module, "ThinkingEmitter", lambda **kwargs: emitter)

    monkeypatch.setattr(
        tool_module, "produce_maintenance_plan_answer", lambda *args, **kwargs: {"answer_verified": False}
    )

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result == PLAN_VERIFICATION_FAILED_MESSAGE
    assert any(args[:2] == ("generate_plan", "failed") for args, kwargs in emitter.events)


def test_scene_without_any_content_returns_no_scene_message(monkeypatch):
    """场景四类子节点全空时不得发布空方案，按“暂无场景”固定文案收口。"""

    empty = ScenarioEvidence(
        device=DEVICE,
        scenario=SCENE,
        branch=BranchSelection("布置位置", "站内开关室", "分支1", source="redis", ledger_id="L1"),
    )
    _install(monkeypatch, evidence=empty)

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result == NO_SCENE_MESSAGE


def test_tie_of_top_scores_asks_user(monkeypatch):
    """并列最高分（同为满分匹配）必须发中断让用户选择，而不是按排序取第一个。"""

    first = Scenario("SCENE_A2", "开关柜整柜更换（柜体锈蚀、五防失效）", level="A级", branch_dimension="布置位置")
    second = Scenario("SCENE_B4", "开关柜柜内检修（活门挡板处理）", level="B级", branch_dimension="柜型结构")
    recorder = _InterruptRecorder([{"maintenance_plan_scene": {"value": "SCENE_B4"}}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)
    monkeypatch.setattr(
        graph_plan_module,
        "rank_scenes",
        lambda items, query: [replace(items[0], score=100.0), replace(items[1], score=100.0)],
    )

    result = tool_module._choose_scene(
        [first, second], PlanQuery(raw_text="开关柜整柜更换"), [0], emitter=_Emitter()
    )

    assert result.scene_id == "SCENE_B4"
    assert recorder.payloads[0]["source"] == SCENE_CHOICE_SOURCE


def test_cache_is_scoped_per_query_text(monkeypatch):
    """同一轮内同描述命中缓存不重复装配；不同描述各自独立，不能共用上一设备的结果。"""

    _install(monkeypatch)
    calls: list[int] = []
    real_produce = tool_module.produce_maintenance_plan_answer

    def counting(*args, **kwargs):
        calls.append(1)
        return real_produce(*args, **kwargs)

    monkeypatch.setattr(tool_module, "produce_maintenance_plan_answer", counting)
    runtime = _context()

    first = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=runtime))
    second = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=runtime))
    assert second is first
    assert calls == [1]

    third = asyncio.run(GENERATE("991SVG 开关柜柜内检修", runtime=runtime))
    assert len(calls) == 2
    cache = getattr(runtime.context, tool_module.RESULT_ATTR)
    assert set(cache) == {"3k18 开关柜整柜更换", "991SVG 开关柜柜内检修"}


def test_scene_choice_payload_and_selection(monkeypatch):
    other = Scenario("SCENE_B4", "开关柜柜内检修", level="B级", branch_dimension="柜型结构")
    recorder = _InterruptRecorder([{"maintenance_plan_scene": {"value": "SCENE_B4"}}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)
    monkeypatch.setattr(graph_plan_module, "rank_scenes", lambda items, query: list(items))

    result = tool_module._choose_scene([SCENE, other], PlanQuery(raw_text="3k18 开关柜"), [0], emitter=_Emitter())

    assert result.scene_id == "SCENE_B4"
    assert recorder.payloads[0]["source"] == SCENE_CHOICE_SOURCE


def test_choice_budget_aborts(monkeypatch):
    recorder = _InterruptRecorder([])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    result = tool_module._choose_scene([SCENE, SCENE], PlanQuery(raw_text="3k18"), [tool_module.MAX_CHOICE_ROUNDS], emitter=_Emitter())

    assert result == CHOICE_ABORTED_MESSAGE
