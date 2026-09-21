from __future__ import annotations

import os
from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from yuxi.agents.backends.knowledge_base_backend import resolve_visible_knowledge_bases_for_context
from yuxi.agents.toolkits.registry import tool
from yuxi.config.runtime import knowledge_capability_enabled
from yuxi.knowledge.power_spec.answer import build_subagent_caller, produce_power_spec_answer
from yuxi.knowledge.power_spec.contracts import PowerSpecQuery
from yuxi.knowledge.power_spec.ingestion import is_power_spec_metadata_ready
from yuxi.knowledge.power_spec.intent import (
    INTENT_ATTEMPTS_EXHAUSTED_MESSAGE,
    MAX_INTENT_ATTEMPTS,
    PowerSpecIntentModelCall,
    build_confirmation_payload,
    build_correction_payload,
    build_intent_plan,
    extract_correction_text,
    extract_power_spec_intent,
    parse_intent_confirmation,
)
from yuxi.knowledge.power_spec.retrieval import (
    PowerSpecSearchService,
    adapt_vector_results,
    review_clause_metadata,
    verify_evidence_projection,
)
from yuxi.knowledge.runtime import knowledge_base
from yuxi.repositories.power_spec_repository import PowerSpecRepository
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils import logger


POWER_SPEC_RETRIEVAL_OPTIONS = {
    "recall_top_k": 30,
    "bm25_top_k": 30,
    "graph_top_k": 20,
    "final_top_k": 20,
}
NO_EVIDENCE_MESSAGE = "未检索到资料"
KNOWLEDGE_BASE_NOT_READY_MESSAGE = "电力规范知识库尚未完成条款级重建"
POWER_SPEC_KEYWORD_QUERY_TERMS = int(os.getenv("POWER_SPEC_KEYWORD_QUERY_TERMS", "8"))
POWER_SPEC_CLAUSE_REVIEW_ENABLED = os.getenv("POWER_SPEC_CLAUSE_REVIEW_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
POWER_SPEC_INTENT_ENABLED = os.getenv("POWER_SPEC_INTENT_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
POWER_SPEC_INTENT_TIMEOUT_SECONDS = float(os.getenv("POWER_SPEC_INTENT_TIMEOUT", "300"))
POWER_SPEC_INTENT_MAX_TOKENS = int(os.getenv("POWER_SPEC_INTENT_MAX_TOKENS", "65536"))


class PowerSpecSearchInput(BaseModel):
    """电力规范检索 Tool 输入。"""

    kb_id: str | None = Field(default=None, description="可选；未提供时仅在当前 Agent 恰好可见一个知识库时自动选择")
    query_text: str = Field(description="自然语言规范检索问题")
    confirmed_intent: bool = Field(default=False, description="用户是否确认了工具返回的意图识别结果")


@tool(
    category="knowledge",
    tags=["电力规范", "知识图谱", "规范检索"],
    display_name="检索电力规范",
    args_schema=PowerSpecSearchInput,
)
async def search_power_spec(
    query_text: str,
    kb_id: str | None = None,
    confirmed_intent: bool = False,
    runtime: ToolRuntime = None,
) -> dict[str, Any] | str:
    """检索现行电力规范并返回可验证的条款证据。"""

    if not knowledge_capability_enabled():
        return "当前运行模式未启用知识库能力"
    if not query_text.strip():
        return "请提供查询内容"

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return "无法获取当前用户知识库权限"
    cached_result = getattr(context, "_power_spec_search_result", None)
    if cached_result is not None:
        return cached_result
    visible_kbs = await resolve_visible_knowledge_bases_for_context(context)
    if not kb_id:
        if len(visible_kbs) != 1:
            return NO_EVIDENCE_MESSAGE
        kb_id = str(visible_kbs[0].get("kb_id") or "")
    if not kb_id.strip():
        return NO_EVIDENCE_MESSAGE
    if not any(str(item.get("kb_id")) == kb_id for item in visible_kbs):
        return NO_EVIDENCE_MESSAGE

    if confirmed_intent:
        query = await extract_power_spec_intent(query_text, model_call=_build_intent_model_call(context))
    else:
        query = await _confirm_intent_with_user(query_text, context=context)
        if isinstance(query, str):
            return query
    if query.clarification_required:
        return {
            "query": query.model_dump(mode="json"),
            "clarification": query.clarification_message,
        }

    config = await knowledge_base.get_kb_config(kb_id)
    reranker_model = config.query_options.get("reranker_model")
    if not reranker_model:
        return NO_EVIDENCE_MESSAGE

    executor = await knowledge_base.get_kb_executor(kb_id)
    async with pg_manager.get_async_session_context() as db:
        power_spec_repository = PowerSpecRepository()
        metadata_by_file_id = await power_spec_repository.list_power_spec_extractions(db, kb_id=kb_id)

    raw_results: list[dict[str, Any]] | None = None
    evidence_cache = None
    knowledge_base_not_ready = False

    async def load_evidence():
        nonlocal raw_results, evidence_cache, knowledge_base_not_ready
        if evidence_cache is not None:
            return evidence_cache
        raw_results = await _retrieve_for_query(executor, config, kb_id, query, reranker_model)
        ready_file_ids = {
            file_id for file_id, metadata in metadata_by_file_id.items() if is_power_spec_metadata_ready(metadata)
        }
        filtered_results = [
            item for item in raw_results if str((item.get("metadata") or {}).get("file_id") or "") in ready_file_ids
        ]
        knowledge_base_not_ready = not filtered_results and bool(raw_results)
        evidence_cache = adapt_vector_results(filtered_results, metadata_by_file_id, query.query)
        return evidence_cache

    async def graph_search(_query):
        evidence = await load_evidence()
        return [item for item in evidence if item.metadata.get("graph_score") is not None]

    async def vector_search(_query_text):
        return await load_evidence()

    result = await PowerSpecSearchService(graph_search, vector_search).search(query)
    if knowledge_base_not_ready:
        result_payload = KNOWLEDGE_BASE_NOT_READY_MESSAGE
        setattr(context, "_power_spec_search_result", result_payload)
        return result_payload
    if result.no_result:
        setattr(context, "_power_spec_search_result", NO_EVIDENCE_MESSAGE)
        return NO_EVIDENCE_MESSAGE
    verification = verify_evidence_projection(result.evidence)
    if not verification.valid:
        setattr(context, "_power_spec_search_result", NO_EVIDENCE_MESSAGE)
        return NO_EVIDENCE_MESSAGE

    reviewed = await _review_clause_metadata(context, result.evidence)
    organized = await produce_power_spec_answer(
        query=query,
        evidences=reviewed,
        call_subagent=build_subagent_caller(context),
        tool_call_id=getattr(runtime, "tool_call_id", None),
        ask_acceptance=interrupt,
    )
    retrieval_mode = (
        "graph+vector+rerank"
        if any(item.metadata.get("graph_score") is not None for item in reviewed)
        else "vector+rerank"
    )
    result_payload = {
        "status": "ok",
        "evidence_found": True,
        "spec_results": organized["items"],
        "answer_markdown": organized["answer_markdown"],
        "retrieval_mode": retrieval_mode,
        "evidence_verified": True,
        "answer_source": organized["answer_source"],
        "answer_verified": organized["answer_verified"],
    }
    setattr(context, "_power_spec_search_result", result_payload)
    return result_payload


async def _review_clause_metadata(context: Any, evidence: list[Any]) -> list[Any]:
    """由模型校对条款编号与标题，只应用通过代码校验的字符级修复。"""

    if not POWER_SPEC_CLAUSE_REVIEW_ENABLED or not getattr(context, "model", None):
        return evidence

    async def model_call(prompt: str) -> str:
        model = _select_context_model(context, max_tokens=POWER_SPEC_INTENT_MAX_TOKENS)
        response = await model.call(prompt, stream=False)
        return getattr(response, "content", "") or ""

    outcome = await review_clause_metadata(evidence, model_call=model_call)
    return outcome.evidence


async def _retrieve_for_query(
    executor: Any,
    config: Any,
    kb_id: str,
    query: PowerSpecQuery,
    reranker_model: str,
) -> list[dict[str, Any]]:
    """按模型给出的问题与关键词召回，多路结果按 chunk 合并。

    模型已经判断过应该用什么词检索，因此除重述后的完整问题外，再用关键词召回一路，
    弥补长问题在稠密检索中语义被稀释的情况。
    """

    base = await executor.aquery(
        query.query,
        kb_id,
        config=config,
        agent_call=True,
        use_graph_retrieval=False,
        use_reranker=True,
        reranker_model=reranker_model,
        **POWER_SPEC_RETRIEVAL_OPTIONS,
    )
    keyword_query = _keyword_query_text(query)
    if not keyword_query:
        return base
    try:
        keyword_results = await executor.aquery(
            keyword_query,
            kb_id,
            config=config,
            agent_call=True,
            use_graph_retrieval=False,
            use_reranker=True,
            reranker_model=reranker_model,
            **POWER_SPEC_RETRIEVAL_OPTIONS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"关键词召回失败，仅使用问题召回: {exc}")
        return base
    logger.info(f"电力规范多路召回：问题 {len(base)} 条，关键词「{keyword_query}」{len(keyword_results)} 条")
    return _merge_results(base, keyword_results)


def _keyword_query_text(query: PowerSpecQuery) -> str:
    """把模型给出的关键词拼成召回语句，没有关键词时返回空串。"""

    terms = [term for term in query.normalized_terms if term.strip()]
    if not terms:
        return ""
    return " ".join(terms[:POWER_SPEC_KEYWORD_QUERY_TERMS])


def _merge_results(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 chunk 身份合并两路召回，保留重排分数更高的一条。"""

    merged: dict[str, dict[str, Any]] = {}
    for item in [*base, *extra]:
        key = _result_identity(item)
        current = merged.get(key)
        if current is None or float(item.get("rerank_score") or 0.0) > float(current.get("rerank_score") or 0.0):
            merged[key] = item
    return list(merged.values())


def _result_identity(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    chunk_id = str(metadata.get("chunk_id") or item.get("chunk_id") or item.get("id") or "")
    file_id = str(metadata.get("file_id") or item.get("file_id") or "")
    if chunk_id:
        return f"{file_id}#{chunk_id}"
    return f"{file_id}#{hash(str(item.get('content') or ''))}"


async def _confirm_intent_with_user(query_text: str, *, context: Any) -> PowerSpecQuery | str:
    """与用户确认意图；否定时按用户修正重新识别，超过上限返回提示文本。

    LangGraph 每次 resume 都会重新执行整个节点，因此先前轮次的识别会重跑。
    意图识别使用 temperature=0，使展示给用户的计划与确认后实际使用的计划一致。
    """

    model_call = _build_intent_model_call(context)
    feedback: str | None = None
    previous: PowerSpecQuery | None = None
    for attempt in range(1, MAX_INTENT_ATTEMPTS + 1):
        query = await extract_power_spec_intent(
            query_text,
            model_call=model_call,
            previous=previous,
            feedback=feedback,
        )
        intent_plan = build_intent_plan(query)
        intent_plan["attempt"] = attempt
        setattr(context, "_power_spec_intent_plan", intent_plan)
        decision, correction = parse_intent_confirmation(interrupt(build_confirmation_payload(intent_plan)))
        if decision == "confirm":
            return query
        if decision == "invalid" or not correction:
            correction = extract_correction_text(interrupt(build_correction_payload(intent_plan)))
        if not correction:
            return INTENT_ATTEMPTS_EXHAUSTED_MESSAGE
        previous = query
        feedback = correction
    return INTENT_ATTEMPTS_EXHAUSTED_MESSAGE


def _build_intent_model_call(context: Any) -> PowerSpecIntentModelCall | None:
    """构造意图识别模型调用；未启用或上下文没有模型标识时返回 None 以回退规则解析。"""

    if not POWER_SPEC_INTENT_ENABLED:
        return None

    async def model_call(prompt: str) -> str:
        model = _select_context_model(context, max_tokens=POWER_SPEC_INTENT_MAX_TOKENS)
        response = await model.call(prompt, stream=False)
        return getattr(response, "content", "") or ""

    return model_call if getattr(context, "model", None) else None


def _select_context_model(context: Any, *, max_tokens: int):
    """按当前 Agent 的模型标识构造聊天模型，关闭思考并固定随机性。

    服务把输入与输出一起计入 `max_model_len`，因此输出额度必须为提示词留出空间，
    超过上限的请求会被拒绝而不是截断。
    """

    from yuxi.models.chat import select_model

    return select_model(
        model_spec=str(context.model),
        timeout=POWER_SPEC_INTENT_TIMEOUT_SECONDS,
        model_params={
            "max_tokens": max_tokens,
            "temperature": 0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
    )
