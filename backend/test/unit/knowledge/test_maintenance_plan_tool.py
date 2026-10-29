"""T9 工具编排单测：两类 interrupt、恢复值解析、缓存、错误投影与正常路径。

全部使用本地假对象（假 kg 客户端、假配置项、夹具证据），不连接任何数据库、图谱、
大模型或在线服务；``interrupt`` 用记录载荷的替身替代，按顺序返回预设恢复值。
"""

from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from langgraph.types import interrupt as LANGGRAPH_INTERRUPT

from yuxi.knowledge.maintenance_plan import graph_plan as graph_plan_module
from yuxi.knowledge.maintenance_plan import tool as tool_module
from yuxi.knowledge.maintenance_plan.contracts import (
    CHOICE_ABORTED_MESSAGE,
    CLARIFY_ALL_MESSAGE,
    DEVICE_CHOICE_QUESTION,
    DEVICE_CHOICE_SOURCE,
    DEVICE_QUESTION_ID,
    MAX_CHOICE_ROUNDS,
    NO_DEVICE_MESSAGE,
    NO_TEMPLATE_MESSAGE,
    PLAIN_TOOL_MESSAGES,
    PLAN_GRAPH_NOT_CONFIGURED_MESSAGE,
    PLAN_VERIFICATION_FAILED_MESSAGE,
    TASK_TYPE_CHOICE_SOURCE,
    TASK_TYPE_QUESTION_ID,
    TASK_TYPES,
    ClauseRef,
    DeviceCandidate,
    PlanEvidence,
    PlanQuery,
    SafetyDistance,
    SpareInfo,
    TeamInfo,
    WorkTemplate,
)

pytestmark = pytest.mark.unit

# 生产环境 ``@tool`` 返回 StructuredTool（可调用体在 .coroutine）；本地桩保持普通异步函数
GENERATE = getattr(tool_module.generate_maintenance_plan, "coroutine", None) or tool_module.generate_maintenance_plan

KG_OPTION = {"server_url": "bolt://fake:7688", "graph": "kg3", "username": "u", "password": "p"}


# ---------------------------------------------------------------------------
# 假对象与夹具
# ---------------------------------------------------------------------------
class _FakeOption:
    """管理员配置项替身。"""

    def __init__(self, value: dict) -> None:
        self._value = value

    async def get(self) -> dict:
        return dict(self._value)


class _FakeClient:
    """假 kg 客户端：不查询，只记录实例与 close。"""

    instances: list[_FakeClient] = []

    def __init__(self, config) -> None:
        self.config = config
        self.closed = False
        _FakeClient.instances.append(self)

    def close(self) -> None:
        self.closed = True


class InterruptPending(Exception):
    """模拟 langgraph 在恢复值不足时抛出的中断信号，用于检查载荷后停止执行。"""


class _InterruptRecorder:
    """``interrupt`` 替身：记录载荷并按顺序返回预设恢复值。"""

    def __init__(self, values: list | None = None) -> None:
        self.values = list(values or [])
        self.payloads: list[dict] = []

    def __call__(self, payload: dict) -> object:
        self.payloads.append(payload)
        if not self.values:
            raise InterruptPending()
        return self.values.pop(0)


def _candidate(
    device_id: str,
    name: str,
    *,
    device_type: str = "开关柜",
    voltage_level: str = "10kV",
    path: str = "#3开闭所 > 3k18",
    score: float = 100.0,
) -> DeviceCandidate:
    return DeviceCandidate(
        device_id=device_id,
        name=name,
        device_type=device_type,
        voltage_level=voltage_level,
        path=path,
        ledger_class="中压断路器柜",
        score=score,
    )


def _template() -> WorkTemplate:
    return WorkTemplate(
        template_id="TPL-SWG-REPLACE",
        name="开关柜更换作业",
        task_type="更换",
        device_type="开关柜",
        work_object="断路器",
        outage_required="需停电",
        isolation_measures="断开{设备名称}两侧隔离开关并验电接地",
        step_sequence=["核对设备双重名称", "办理工作票并布置安全措施"],
        key_points=["确认无电压后装设接地线"],
        tech_params=["回路电阻不大于 50 μΩ"],
        personal_protection="穿绝缘鞋、戴绝缘手套",
        grounding_requirements="在{设备名称}两侧装设接地线",
        work_safety=["工作现场设专人监护"],
        tools=["绝缘手套"],
        materials=["断路器 1 台"],
        acceptance=["传动试验合格"],
        notes=["作业结束后清理现场"],
        staffing="4人：工作负责人1人、作业人员2人、专责监护人1人",
        clauses=[ClauseRef(standard_no="DL/T 408—2023", clause_no="7.3.1", title="安全距离", excerpt="…")],
        source_note="规程",
    )


def _evidence(device: DeviceCandidate, *, template: WorkTemplate | None = None) -> PlanEvidence:
    evidence = PlanEvidence(
        device=device,
        ancestors=["3k18", "#3开闭所"],
        outage_scope=["3k18", "#3开闭所"],
        electrical_neighbors=[],
        template=template,
        test_items=["绝缘电阻试验"],
        patrol_items=["外观检查"],
        safety_distance=SafetyDistance(voltage_level="10kV", no_outage_m=0.7, work_range_m=0.35, live_work_m=0.4),
        team=TeamInfo(name="#3开闭所运维班", member_count=4, leader="李工", managed_area="#3开闭所"),
        spares=[SpareInfo(name="断路器", model="ZN63", quantity=2, location="备件库")],
        retrieval_log=["按台账检索到目标设备"],
    )
    return evidence


def _query(**overrides) -> PlanQuery:
    base = {
        "raw_text": "更换#3开闭所3k18断路器",
        "task_type": "更换",
        "device_name": "3k18",
        "device_type": "开关柜",
        "voltage_level": "10kV",
        "work_object": "断路器",
        "location_hint": "#3开闭所",
    }
    base.update(overrides)
    return PlanQuery(**base)


def _context(**overrides) -> SimpleNamespace:
    base = {"model": None, "skills": ["maintenance-plan"], "_maintenance_plan_result": None}
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def env(monkeypatch):
    """默认环境：配置可用、假客户端、无中断恢复值、search/evidence/意图按测试替换。"""

    _FakeClient.instances = []
    monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption(KG_OPTION))
    monkeypatch.setattr(tool_module, "TuGraphClient", _FakeClient)
    recorder = _InterruptRecorder()
    monkeypatch.setattr(tool_module, "interrupt", recorder)
    return SimpleNamespace(monkeypatch=monkeypatch, interrupt=recorder)


def _patch_search(monkeypatch, handler):
    """替换 ``search_devices``；``handler(轮次, query)`` 返回候选列表，并记录收到的 query。"""

    calls: list[PlanQuery] = []

    def fake(client, query, **_kwargs):
        calls.append(query)
        return handler(len(calls), query)

    monkeypatch.setattr(graph_plan_module, "search_devices", fake)
    return calls


def _patch_evidence(monkeypatch, handler):
    """替换 ``collect_plan_evidence``；``handler(query, device)`` 返回夹具证据。"""

    calls: list[tuple[PlanQuery, DeviceCandidate]] = []

    def fake(client, query, device):
        calls.append((query, device))
        return handler(query, device)

    monkeypatch.setattr(graph_plan_module, "collect_plan_evidence", fake)
    return calls


def _patch_intent(monkeypatch, handler):
    """替换 ``extract_plan_intent``；``handler(text, previous)`` 返回 PlanQuery。"""

    calls: list[tuple[str, PlanQuery | None]] = []

    async def fake(text, *, model_call=None, previous=None, feedback=None):
        calls.append((text, previous))
        return handler(text, previous)

    monkeypatch.setattr(tool_module, "extract_plan_intent", fake)
    return calls


def _ready_pipeline(env, *, candidates=None, evidence=None, device=None):
    """把 search/evidence 都接上：单候选直出、证据含模板。"""

    target = device or (candidates or [_candidate("LGR#1", "3k18")])[0]
    search_calls = _patch_search(env.monkeypatch, lambda _n, _q: candidates or [target])
    evidence_calls = _patch_evidence(
        env.monkeypatch, lambda _q, _d: evidence or _evidence(target, template=_template())
    )
    return search_calls, evidence_calls


# ---------------------------------------------------------------------------
# 输入、上下文与缓存
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_query_returns_clarify_all_message():
    assert await GENERATE(query_text="   ", runtime=None) == CLARIFY_ALL_MESSAGE


@pytest.mark.asyncio
async def test_missing_runtime_or_context_returns_permission_message():
    assert await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=None) == tool_module.NO_PERMISSION_MESSAGE
    runtime = SimpleNamespace(context=None, tool_call_id="t1")
    assert await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=runtime) == tool_module.NO_PERMISSION_MESSAGE


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_result_without_graph(env):
    cached = {"status": "ok", "answer_source": "maintenance_plan", "answer_markdown": "缓存"}
    context = _context(_maintenance_plan_result=cached)

    def boom(_n, _q):  # pragma: no cover - 缓存命中时不应被调用
        raise AssertionError("缓存命中不应再检索图谱")

    _patch_search(env.monkeypatch, boom)

    result = await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=SimpleNamespace(context=context))

    assert result is cached


# ---------------------------------------------------------------------------
# 配置与错误投影
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_kg_config_returns_not_configured(env):
    env.monkeypatch.setattr(tool_module, "fault_trace_opts", _FakeOption({}))

    result = await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=SimpleNamespace(context=_context()))

    assert result == PLAN_GRAPH_NOT_CONFIGURED_MESSAGE
    assert _FakeClient.instances == []


@pytest.mark.asyncio
async def test_kg_error_projects_to_not_configured_and_caches(env):
    async def fake_intent(text, *, model_call=None, previous=None, feedback=None):
        return _query()

    env.monkeypatch.setattr(tool_module, "extract_plan_intent", fake_intent)

    def boom(_n, _q):
        raise tool_module.TuGraphError("boom")

    _patch_search(env.monkeypatch, boom)
    context = _context()

    result = await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=SimpleNamespace(context=context))

    assert result == PLAN_GRAPH_NOT_CONFIGURED_MESSAGE
    assert context._maintenance_plan_result == result
    assert _FakeClient.instances[0].closed is True


# ---------------------------------------------------------------------------
# 方案三：澄清
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_clarify_all_when_task_type_and_device_both_missing(env):
    _patch_intent(
        env.monkeypatch,
        lambda _t, _p: PlanQuery(
            raw_text="帮我出个方案",
            clarification_required=True,
            clarification_message=CLARIFY_ALL_MESSAGE,
        ),
    )

    def boom(_n, _q):  # pragma: no cover - 澄清时不应检索
        raise AssertionError("澄清分支不应检索设备")

    _patch_search(env.monkeypatch, boom)
    context = _context()

    result = await GENERATE(query_text="帮我出个方案", runtime=SimpleNamespace(context=context))

    assert result == CLARIFY_ALL_MESSAGE
    assert context._maintenance_plan_result is None
    assert _FakeClient.instances == []


# ---------------------------------------------------------------------------
# 缺任务类型：interrupt 选任务类型
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_task_type_interrupt_payload_and_resume(env):
    _patch_intent(env.monkeypatch, lambda _t, _p: _query(task_type=None, work_object=None))
    env.interrupt.values = ["检修"]
    search_calls, _evidence_calls = _ready_pipeline(env)

    result = await GENERATE(query_text="1号主变", runtime=SimpleNamespace(context=_context()))

    assert len(env.interrupt.payloads) == 1
    payload = env.interrupt.payloads[0]
    assert payload["source"] == TASK_TYPE_CHOICE_SOURCE
    question = payload["questions"][0]
    assert question["question_id"] == TASK_TYPE_QUESTION_ID
    assert [option["value"] for option in question["options"]] == list(TASK_TYPES)
    assert question["multi_select"] is False
    assert question["allow_other"] is True
    assert payload["query"]["device_name"] == "3k18"
    assert search_calls[0].task_type == "检修"
    assert result["answer_source"] == "maintenance_plan"


@pytest.mark.asyncio
async def test_task_type_free_text_falls_back_to_rule_extract(env):
    _patch_intent(env.monkeypatch, lambda _t, _p: _query(task_type=None))
    env.interrupt.values = ["做个预防性试验"]
    search_calls, _evidence_calls = _ready_pipeline(env)

    result = await GENERATE(query_text="1号主变", runtime=SimpleNamespace(context=_context()))

    assert search_calls[0].task_type == "试验"
    assert result["answer_source"] == "maintenance_plan"


@pytest.mark.asyncio
async def test_task_type_aborted_after_max_rounds(env):
    _patch_intent(env.monkeypatch, lambda _t, _p: _query(task_type=None))
    env.interrupt.values = ["其他", "其他", "其他"]

    def boom(_n, _q):  # pragma: no cover - 交互中止时不应检索
        raise AssertionError("任务类型未确定时不应检索设备")

    _patch_search(env.monkeypatch, boom)
    context = _context()

    result = await GENERATE(query_text="1号主变", runtime=SimpleNamespace(context=context))

    assert result == CHOICE_ABORTED_MESSAGE
    assert len(env.interrupt.payloads) == MAX_CHOICE_ROUNDS
    assert context._maintenance_plan_result is None


@pytest.mark.asyncio
async def test_task_type_interrupt_pending_stops_before_graph(env):
    """恢复值不足时中断抛出，载荷已记录且未建客户端（无副作用）。"""

    _patch_intent(env.monkeypatch, lambda _t, _p: _query(task_type=None))

    with pytest.raises(InterruptPending):
        await GENERATE(query_text="1号主变", runtime=SimpleNamespace(context=_context()))

    assert env.interrupt.payloads[0]["source"] == TASK_TYPE_CHOICE_SOURCE
    assert _FakeClient.instances == []


# ---------------------------------------------------------------------------
# 设备选择：直出与 interrupt
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_single_candidate_direct_without_interrupt(env):
    _patch_intent(env.monkeypatch, lambda _t, _p: _query())
    device = _candidate("LGR#3k18", "3k18")
    _search_calls, evidence_calls = _ready_pipeline(env, device=device)

    result = await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=SimpleNamespace(context=_context()))

    assert env.interrupt.payloads == []
    assert evidence_calls[0][1] is device
    assert result["answer_source"] == "maintenance_plan"
    assert _FakeClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_unique_top_score_matching_name_direct(env):
    """最高分唯一且名称与用户说法一致（含空格/大小写差异）→ 不发中断。"""

    _patch_intent(env.monkeypatch, lambda _t, _p: _query(device_name="991 svg开关", device_type="断路器"))
    top = _candidate("LGR#991A", "991SVG开关", score=100.0)
    other = _candidate("LGR#991B", "991SVG开关2", score=40.0)
    _search_calls, evidence_calls = _ready_pipeline(env, candidates=[top, other])

    result = await GENERATE(query_text="更换991SVG开关", runtime=SimpleNamespace(context=_context()))

    assert env.interrupt.payloads == []
    assert evidence_calls[0][1] is top
    assert result["answer_source"] == "maintenance_plan"


@pytest.mark.asyncio
async def test_two_same_name_candidates_device_interrupt_and_choose_second(env):
    _patch_intent(env.monkeypatch, lambda _t, _p: _query(device_name="992SVG开关"))
    first = _candidate("LGR#992A", "992SVG开关", path="35kV#1变电站 > 992SVG开关", score=100.0)
    second = _candidate("LGR#992B", "992SVG开关", path="35kV#2变电站 > 992SVG开关", score=100.0)
    env.interrupt.values = [second.device_id]
    _search_calls, evidence_calls = _ready_pipeline(env, candidates=[first, second])

    result = await GENERATE(query_text="992SVG开关检修", runtime=SimpleNamespace(context=_context()))

    assert len(env.interrupt.payloads) == 1
    payload = env.interrupt.payloads[0]
    assert payload["source"] == DEVICE_CHOICE_SOURCE
    question = payload["questions"][0]
    assert question["question_id"] == DEVICE_QUESTION_ID
    assert question["question"] == DEVICE_CHOICE_QUESTION
    assert [option["value"] for option in question["options"]] == ["LGR#992A", "LGR#992B"]
    assert question["options"][1]["label"] == second.label()
    assert evidence_calls[0][1] is second
    assert result["answer_source"] == "maintenance_plan"


@pytest.mark.asyncio
async def test_device_free_text_triggers_research_with_previous(env):
    """用户自由输入位置线索：作为补充重新解析并重新检索，检索收到的是新 query。"""

    first = _candidate("LGR#992A", "992SVG开关", path="35kV#1变电站 > 992SVG开关")
    second = _candidate("LGR#992B", "992SVG开关", path="35kV#2变电站 > 992SVG开关")
    resolved = _candidate("LGR#992B", "992SVG开关", path="35kV#2变电站 > 992SVG开关")
    intent_calls = _patch_intent(
        env.monkeypatch,
        lambda text, previous: (
            _query(device_name="992SVG开关")
            if previous is None
            else _query(device_name="992SVG开关", location_hint=text)
        ),
    )
    search_calls = _patch_search(env.monkeypatch, lambda n, _q: [first, second] if n == 1 else [resolved])
    evidence_calls = _patch_evidence(env.monkeypatch, lambda _q, d: _evidence(d, template=_template()))
    env.interrupt.values = ["35kV#2变电站"]

    result = await GENERATE(query_text="992SVG开关检修", runtime=SimpleNamespace(context=_context()))

    assert len(search_calls) == 2
    assert intent_calls[1][0] == "35kV#2变电站"
    assert intent_calls[1][1] is not None
    assert search_calls[1].location_hint == "35kV#2变电站"
    assert evidence_calls[0][1] is resolved
    assert result["answer_source"] == "maintenance_plan"


@pytest.mark.asyncio
async def test_followup_task_type_change_reaches_evidence_and_answer(env):
    """设备选择时用户改任务类型：证据收集与方案装配都必须收到更新后的 query（返修项）。"""

    old = _query(task_type="检修", device_name="992SVG开关", work_object=None, location_hint=None)
    new = _query(
        task_type="更换",
        device_name="992SVG开关",
        work_object="断路器",
        location_hint=None,
        raw_text="992SVG开关检修；改为更换断路器",
    )
    intent_calls = _patch_intent(env.monkeypatch, lambda _t, previous: new if previous is not None else old)
    first = _candidate("LGR#992A", "992SVG开关", path="35kV#1变电站 > 992SVG开关")
    second = _candidate("LGR#992B", "992SVG开关", path="35kV#2变电站 > 992SVG开关")
    _patch_search(env.monkeypatch, lambda n, _q: [first, second] if n == 1 else [second])
    evidence_calls = _patch_evidence(env.monkeypatch, lambda _q, d: _evidence(d, template=_template()))
    answers: list[PlanQuery] = []

    def fake_produce(query, evidence, **kwargs):
        answers.append(query)
        return {"status": "ok", "answer_source": "maintenance_plan", "answer_verified": True}

    env.monkeypatch.setattr(tool_module, "produce_maintenance_plan_answer", fake_produce)
    env.interrupt.values = [{"text": "改为更换断路器"}]

    result = await GENERATE(query_text="992SVG开关检修", runtime=SimpleNamespace(context=_context()))

    assert result["answer_source"] == "maintenance_plan"
    assert intent_calls[1][0] == "改为更换断路器"
    assert intent_calls[1][1] is old
    assert evidence_calls[0][0] is new
    assert evidence_calls[0][0].task_type == "更换"
    assert evidence_calls[0][0].work_object == "断路器"
    assert answers[0] is new
    assert answers[0].task_type == "更换"
    assert answers[0].work_object == "断路器"
    assert answers[0].raw_text == "992SVG开关检修；改为更换断路器"


@pytest.mark.asyncio
async def test_followup_location_only_reaches_evidence_and_answer(env):
    """设备选择时用户只补位置线索：任务类型保持原样，位置与原文更新后传给证据与装配。"""

    old = _query(task_type="更换", device_name="992SVG开关", location_hint=None)
    new = _query(
        task_type="更换",
        device_name="992SVG开关",
        location_hint="#3开闭所",
        raw_text="992SVG开关更换；#3开闭所",
    )
    intent_calls = _patch_intent(env.monkeypatch, lambda _t, previous: new if previous is not None else old)
    first = _candidate("LGR#992A", "992SVG开关", path="35kV#1变电站 > 992SVG开关")
    second = _candidate("LGR#992B", "992SVG开关", path="35kV#2变电站 > 992SVG开关")
    _patch_search(env.monkeypatch, lambda n, _q: [first, second] if n == 1 else [second])
    evidence_calls = _patch_evidence(env.monkeypatch, lambda _q, d: _evidence(d, template=_template()))
    answers: list[PlanQuery] = []

    def fake_produce(query, evidence, **kwargs):
        answers.append(query)
        return {"status": "ok", "answer_source": "maintenance_plan", "answer_verified": True}

    env.monkeypatch.setattr(tool_module, "produce_maintenance_plan_answer", fake_produce)
    env.interrupt.values = [{"text": "#3开闭所"}]

    result = await GENERATE(query_text="992SVG开关更换", runtime=SimpleNamespace(context=_context()))

    assert result["answer_source"] == "maintenance_plan"
    assert intent_calls[1][0] == "#3开闭所"
    assert evidence_calls[0][0].task_type == "更换"
    assert evidence_calls[0][0].location_hint == "#3开闭所"
    assert answers[0].raw_text == "992SVG开关更换；#3开闭所"
    assert answers[0].location_hint == "#3开闭所"


@pytest.mark.asyncio
async def test_choice_budget_is_global_across_task_type_and_device(env):
    """整次请求只允许 MAX_CHOICE_ROUNDS 次引导选择：无效任务类型 → 有效任务类型 → 设备选择用尽预算。

    第 4 次需要中断时返回 CHOICE_ABORTED_MESSAGE，且不得收集证据、不得装配方案。
    """

    _patch_intent(env.monkeypatch, lambda _t, _p: _query(task_type=None))
    first = _candidate("LGR#992A", "992SVG开关")
    second = _candidate("LGR#992B", "992SVG开关")
    _patch_search(env.monkeypatch, lambda _n, _q: [first, second])
    evidence_calls = _patch_evidence(env.monkeypatch, lambda _q, d: _evidence(d, template=_template()))
    answers: list[PlanQuery] = []

    def fake_produce(query, evidence, **kwargs):  # pragma: no cover - 预算用尽时不应装配
        answers.append(query)
        return {"status": "ok"}

    env.monkeypatch.setattr(tool_module, "produce_maintenance_plan_answer", fake_produce)
    # 第 1 次：无效任务类型；第 2 次：有效任务类型；第 3 次：设备选择仍没给出可用选择
    env.interrupt.values = ["其他", "检修", "__other__"]
    context = _context()

    result = await GENERATE(query_text="992SVG开关检修", runtime=SimpleNamespace(context=context))

    assert result == CHOICE_ABORTED_MESSAGE
    assert len(env.interrupt.payloads) == MAX_CHOICE_ROUNDS
    assert [payload["source"] for payload in env.interrupt.payloads] == [
        TASK_TYPE_CHOICE_SOURCE,
        TASK_TYPE_CHOICE_SOURCE,
        DEVICE_CHOICE_SOURCE,
    ]
    assert evidence_calls == []
    assert answers == []
    assert context._maintenance_plan_result is None


@pytest.mark.asyncio
async def test_choice_budget_allows_one_task_type_and_one_device_round(env):
    """正常路径只花两次引导选择（任务类型一次 + 设备一次），在 3 次预算内完成。"""

    _patch_intent(env.monkeypatch, lambda _t, _p: _query(task_type=None, work_object=None))
    first = _candidate("LGR#992A", "992SVG开关", path="35kV#1变电站 > 992SVG开关")
    second = _candidate("LGR#992B", "992SVG开关", path="35kV#2变电站 > 992SVG开关")
    _patch_search(env.monkeypatch, lambda _n, _q: [first, second])
    evidence_calls = _patch_evidence(env.monkeypatch, lambda _q, d: _evidence(d, template=_template()))
    env.interrupt.values = ["检修", second.device_id]

    result = await GENERATE(query_text="992SVG开关", runtime=SimpleNamespace(context=_context()))

    assert len(env.interrupt.payloads) == 2
    assert result["answer_source"] == "maintenance_plan"
    assert evidence_calls[0][0].task_type == "检修"
    assert evidence_calls[0][1] is second


@pytest.mark.asyncio
async def test_device_choice_other_value_aborts(env):
    _patch_intent(env.monkeypatch, lambda _t, _p: _query(device_name="992SVG开关"))
    first = _candidate("LGR#992A", "992SVG开关")
    second = _candidate("LGR#992B", "992SVG开关")
    env.interrupt.values = ["__other__", "其他", "other"]
    _search_calls, _evidence_calls = _ready_pipeline(env, candidates=[first, second])
    context = _context()

    result = await GENERATE(query_text="992SVG开关检修", runtime=SimpleNamespace(context=context))

    assert result == CHOICE_ABORTED_MESSAGE
    assert len(env.interrupt.payloads) == MAX_CHOICE_ROUNDS
    assert context._maintenance_plan_result is None


# ---------------------------------------------------------------------------
# 无候选 / 无模板
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_device_returns_no_device_message(env):
    _patch_intent(env.monkeypatch, lambda _t, _p: _query(device_name="XYZ999"))
    _patch_search(env.monkeypatch, lambda _n, _q: [])
    context = _context()

    result = await GENERATE(query_text="更换XYZ999", runtime=SimpleNamespace(context=context))

    assert result == NO_DEVICE_MESSAGE
    assert context._maintenance_plan_result is None
    assert _FakeClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_no_template_returns_message_and_caches(env):
    _patch_intent(env.monkeypatch, lambda _t, _p: _query(device_type="避雷器", work_object=None))
    device = _candidate("LGR#MOA1", "1号避雷器", device_type="避雷器")
    _patch_search(env.monkeypatch, lambda _n, _q: [device])
    _patch_evidence(env.monkeypatch, lambda _q, d: _evidence(d, template=None))
    context = _context()

    result = await GENERATE(query_text="1号避雷器检修", runtime=SimpleNamespace(context=context))

    assert result == NO_TEMPLATE_MESSAGE
    assert context._maintenance_plan_result == NO_TEMPLATE_MESSAGE
    assert _FakeClient.instances[0].closed is True


# ---------------------------------------------------------------------------
# 输出校验门：只有 answer_verified is True 才返回报告
# ---------------------------------------------------------------------------
def _unverified_payload(**overrides) -> dict:
    payload = {
        "status": "ok",
        "answer_source": "maintenance_plan",
        "report": {"title": "未校验方案", "task_type": "更换"},
        "answer_markdown": "# 未校验方案",
        "answer_verified": False,
    }
    payload.update(overrides)
    return payload


def test_verification_failed_message_is_plain_tool_message():
    """固定文案必须在契约 PLAIN_TOOL_MESSAGES 中，发布门才会原样透传。"""

    assert PLAN_VERIFICATION_FAILED_MESSAGE in PLAIN_TOOL_MESSAGES


@pytest.mark.parametrize(
    "payload",
    [
        _unverified_payload(),
        _unverified_payload(answer_verified=None),
        _unverified_payload(answer_verified="true"),
        _unverified_payload(answer_verified=1),
        {k: v for k, v in _unverified_payload().items() if k != "answer_verified"},
        {},
    ],
)
@pytest.mark.asyncio
async def test_unverified_answer_is_replaced_by_fixed_message(env, payload):
    """装配结果未被校验（False/缺失/非布尔）时只返回固定文案并缓存，不外泄 report/Markdown。"""

    _patch_intent(env.monkeypatch, lambda _t, _p: _query())
    device = _candidate("LGR#3k18", "3k18")
    _ready_pipeline(env, device=device)
    env.monkeypatch.setattr(tool_module, "produce_maintenance_plan_answer", lambda *a, **k: payload)
    context = _context()

    result = await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=SimpleNamespace(context=context))

    assert isinstance(result, str)
    assert result == PLAN_VERIFICATION_FAILED_MESSAGE
    assert context._maintenance_plan_result == PLAN_VERIFICATION_FAILED_MESSAGE
    assert _FakeClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_verified_answer_payload_passes_through_unchanged(env):
    """校验通过的载荷原样返回（校验门不误伤正常结果）。"""

    payload = _unverified_payload(answer_verified=True)
    _patch_intent(env.monkeypatch, lambda _t, _p: _query())
    device = _candidate("LGR#3k18", "3k18")
    _ready_pipeline(env, device=device)
    env.monkeypatch.setattr(tool_module, "produce_maintenance_plan_answer", lambda *a, **k: payload)
    context = _context()

    result = await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=SimpleNamespace(context=context))

    assert result is payload
    assert context._maintenance_plan_result is payload


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_graph_queries_run_in_worker_thread(env):
    """同步图查询必须经 asyncio.to_thread 执行，不阻塞事件循环。"""

    _patch_intent(env.monkeypatch, lambda _t, _p: _query())
    device = _candidate("LGR#3k18", "3k18")
    seen: dict[str, threading.Thread] = {}

    def fake_search(client, query, **_kwargs):
        seen["search"] = threading.current_thread()
        return [device]

    def fake_evidence(client, query, target):
        seen["evidence"] = threading.current_thread()
        return _evidence(target, template=_template())

    env.monkeypatch.setattr(graph_plan_module, "search_devices", fake_search)
    env.monkeypatch.setattr(graph_plan_module, "collect_plan_evidence", fake_evidence)

    result = await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=SimpleNamespace(context=_context()))

    assert result["answer_source"] == "maintenance_plan"
    assert seen["search"] is not threading.main_thread()
    assert seen["evidence"] is not threading.main_thread()


@pytest.mark.asyncio
async def test_normal_path_returns_maintenance_plan_payload(env):
    _patch_intent(env.monkeypatch, lambda _t, _p: _query())
    device = _candidate("LGR#3k18", "3k18")
    _ready_pipeline(env, device=device)
    context = _context()

    result = await GENERATE(query_text="更换#3开闭所3k18断路器", runtime=SimpleNamespace(context=context))

    assert result["answer_source"] == "maintenance_plan"
    assert result["status"] == "ok"
    assert result["answer_verified"] is True
    assert "answer_markdown" in result and result["answer_markdown"].strip()
    assert "staffing" in result["report"]
    assert context._maintenance_plan_result is result
    assert _FakeClient.instances[0].closed is True


# ---------------------------------------------------------------------------
# 真实 LangGraph 重放：任务类型中断 → 设备中断 → 完成
# ---------------------------------------------------------------------------
class _PlanState(TypedDict, total=False):
    """重放用最小图状态：只放可序列化的输入与输出。"""

    query_text: str
    result: Any


@pytest.mark.asyncio
async def test_langgraph_replay_task_type_then_device_then_complete(env):
    """真正驱动 StateGraph + InMemorySaver + Command(resume=...)，证明连续两次中断的重放。

    这里不替换 ``interrupt``：节点里调用的是 langgraph 真实 ``interrupt``，恢复值由
    ``Command(resume=...)`` 经 checkpointer 提交，重放时前面的 ``interrupt`` 依次返回已提交值。
    """

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    env.monkeypatch.setattr(tool_module, "interrupt", LANGGRAPH_INTERRUPT)
    _patch_intent(env.monkeypatch, lambda _t, _p: _query(task_type=None, work_object=None))
    first = _candidate("LGR#992A", "992SVG开关", path="35kV#1变电站 > 992SVG开关")
    second = _candidate("LGR#992B", "992SVG开关", path="35kV#2变电站 > 992SVG开关")
    search_calls = _patch_search(env.monkeypatch, lambda _n, _q: [first, second])
    evidence_calls = _patch_evidence(env.monkeypatch, lambda _q, d: _evidence(d, template=_template()))
    context = _context()

    async def plan_node(state: _PlanState) -> dict:
        result = await GENERATE(state["query_text"], runtime=SimpleNamespace(context=context))
        return {"result": result}

    builder = StateGraph(_PlanState)
    builder.add_node("plan", plan_node)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t9-replay"}}

    paused_on_task_type = await graph.ainvoke({"query_text": "992SVG开关检修"}, config)
    assert paused_on_task_type["__interrupt__"][0].value["source"] == TASK_TYPE_CHOICE_SOURCE
    assert search_calls == []

    paused_on_device = await graph.ainvoke(Command(resume={"value": "检修"}), config)
    assert paused_on_device["__interrupt__"][0].value["source"] == DEVICE_CHOICE_SOURCE
    assert len(search_calls) == 1

    done = await graph.ainvoke(Command(resume={"value": second.device_id}), config)

    assert done["result"]["answer_source"] == "maintenance_plan"
    assert len(search_calls) == 2
    assert len(evidence_calls) == 1
    assert evidence_calls[0][0].task_type == "检修"
    assert evidence_calls[0][1] is second


@pytest.mark.asyncio
async def test_langgraph_replay_aborts_when_choice_budget_is_global_and_exhausted(env):
    """真实连续恢复的边界：无效任务类型 → 有效任务类型 → 设备选择，3 次中断后第 4 次需要时中止。

    用真实 ``interrupt`` 驱动 ``StateGraph`` + ``InMemorySaver``：三次 ``Command(resume=...)`` 依次
    提交恢复值，预算用尽时不再发第 4 次中断，直接返回 ``CHOICE_ABORTED_MESSAGE``，
    且不收集证据、不装配方案。
    """

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    env.monkeypatch.setattr(tool_module, "interrupt", LANGGRAPH_INTERRUPT)
    _patch_intent(env.monkeypatch, lambda _t, _p: _query(task_type=None, work_object=None))
    first = _candidate("LGR#992A", "992SVG开关", path="35kV#1变电站 > 992SVG开关")
    second = _candidate("LGR#992B", "992SVG开关", path="35kV#2变电站 > 992SVG开关")
    _patch_search(env.monkeypatch, lambda _n, _q: [first, second])
    evidence_calls = _patch_evidence(env.monkeypatch, lambda _q, d: _evidence(d, template=_template()))
    answers: list[PlanQuery] = []

    def fake_produce(query, evidence, **kwargs):  # pragma: no cover - 预算用尽时不应装配
        answers.append(query)
        return {"status": "ok"}

    env.monkeypatch.setattr(tool_module, "produce_maintenance_plan_answer", fake_produce)
    context = _context()

    async def plan_node(state: _PlanState) -> dict:
        result = await GENERATE(state["query_text"], runtime=SimpleNamespace(context=context))
        return {"result": result}

    builder = StateGraph(_PlanState)
    builder.add_node("plan", plan_node)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t9-budget"}}

    paused_1 = await graph.ainvoke({"query_text": "992SVG开关检修"}, config)
    assert paused_1["__interrupt__"][0].value["source"] == TASK_TYPE_CHOICE_SOURCE

    paused_2 = await graph.ainvoke(Command(resume={"value": "其他"}), config)  # 无效任务类型
    assert paused_2["__interrupt__"][0].value["source"] == TASK_TYPE_CHOICE_SOURCE

    paused_3 = await graph.ainvoke(Command(resume={"value": "检修"}), config)  # 有效任务类型
    assert paused_3["__interrupt__"][0].value["source"] == DEVICE_CHOICE_SOURCE

    done = await graph.ainvoke(Command(resume={"value": "__other__"}), config)  # 设备仍没给出可用选择

    assert done["result"] == CHOICE_ABORTED_MESSAGE
    assert evidence_calls == []
    assert answers == []
    assert context._maintenance_plan_result is None


# ---------------------------------------------------------------------------
# 模型调用参数（假 select_model）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_context_model_call_uses_temperature_zero_and_disables_thinking(env):
    """意图识别的模型调用必须 temperature=0 且 enable_thinking=False（用假 select_model 验证）。"""

    captured: dict[str, Any] = {}

    class _FakeResponse:
        content = "{}"

    class _FakeModel:
        async def call(self, prompt, *, stream=False):
            captured["prompt"] = prompt
            captured["stream"] = stream
            return _FakeResponse()

    def fake_select_model(*, model_spec, timeout, model_params):
        captured["model_spec"] = model_spec
        captured["timeout"] = timeout
        captured["model_params"] = model_params
        return _FakeModel()

    models_pkg = types.ModuleType("yuxi.models")
    models_pkg.__path__ = []  # type: ignore[attr-defined]
    chat_module = types.ModuleType("yuxi.models.chat")
    chat_module.select_model = fake_select_model
    models_pkg.chat = chat_module
    env.monkeypatch.setitem(sys.modules, "yuxi.models", models_pkg)
    env.monkeypatch.setitem(sys.modules, "yuxi.models.chat", chat_module)
    env.monkeypatch.setattr(sys.modules["yuxi"], "models", models_pkg, raising=False)

    model_call = tool_module._context_model_call(_context(model="qwen3.6-35b-a3b"))
    assert model_call is not None

    content = await model_call("抽取意图")

    assert content == "{}"
    assert captured["stream"] is False
    assert captured["model_spec"] == "qwen3.6-35b-a3b"
    assert captured["model_params"]["temperature"] == 0
    assert captured["model_params"]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


# ---------------------------------------------------------------------------
# 恢复值解析：6 种形态
# ---------------------------------------------------------------------------
def test_parse_choice_nested_question_id():
    value = {DEVICE_QUESTION_ID: {"selected": ["LGR#992B"]}}
    assert tool_module._parse_choice(value, DEVICE_QUESTION_ID) == ("LGR#992B", None)


def test_parse_choice_selected_list():
    assert tool_module._parse_choice({"selected": ["检修"]}, DEVICE_QUESTION_ID) == ("检修", None)


def test_parse_choice_value_field():
    assert tool_module._parse_choice({"value": "更换"}, DEVICE_QUESTION_ID) == ("更换", None)


def test_parse_choice_text_field():
    assert tool_module._parse_choice({"text": "做个预防性试验"}, DEVICE_QUESTION_ID) == (None, "做个预防性试验")


def test_parse_choice_list_takes_first_usable():
    value = [{"value": "__other__"}, {"value": "巡视"}]
    assert tool_module._parse_choice(value, DEVICE_QUESTION_ID) == ("巡视", None)


def test_parse_choice_plain_string():
    assert tool_module._parse_choice("试验", DEVICE_QUESTION_ID) == ("试验", None)


@pytest.mark.parametrize("value", ["__other__", "other", "其他", "  ", None, 42, {"value": "其他"}])
def test_parse_choice_other_and_empty_values(value):
    assert tool_module._parse_choice(value, DEVICE_QUESTION_ID) == (None, None)


def test_normalize_name_fullwidth_space_and_case():
    assert tool_module._normalize_name("９９１ ＳＶＧ开关") == tool_module._normalize_name("991svg开关")
    assert tool_module._normalize_name(None) == ""
