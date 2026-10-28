"""故障溯源工具入口。

工具只负责参数校验、配置读取、结果缓存与调用显式工作流；设备校验、图谱查询、
实时值、规则求值与报告装配由 ``workflow`` 和各自的能力模块完成。
"""

from __future__ import annotations

from time import monotonic
from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, Field

from yuxi.agents.thinking import ThinkingEmitter
from yuxi.agents.toolkits.registry import tool
from yuxi.agents.toolkits.tugraph import TuGraphConfig
from yuxi.config.options import fault_trace_opts
from yuxi.knowledge.fault_trace.contracts import (
    FAULT_GRAPH_NOT_CONFIGURED_MESSAGE,
    FAULT_TRACE_AGENT_SLUG,
    FAULT_TRACE_THINKING_STEPS,
    NO_FAULT_GRAPH_MESSAGE,
)
from yuxi.utils import logger

# ``workflow`` 在函数内延迟导入：本模块由 ``yuxi.agents.toolkits`` 在包初始化时导入，
# 而 ``workflow`` 又依赖 ``yuxi.agents``；模块级互相导入会让
# ``import yuxi.knowledge.fault_trace.workflow`` 先失败于循环导入。

__all__ = ["FaultTraceInput", "trace_fault"]


class FaultTraceInput(BaseModel):
    """故障溯源 Tool 输入。"""

    query_text: str = Field(description="用户对故障现象的文字/语音描述")


@tool(
    category="knowledge",
    tags=["故障溯源", "知识图谱"],
    display_name="故障溯源",
    args_schema=FaultTraceInput,
)
async def trace_fault(
    query_text: str,
    confirmed_intent: bool = False,
    runtime: ToolRuntime = None,
) -> dict[str, Any] | str:
    """结合用户描述与图片识别结果，沿故障知识图谱生成五段式溯源报告。

    ``confirmed_intent`` 不在 ``FaultTraceInput`` 里：模型无法通过工具参数跳过确认卡片，
    确认结论只来自 ``interrupt`` 返回的用户选择；该形参仅供测试模拟已确认的用户动作。
    """

    if not query_text.strip():
        return "请提供故障描述"

    from yuxi.knowledge.fault_trace.workflow import FaultTraceState, run_fault_trace

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

    emitter = ThinkingEmitter(agent_slug=FAULT_TRACE_AGENT_SLUG, steps=FAULT_TRACE_THINKING_STEPS)
    emitter.emit("parse_fault", "started", content="正在解析故障描述与图片结果。")
    state = FaultTraceState(
        query_text=query_text,
        confirmed_intent=confirmed_intent,
        context=context,
        runtime=runtime,
        config=config,
        started_at=monotonic(),
        emitter=emitter,
    )
    result = await run_fault_trace(state)
    if isinstance(result, dict) and "report" in result:
        _cache_result(context, cache_key, result)
    elif result == NO_FAULT_GRAPH_MESSAGE:
        _cache_result(context, cache_key, result)
    return result


def _cache_result(context: Any, cache_key: str, result: Any) -> None:
    """把工具结果按故障描述缓存到运行上下文，供 interrupt 恢复后的重放复用。"""

    cache = getattr(context, "_fault_trace_results", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(context, "_fault_trace_results", cache)
    cache[cache_key] = result


async def _resolve_kg_config() -> TuGraphConfig | None:
    """读取管理员配置的故障图谱连接；关键字段缺失时返回 None。"""

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
    return TuGraphConfig(uri=uri, graph=graph, username=username, password=password)
