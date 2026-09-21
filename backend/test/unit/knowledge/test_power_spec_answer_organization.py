import json

import pytest

from yuxi.knowledge.power_spec import tool as tool_module
from yuxi.knowledge.power_spec.answer import (
    ANSWER_SOURCE_FIXED_CARD,
    ANSWER_SOURCE_MODEL,
    ORGANIZER_SLUG,
    REVIEWER_SLUG,
    SubagentReply,
    produce_power_spec_answer,
)
from yuxi.knowledge.power_spec.contracts import SpecClauseEvidence
from yuxi.knowledge.power_spec.intent import parse_power_spec_query
from yuxi.knowledge.power_spec.tool import NO_EVIDENCE_MESSAGE, search_power_spec

RAW_BODY = "220kV:6个月；35kV～110kV：1年；总烃：150"
QUESTION = "220kV变压器油中溶解气体分析的周期"

# 原文中没有 12，用于验证数字前置门能拦住编造。
FABRICATED = "220kV 变压器的试验周期为 12 个月。"
GROUNDED = "220kV 变压器的试验周期为 6 个月。"
IMPRECISE = "所有变压器都应执行该试验。"


def _draft(body: str) -> str:
    """构造整理子智能体返回的结构化 JSON。"""

    return json.dumps(
        {"clauses": [{"citation_key": "file-1#6.1", "clause_title": "油浸式电力变压器", "body": body}]},
        ensure_ascii=False,
    )


class Context:
    uid = "u1"
    knowledges = ["kb1"]
    run_id = "run-parent"
    model = "local:test-model"


class Runtime:
    """每次实例化都给出独立上下文，避免工具的结果缓存在测试之间泄漏。"""

    def __init__(self):
        self.context = Context()
        self.tool_call_id = "call-1"


class _SessionStub:
    """替换未初始化的数据库管理器，避免单元测试连接真实 PostgreSQL。"""

    def get_async_session_context(self):
        class _Context:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_exc):
                return False

        return _Context()


def _evidence() -> SpecClauseEvidence:
    return SpecClauseEvidence(
        document_id="file-1",
        standard_name="DL/T 596—2021",
        standard_no="DL/T596—2021",
        version="2021",
        status="现行",
        clause_no="6.1",
        clause_title="油浸式电力变压器",
        clause_body=RAW_BODY,
        rerank_score=0.7,
        source_file="标准.pdf",
        metadata={"raw_clause_body": RAW_BODY, "raw_clause_no": "6.1", "raw_clause_title": "| 油浸式电力变压器"},
    )


def _plain(markdown: str) -> str:
    """去掉关键词高亮标记，便于断言正文内容。"""

    return markdown.replace("<mark>", "").replace("</mark>", "")


def _verdict(passed: bool, detail: str = "原文按电压等级区分周期") -> str:
    issues = [] if passed else [{"kind": "事实错误", "detail": detail}]
    return json.dumps({"verdict": "pass" if passed else "fail", "issues": issues}, ensure_ascii=False)


class Caller:
    """按 slug 回放子智能体输出，记录整理与审查收到的输入。"""

    def __init__(self, organizer_outputs, reviewer_outputs):
        self._organizer = list(organizer_outputs)
        self._reviewer = list(reviewer_outputs)
        self.calls = []

    async def __call__(self, *, slug, message, tool_call_id, continue_thread_id=None):
        self.calls.append({"slug": slug, "message": message, "tool_call_id": tool_call_id})
        if slug == ORGANIZER_SLUG:
            return SubagentReply(output=self._organizer.pop(0), thread_id="organizer-thread")
        return SubagentReply(output=self._reviewer.pop(0), thread_id="reviewer-thread")


def _reject_acceptance(_payload):
    """默认不接受未通过审查的结论，与真实按钮语义一致。"""

    return {"power_spec_answer_acceptance": "reject"}


def _organize(_monkeypatch, caller, **kwargs):
    """直接注入子智能体入口，不再依赖 Tool 的运行上下文。"""

    return produce_power_spec_answer(
        query=parse_power_spec_query(QUESTION),
        evidences=[_evidence()],
        call_subagent=caller,
        tool_call_id=kwargs.pop("tool_call_id", "call-1"),
        ask_acceptance=kwargs.pop("ask_acceptance", _reject_acceptance),
    )


@pytest.mark.asyncio
async def test_passing_review_publishes_organized_answer(monkeypatch):
    caller = Caller([_draft(GROUNDED)], [_verdict(True)])

    organized = await _organize(monkeypatch, caller)

    assert organized["answer_source"] == ANSWER_SOURCE_MODEL
    assert organized["answer_verified"] is True
    assert "**规范名称**" in organized["answer_markdown"]
    assert _plain(organized["answer_markdown"]).find(GROUNDED) >= 0
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG, REVIEWER_SLUG]


@pytest.mark.asyncio
async def test_missing_tool_call_id_falls_back_to_fixed_card(monkeypatch):
    caller = Caller([_draft(GROUNDED)], [_verdict(True)])

    organized = await _organize(monkeypatch, caller, tool_call_id=None)

    assert organized["answer_source"] == ANSWER_SOURCE_FIXED_CARD
    assert caller.calls == []


@pytest.mark.asyncio
async def test_unavailable_subagent_falls_back_to_fixed_card(monkeypatch):
    async def failing(*, slug, message, tool_call_id, continue_thread_id=None):
        raise RuntimeError("子智能体不可用")

    organized = await _organize(monkeypatch, failing)

    assert organized["answer_source"] == ANSWER_SOURCE_FIXED_CARD
    assert organized["answer_verified"] is True


@pytest.mark.asyncio
async def test_fabricated_number_is_blocked_before_review(monkeypatch):
    caller = Caller([_draft(FABRICATED), _draft(GROUNDED)], [_verdict(True)])

    organized = await _organize(monkeypatch, caller)

    assert organized["answer_source"] == ANSWER_SOURCE_MODEL
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG, ORGANIZER_SLUG, REVIEWER_SLUG]
    assert "12" in [call for call in caller.calls if call["slug"] == ORGANIZER_SLUG][1]["message"]


@pytest.mark.asyncio
async def test_review_failure_then_user_rejects_falls_back(monkeypatch):
    caller = Caller([_draft(IMPRECISE)], [_verdict(False)] * 3)
    monkeypatch.setattr(
        tool_module,
        "interrupt",
        lambda payload: {"power_spec_answer_acceptance": "reject"},
    )

    organized = await _organize(monkeypatch, caller)

    assert organized["answer_source"] == ANSWER_SOURCE_FIXED_CARD


@pytest.mark.asyncio
async def test_review_failure_then_user_accepts_marks_unverified(monkeypatch):
    caller = Caller([_draft(IMPRECISE)] * 3, [_verdict(False)] * 3)
    payloads: list[dict] = []

    def accept(payload):
        payloads.append(payload)
        return {"power_spec_answer_acceptance": {"selected": ["accept"]}}

    organized = await _organize(monkeypatch, caller, ask_acceptance=accept)

    assert organized["answer_source"] == ANSWER_SOURCE_MODEL
    assert organized["answer_verified"] is False
    assert IMPRECISE in organized["answer_markdown"]
    assert payloads[0]["source"] == "power_spec_answer_acceptance"
    assert payloads[0]["attempts"] == 3
    assert "事实错误" in payloads[0]["questions"][0]["question"]


@pytest.mark.asyncio
async def test_confirmed_intent_path_returns_organized_answer(monkeypatch):
    caller = Caller([_draft(GROUNDED)], [_verdict(True)])

    async def visible(_context):
        return [{"kb_id": "kb1"}]

    class Config:
        query_options = {"reranker_model": "local:reranker"}

    class Executor:
        async def aquery(self, *_args, **_kwargs):
            return [
                {
                    "content": RAW_BODY,
                    "rerank_score": 0.7,
                    "metadata": {"file_id": "file-1", "chunk_id": "chunk-1"},
                }
            ]

    class KnowledgeBase:
        async def get_kb_config(self, _kb_id):
            return Config()

        async def get_kb_executor(self, _kb_id):
            return Executor()

    class Repository:
        async def list_power_spec_extractions(self, _db, *, kb_id):
            return {
                "file-1": {
                    "standard_no": "DL/T596—2021",
                    "standard_name": "DL/T 596—2021",
                    "version": "2021",
                    "status": "现行",
                    "clauses": [{"clause_no": "6.1", "clause_title": "油浸式电力变压器", "clause_body": RAW_BODY}],
                }
            }

    async def fake_intent(query_text, **_kwargs):
        return parse_power_spec_query(query_text)

    monkeypatch.setattr(tool_module, "resolve_visible_knowledge_bases_for_context", visible)
    monkeypatch.setattr(tool_module, "knowledge_base", KnowledgeBase())
    monkeypatch.setattr(tool_module, "PowerSpecRepository", Repository)
    monkeypatch.setattr(tool_module, "extract_power_spec_intent", fake_intent)
    monkeypatch.setattr(tool_module, "build_subagent_caller", lambda _context: caller)
    monkeypatch.setattr(tool_module, "pg_manager", _SessionStub())

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=True, runtime=Runtime())

    assert isinstance(result, dict)
    assert result["answer_source"] == ANSWER_SOURCE_MODEL
    assert result["answer_verified"] is True
    assert result["evidence_found"] is True


@pytest.mark.asyncio
async def test_no_evidence_still_returns_message(monkeypatch):
    class KnowledgeBase:
        async def get_kb_config(self, _kb_id):
            return type("Config", (), {"query_options": {}})()

    async def visible(_context):
        return [{"kb_id": "kb1"}]

    async def fake_intent(query_text, **_kwargs):
        return parse_power_spec_query(query_text)

    monkeypatch.setattr(tool_module, "resolve_visible_knowledge_bases_for_context", visible)
    monkeypatch.setattr(tool_module, "knowledge_base", KnowledgeBase())
    monkeypatch.setattr(tool_module, "extract_power_spec_intent", fake_intent)

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=True, runtime=Runtime())

    assert result == NO_EVIDENCE_MESSAGE
