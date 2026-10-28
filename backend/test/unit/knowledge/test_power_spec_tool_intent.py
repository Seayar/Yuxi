import pytest

from yuxi.knowledge.power_spec import tool as tool_module
from yuxi.knowledge.power_spec.contracts import (
    INTENT_ATTEMPTS_EXHAUSTED_MESSAGE,
    INTENT_PLAN_CHANGED_MESSAGE,
    PowerSpecQuery,
)
from yuxi.knowledge.power_spec.intent import (
    extract_correction_text,
    parse_intent_confirmation,
    parse_power_spec_query,
    plan_token,
)
from yuxi.knowledge.power_spec.tool import NO_EVIDENCE_MESSAGE, search_power_spec

QUESTION = "220 的变压器色谱多久做一回？氢气总烃多少算超标？"


class Context:
    uid = "u1"
    knowledges = ["kb1"]


class Runtime:
    """每次实例化都给出独立上下文，避免工具的结果缓存在测试之间泄漏。"""

    def __init__(self):
        self.context = Context()


class _FakeKnowledgeBase:
    """缺重排模型时工具不再报错，而是在确认意图后继续检索；这里用空执行器让流程走到「无资料」。"""

    async def get_kb_config(self, kb_id):
        return type("Config", (), {"query_options": {}})()

    async def get_kb_executor(self, kb_id):
        return _EmptyExecutor()


class _EmptyExecutor:
    async def aquery(self, *_args, **_kwargs):
        return []


class _EmptyRepository:
    async def list_power_spec_extractions(self, _db, *, kb_id):
        return {}


class _SessionStub:
    def get_async_session_context(self):
        class _Context:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_args):
                return False

        return _Context()


ECHO_CONFIRM = "echo-confirm"


class _InterruptQueue:
    """按顺序回放用户答案并记录每次向用户展示的载荷。

    ``ECHO_CONFIRM`` 表示“用户点了卡片上展示的确认项”，用于回显计划指纹；
    其他值原样返回，可用来模拟缺少指纹或自由文本。
    """

    def __init__(self, *answers):
        self._answers = list(answers)
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        answer = self._answers.pop(0) if self._answers else None
        if answer == ECHO_CONFIRM:
            question = payload["questions"][0]
            return {question["question_id"]: question["options"][0]["value"]}
        return answer


def _install(monkeypatch, *answers, intent_calls=None):
    async def visible(_context):
        return [{"kb_id": "kb1"}]

    async def fake_intent(query_text, *, model_call=None, previous=None, feedback=None):
        if intent_calls is not None:
            intent_calls.append({"previous": previous, "feedback": feedback})
        return parse_power_spec_query(query_text)

    queue = _InterruptQueue(*answers)
    monkeypatch.setattr(tool_module, "resolve_visible_knowledge_bases_for_context", visible)
    monkeypatch.setattr(tool_module, "extract_power_spec_intent", fake_intent)
    monkeypatch.setattr(tool_module, "knowledge_base", _FakeKnowledgeBase())
    monkeypatch.setattr(tool_module, "PowerSpecRepository", _EmptyRepository)
    monkeypatch.setattr(tool_module, "pg_manager", _SessionStub())
    monkeypatch.setattr(tool_module, "interrupt", queue)
    return queue


@pytest.mark.asyncio
async def test_confirming_intent_once_continues_to_retrieval(monkeypatch):
    queue = _install(monkeypatch, ECHO_CONFIRM)

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=False, runtime=Runtime())

    assert result == NO_EVIDENCE_MESSAGE
    assert len(queue.payloads) == 1
    plan = queue.payloads[0]["intent_plan"]
    assert plan["voltage_level"] == "220kV"
    assert "色谱" in plan["answer_dimensions"]
    assert plan["attempt"] == 1


@pytest.mark.asyncio
async def test_rejecting_intent_with_correction_asks_for_confirmation_again(monkeypatch):
    intent_calls = []
    queue = _install(
        monkeypatch,
        {"power_spec_intent_confirm": {"type": "other", "text": "电压等级是 110kV", "selected": []}},
        ECHO_CONFIRM,
        intent_calls=intent_calls,
    )

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=False, runtime=Runtime())

    assert result == NO_EVIDENCE_MESSAGE
    assert len(queue.payloads) == 2
    assert queue.payloads[0]["source"] == "power_spec_intent_confirmation"
    assert queue.payloads[1]["source"] == "power_spec_intent_confirmation"
    assert queue.payloads[1]["intent_plan"]["attempt"] == 2
    assert intent_calls[1]["feedback"] == "电压等级是 110kV"
    assert intent_calls[1]["previous"] is not None


@pytest.mark.asyncio
async def test_rejecting_intent_without_correction_asks_for_details(monkeypatch):
    intent_calls = []
    queue = _install(
        monkeypatch,
        {"power_spec_intent_confirm": "correct"},
        {"power_spec_intent_correction": {"type": "other", "text": "我要问 110kV 开关柜", "selected": []}},
        ECHO_CONFIRM,
        intent_calls=intent_calls,
    )

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=False, runtime=Runtime())

    assert result == NO_EVIDENCE_MESSAGE
    assert [payload["source"] for payload in queue.payloads] == [
        "power_spec_intent_confirmation",
        "power_spec_intent_correction",
        "power_spec_intent_confirmation",
    ]
    assert intent_calls[1]["feedback"] == "我要问 110kV 开关柜"


@pytest.mark.asyncio
async def test_repeated_rejection_stops_after_attempt_limit(monkeypatch):
    rejection = {"power_spec_intent_confirm": {"type": "other", "text": "还是不对", "selected": []}}
    queue = _install(monkeypatch, *[rejection] * 3)

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=False, runtime=Runtime())

    assert result == INTENT_ATTEMPTS_EXHAUSTED_MESSAGE
    assert len(queue.payloads) == 3
    assert [payload["intent_plan"]["attempt"] for payload in queue.payloads] == [1, 2, 3]


@pytest.mark.asyncio
async def test_rejection_without_correction_text_stops_immediately(monkeypatch):
    queue = _install(monkeypatch, {"power_spec_intent_confirm": "correct"}, {})

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=False, runtime=Runtime())

    assert result == INTENT_ATTEMPTS_EXHAUSTED_MESSAGE
    assert [payload["source"] for payload in queue.payloads] == [
        "power_spec_intent_confirmation",
        "power_spec_intent_correction",
    ]


@pytest.mark.asyncio
async def test_confirmed_intent_skips_user_confirmation(monkeypatch):
    queue = _install(monkeypatch)

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=True, runtime=Runtime())

    assert result == NO_EVIDENCE_MESSAGE
    assert queue.payloads == []


def test_tool_schema_hides_confirmation_from_model():
    """模型不能通过工具参数自行确认意图；确认结论只来自确认卡片。"""

    assert "confirmed_intent" not in tool_module.PowerSpecSearchInput.model_fields
    parsed = tool_module.PowerSpecSearchInput.model_validate({"query_text": QUESTION, "confirmed_intent": True})
    assert parsed.model_dump() == {"query_text": QUESTION, "kb_id": None}


@pytest.mark.asyncio
async def test_confirm_without_plan_token_is_accepted(monkeypatch):
    """不回传选项值的客户端（预构建前端）按已确认放行，不能被永久挡在确认循环里。"""

    _install(monkeypatch, {"power_spec_intent_confirm": "confirm"})

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=False, runtime=Runtime())

    assert result == NO_EVIDENCE_MESSAGE


@pytest.mark.asyncio
async def test_replay_with_changed_plan_is_rejected(monkeypatch):
    """重放的识别结果与用户确认过的计划不一致时，不按新计划检索。

    真实链路里 ``interrupt`` 在 resume 时返回 checkpoint 保存的选项值；这里用记录首次
    选项值的假 ``interrupt`` 复现：用户确认第一份计划，重放重算出第二份计划时应拒绝。
    """

    confirmed: list[str] = []
    shown: list[dict] = []

    def confirm_once(payload):
        shown.append(payload)
        if not confirmed:
            confirmed.append(payload["questions"][0]["options"][0]["value"])
        return {payload["questions"][0]["question_id"]: confirmed[0]}

    _install(monkeypatch)
    monkeypatch.setattr(tool_module, "interrupt", confirm_once)
    await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=False, runtime=Runtime())

    async def changed_intent(query_text, *, model_call=None, previous=None, feedback=None):
        return PowerSpecQuery(query="110kV 开关柜触头温度限值", intent="设备参数查询", voltage_level="110kV")

    monkeypatch.setattr(tool_module, "extract_power_spec_intent", changed_intent)

    result = await search_power_spec.coroutine(
        query_text="110kV 开关柜触头温度限值是多少",
        confirmed_intent=False,
        runtime=Runtime(),
    )

    assert result == INTENT_PLAN_CHANGED_MESSAGE
    assert shown[0]["questions"][0]["options"][0]["value"].startswith("confirm#")


def test_plan_token_tracks_only_fields_the_user_confirmed():
    """指纹只覆盖确认卡片上的语义；模型每次都会变的关键词与重述句不能参与。"""

    base = {
        "intent": "检修标准查询",
        "device_type": "变压器",
        "voltage_level": "220kV",
        "answer_dimensions": ["周期"],
        "normalized_terms": ["周期", "变压器", "DGA"],
        "unknown_terms": ["DGA"],
        "normalized_query": "220kV 变压器色谱周期",
    }
    volatile = {
        **base,
        "normalized_terms": ["周期", "变压器", "检修规程"],
        "unknown_terms": ["检修规程"],
        "normalized_query": "220kV 变压器色谱试验多久一次？",
    }

    assert plan_token(base) == plan_token(volatile)
    assert plan_token(base) != plan_token({**base, "voltage_level": "110kV"})
    assert plan_token(base) != plan_token({**base, "answer_dimensions": ["周期", "限值"]})


def test_plan_token_ignores_dimension_order():
    """同一组维度按集合比较，不因模型输出顺序不同而判为不一致。"""

    base = {
        "intent": "检修标准查询",
        "device_type": "变压器",
        "voltage_level": "220kV",
        "answer_dimensions": ["周期", "限值"],
    }

    assert plan_token(base) == plan_token({**base, "answer_dimensions": ["限值", "周期"]})


def test_parse_intent_confirmation_accepts_frontend_answer_shapes():
    assert parse_intent_confirmation({"power_spec_intent_confirm": "confirm"}) == ("confirm", None)
    assert parse_intent_confirmation({"power_spec_intent_confirm": "正确，继续检索"}) == ("confirm", None)
    assert parse_intent_confirmation({"power_spec_intent_confirm": "correct"}) == ("reject", None)
    assert parse_intent_confirmation(
        {"power_spec_intent_confirm": {"type": "other", "text": "电压是 110kV", "selected": []}}
    ) == ("reject", "电压是 110kV")
    assert parse_intent_confirmation({"power_spec_intent_confirm": ["confirm"]}) == ("confirm", None)
    assert parse_intent_confirmation({"power_spec_intent_confirm": {"selected": ["confirm"]}}) == ("confirm", None)
    assert parse_intent_confirmation("电压是 110kV") == ("reject", "电压是 110kV")
    assert parse_intent_confirmation(None) == ("invalid", None)
    assert parse_intent_confirmation({"power_spec_intent_confirm": ""}) == ("invalid", None)


def test_extract_correction_text_ignores_empty_other_selection():
    assert extract_correction_text({"power_spec_intent_correction": {"type": "other", "text": " 110kV "}}) == "110kV"
    assert extract_correction_text("__other__") is None
    assert extract_correction_text({"power_spec_intent_correction": {"selected": []}}) is None
    assert extract_correction_text(None) is None
