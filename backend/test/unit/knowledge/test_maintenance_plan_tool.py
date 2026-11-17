"""处置方案工具编排单元测试（假图、假 Redis、假 MinIO）。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from yuxi.knowledge.maintenance_plan import graph_plan as graph_plan_module
from yuxi.knowledge.maintenance_plan import tool as tool_module
from yuxi.knowledge.fault_trace.ledger import LedgerDevice
from yuxi.knowledge.maintenance_plan.branch import BranchLookup
from yuxi.knowledge.maintenance_plan.contracts import (
    BRANCH_UNRESOLVED_MESSAGE,
    CHOICE_ABORTED_MESSAGE,
    CHOICE_CANCELLED_MESSAGE,
    NO_SCENE_MESSAGE,
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
)
from yuxi.knowledge.maintenance_plan.sources import TeamRecord, ToolRecord

pytestmark = pytest.mark.unit

GENERATE = getattr(tool_module.generate_maintenance_plan, "coroutine", None) or tool_module.generate_maintenance_plan

KG_OPTION = {
    "server_url": "bolt://fake:7688",
    "graph": "kg3",
    "maintenance_plan_graph": "kg5",
    "username": "u",
    "password": "p",
}


@pytest.fixture(autouse=True)
def empty_ledger_candidates(monkeypatch):
    async def search(*args, **kwargs):
        del args, kwargs
        return []

    monkeypatch.setattr(tool_module.ledger_repository, "search_devices", search)


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

    async def load_teams(self):
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
)


def _install(monkeypatch, *, branch=None, evidence=None, tools=None, personnel=None, scenes=None, candidates=None):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(KG_OPTION))
    monkeypatch.setattr(tool_module, "TuGraphClient", _FakeClient)

    async def fake_intent(text, *, model_call, previous=None, feedback=None, device_override=None):
        del device_override
        return PlanQuery(device_name="3k18")

    monkeypatch.setattr(tool_module, "extract_plan_intent", fake_intent)
    monkeypatch.setattr(
        graph_plan_module,
        "search_devices",
        lambda client, query, limit=24, graph_key="", ledger_candidates=(): (
            candidates if candidates is not None else [DEVICE]
        ),
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

    def confirm_first_option(payload):
        """设备卡片每次都会弹；测试里默认选第一个候选，等价于用户点了首项。"""

        question = payload["questions"][0]
        if not question["options"]:
            return None
        return {question["question_id"]: question["options"][0]["value"]}

    monkeypatch.setattr(tool_module, "interrupt", confirm_first_option)

    async def fake_lookup(device, scene):
        return branch if branch is not None else BranchLookup("ok", value="站内开关室", ledger_id="L1", branch="分支1")

    monkeypatch.setattr(tool_module, "lookup_branch", fake_lookup)
    monkeypatch.setattr(
        tool_module,
        "MaintenanceDataRepository",
        lambda: _FakeRepository(
            tools if tools is not None else [ToolRecord("吊车", True)],
            personnel if personnel is not None else [TeamRecord("电力巡护班组", (), ("张三",))],
        ),
    )


def _context():
    return SimpleNamespace(context=SimpleNamespace())


def test_returns_fixed_message_without_graph_config(monkeypatch):
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption({}))
    monkeypatch.setattr(tool_module, "extract_plan_intent", lambda *a, **k: None)
    monkeypatch.setattr(tool_module, "TuGraphClient", _FakeClient)

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result == PLAN_GRAPH_NOT_CONFIGURED_MESSAGE


def test_graph_name_falls_back_to_default_when_missing(monkeypatch):
    """系统配置没存 maintenance_plan_graph 时回退默认图名，而不是回退 graph 字段。"""

    option = {
        "server_url": "bolt://fake:7688",
        "graph": "kg3",
        "username": "u",
        "password": "p",
    }
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(option))
    monkeypatch.delenv("MAINTENANCE_PLAN_GRAPH", raising=False)

    config = asyncio.run(tool_module._resolve_kg_config())

    assert config is not None
    assert config.graph == "kg5"


def test_full_flow_returns_verified_plan(monkeypatch):
    _install(monkeypatch)

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result["answer_source"] == "maintenance_plan"
    assert result["answer_verified"] is True
    assert "执行分支：布置位置 = 站内开关室（分支1）" in result["answer_markdown"]
    assert "吊车 1 台【在库】" in result["answer_markdown"]
    assert "- 责任班组：电力巡护班组" in result["answer_markdown"]
    assert "- 所需人数：1 人" in result["answer_markdown"]
    assert "- 工作负责人：张三" in result["answer_markdown"]
    assert "## 五、数据说明" not in result["answer_markdown"]
    titles = [step["title"] for step in result["thinking_steps"]]
    assert titles == ["解析任务需求", "检索知识图谱", "组装方案模板", "生成标准化方案"]
    assert all(step["state"] == "completed" for step in result["thinking_steps"])
    # 处置方案必须连 maintenance_plan_graph 指定的图，不能误用故障溯源的 graph 图名。
    assert _FakeClient.instances[-1].config.graph == "kg5"


def test_device_not_found_opens_free_text_card_then_aborts(monkeypatch):
    """零候选仍显示自由输入卡片，持续没有输入才按交互预算终止。"""

    _install(monkeypatch, candidates=[])
    recorder = _InterruptRecorder()
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    result = asyncio.run(GENERATE("不存在设备", runtime=_context()))

    assert result == CHOICE_ABORTED_MESSAGE
    assert len(recorder.payloads) == 4
    assert recorder.payloads[0]["questions"][0]["options"] == []


def test_missing_device_fields_also_open_free_text_card(monkeypatch):
    """意图完全没抽出设备时也进入设备卡片，不直接结束为普通澄清文案。"""

    _install(monkeypatch, candidates=[])

    async def unclear(text, *, model_call, previous=None, feedback=None, device_override=None):
        del model_call, previous, feedback, device_override
        return PlanQuery(
            raw_text=text,
            clarification_required=True,
            clarification_message="未能识别设备",
        )

    recorder = _InterruptRecorder()
    monkeypatch.setattr(tool_module, "extract_plan_intent", unclear)
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    result = asyncio.run(GENERATE("帮我看看这个设备", runtime=_context()))

    assert result == CHOICE_ABORTED_MESSAGE
    assert recorder.payloads[0]["questions"][0]["options"] == []
    assert "填写正确的设备名称" in recorder.payloads[0]["questions"][0]["question"]


def test_missing_device_fields_still_use_raw_text_for_ledger_recall(monkeypatch):
    """意图字段为空时，台账可从完整原文召回，映射图谱后直接进入确认卡片。"""

    _install(monkeypatch)
    captured: dict[str, Any] = {}

    async def unclear(text, *, model_call, previous=None, feedback=None, device_override=None):
        del model_call, previous, feedback, device_override
        return PlanQuery(raw_text=text, clarification_required=True, clarification_message="未能识别设备")

    async def ledger_search(text, **kwargs):
        captured["text"] = text
        captured["kwargs"] = kwargs
        return [LedgerDevice("ledger-line", "银开线", "输电线路", "110kV", ("输电设施",), score=92.0)]

    def graph_search(client, query, *, graph_key="", ledger_candidates=(), limit=24):
        del client, query, graph_key, limit
        captured["ledger_count"] = len(ledger_candidates)
        return [DEVICE]

    recorder = _InterruptRecorder([{"maintenance_plan_device": DEVICE.device_id}])
    monkeypatch.setattr(tool_module, "extract_plan_intent", unclear)
    monkeypatch.setattr(tool_module.ledger_repository, "search_devices", ledger_search)
    monkeypatch.setattr(graph_plan_module, "search_devices", graph_search)
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    result = asyncio.run(GENERATE("110kV银开线故障，请出处置方案", runtime=_context()))

    assert "report" in result
    assert captured["text"] == "110kV银开线故障，请出处置方案"
    assert captured["kwargs"]["minimum_score"] == 60.0
    assert captured["ledger_count"] == 1


def test_scene_not_found_returns_fixed_message(monkeypatch):
    _install(monkeypatch, scenes=[])

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result == NO_SCENE_MESSAGE


def test_small_team_still_generates_plan(monkeypatch):
    """班组人数小于所需人数不再阻断方案；所需人数仍取人员需求合计。"""

    evidence = ScenarioEvidence(
        device=DEVICE,
        scenario=SCENE,
        branch=BranchSelection("布置位置", "站内开关室", "分支1", source="redis", ledger_id="L1"),
        steps=[Step("S1", 1, "停电、验电")],
        safety=[],
        resources=[],
        personnel=[PersonnelRequirement("PER1", "检修工", 5, common=True)],
    )
    _install(monkeypatch, evidence=evidence, personnel=[TeamRecord("电力巡护班组", (), ("张三",))])

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result["answer_verified"] is True
    assert "- 所需人数：5 人" in result["answer_markdown"]
    assert "- 工作负责人：张三" in result["answer_markdown"]


def test_branch_missing_returns_fixed_message_without_interrupt(monkeypatch):
    """Redis 无分支值时不再让人工选择，直接返回固定失败文案。"""

    recorder = _InterruptRecorder()
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    async def missing(device, scene):
        return BranchLookup("missing", reason="没有可用 key")

    monkeypatch.setattr(tool_module, "lookup_branch", missing)

    result = asyncio.run(tool_module._resolve_branch(DEVICE, SCENE))

    assert result == BRANCH_UNRESOLVED_MESSAGE
    assert recorder.payloads == []


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

    result = tool_module._choose_scene([first, second], PlanQuery(raw_text="开关柜整柜更换"), [0], emitter=_Emitter())

    assert result.scene_id == "SCENE_B4"
    assert recorder.payloads[0]["source"] == SCENE_CHOICE_SOURCE


def test_pick_device_always_asks_even_when_one_candidate(monkeypatch):
    """处置方案必须每次让用户确认设备：唯一候选也要弹卡片。"""

    kiosk = DeviceCandidate(
        device_id="LGR#kbs1",
        name="#1开闭所",
        device_type="开闭所",
        voltage_level="10kV",
        path="变电设施 > 110kV变电站 > 10kV电压等级区域 > #1开闭所",
        score=130.0,
    )
    recorder = _InterruptRecorder([{"maintenance_plan_device": "LGR#kbs1"}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    picked = tool_module._pick_device(
        [kiosk],
        PlanQuery(raw_text="#1开闭所检修", device_type="开闭所", location_hint="#1开闭所"),
        [0],
        emitter=_Emitter(),
    )

    assert picked is kiosk
    assert len(recorder.payloads) == 1
    option = recorder.payloads[0]["questions"][0]["options"][0]
    assert option["label"] == "#1开闭所（开闭所，10kV）"
    assert option["value"] == "LGR#kbs1"


def test_pick_device_asks_with_all_candidates_and_returns_selected(monkeypatch):
    """多候选同样弹卡片；用户改选哪个候选就返回哪个。"""

    kiosk = DeviceCandidate(
        device_id="LGR#kbs1",
        name="#1开闭所",
        device_type="开闭所",
        voltage_level="10kV",
        path="变电设施 > 110kV变电站 > 10kV电压等级区域 > #1开闭所",
        score=130.0,
    )
    cabinet = DeviceCandidate(
        device_id="LGR#cab1",
        name="#1开闭所PLC柜",
        device_type="开关柜",
        voltage_level="10kV",
        path="变电设施 > 110kV变电站 > 10kV电压等级区域 > #1开闭所 > #1开闭所PLC柜",
        score=31.0,
    )
    recorder = _InterruptRecorder([{"maintenance_plan_device": "LGR#cab1"}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    picked = tool_module._pick_device(
        [kiosk, cabinet],
        PlanQuery(raw_text="#1开闭所检修", device_type="开闭所", location_hint="#1开闭所"),
        [0],
        emitter=_Emitter(),
    )

    assert picked is cabinet
    options = recorder.payloads[0]["questions"][0]["options"]
    assert [item["value"] for item in options] == ["LGR#kbs1", "LGR#cab1"]


def test_device_payload_puts_path_in_description():
    """卡片选项标题只放设备名/类型/电压，层级路径作为次要说明。"""

    candidate = DeviceCandidate(
        device_id="LGR#kbs1",
        name="#1开闭所",
        device_type="开闭所",
        voltage_level="10kV",
        path="变电设施 > 110kV变电站 > 10kV电压等级区域 > #1开闭所",
    )

    option = tool_module._device_payload(PlanQuery(raw_text="#1开闭所检修"), [candidate])["questions"][0]["options"][0]

    assert option["label"] == "#1开闭所（开闭所，10kV）"
    assert option["description"] == "变电设施 > 110kV变电站 > 10kV电压等级区域 > #1开闭所"


def test_device_card_close_cancels_without_reintent(monkeypatch):
    """关闭设备卡片是取消：不能把 reject 当补充设备名再识别一次。"""

    _install(monkeypatch)
    calls: list[int] = []

    async def counting(text, *, model_call, previous=None, feedback=None, device_override=None):
        del device_override
        calls.append(1)
        return PlanQuery(device_name="3k18")

    monkeypatch.setattr(tool_module, "extract_plan_intent", counting)
    monkeypatch.setattr(tool_module, "interrupt", lambda payload: "reject")

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result == CHOICE_CANCELLED_MESSAGE
    assert len(calls) == 1


def test_scene_card_close_cancels(monkeypatch):
    other = Scenario("SCENE_B4", "开关柜柜内检修", level="B级", branch_dimension="柜型结构")
    _install(monkeypatch, scenes=[SCENE, other])
    answers = iter([{"maintenance_plan_device": DEVICE.device_id}, "reject"])
    monkeypatch.setattr(tool_module, "interrupt", lambda payload: next(answers))

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert result == CHOICE_CANCELLED_MESSAGE


def test_locate_device_uses_model_selection_as_recommendation(monkeypatch):
    """模型在候选内选中设备只决定“推荐”，用户仍需在卡片上确认。"""

    other = DeviceCandidate(
        "LGR#kbs1",
        "#1开闭所",
        "开闭所",
        "10kV",
        "变电设施 > 110kV变电站 > #1开闭所",
    )
    candidates = [DEVICE, other]
    monkeypatch.setattr(
        graph_plan_module,
        "search_devices",
        lambda client, query, limit=24, graph_key="", ledger_candidates=(): candidates,
    )
    recorder = _InterruptRecorder([{"maintenance_plan_device": other.device_id}])
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    async def choosing(_prompt):
        return json.dumps({"device_id": other.device_id})

    result = asyncio.run(
        tool_module._locate_device(
            object(),
            PlanQuery(raw_text="#1开闭所检修", device_type="开闭所"),
            model_call=choosing,
            rounds=[0],
            emitter=_Emitter(),
        )
    )

    assert result[1] is other
    options = recorder.payloads[0]["questions"][0]["options"]
    # 推荐候选被提到第一位，保证在卡片可见范围内；仍由用户点击确认。
    assert [item["value"] for item in options] == [other.device_id, DEVICE.device_id]
    assert options[0]["label"].endswith("（推荐）")
    assert not options[1]["label"].endswith("（推荐）")


def test_free_text_replaces_previous_device_instead_of_backfilling(monkeypatch):
    """设备卡片改填后，旧设备名不能通过上一轮合并再次成为检索条件。"""

    other = DeviceCandidate(
        "LGR#kbs1",
        "#1开闭所",
        "开闭所",
        "10kV",
        "变电设施 > 110kV变电站 > #1开闭所",
    )
    monkeypatch.setattr(
        graph_plan_module,
        "search_devices",
        lambda client, query, limit=24, graph_key="", ledger_candidates=(): (
            [DEVICE] if query.device_name == "3k18" else [other]
        ),
    )
    calls: list[str | None] = []

    async def fake_extract(text, *, model_call, previous=None, feedback=None, device_override=None):
        del model_call, previous
        calls.append(device_override)
        return PlanQuery(raw_text=text, device_name=device_override or "3k18", device_type="开闭所")

    monkeypatch.setattr(tool_module, "extract_plan_intent", fake_extract)
    recorder = _InterruptRecorder(
        [
            {"maintenance_plan_device": {"type": "other", "text": "#1开闭所"}},
            {"maintenance_plan_device": other.device_id},
        ]
    )
    monkeypatch.setattr(tool_module, "interrupt", recorder)

    result = asyncio.run(
        tool_module._locate_device(
            object(),
            PlanQuery(raw_text="3k18 检修", device_name="3k18"),
            model_call=None,
            rounds=[0],
            emitter=_Emitter(),
        )
    )

    assert calls == ["#1开闭所"]
    assert result[1] is other


def test_single_candidate_does_not_call_model(monkeypatch):
    """唯一候选不需要模型选择，避免每次请求多一次模型往返。"""

    _install(monkeypatch)
    called: list[int] = []

    async def counting_select(*args, **kwargs):
        called.append(1)
        return None, "no_selection"

    monkeypatch.setattr(tool_module, "select_device", counting_select)

    result = asyncio.run(GENERATE("3k18 开关柜整柜更换", runtime=_context()))

    assert "report" in result
    assert called == []


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

    asyncio.run(GENERATE("991SVG 开关柜柜内检修", runtime=runtime))
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

    result = tool_module._choose_scene(
        [SCENE, SCENE], PlanQuery(raw_text="3k18"), [tool_module.MAX_CHOICE_ROUNDS], emitter=_Emitter()
    )

    assert result == CHOICE_ABORTED_MESSAGE
