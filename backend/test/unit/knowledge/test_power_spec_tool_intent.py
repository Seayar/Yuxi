import pytest

from yuxi.knowledge.power_spec import tool as tool_module
from yuxi.knowledge.power_spec.intent import (
    INTENT_ATTEMPTS_EXHAUSTED_MESSAGE,
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
    context = Context()


class _FakeKnowledgeBase:
    """缺少 reranker 配置时工具在确认之后直接返回无证据。"""

    async def get_kb_config(self, kb_id):
        return type("Config", (), {"query_options": {}})()


class _InterruptQueue:
    """按顺序回放用户答案并记录每次向用户展示的载荷。"""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(payload)
        return self._answers.pop(0) if self._answers else None


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
    monkeypatch.setattr(tool_module, "interrupt", queue)
    return queue


@pytest.mark.asyncio
async def test_confirming_intent_once_continues_to_retrieval(monkeypatch):
    queue = _install(monkeypatch, {"power_spec_intent_confirm": "confirm"})

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
        {"power_spec_intent_confirm": "confirm"},
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
        {"power_spec_intent_confirm": "confirm"},
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
