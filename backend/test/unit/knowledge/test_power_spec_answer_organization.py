"""电力规范答案的发布与降级判断。"""

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

# 原文中没有 12，用于验证数字检查能识别编造。
FABRICATED = "220kV 变压器的试验周期为 12 个月。"
GROUNDED = "220kV 变压器的试验周期为 6 个月。"
IMPRECISE = "所有变压器都应执行该试验。"
FINAL_BODY = "220kV 变压器每 6 个月执行一次。"


def _draft(body: str) -> str:
    """构造整理（或最终整理）返回的结构化 JSON。"""

    return json.dumps(
        {"clauses": [{"citation_key": "file-1#6.1", "body": body}]},
        ensure_ascii=False,
    )


def _opinions(*issues: dict) -> str:
    """构造审查子智能体应当返回的意见列表。"""

    return json.dumps({"issues": list(issues)}, ensure_ascii=False)


def _issue(kind: str = "事实错误", detail: str = "原文按电压等级区分周期") -> dict:
    return {"kind": kind, "detail": detail}


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


class Caller:
    """按 slug 回放子智能体输出，记录整理与审查收到的输入。"""

    def __init__(self, organizer_outputs, reviewer_outputs):
        self._organizer = list(organizer_outputs)
        self._reviewer = list(reviewer_outputs)
        self.calls = []

    async def __call__(self, *, slug, message, tool_call_id):
        self.calls.append({"slug": slug, "message": message, "tool_call_id": tool_call_id})
        if slug == ORGANIZER_SLUG:
            return SubagentReply(output=self._organizer.pop(0), thread_id="organizer-thread")
        return SubagentReply(output=self._reviewer.pop(0), thread_id="reviewer-thread")


class Composer:
    """记录最终整理收到的提示词，并按顺序回放模型输出。"""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._outputs.pop(0)


def _organize(caller, composer, *, tool_call_id="call-1"):
    """直接注入子智能体与最终整理入口，不再依赖 Tool 的运行上下文。"""

    return produce_power_spec_answer(
        query=parse_power_spec_query(QUESTION),
        evidences=[_evidence()],
        call_subagent=caller,
        call_compose=composer,
        tool_call_id=tool_call_id,
    )


@pytest.mark.asyncio
async def test_merged_answer_is_published():
    caller = Caller([_draft(GROUNDED)], [_opinions()])
    composer = Composer([_draft(FINAL_BODY)])

    organized = await _organize(caller, composer)

    assert organized["answer_source"] == ANSWER_SOURCE_MODEL
    assert organized["answer_verified"] is True
    assert "**规范名称**" in organized["answer_markdown"]
    assert _plain(organized["answer_markdown"]).find(FINAL_BODY) >= 0
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG, REVIEWER_SLUG]
    assert len(composer.prompts) == 1


@pytest.mark.asyncio
async def test_review_opinions_are_merged_and_only_marked_unverified():
    """审查提出意见时仍发布合并结果，只把 answer_verified 标为 False。"""

    caller = Caller([_draft(IMPRECISE)], [_opinions(_issue())])
    composer = Composer([_draft(FINAL_BODY)])

    organized = await _organize(caller, composer)

    assert organized["answer_source"] == ANSWER_SOURCE_MODEL
    assert organized["answer_verified"] is False
    assert _plain(organized["answer_markdown"]).find(FINAL_BODY) >= 0
    assert "原文按电压等级区分周期" in composer.prompts[0]
    assert len(caller.calls) == 2


@pytest.mark.asyncio
async def test_ungrounded_number_is_reported_to_final_compose():
    """负向：编造数字不再阻断发布，而是作为意见交给最终整理修正。"""

    caller = Caller([_draft(FABRICATED)], [_opinions()])
    composer = Composer([_draft(GROUNDED)])

    organized = await _organize(caller, composer)

    assert organized["answer_source"] == ANSWER_SOURCE_MODEL
    assert organized["answer_verified"] is False
    assert "12" in composer.prompts[0]
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG, REVIEWER_SLUG]


@pytest.mark.asyncio
async def test_missing_tool_call_id_falls_back_to_fixed_card():
    caller = Caller([_draft(GROUNDED)], [_opinions()])
    composer = Composer([_draft(FINAL_BODY)])

    organized = await _organize(caller, composer, tool_call_id=None)

    assert organized["answer_source"] == ANSWER_SOURCE_FIXED_CARD
    assert caller.calls == []
    assert composer.prompts == []


@pytest.mark.asyncio
async def test_unavailable_subagent_falls_back_to_fixed_card():
    async def failing(*, slug, message, tool_call_id):
        raise RuntimeError("子智能体不可用")

    organized = await _organize(failing, Composer([_draft(FINAL_BODY)]))

    assert organized["answer_source"] == ANSWER_SOURCE_FIXED_CARD
    assert organized["answer_verified"] is True


@pytest.mark.asyncio
async def test_unavailable_compose_falls_back_to_fixed_card():
    caller = Caller([_draft(GROUNDED)], [_opinions()])

    async def failing_compose(_prompt: str) -> str:
        raise RuntimeError("模型不可用")

    organized = await _organize(caller, failing_compose)

    assert organized["answer_source"] == ANSWER_SOURCE_FIXED_CARD
    assert organized["answer_verified"] is True


@pytest.mark.asyncio
async def test_confirmed_intent_path_returns_organized_answer(monkeypatch):
    caller = Caller([_draft(GROUNDED)], [_opinions()])
    composer = Composer([_draft(FINAL_BODY)])

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
    # 条款字段校对与最终整理共用同一个模型调用构造器；这里只验证发布路径。
    monkeypatch.setattr(tool_module, "POWER_SPEC_CLAUSE_REVIEW_ENABLED", False)
    monkeypatch.setattr(tool_module, "_context_model_call", lambda _context: composer)
    monkeypatch.setattr(tool_module, "pg_manager", _SessionStub())

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=True, runtime=Runtime())

    assert isinstance(result, dict)
    assert result["answer_source"] == ANSWER_SOURCE_MODEL
    assert result["answer_verified"] is True
    assert result["evidence_found"] is True
    assert set(result["spec_results"][0]) == {"standard_name", "standard_no", "clause_no", "clause_body"}


@pytest.mark.asyncio
async def test_missing_reranker_keeps_results_and_reports_warning(monkeypatch):
    """重排是可选能力：没有重排模型时仍返回卡片，并把降级原因写进 warnings。"""

    calls = []

    class Executor:
        async def aquery(self, *_args, **kwargs):
            calls.append(kwargs)
            return [
                {
                    "content": RAW_BODY,
                    "score": 0.82,
                    "metadata": {"file_id": "file-1", "chunk_id": "chunk-1"},
                }
            ]

    class KnowledgeBase:
        async def get_kb_config(self, _kb_id):
            return type("Config", (), {"query_options": {}})()

        async def get_kb_executor(self, _kb_id):
            return Executor()

    class Repository:
        async def list_power_spec_extractions(self, _db, *, kb_id):
            return {
                "file-1": {
                    "standard_no": "DL/T 596-2021",
                    "standard_name": "DL/T 596-2021",
                    "version": "2021",
                    "status": "现行",
                    "clauses": [{"clause_no": "6.1", "clause_title": "油浸式电力变压器", "clause_body": RAW_BODY}],
                }
            }

    async def visible(_context):
        return [{"kb_id": "kb1"}]

    async def fake_intent(query_text, **_kwargs):
        return parse_power_spec_query(query_text)

    async def fake_answer(**_kwargs):
        return {
            "items": [
                {
                    "standard_name": "DL/T 596-2021",
                    "standard_no": "DL/T 596-2021",
                    "clause_no": "6.1",
                    "clause_body": RAW_BODY,
                }
            ],
            "answer_markdown": "### 规范卡片 1",
            "answer_source": ANSWER_SOURCE_FIXED_CARD,
            "answer_verified": True,
        }

    monkeypatch.setattr(tool_module, "resolve_visible_knowledge_bases_for_context", visible)
    monkeypatch.setattr(tool_module, "knowledge_base", KnowledgeBase())
    monkeypatch.setattr(tool_module, "PowerSpecRepository", Repository)
    monkeypatch.setattr(tool_module, "extract_power_spec_intent", fake_intent)
    monkeypatch.setattr(tool_module, "produce_power_spec_answer", fake_answer)
    monkeypatch.setattr(tool_module, "POWER_SPEC_CLAUSE_REVIEW_ENABLED", False)
    monkeypatch.setattr(tool_module, "pg_manager", _SessionStub())

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=True, runtime=Runtime())

    assert calls, "未配置重排模型时仍应发起检索"
    assert all(call["use_reranker"] is False for call in calls)
    assert result["warnings"] == ["未配置重排模型，已按召回分数排序"]
    assert result["spec_results"][0]["clause_no"] == "6.1"
    assert result["evidence_found"] is True


@pytest.mark.asyncio
async def test_no_evidence_still_returns_message(monkeypatch):
    class KnowledgeBase:
        async def get_kb_config(self, _kb_id):
            return type("Config", (), {"query_options": {"reranker_model": "local:reranker"}})()

        async def get_kb_executor(self, _kb_id):
            return Executor()

    class Executor:
        async def aquery(self, *_args, **_kwargs):
            return []

    class Repository:
        async def list_power_spec_extractions(self, _db, *, kb_id):
            return {}

    async def visible(_context):
        return [{"kb_id": "kb1"}]

    async def fake_intent(query_text, **_kwargs):
        return parse_power_spec_query(query_text)

    monkeypatch.setattr(tool_module, "resolve_visible_knowledge_bases_for_context", visible)
    monkeypatch.setattr(tool_module, "knowledge_base", KnowledgeBase())
    monkeypatch.setattr(tool_module, "PowerSpecRepository", Repository)
    monkeypatch.setattr(tool_module, "extract_power_spec_intent", fake_intent)
    monkeypatch.setattr(tool_module, "pg_manager", _SessionStub())

    result = await search_power_spec.coroutine(query_text=QUESTION, confirmed_intent=True, runtime=Runtime())

    assert result == NO_EVIDENCE_MESSAGE
