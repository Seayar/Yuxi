from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from yuxi.agents.backends.knowledge_base_backend import resolve_visible_knowledge_bases_for_context
from yuxi.agents.thinking import ThinkingEmitter
from yuxi.agents.toolkits.registry import tool
from yuxi.knowledge.power_spec.answer import build_subagent_caller, produce_power_spec_answer
from yuxi.knowledge.power_spec.contracts import (
    INTENT_ATTEMPTS_EXHAUSTED_MESSAGE,
    INTENT_PLAN_CHANGED_MESSAGE,
    KNOWLEDGE_BASE_NOT_READY_MESSAGE,
    NO_EVIDENCE_MESSAGE,
    POWER_SPEC_AGENT_SLUG,
    POWER_SPEC_THINKING_STEPS,
    PowerSpecQuery,
)
from yuxi.knowledge.power_spec.ingestion import is_power_spec_metadata_ready
from yuxi.knowledge.power_spec.intent import (
    MAX_INTENT_ATTEMPTS,
    PowerSpecIntentModelCall,
    build_confirmation_payload,
    build_correction_payload,
    build_intent_plan,
    extract_correction_text,
    extract_power_spec_intent,
    parse_confirmation_token,
    parse_intent_confirmation,
    plan_token,
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
POWER_SPEC_KEYWORD_QUERY_TERMS = int(os.getenv("POWER_SPEC_KEYWORD_QUERY_TERMS", "8"))
POWER_SPEC_CLAUSE_REVIEW_ENABLED = os.getenv("POWER_SPEC_CLAUSE_REVIEW_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
POWER_SPEC_INTENT_ENABLED = os.getenv("POWER_SPEC_INTENT_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
POWER_SPEC_INTENT_TIMEOUT_SECONDS = float(os.getenv("POWER_SPEC_INTENT_TIMEOUT", "300"))
# 输出额度与提示词共享服务端 max_model_len（当前 20480），需为提示词留出余量；
# 超过上限的请求会被拒绝而不是截断。
POWER_SPEC_INTENT_MAX_TOKENS = int(os.getenv("POWER_SPEC_INTENT_MAX_TOKENS", "4096"))


class PowerSpecSearchInput(BaseModel):
    """电力规范检索 Tool 输入。"""

    kb_id: str | None = Field(default=None, description="可选；未提供时仅在当前 Agent 恰好可见一个知识库时自动选择")
    query_text: str = Field(description="自然语言规范检索问题")


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
    """检索现行电力规范并返回可验证的条款证据。

    ``confirmed_intent`` 不在 ``PowerSpecSearchInput`` 里：模型无法通过工具参数跳过确认卡片，
    确认结论只来自 ``interrupt`` 返回的用户选择；该形参仅供测试模拟已确认的用户动作。
    """

    if not query_text.strip():
        return "请提供查询内容"

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return "无法获取当前用户知识库权限"
    cached_result = getattr(context, "_power_spec_search_result", None)
    if cached_result is not None:
        return cached_result

    emitter = ThinkingEmitter(agent_slug=POWER_SPEC_AGENT_SLUG, steps=POWER_SPEC_THINKING_STEPS)
    emitter.emit("parse_query", "started", content="正在解析查询意图。")

    if confirmed_intent:
        query = await extract_power_spec_intent(query_text, model_call=_build_intent_model_call(context))
    else:
        emitter.emit(
            "parse_query",
            "waiting",
            content="等待用户确认查询意图。",
            data={"needs_confirmation": True},
        )
        query = await _confirm_intent_with_user(query_text, context=context)
        if isinstance(query, str):
            emitter.emit(
                "parse_query",
                "failed",
                content=query,
                error={"code": "intent_attempts_exhausted", "message": query},
            )
            return query
    if query.clarification_required:
        emitter.emit(
            "parse_query",
            "waiting",
            content=query.clarification_message or "请补充查询信息。",
            data={"clarification": query.clarification_message, "needs_confirmation": True},
        )
        return {
            "query": query.model_dump(mode="json"),
            "clarification": query.clarification_message,
            "needs_clarification": True,
            "thinking_steps": emitter.snapshot(),
        }
    emitter.emit(
        "parse_query",
        "completed",
        content=_intent_summary(query),
        data=_intent_data(query),
    )

    emitter.emit("retrieve_kb", "started", content="正在检索规程标准知识库。")
    visible_kbs = await resolve_visible_knowledge_bases_for_context(context)
    if not kb_id:
        if len(visible_kbs) != 1:
            emitter.emit(
                "retrieve_kb",
                "failed",
                content=NO_EVIDENCE_MESSAGE,
                error={"code": "kb_unavailable", "message": "未找到唯一可见知识库"},
            )
            return NO_EVIDENCE_MESSAGE
        kb_id = str(visible_kbs[0].get("kb_id") or "")
    if not kb_id.strip() or not any(str(item.get("kb_id")) == kb_id for item in visible_kbs):
        emitter.emit(
            "retrieve_kb",
            "failed",
            content=NO_EVIDENCE_MESSAGE,
            error={"code": "kb_unavailable", "message": "知识库不可见"},
        )
        return NO_EVIDENCE_MESSAGE

    config = await knowledge_base.get_kb_config(kb_id)
    reranker_model = config.query_options.get("reranker_model") or None
    degradation_warnings: list[str] = []
    if not reranker_model:
        # 重排是可选能力：没有它仍然检索，排序退化为召回分数，并把降级原因带进结果。
        logger.warning(f"知识库 {kb_id} 未配置 reranker_model，按召回分数排序")
        degradation_warnings.append("未配置重排模型，已按召回分数排序")

    executor = await knowledge_base.get_kb_executor(kb_id)
    async with pg_manager.get_async_session_context() as db:
        power_spec_repository = PowerSpecRepository()
        metadata_by_file_id = await power_spec_repository.list_power_spec_extractions(db, kb_id=kb_id)
    files_ready = sum(1 for metadata in metadata_by_file_id.values() if is_power_spec_metadata_ready(metadata))

    raw_results: list[dict[str, Any]] | None = None
    evidence_cache = None
    knowledge_base_not_ready = False

    async def load_evidence():
        nonlocal raw_results, evidence_cache, knowledge_base_not_ready
        if evidence_cache is not None:
            return evidence_cache
        raw_results = await _retrieve_for_query(
            executor,
            config,
            kb_id,
            query,
            reranker_model,
            degradation_warnings,
        )
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
    keyword_query = _keyword_query_text(query)
    retrieval_data = {
        "kb_id": kb_id,
        "files_ready": files_ready,
        "vector_count": len(raw_results or []),
        "keyword_count": len(keyword_query.split()) if keyword_query else 0,
        "candidate_count": len(result.evidence),
        "reranker": reranker_model or "none",
        "degraded": not reranker_model,
    }
    emitter.emit(
        "retrieve_kb",
        "completed",
        content=f"已召回 {retrieval_data['vector_count']} 条候选，进入条款定位。",
        data=retrieval_data,
    )

    if knowledge_base_not_ready:
        emitter.emit(
            "locate_clauses",
            "failed",
            content=KNOWLEDGE_BASE_NOT_READY_MESSAGE,
            error={"code": "kb_not_ready", "message": KNOWLEDGE_BASE_NOT_READY_MESSAGE},
        )
        setattr(context, "_power_spec_search_result", KNOWLEDGE_BASE_NOT_READY_MESSAGE)
        return KNOWLEDGE_BASE_NOT_READY_MESSAGE
    if result.no_result:
        emitter.emit(
            "locate_clauses",
            "failed",
            content=NO_EVIDENCE_MESSAGE,
            error={"code": "no_evidence", "message": NO_EVIDENCE_MESSAGE},
        )
        setattr(context, "_power_spec_search_result", NO_EVIDENCE_MESSAGE)
        return NO_EVIDENCE_MESSAGE

    emitter.emit("locate_clauses", "started", content="正在校验条款证据。")
    verification = verify_evidence_projection(result.evidence)
    if not verification.valid:
        emitter.emit(
            "locate_clauses",
            "failed",
            content=NO_EVIDENCE_MESSAGE,
            error={"code": "evidence_unverified", "message": "证据投影校验失败"},
        )
        setattr(context, "_power_spec_search_result", NO_EVIDENCE_MESSAGE)
        return NO_EVIDENCE_MESSAGE

    reviewed, review_fixes = await _review_clause_metadata(context, result.evidence)
    emitter.emit(
        "locate_clauses",
        "completed",
        content=f"已定位 {len(reviewed)} 条可回溯条款。",
        data={
            "evidence_count": len(reviewed),
            "verified": True,
            "review_fixes": review_fixes,
            "warnings": list(degradation_warnings),
        },
    )

    emitter.emit("generate_result", "started", content="正在生成规范条款卡片。")
    try:
        organized = await produce_power_spec_answer(
            query=query,
            evidences=reviewed,
            call_subagent=build_subagent_caller(context),
            call_compose=_context_model_call(context),
            tool_call_id=getattr(runtime, "tool_call_id", None),
        )
    except Exception as exc:  # noqa: BLE001
        emitter.emit(
            "generate_result",
            "failed",
            content="生成检索结果失败。",
            error={"code": "generate_result_failed", "message": str(exc)},
        )
        raise
    retrieval_mode = ("graph+" if any(item.metadata.get("graph_score") is not None for item in reviewed) else "") + (
        "vector+rerank" if reranker_model else "vector"
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
        "warnings": degradation_warnings,
        "thinking_steps": emitter.snapshot(),
    }
    emitter.emit(
        "generate_result",
        "completed",
        content=f"已生成 {len(organized['items'])} 张规范条款卡片。",
        data={
            "card_count": len(organized["items"]),
            "answer_source": organized["answer_source"],
            "answer_verified": organized["answer_verified"],
        },
    )
    result_payload["thinking_steps"] = emitter.snapshot()
    setattr(context, "_power_spec_search_result", result_payload)
    return result_payload


def _intent_summary(query: PowerSpecQuery) -> str:
    parts = [query.intent]
    if query.device_name or query.device_type:
        parts.append(query.device_name or query.device_type or "")
    if query.voltage_level:
        parts.append(query.voltage_level)
    return "识别为" + "，".join(part for part in parts if part) + "问题。"


def _intent_data(query: PowerSpecQuery) -> dict[str, Any]:
    return {
        "query_type": query.intent,
        "device": query.device_name,
        "device_type": query.device_type,
        "voltage": query.voltage_level,
        "dimensions": list(query.answer_dimensions),
        "keywords": list(query.normalized_terms),
        "needs_confirmation": False,
        "clarification": query.clarification_message,
    }


async def _review_clause_metadata(context: Any, evidence: list[Any]) -> tuple[list[Any], int]:
    """由模型校对条款编号与标题，只应用通过代码校验的字符级修复；返回修复条数。"""

    model_call = _context_model_call(context)
    if not POWER_SPEC_CLAUSE_REVIEW_ENABLED or model_call is None:
        return evidence, 0

    outcome = await review_clause_metadata(evidence, model_call=model_call)
    return outcome.evidence, len(outcome.applied)


async def _retrieve_for_query(
    executor: Any,
    config: Any,
    kb_id: str,
    query: PowerSpecQuery,
    reranker_model: str | None,
    degradation_warnings: list[str],
) -> list[dict[str, Any]]:
    """按模型给出的问题与关键词召回，多路结果按 chunk 合并。

    模型已经判断过应该用什么词检索，因此除重述后的完整问题外，再用关键词召回一路，
    弥补长问题在稠密检索中语义被稀释的情况。未配置重排模型时两路都关闭重排。
    """

    rerank_kwargs = (
        {"use_reranker": True, "reranker_model": reranker_model} if reranker_model else {"use_reranker": False}
    )
    base = await executor.aquery(
        query.query,
        kb_id,
        config=config,
        agent_call=True,
        use_graph_retrieval=False,
        **rerank_kwargs,
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
            **rerank_kwargs,
            **POWER_SPEC_RETRIEVAL_OPTIONS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"关键词召回失败，仅使用问题召回: {exc}")
        # 可选召回路径降级必须可观察：结果里带上降级原因，不能只留日志。
        degradation_warnings.append(f"关键词召回失败，已仅使用问题召回: {exc}")
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
    """按 chunk 身份合并两路召回，并标出「只来自关键词路」的条目。

    两路的 rerank 分数是各自针对不同查询算出来的，不可直接比较：关键词路用模型给的
    泛化词检索（如「操作顺序」「检查项目」），会让语义相邻的条款拿到高分，进而在最终
    证据名额里挤掉真正回答问题的条款。实测「10kV 开关柜装设接地线应先接哪端」一题，
    问题路第一位就是正确条款（DL/T 409 6.4.3「应先装接地端，后装导线端」），最终输出
    却变成「操作顺序」类的无关内容。

    但关键词路也有真实价值（关键词具有判别性时它能召回问题路漏掉的正确条款），所以
    它独有
    的条目仍然保留，只是分数被压到不高于问题路最低分——可进候选，不得占据问题路排名
    之上。两路都命中时保留问题路的那份，不拿关键词路的分数改写问题路排名。
    """

    base_scores = sorted(float(item.get("rerank_score") or item.get("score") or 0.0) for item in base)
    floor = min(base_scores) if base_scores else 0.0

    merged: dict[str, dict[str, Any]] = {}
    for item in base:
        merged[_result_identity(item)] = item
    for item in extra:
        key = _result_identity(item)
        if key in merged:
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        merged[key] = {
            **item,
            "metadata": {**metadata, "keyword_only": True},
            "rerank_score": min(float(item.get("rerank_score") or item.get("score") or 0.0), floor),
        }
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
        answer = interrupt(build_confirmation_payload(intent_plan))
        decision, correction = parse_intent_confirmation(answer)
        if decision == "confirm":
            # resume 会重新识别意图；用户确认的计划指纹随选项值持久化在 checkpoint 里，
            # 重算结果与它不一致（或指纹缺失、无法证明确认过哪份计划）时拒绝执行。
            if parse_confirmation_token(answer) != plan_token(intent_plan):
                return INTENT_PLAN_CHANGED_MESSAGE
            return query
        if decision == "invalid" or not correction:
            correction = extract_correction_text(interrupt(build_correction_payload(intent_plan)))
        if not correction:
            return INTENT_ATTEMPTS_EXHAUSTED_MESSAGE
        previous = query
        feedback = correction
    return INTENT_ATTEMPTS_EXHAUSTED_MESSAGE


def _context_model_call(context: Any) -> Callable[[str], Awaitable[str]] | None:
    """构造按当前 Agent 模型执行的非流式单次调用；上下文没有模型标识时返回 None。

    意图识别、条款字段校对与最终整理共用这条路径，避免重复的模型构造代码。
    """

    if not getattr(context, "model", None):
        return None

    async def model_call(prompt: str) -> str:
        model = _select_context_model(context, max_tokens=POWER_SPEC_INTENT_MAX_TOKENS)
        response = await model.ainvoke(
            prompt,
            config={
                "metadata": {"yuxi_internal_model": True, "purpose": "power_spec_internal"},
                "tags": ["yuxi_internal_model"],
            },
        )
        return getattr(response, "content", "") or ""

    return model_call


def _build_intent_model_call(context: Any) -> PowerSpecIntentModelCall | None:
    """构造意图识别模型调用；未启用或上下文没有模型标识时返回 None 以回退规则解析。"""

    if not POWER_SPEC_INTENT_ENABLED:
        return None
    return _context_model_call(context)


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
