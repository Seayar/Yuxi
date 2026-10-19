"""故障溯源工具：结合用户描述与图片识别结果，沿故障知识图谱生成溯源报告。

工具只做权限、意图确认与行为链编排；意图识别、图谱查询与报告装配各自拥有模块，
跨模块流动的数据形状统一由 ``contracts`` 拥有。图片识别由前端经
``/api/fault/images`` 完成，工具只接收其结果文本，不再直接访问 YOLO。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from yuxi.agents.toolkits.registry import tool
from yuxi.config.options import fault_trace_opts
from yuxi.knowledge.fault_trace.answer import produce_fault_trace_answer
from yuxi.knowledge.fault_trace.contracts import (
    FAULT_GRAPH_NOT_CONFIGURED_MESSAGE,
    KG_GRAPH_UNAVAILABLE_MESSAGE,
    NO_FAULT_GRAPH_MESSAGE,
    FaultTraceQuery,
)
from yuxi.knowledge.fault_trace.graph_kg import (
    KgGraphClient,
    KgGraphConfig,
    KgGraphError,
    collect_kg_fault_evidence,
)
from yuxi.knowledge.fault_trace.intent import (
    MAX_INTENT_ATTEMPTS,
    apply_input_constraints,
    build_confirmation_payload,
    build_correction_payload,
    build_intent_plan,
    extract_correction_text,
    extract_fault_trace_intent,
    parse_intent_confirmation,
)
from yuxi.utils import logger

FAULT_TRACE_INTENT_ENABLED = os.getenv("FAULT_TRACE_INTENT_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
FAULT_TRACE_INTENT_TIMEOUT_SECONDS = float(os.getenv("FAULT_TRACE_INTENT_TIMEOUT", "300"))
# 输出额度与提示词共享服务端 max_model_len（当前 20480），需为提示词留出余量。
FAULT_TRACE_INTENT_MAX_TOKENS = int(os.getenv("FAULT_TRACE_INTENT_MAX_TOKENS", "4096"))


class FaultTraceInput(BaseModel):
    """故障溯源 Tool 输入。"""

    query_text: str = Field(description="用户对故障现象的文字/语音描述")
    image_result: str | None = Field(
        default=None,
        description="可选；/api/fault/images 返回的 YOLO 识别结果 JSON 字符串（fault/fault_type/fault_name）",
    )
    confirmed_intent: bool = Field(default=False, description="用户是否确认了工具返回的意图识别结果")


@tool(
    category="knowledge",
    tags=["故障溯源", "知识图谱"],
    display_name="故障溯源",
    args_schema=FaultTraceInput,
)
async def trace_fault(
    query_text: str,
    image_result: str | None = None,
    confirmed_intent: bool = False,
    runtime: ToolRuntime = None,
) -> dict[str, Any] | str:
    """结合用户描述与图片识别结果，沿故障知识图谱生成五段式溯源报告。

    图谱数据来自人工录入的独立 TuGraph ``kg`` 图（管理员配置 ``fault_trace_opts``），
    由 ``graph_kg`` 适配器按中文标签读取因果链与电气拓扑。
    """

    if not query_text.strip():
        return "请提供故障描述"

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return "无法获取当前用户的知识库权限"
    # 结果按故障描述缓存：interrupt 恢复后 LangGraph 会重放本轮工具调用，重放时
    # query_text 不变，缓存保证不重复查图；不同描述各自独立，不能共用一份结果。
    cache_key = query_text.strip()
    cached_results = getattr(context, "_fault_trace_results", None)
    if isinstance(cached_results, dict) and cache_key in cached_results:
        return cached_results[cache_key]

    config = await _resolve_kg_config()
    if config is None:
        return FAULT_GRAPH_NOT_CONFIGURED_MESSAGE

    image_payload = _parse_image_result(image_result)
    if confirmed_intent:
        query = await extract_fault_trace_intent(
            query_text,
            image_result=image_payload,
            model_call=_context_model_call(context),
        )
    else:
        query = await _confirm_fault_intent_with_user(
            query_text,
            image_result=image_payload,
            context=context,
        )
        if isinstance(query, str):
            return query
    if query.clarification_required:
        return {
            "query": query.model_dump(),
            "clarification": query.clarification_message,
        }

    client = KgGraphClient(config)
    try:
        evidence = await _collect_kg_evidence(client, query)
    except KgGraphError as exc:
        # 图谱宕机/查询失败与「管理员未配置」是两类故障，用户可行动性不同，分开投影。
        logger.warning(f"故障溯源图谱查询失败: {exc}")
        result_payload: dict[str, Any] | str = KG_GRAPH_UNAVAILABLE_MESSAGE
        _cache_result(context, cache_key, result_payload)
        return result_payload
    finally:
        client.close()

    if not evidence.nodes:
        result_payload = NO_FAULT_GRAPH_MESSAGE
        _cache_result(context, cache_key, result_payload)
        return result_payload

    result = await produce_fault_trace_answer(
        query=query,
        evidence=evidence,
        call_compose=_context_model_call(context),
    )
    _cache_result(context, cache_key, result)
    return result


def _cache_result(context: Any, cache_key: str, result: Any) -> None:
    """把工具结果按故障描述缓存到运行上下文，供 interrupt 恢复后的重放复用。"""

    cache = getattr(context, "_fault_trace_results", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(context, "_fault_trace_results", cache)
    cache[cache_key] = result


async def _confirm_fault_intent_with_user(
    query_text: str,
    *,
    image_result: dict[str, Any] | None,
    context: Any,
) -> FaultTraceQuery | str:
    """与用户确认意图；否定时按用户修正重新识别，超过上限返回提示文本。"""

    model_call = _context_model_call(context)
    feedback: str | None = None
    previous: FaultTraceQuery | None = None
    for attempt in range(1, MAX_INTENT_ATTEMPTS + 1):
        query = await extract_fault_trace_intent(
            query_text,
            image_result=image_result,
            model_call=model_call if FAULT_TRACE_INTENT_ENABLED else None,
            previous=previous,
            feedback=feedback,
        )
        query = apply_input_constraints(query)
        if query.clarification_required:
            return query
        intent_plan = build_intent_plan(query)
        intent_plan["attempt"] = attempt
        decision, correction = parse_intent_confirmation(interrupt(build_confirmation_payload(intent_plan)))
        if decision == "confirm":
            return query
        if decision == "invalid" or not correction:
            correction = extract_correction_text(interrupt(build_correction_payload(intent_plan)))
        if not correction:
            return "意图确认交互过多，未完成故障溯源"
        previous = query
        feedback = correction
    return "意图确认交互过多，未完成故障溯源"


def _parse_image_result(image_result: str | None) -> dict[str, Any] | None:
    """解析前端传入的图片识别结果；不合法时返回 None。"""

    if not image_result or not str(image_result).strip():
        return None
    try:
        payload = json.loads(str(image_result))
    except json.JSONDecodeError as exc:
        logger.warning(f"图片识别结果不是合法 JSON: {exc}")
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("fault"), bool):
        return None
    return payload


async def _resolve_kg_config() -> KgGraphConfig | None:
    """读取管理员配置的外部故障图谱连接；关键字段缺失时返回 None。"""

    try:
        resolved = await fault_trace_opts.get()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取故障溯源知识图谱配置失败: {exc}")
        return None
    uri = str(resolved.get("server_url") or "").strip()
    graph = str(resolved.get("graph") or "").strip()
    username = str(resolved.get("username") or "").strip()
    password = str(resolved.get("password") or "").strip()
    if not uri or not graph:
        return None
    if not username or not password:
        return None
    return KgGraphConfig(uri=uri, graph=graph, username=username, password=password)


async def _collect_kg_evidence(client: KgGraphClient, query: FaultTraceQuery):
    """按查询要素从 kg 图收集证据（同步图查询放到线程执行）。"""

    device_names = [query.device_name] if query.device_name else []
    return await asyncio.to_thread(
        collect_kg_fault_evidence,
        client,
        device_names=device_names,
        device_type=query.device_type,
        phenomena=query.phenomena,
        image_fault_name=query.image_fault_name,
    )


def _context_model_call(context: Any) -> Callable[[str], Awaitable[str]] | None:
    """构造按当前 Agent 模型执行的非流式单次调用；上下文没有模型标识时返回 None。

    意图识别与报告润色共用这条路径，避免重复的模型构造代码。
    """

    if not getattr(context, "model", None):
        return None

    async def model_call(prompt: str) -> str:
        model = _select_context_model(context, max_tokens=FAULT_TRACE_INTENT_MAX_TOKENS)
        response = await model.call(prompt, stream=False)
        return getattr(response, "content", "") or ""

    return model_call


def _select_context_model(context: Any, *, max_tokens: int):
    """按当前 Agent 的模型标识构造聊天模型，关闭思考并固定随机性。"""

    from yuxi.models.chat import select_model

    return select_model(
        model_spec=str(context.model),
        timeout=FAULT_TRACE_INTENT_TIMEOUT_SECONDS,
        model_params={
            "max_tokens": max_tokens,
            "temperature": 0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
    )
