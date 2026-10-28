"""处置方案生成工具：把意图解析、引导选择、设备检索、证据收集与方案装配串成一条链路。

工具只做权限校验、结果缓存、kg 配置读取与流程编排；意图解析（``intent``）、图谱检索
（``graph_plan``）与方案装配（``answer``）各自拥有模块，跨模块流动的数据形状统一由
``contracts`` 拥有。方案内容不调用大模型，只有意图识别可选调用当前 Agent 的模型。

两类引导选择都走 LangGraph ``interrupt``（载荷沿用 Yuxi 既有选择卡片格式 ``source + questions[]``，
与故障溯源确认卡片一致，前端无需新增渲染器）：缺任务类型时选任务类型，设备候选不唯一时选设备。
``interrupt`` 会让工具从头重放，因此中断之前的步骤必须确定性且不产生副作用（不写图、不写外部日志），
用时以最后一次执行为准。所有同步图查询一律放到 ``asyncio.to_thread`` 执行。

装配结果只有 ``answer_verified is True`` 才作为报告返回；校验未通过（False/缺失/非布尔）时只返回
契约固定文案 ``PLAN_VERIFICATION_FAILED_MESSAGE``，不把未校验的 report/Markdown 交给直接消费者。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from yuxi.agents.toolkits.registry import tool
from yuxi.config.options import fault_trace_opts
from yuxi.agents.toolkits.tugraph import TuGraphClient, TuGraphConfig, TuGraphError
from yuxi.knowledge.maintenance_plan.answer import produce_maintenance_plan_answer
from yuxi.knowledge.maintenance_plan.contracts import (
    CHOICE_ABORTED_MESSAGE,
    CLARIFY_ALL_MESSAGE,
    DEVICE_CHOICE_QUESTION,
    DEVICE_CHOICE_SOURCE,
    DEVICE_QUESTION_ID,
    MAX_CHOICE_ROUNDS,
    MAX_DEVICE_CANDIDATES,
    NO_DEVICE_MESSAGE,
    NO_TEMPLATE_MESSAGE,
    PLAN_GRAPH_NOT_CONFIGURED_MESSAGE,
    PLAN_VERIFICATION_FAILED_MESSAGE,
    TASK_TYPE_CHOICE_QUESTION,
    TASK_TYPE_CHOICE_SOURCE,
    TASK_TYPE_QUESTION_ID,
    TASK_TYPES,
    DeviceCandidate,
    PlanQuery,
)
from yuxi.knowledge.maintenance_plan.intent import extract_plan_intent, rule_extract
from yuxi.utils import logger

# ``graph_plan`` 在函数内延迟导入：本模块由 ``yuxi.agents.toolkits`` 在包初始化时导入，
# 而 ``graph_plan`` 又依赖 ``yuxi.agents.toolkits``；模块级互相导入会让
# ``import yuxi.knowledge.maintenance_plan.graph_plan`` 先失败于循环导入。

# 意图识别开关与模型调用参数（与故障溯源同款，只有开关名换成本模块的）
MAINTENANCE_PLAN_INTENT_ENABLED = os.getenv("MAINTENANCE_PLAN_INTENT_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
MAINTENANCE_PLAN_INTENT_TIMEOUT_SECONDS = float(os.getenv("MAINTENANCE_PLAN_INTENT_TIMEOUT", "300"))
# 输出额度与提示词共享服务端 max_model_len（当前 20480），需为提示词留出余量。
MAINTENANCE_PLAN_INTENT_MAX_TOKENS = int(os.getenv("MAINTENANCE_PLAN_INTENT_MAX_TOKENS", "4096"))

# 上下文缺少用户权限信息时的固定文案（与故障溯源一致）
NO_PERMISSION_MESSAGE = "无法获取当前用户的知识库权限"
# 最终结果在对话上下文上的缓存属性名，防止模型重复调用工具
RESULT_ATTR = "_maintenance_plan_result"
# 选择卡片里代表“其他（自由输入）”的取值，视为用户没有选中任何选项
OTHER_VALUES = frozenset({"__other__", "other", "其他"})


class MaintenancePlanInput(BaseModel):
    """处置方案生成 Tool 输入。"""

    query_text: str = Field(
        description="用户的运维任务原文（文字或语音转写）；若用户是在补充上一轮澄清的信息，传入上一轮任务原文与补充内容拼接后的完整描述"
    )


@tool(
    category="knowledge",
    tags=["处置方案", "知识图谱"],
    display_name="处置方案生成",
    args_schema=MaintenancePlanInput,
)
async def generate_maintenance_plan(query_text: str, runtime: ToolRuntime = None) -> dict[str, Any] | str:
    """根据运维任务从 kg3 图检索台账设备、作业模板与规程条款，确定性装配六段式处置方案。

    图谱数据来自人工录入的 TuGraph ``kg3`` 图（管理员配置 ``fault_trace_opts``），
    由 ``graph_plan`` 适配器按中文标签读取台账、模板、安全距离、班组与备件。
    """

    if not (query_text or "").strip():
        return CLARIFY_ALL_MESSAGE

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return NO_PERMISSION_MESSAGE
    cached_result = getattr(context, RESULT_ATTR, None)
    if cached_result is not None:
        return cached_result

    config = await _resolve_kg_config()
    if config is None:
        return PLAN_GRAPH_NOT_CONFIGURED_MESSAGE

    start = time.monotonic()
    model_call = _context_model_call(context) if MAINTENANCE_PLAN_INTENT_ENABLED else None
    query = await extract_plan_intent(query_text, model_call=model_call)
    if query.clarification_required:
        # 方案三：任务类型与目标设备都缺，返回固定澄清文案，对话不结束；
        # 下一轮由智能体把上一轮原文与用户补充拼接后重新调用本工具。
        return query.clarification_message

    # 整次请求共享的引导选择轮次预算（契约 MAX_CHOICE_ROUNDS：一次请求内最多的引导选择/补充轮次）。
    # 只放在本次函数执行的局部变量里：重放时工具从头再跑一遍，已提交的 interrupt 依次返回值并各扣一轮，
    # 因此预算按“已消费的 interrupt 次数”确定性重建，不需要任何持久状态。
    rounds = [0]
    if query.task_type not in TASK_TYPES:
        resolved = _resolve_task_type(query, rounds)
        if isinstance(resolved, str):
            return resolved
        query = resolved

    client = TuGraphClient(config)
    from yuxi.knowledge.maintenance_plan.graph_plan import collect_plan_evidence

    try:
        located = await _locate_device(client, query, model_call=model_call, rounds=rounds)
        if isinstance(located, str):
            return located
        query, device = located
        evidence = await asyncio.to_thread(collect_plan_evidence, client, query, device)
    except TuGraphError as exc:
        logger.warning(f"处置方案图谱查询失败: {exc}")
        return _remember(context, PLAN_GRAPH_NOT_CONFIGURED_MESSAGE)
    finally:
        client.close()

    if evidence.template is None:
        return _remember(context, NO_TEMPLATE_MESSAGE)

    result = produce_maintenance_plan_answer(query, evidence, elapsed_seconds=round(time.monotonic() - start, 1))
    if not isinstance(result, dict) or result.get("answer_verified") is not True:
        # 校验门未通过（False/缺失/非布尔）：只返回固定文案，绝不外泄未校验的 report/Markdown
        logger.warning("处置方案证据校验未通过，只返回固定文案，不返回未校验的报告")
        return _remember(context, PLAN_VERIFICATION_FAILED_MESSAGE)
    return _remember(context, result)


# ---------------------------------------------------------------------------
# 引导选择（方案二）：缺任务类型 / 设备候选不唯一
#
# 两类选择共用整次请求的轮次预算 ``rounds``（单元素计数器，就地累加）：每发一次 interrupt 扣一轮，
# 预算用尽即返回 CHOICE_ABORTED_MESSAGE，不再发中断、不再继续查证据与装配。
# ---------------------------------------------------------------------------
def _ask_choice(payload: dict[str, Any], question_id: str, rounds: list[int]) -> tuple[str | None, str | None] | None:
    """发一次引导选择中断并解析恢复值；整次请求的轮次预算已用尽时返回 None（不发中断）。"""

    if rounds[0] >= MAX_CHOICE_ROUNDS:
        return None
    rounds[0] += 1
    return _parse_choice(interrupt(payload), question_id)


def _resolve_task_type(query: PlanQuery, rounds: list[int]) -> PlanQuery | str:
    """缺任务类型时发中断让用户选择；用户自由输入文字时用规则抽取再判，预算用尽返回中止文案。"""

    while True:
        answer = _ask_choice(_task_type_payload(query), TASK_TYPE_QUESTION_ID, rounds)
        if answer is None:
            return CHOICE_ABORTED_MESSAGE
        task_type = _task_type_from(*answer)
        if task_type:
            query.task_type = task_type
            return query


def _task_type_payload(query: PlanQuery) -> dict[str, Any]:
    """任务类型选择卡片：四个任务类型各一个选项，允许自由输入。"""

    return {
        "source": TASK_TYPE_CHOICE_SOURCE,
        "questions": [
            {
                "question_id": TASK_TYPE_QUESTION_ID,
                "question": TASK_TYPE_CHOICE_QUESTION,
                "options": [{"label": task_type, "value": task_type} for task_type in TASK_TYPES],
                "multi_select": False,
                "allow_other": True,
            }
        ],
        "query": query.model_dump(),
    }


def _task_type_from(selected: str | None, free_text: str | None) -> str | None:
    """把用户的选择或自由文字收敛到四类任务类型之一；识别不出返回 None。"""

    if selected in TASK_TYPES:
        return selected
    text = free_text or selected
    return rule_extract(text).task_type if text else None


async def _locate_device(
    client: TuGraphClient,
    query: PlanQuery,
    *,
    model_call: Callable[[str], Awaitable[str]] | None,
    rounds: list[int],
) -> tuple[PlanQuery, DeviceCandidate] | str:
    """检索设备；候选不唯一时引导用户选择，用户补充文字后带上上一轮结果重新检索。

    返回 ``(最终 query, 设备)``：用户补充的信息（改任务类型、补位置线索等）会并进最终 query，
    由主流程继续传给证据收集与方案装配，避免装配仍用旧的意图；返回字符串是用户可见文案
    （无候选或交互过多）。引导选择的轮次从整次请求的全局预算里扣（与任务类型中断共用
    ``MAX_CHOICE_ROUNDS``），用户没有给出可用选择时在预算内重发中断。
    """

    from yuxi.knowledge.maintenance_plan.graph_plan import search_devices

    while True:
        candidates = await asyncio.to_thread(search_devices, client, query)
        if not candidates:
            return NO_DEVICE_MESSAGE
        picked = _pick_device(candidates, query, rounds)
        if isinstance(picked, DeviceCandidate):
            return query, picked
        if picked:
            query = await extract_plan_intent(picked, model_call=model_call, previous=query)
            continue
        if rounds[0] >= MAX_CHOICE_ROUNDS:
            return CHOICE_ABORTED_MESSAGE


def _pick_device(
    candidates: list[DeviceCandidate], query: PlanQuery, rounds: list[int]
) -> DeviceCandidate | str | None:
    """从候选中定设备：唯一候选、或唯一最高分且名称与用户说法一致时直接采用，否则发中断引导选择。

    返回 ``DeviceCandidate`` 表示已确定；返回字符串是用户的补充文字（调用方重新检索）；
    返回 ``None`` 表示没有可用选择（用户没选、或轮次预算已用尽因而没有发中断）。
    """

    if len(candidates) == 1:
        return candidates[0]

    top = candidates[0]
    wanted = _normalize_name(query.device_name)
    if wanted and top.score > candidates[1].score and _normalize_name(top.name) == wanted:
        return top

    answer = _ask_choice(_device_payload(query, candidates), DEVICE_QUESTION_ID, rounds)
    if answer is None:
        return None
    selected, free_text = answer
    if selected:
        matched = next((candidate for candidate in candidates if candidate.device_id == selected), None)
        if matched is not None:
            return matched
    if free_text:
        return free_text
    return selected or None


def _device_payload(query: PlanQuery, candidates: list[DeviceCandidate]) -> dict[str, Any]:
    """设备选择卡片：展示前 MAX_DEVICE_CANDIDATES 个候选，value 用设备 id 便于回填。"""

    return {
        "source": DEVICE_CHOICE_SOURCE,
        "questions": [
            {
                "question_id": DEVICE_QUESTION_ID,
                "question": DEVICE_CHOICE_QUESTION,
                "options": [
                    {"label": candidate.label(), "value": candidate.device_id}
                    for candidate in candidates[:MAX_DEVICE_CANDIDATES]
                ],
                "multi_select": False,
                "allow_other": True,
            }
        ],
        "query": query.model_dump(),
    }


def _parse_choice(value: Any, question_id: str) -> tuple[str | None, str | None]:
    """解析 interrupt 恢复值，返回 ``(选中值, 自由文字)``。

    兼容 fault_trace ``parse_intent_confirmation`` 处理的所有形态：``{question_id: ...}`` 嵌套、
    ``{"selected": [...]}``、``{"value": ...}``、``{"text": ...}``、列表与纯字符串；
    ``"__other__" / "other" / "其他"`` 视为用户没有选中任何选项。
    """

    if isinstance(value, dict):
        if question_id in value:
            return _parse_choice(value[question_id], question_id)
        text = str(value.get("text") or "").strip()
        if text:
            return (None, None) if _is_other(text) else (None, text)
        selected = value.get("selected")
        if isinstance(selected, (list, tuple)) and selected:
            return _parse_choice(selected[0], question_id)
        picked = value.get("value")
        if picked is not None and str(picked).strip():
            picked_text = str(picked).strip()
            return (None, None) if _is_other(picked_text) else (picked_text, None)
        return None, None
    if isinstance(value, (list, tuple)):
        for item in value:
            selected, text = _parse_choice(item, question_id)
            if selected or text:
                return selected, text
        return None, None
    if isinstance(value, str):
        text = value.strip()
        if not text or _is_other(text):
            return None, None
        return text, None
    return None, None


def _is_other(text: str) -> bool:
    """判断是否为“其他（自由输入）”占位取值。"""

    return text.strip().lower() in OTHER_VALUES


def _normalize_name(value: str | None) -> str:
    """规范化设备名称用于比较：全角转半角、去空白、忽略大小写。"""

    if not value:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


# ---------------------------------------------------------------------------
# 配置、模型调用与缓存
# ---------------------------------------------------------------------------
async def _resolve_kg_config() -> TuGraphConfig | None:
    """读取管理员配置的 kg3 图连接（复用 fault_trace_opts）；关键字段缺失时返回 None。"""

    try:
        resolved = await fault_trace_opts.get()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取处置方案知识图谱配置失败: {exc}")
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


def _context_model_call(context: Any) -> Callable[[str], Awaitable[str]] | None:
    """构造按当前 Agent 模型执行的非流式单次调用；上下文没有模型标识时返回 None。

    只有意图识别走这条路径；方案装配不使用大模型。
    """

    if not getattr(context, "model", None):
        return None

    async def model_call(prompt: str) -> str:
        model = _select_context_model(context, max_tokens=MAINTENANCE_PLAN_INTENT_MAX_TOKENS)
        response = await model.call(prompt, stream=False)
        return getattr(response, "content", "") or ""

    return model_call


def _select_context_model(context: Any, *, max_tokens: int):
    """按当前 Agent 的模型标识构造聊天模型，关闭思考并固定随机性（temperature 0）。"""

    from yuxi.models.chat import select_model

    return select_model(
        model_spec=str(context.model),
        timeout=MAINTENANCE_PLAN_INTENT_TIMEOUT_SECONDS,
        model_params={
            "max_tokens": max_tokens,
            "temperature": 0,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
    )


def _remember(context: Any, result: Any) -> Any:
    """把最终结果缓存到对话上下文，避免模型在同一轮内重复调用工具。"""

    setattr(context, RESULT_ATTR, result)
    return result
