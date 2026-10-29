import pytest

from yuxi.knowledge.power_spec import tool as tool_module
from yuxi.knowledge.power_spec.contracts import (
    INTENT_ATTEMPTS_EXHAUSTED_MESSAGE,
    PowerSpecQuery,
)
from yuxi.knowledge.power_spec.intent import (
    decode_confirmed_plan,
    encode_confirmed_plan,
    extract_correction_text,
    parse_intent_confirmation,
    parse_power_spec_query,
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


def _confirmed_answer(plan: dict) -> dict:
    """构造“用户点了正确”的恢复值：选项值里带着整份确认计划。"""

    return {"power_spec_intent_confirm": f"confirm#{encode_confirmed_plan(plan)}"}


@pytest.mark.asyncio
async def test_confirmed_plan_is_used_for_retrieval(monkeypatch):
    """用户确认的计划就是检索条件：重放识别出别的字段时也按确认的计划检索，不报错。"""

    seen: list[PowerSpecQuery] = []

    async def capture(_executor, _config, _kb_id, query, _reranker, _warnings):
        seen.append(query)
        return []

    monkeypatch.setattr(tool_module, "_retrieve_for_query", capture)
    _install(monkeypatch)
    monkeypatch.setattr(
        tool_module,
        "interrupt",
        lambda _payload: _confirmed_answer(
            {
                "intent": "检修标准查询",
                "device_type": "变压器",
                "voltage_level": "220kV",
                "answer_dimensions": ["周期"],
                "normalized_terms": ["周期", "变压器"],
                "unknown_terms": [],
                "normalized_query": "220kV 变压器色谱周期",
            }
        ),
    )

    async def model_redentifies_differently(_query_text, **_kwargs):
        return PowerSpecQuery(
            query="110kV 开关柜触头温度限值",
            intent="设备参数查询",
            voltage_level="110kV",
        )

    monkeypatch.setattr(tool_module, "extract_power_spec_intent", model_redentifies_differently)

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=False, runtime=Runtime())

    assert result == NO_EVIDENCE_MESSAGE
    assert seen and seen[-1].voltage_level == "220kV"
    assert seen[-1].query == "220kV 变压器色谱周期"


@pytest.mark.asyncio
async def test_missing_plan_value_falls_back_to_recomputed_query(monkeypatch):
    """只回传 confirm 的客户端没有计划可还原，按本次识别的结果检索，同样不报错。"""

    seen: list[PowerSpecQuery] = []

    async def capture(_executor, _config, _kb_id, query, _reranker, _warnings):
        seen.append(query)
        return []

    monkeypatch.setattr(tool_module, "_retrieve_for_query", capture)
    _install(monkeypatch, {"power_spec_intent_confirm": "confirm"})

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=False, runtime=Runtime())

    assert result == NO_EVIDENCE_MESSAGE
    assert seen and seen[-1].voltage_level == "220kV"


def test_confirmed_plan_roundtrips_through_option_value():
    """选项值里的计划能原样还原；损坏或旧格式的取值返回 None，不抛异常。"""

    plan = {
        "intent": "检修标准查询",
        "device_type": "变压器",
        "voltage_level": "220kV",
        "answer_dimensions": ["周期"],
        "normalized_terms": ["周期"],
        "unknown_terms": [],
        "normalized_query": "220kV 变压器色谱周期",
    }
    answer = _confirmed_answer(plan)

    assert decode_confirmed_plan(answer) == plan
    assert decode_confirmed_plan({"power_spec_intent_confirm": "confirm"}) is None
    assert decode_confirmed_plan({"power_spec_intent_confirm": "confirm#不是base64"}) is None
    assert decode_confirmed_plan(None) is None


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
