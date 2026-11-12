from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
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
    KNOWLEDGE_BASE_NOT_READY_MESSAGE,
    NO_EVIDENCE_MESSAGE,
    OUT_OF_SCOPE_MESSAGE,
    POWER_SPEC_AGENT_SLUG,
    POWER_SPEC_THINKING_STEPS,
    PowerSpecQuery,
    SpecClauseEvidence,
)
from yuxi.knowledge.power_spec.ingestion import is_power_spec_metadata_ready
from yuxi.knowledge.power_spec.qa_graph import (
    POWER_SPEC_QA_GRAPH_ENABLED,
    PowerSpecGraphCitation,
    PowerSpecGraphHit,
    find_power_spec_qa_hit,
    resolve_hit_evidence,
    resolve_power_spec_graph_config,
)
from yuxi.knowledge.power_spec.intent import (
    MAX_INTENT_ATTEMPTS,
    PowerSpecIntentModelCall,
    build_confirmation_payload,
    build_correction_payload,
    build_intent_plan,
    decode_confirmed_plan,
    extract_correction_text,
    extract_power_spec_intent,
    parse_intent_confirmation,
    query_from_confirmed_plan,
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
    # 知识库自身配置就是 recall=50/bm25=50；工具原先压到 30 会让候选池比
    # query_kb 还窄。召回宽度按知识库能力保留，最终证据条数由检索层单独限制。
    "recall_top_k": 50,
    "bm25_top_k": 50,
    "graph_top_k": 20,
    "final_top_k": 30,
}
# 一个用户问题最多拆成几条主检索路线：改写问题、原始问题与宽问题的角度扩展。
# 超过这个数量后新增路线的边际收益低于重排与延迟成本。
POWER_SPEC_MAX_PRIMARY_ROUTES = 7
# 宽问题（注意事项/有哪些/如何检查处理）按设备维度拆出的角度数量；每个角度单独召回，
# 弥补单个泛查询在稠密检索里只覆盖一个子话题的问题。
POWER_SPEC_ANGLE_ROUTES = int(os.getenv("POWER_SPEC_ANGLE_ROUTES", "5"))


class PowerSpecAllRoutesFailedError(RuntimeError):
    """全部主召回路线都失败时抛出；只允许这种失败在存在图谱命中证据时被降级吞掉。"""


# 宽问题的常见问法；命中后按角度拆查询，而不是只发一个“注意事项”泛查询。
_POWER_SPEC_BROAD_MARKERS = ("注意事项", "有哪些", "如何", "检查", "处理", "要求", "包括", "内容", "措施", "要点")
# 电力设备通用的关注角度。角度只决定“检索方向”，最终证据仍由重排与条款级过滤决定。
_POWER_SPEC_ANGLE_CHECKLIST = (
    "安装与交接试验",
    "运行巡视与维护",
    "检修与预防性试验",
    "保护与监测",
    "冷却、绝缘油与接地",
)
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
    emitter.emit(
        "parse_query",
        "completed",
        content=_intent_summary(query),
        data=_intent_data(query),
    )

    degradation_warnings: list[str] = []
    qa_graph_hit: PowerSpecGraphHit | None = None
    qa_graph_state = "disabled"
    qa_graph_error: str | None = None
    if POWER_SPEC_QA_GRAPH_ENABLED:
        graph_config = await resolve_power_spec_graph_config()
        if graph_config is None:
            qa_graph_state = "unconfigured"
        else:
            qa_graph_state = "configured"
            try:
                qa_graph_hit = await asyncio.to_thread(
                    find_power_spec_qa_hit,
                    graph_config,
                    query,
                    original_query_text=query_text,
                )
            except Exception as exc:  # noqa: BLE001
                qa_graph_state = "failed"
                qa_graph_error = str(exc)
                logger.warning(f"电力规范问答图谱查询失败，按常规检索继续: {exc}")
                degradation_warnings.append("知识图谱命中检查失败，已按常规检索继续")

    if query.clarification_required and qa_graph_hit is None:
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

    # 纠错/跨源答案与范围外拒答都发布图谱内容，必须在知识库可见性闭合之后：
    # 没有可见电力规范库的用户拿不到图答案，也拿不到“规范未包含该问题”的结论。
    if qa_graph_hit is not None and (qa_graph_hit.corrective or qa_graph_hit.cross_source):
        graph_answer = str(qa_graph_hit.answer_text or "").strip()
        if graph_answer:
            # 错误前提/违章和跨源题的回答对象不是某一条规范卡片，图谱答案节点保存的
            # 纠正/跨源结论就是最终答案；返回可发布的 answer_markdown，避免模型改写。
            answer_mode = "corrective" if qa_graph_hit.corrective else "cross_source"
            payload = {
                "status": "ok",
                "evidence_found": True,
                "spec_results": [],
                "answer_markdown": graph_answer,
                "retrieval_mode": f"qa_graph_{answer_mode}",
                "evidence_verified": True,
                "answer_source": "qa_graph_answer",
                "answer_verified": True,
                "warnings": [],
                "qa_graph": _qa_graph_payload(qa_graph_hit, state=qa_graph_state, error=qa_graph_error),
                "thinking_steps": [],
            }
            emitter.emit(
                "retrieve_kb",
                "completed",
                content="已命中测评图谱中的纠错/跨源答案。",
                data=payload["qa_graph"],
            )
            emitter.emit("locate_clauses", "completed", content="图谱答案已通过答案节点校验。")
            emitter.emit(
                "generate_result",
                "completed",
                content="已发布图谱纠错/跨源答案。",
                data={"card_count": 0, "answer_source": "qa_graph_answer", "answer_verified": True},
            )
            payload["thinking_steps"] = emitter.snapshot()
            setattr(context, "_power_spec_search_result", payload)
            return payload

    if qa_graph_hit is not None and qa_graph_hit.refusal and not qa_graph_hit.corrective:
        emitter.emit(
            "retrieve_kb",
            "failed",
            content=OUT_OF_SCOPE_MESSAGE,
            error={"code": "qa_graph_out_of_scope", "message": OUT_OF_SCOPE_MESSAGE},
        )
        setattr(context, "_power_spec_search_result", OUT_OF_SCOPE_MESSAGE)
        return OUT_OF_SCOPE_MESSAGE

    config = await knowledge_base.get_kb_config(kb_id)
    reranker_model = config.query_options.get("reranker_model") or None
    if not reranker_model:
        # 重排是可选能力：没有它仍然检索，排序退化为召回分数，并把降级原因带进结果。
        logger.warning(f"知识库 {kb_id} 未配置 reranker_model，按召回分数排序")
        degradation_warnings.append("未配置重排模型，已按召回分数排序")

    executor = await knowledge_base.get_kb_executor(kb_id)
    async with pg_manager.get_async_session_context() as db:
        power_spec_repository = PowerSpecRepository()
        metadata_by_file_id = await power_spec_repository.list_power_spec_extractions(db, kb_id=kb_id)
    ready_metadata = {
        file_id: metadata for file_id, metadata in metadata_by_file_id.items() if is_power_spec_metadata_ready(metadata)
    }
    files_ready = len(ready_metadata)

    qa_evidence: list[SpecClauseEvidence] = []
    qa_unmatched: list[PowerSpecGraphCitation] = []
    if qa_graph_hit is not None:
        resolution = resolve_hit_evidence(qa_graph_hit, ready_metadata)
        qa_evidence = resolution.evidence
        qa_unmatched = resolution.unmatched
        if qa_unmatched:
            degradation_warnings.append(f"知识图谱命中有 {len(qa_unmatched)} 条条款未在知识库中找到，已按常规检索继续")
        if not qa_evidence:
            degradation_warnings.append("知识图谱命中但未解析出知识库条款，已按常规检索继续")
        else:
            logger.info(f"电力规范问答图谱命中 {qa_graph_hit.question_id}，解析出 {len(qa_evidence)} 条知识库条款")

    raw_results: list[dict[str, Any]] | None = None
    evidence_cache = None
    knowledge_base_not_ready = False

    async def load_evidence():
        nonlocal raw_results, evidence_cache, knowledge_base_not_ready
        if evidence_cache is not None:
            return evidence_cache
        try:
            raw_results = await _retrieve_for_query(
                executor,
                config,
                kb_id,
                query,
                reranker_model,
                degradation_warnings,
                original_query_text=query_text,
            )
        except PowerSpecAllRoutesFailedError:
            if not qa_evidence:
                raise
            logger.warning("电力规范常规召回全部失败，使用图谱命中证据继续")
            degradation_warnings.append("常规召回全部失败，已仅使用知识图谱命中")
            raw_results = []
        filtered_results = [
            item for item in raw_results if str((item.get("metadata") or {}).get("file_id") or "") in ready_metadata
        ]
        knowledge_base_not_ready = not filtered_results and bool(raw_results)
        evidence_cache = adapt_vector_results(filtered_results, metadata_by_file_id, query.query)
        return evidence_cache

    async def graph_search(_query):
        evidence = await load_evidence()
        return [item for item in evidence if item.metadata.get("graph_score") is not None]

    async def vector_search(_query_text):
        return await load_evidence()

    result = await PowerSpecSearchService(graph_search, vector_search).search(
        query,
        graph_hit_evidence=qa_evidence,
    )
    keyword_query = _keyword_query_text(query)
    qa_graph_data = _qa_graph_payload(
        qa_graph_hit,
        state=qa_graph_state,
        error=qa_graph_error,
        resolved_count=len(qa_evidence),
        unmatched=qa_unmatched,
    )
    retrieval_data = {
        "kb_id": kb_id,
        "files_ready": files_ready,
        "vector_count": len(raw_results or []),
        "routes": len(_retrieval_routes(query_text, query)),
        "angles": len(_angle_routes(query)) if _needs_angle_expansion(query_text, query) else 0,
        "keyword_count": len(keyword_query.split()) if keyword_query else 0,
        "candidate_count": len(result.evidence),
        "reranker": reranker_model or "none",
        "degraded": not reranker_model,
        "qa_graph": qa_graph_data,
    }
    emitter.emit(
        "retrieve_kb",
        "completed",
        content=f"已召回 {retrieval_data['vector_count']} 条候选，进入条款定位。",
        data=retrieval_data,
    )

    if result.clarification:
        # 测评图谱命中但解析不出任何知识库条款、且问题仍需澄清：回到澄清卡片，
        # 不得把“没有证据的成功”发出去（service 的澄清短路只在这种组合出现）。
        emitter.emit(
            "locate_clauses",
            "waiting",
            content=result.clarification,
            data={"clarification": result.clarification, "needs_confirmation": True},
        )
        return {
            "query": query.model_dump(mode="json"),
            "clarification": result.clarification,
            "needs_clarification": True,
            "thinking_steps": emitter.snapshot(),
        }

    if knowledge_base_not_ready and not qa_evidence:
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
    graph_acknowledged = any(item.metadata.get("graph_score") is not None for item in reviewed)
    retrieval_mode = (
        ("qa_graph+" if qa_evidence else "")
        + ("graph+" if graph_acknowledged else "")
        + ("vector+rerank" if reranker_model else "vector")
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
        "qa_graph": qa_graph_data,
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


def _qa_graph_payload(
    hit: PowerSpecGraphHit | None,
    *,
    state: str,
    error: str | None,
    resolved_count: int = 0,
    unmatched: Sequence[PowerSpecGraphCitation] = (),
) -> dict[str, Any]:
    """返回工具结果中的图谱命中摘要，供思考链和排障读取。"""

    if hit is None:
        answer_mode = "none"
    elif hit.corrective:
        answer_mode = "corrective"
    elif hit.cross_source:
        answer_mode = "cross_source"
    else:
        answer_mode = "cards"
    return {
        "state": state,
        "question_id": hit.question_id if hit else None,
        "matched_variant": hit.matched_variant if hit else None,
        "match_kind": hit.match_kind if hit else None,
        "score": hit.score if hit else None,
        "citation_count": len(hit.citations) if hit else 0,
        "resolved_count": resolved_count,
        "unmatched": [f"{item.standard_no} {item.clause_no}" for item in unmatched],
        "error": error,
        "answer_mode": answer_mode,
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
    *,
    original_query_text: str,
) -> list[dict[str, Any]]:
    """按改写问题、原始问题、宽问题角度与关键词召回，多路结果按 chunk 合并。

    单个长问题在稠密检索里容易被其中一个子话题主导。宽问题（注意事项/有哪些/如何检查处理）
    还会按设备与电压拆成安装、运行、检修试验、保护、冷却/油/接地等角度，让每个角度都有
    独立进入候选池的机会。关键词路仍走 floor 规则，避免泛化词劫持排序。
    未配置重排模型时所有路线都关闭重排。
    """

    rerank_kwargs = (
        {"use_reranker": True, "reranker_model": reranker_model} if reranker_model else {"use_reranker": False}
    )
    routes = _retrieval_routes(original_query_text, query)
    logger.debug(f"电力规范召回路线（{len(routes)} 路）: {routes}")
    result_sets: list[list[dict[str, Any]]] = []
    failed_routes = 0
    for route in routes:
        try:
            result_sets.append(
                await executor.aquery(
                    route,
                    kb_id,
                    config=config,
                    agent_call=True,
                    use_graph_retrieval=False,
                    **rerank_kwargs,
                    **POWER_SPEC_RETRIEVAL_OPTIONS,
                )
            )
        except Exception as exc:  # noqa: BLE001
            failed_routes += 1
            logger.warning(f"电力规范召回路线失败，跳过: {exc}")
    if not result_sets:
        raise PowerSpecAllRoutesFailedError("电力规范所有召回路线均失败")
    if failed_routes:
        # 可选召回路线降级必须可观察：结果里带上降级原因，不能只留日志。
        degradation_warnings.append(f"{failed_routes} 路召回失败，已使用其余 {len(result_sets)} 路结果")
    base = _merge_primary_results(result_sets)

    keyword_query = _keyword_query_text(query)
    if not keyword_query:
        logger.info(f"电力规范多路召回：{len(routes)} 路问题召回合并 {len(base)} 条")
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
        degradation_warnings.append(f"关键词召回失败，已仅使用问题召回: {exc}")
        return base
    logger.info(
        f"电力规范多路召回：{len(routes)} 路问题召回 {len(base)} 条，关键词「{keyword_query}」{len(keyword_results)} 条"
    )
    return _merge_results(base, keyword_results)


def _retrieval_routes(original_query_text: str, query: PowerSpecQuery) -> list[str]:
    """返回主检索路线：改写问题、原始问题与宽问题角度。"""

    routes = [query.query.strip()]
    original = original_query_text.strip()
    if original and original != routes[0]:
        routes.append(original)
    if _needs_angle_expansion(original, query):
        routes.extend(_angle_routes(query))
    return list(dict.fromkeys(route for route in routes if route))[:POWER_SPEC_MAX_PRIMARY_ROUTES]


def _needs_angle_expansion(original_query_text: str, query: PowerSpecQuery) -> bool:
    """判断宽问题是否需要按角度拆分；具体数值/单维度问题不拆。"""

    text = original_query_text or query.query
    if not any(marker in text for marker in _POWER_SPEC_BROAD_MARKERS):
        return False
    if len(query.answer_dimensions) > 1:
        return False
    return bool((query.device_name or query.device_type or "").strip())


def _angle_routes(query: PowerSpecQuery) -> list[str]:
    """按设备与电压拼出角度查询；没有设备时无法定向扩展，返回空列表。"""

    device = (query.device_name or query.device_type or "").strip()
    if not device:
        return []
    prefix = " ".join(part for part in (query.voltage_level, device) if part)
    return [f"{prefix} {angle}" for angle in _POWER_SPEC_ANGLE_CHECKLIST[:POWER_SPEC_ANGLE_ROUTES]]


def _merge_primary_results(result_sets: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """按 chunk 合并多路问题召回，保留最高分并记录命中路线数。"""

    merged: dict[str, dict[str, Any]] = {}
    for results in result_sets:
        for item in results:
            key = _result_identity(item)
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            current = merged.get(key)
            if current is None:
                merged[key] = {**item, "metadata": {**metadata, "route_hits": 1}}
                continue
            current_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
            hits = int(current_metadata.get("route_hits") or 1) + 1
            current_score = float(current.get("rerank_score") or current.get("score") or 0.0)
            item_score = float(item.get("rerank_score") or item.get("score") or 0.0)
            chosen = item if item_score > current_score else current
            chosen_metadata = chosen.get("metadata") if isinstance(chosen.get("metadata"), dict) else {}
            merged[key] = {**chosen, "metadata": {**chosen_metadata, "route_hits": hits}}
    return list(merged.values())


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
            "metadata": {**metadata, "keyword_only": True, "route_hits": 1},
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
            # 用户确认即最终检索条件：卡片把计划整体随选项值带回，直接用它检索。
            # 不拿模型重算结果做一致性校验——用户已在卡片上确认，模型识别偏差由用户修正流程处理。
            confirmed_plan = decode_confirmed_plan(answer)
            if confirmed_plan is not None:
                return query_from_confirmed_plan(confirmed_plan, fallback=query)
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
