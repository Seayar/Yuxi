"""设备预测维护工具：设备识别、两个预测服务调用与维护计划装配。

工具只做权限校验、结果缓存、图配置读取与流程编排；设备检索与维护知识匹配在 ``graph``，
外部服务调用在 ``services``，报告装配与校验在 ``answer``，跨模块流动的数据形状由
``contracts`` 拥有。

设备候选不唯一时走 LangGraph ``interrupt``（载荷沿用 Yuxi 既有选择卡片格式
``source + questions[]``）。``interrupt`` 会让工具从头重放，因此中断之前只做确定性只读
查询，不写图、不写外部服务。所有同步图查询一律放到 ``asyncio.to_thread`` 执行。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import unicodedata
from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from yuxi.agents.thinking import ThinkingEmitter
from yuxi.agents.toolkits.registry import tool
from yuxi.agents.toolkits.tugraph import TuGraphClient, TuGraphConfig, TuGraphError
from yuxi.config.options import fault_trace_opts
from yuxi.knowledge.device_prediction import graph as kg5
from yuxi.knowledge.device_prediction.answer import generate_status_readout, produce_prediction_answer
from yuxi.knowledge.device_prediction.contracts import (
    CHOICE_ABORTED_MESSAGE,
    CLARIFY_DEVICE_MESSAGE,
    DEVICE_CHOICE_QUESTION,
    DEVICE_CHOICE_SOURCE,
    DEVICE_ID_UNRESOLVED_MESSAGE,
    DEVICE_PREDICTION_AGENT_SLUG,
    DEVICE_PREDICTION_THINKING_STEPS,
    DEVICE_QUESTION_ID,
    MAX_CHOICE_ROUNDS,
    MAX_DEVICE_CANDIDATES,
    NO_DEVICE_MESSAGE,
    NO_PERMISSION_MESSAGE,
    OTHER_VALUES,
    PREDICTION_GRAPH_NOT_CONFIGURED_MESSAGE,
    REPORT_VERIFICATION_FAILED_MESSAGE,
    DeviceCandidate,
    FaultPrediction,
    HealthAssessment,
    PredictionEvidence,
    PredictionQuery,
)
from yuxi.knowledge.device_prediction.intent import (
    STATUS_READOUT_MAX_TOKENS,
    PredictionModelCall,
    context_model_call,
    extract_prediction_intent,
)
from yuxi.knowledge.device_prediction.services import (
    PredictionServiceError,
    fetch_fault_prediction,
    fetch_health_assessment,
)
from yuxi.utils import logger

# 最终结果在对话上下文上的缓存属性名，防止模型在同一轮内重复调用工具。
# 缓存按规范化后的问题文本分组：用户补充设备后重新提问不能被上一次的结果挡住。
RESULT_ATTR = "_device_prediction_results"
# 服务调用步骤：并行发起，失败时其余步骤标记 skipped
SERVICE_STEPS = ("health_assessment", "fault_prediction")


class DevicePredictionInput(BaseModel):
    """设备预测维护 Tool 输入。"""

    query_text: str = Field(
        description="用户关于设备健康评估、故障预测或维护计划的问题原文；补充设备信息时传入上一轮问题与补充内容拼接后的完整描述"
    )


@tool(
    category="knowledge",
    tags=["设备预测维护", "知识图谱"],
    display_name="设备预测维护",
    args_schema=DevicePredictionInput,
)
async def predict_device(query_text: str, runtime: ToolRuntime = None) -> dict[str, Any] | str:
    """按设备名称定位设备，调用健康评估与故障预测服务，并按预测故障类型匹配维护知识生成报告。

    图谱数据来自设备预测维护自己的 TuGraph ``kg5`` 图（管理员配置
    ``fault_trace_opts`` 的 ``device_prediction_graph``，默认 kg5）；健康评估与故障预测两个
    服务地址由管理员配置 ``device_prediction_opts``，任一未配置时返回对应固定文案。
    """

    if not (query_text or "").strip():
        return CLARIFY_DEVICE_MESSAGE

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return NO_PERMISSION_MESSAGE
    cache_key = _normalize_text(query_text)
    cached_result = _cached(context, cache_key)
    if cached_result is not None:
        return cached_result

    config = await _resolve_kg_config()
    if config is None:
        return PREDICTION_GRAPH_NOT_CONFIGURED_MESSAGE

    emitter = ThinkingEmitter(agent_slug=DEVICE_PREDICTION_AGENT_SLUG, steps=DEVICE_PREDICTION_THINKING_STEPS)
    emitter.emit("load_device", "started", content="正在解析问题中的目标设备。")
    start = time.monotonic()
    model_call = context_model_call(context)
    query = await extract_prediction_intent(query_text, model_call=model_call)
    if query.clarification_required:
        emitter.emit(
            "load_device",
            "failed",
            content=query.clarification_message,
            error={"code": "clarification_required", "message": query.clarification_message},
        )
        return query.clarification_message

    client = TuGraphClient(config)
    try:
        located = await _locate_device(client, query, model_call=model_call, emitter=emitter)
    except TuGraphError as exc:
        logger.warning(f"设备预测维护图谱查询失败: {exc}")
        emitter.emit(
            "load_device",
            "failed",
            content=PREDICTION_GRAPH_NOT_CONFIGURED_MESSAGE,
            error={"code": "graph_unavailable", "message": str(exc)},
        )
        return PREDICTION_GRAPH_NOT_CONFIGURED_MESSAGE
    finally:
        client.close()
    if isinstance(located, str):
        error_code = "device_not_found" if located == NO_DEVICE_MESSAGE else "choice_aborted"
        emitter.emit("load_device", "failed", content=located, error={"code": error_code, "message": located})
        return _remember(context, cache_key, located)
    query, device = located

    resolved = await kg5.resolve_device_id(device)
    if resolved is None:
        emitter.emit(
            "load_device",
            "failed",
            content=DEVICE_ID_UNRESOLVED_MESSAGE,
            error={"code": "device_id_unresolved", "message": DEVICE_ID_UNRESOLVED_MESSAGE},
        )
        return _remember(context, cache_key, DEVICE_ID_UNRESOLVED_MESSAGE)
    device_id, device_id_source = resolved
    emitter.emit(
        "load_device",
        "completed",
        content=f"已定位设备“{device.label()}”，解析出预测服务设备 ID：{device_id}。",
        data={
            "device_name": device.name,
            "device_id": device_id,
            "device_id_source": device_id_source,
            "device_type": device.device_type,
            "voltage_level": device.voltage_level,
        },
    )

    services = await _call_services(device_id, emitter=emitter)
    if isinstance(services, str):
        return _remember(context, cache_key, services)
    health, prediction = services

    emitter.emit(
        "maintenance_plan",
        "started",
        content=f"正在按故障类型“{prediction.predicted_fault_type}”匹配故障现象与维护知识。",
    )
    client = TuGraphClient(config)
    try:
        guidance = await asyncio.to_thread(
            kg5.lookup_maintenance_guidance,
            client,
            fault_type=prediction.predicted_fault_type,
            device_type=device.device_type or "",
        )
    finally:
        client.close()

    evidence = PredictionEvidence(
        device_name=device.name,
        device_label=device.label(),
        device_type=device.device_type or "",
        voltage_level=device.voltage_level or "",
        device_id=device_id,
        device_id_source=device_id_source,
        health=health,
        prediction=prediction,
        guidance=guidance,
        retrieval_log=[
            f"按名称在知识图谱中定位设备“{device.name}”"
            + (f"（{device.device_type}，{device.voltage_level}）" if device.device_type else ""),
            f"预测服务设备 ID 取自{device_id_source}：{device_id}",
            f"设备健康评估：评分 {health.health_score} 分，等级“{health.health_level}”，"
            f"监测指标 {len(health.indicators)} 项",
            f"故障预测：故障类型“{prediction.predicted_fault_type}”，发生概率 {prediction.probability}，"
            f"预计发生时间 {prediction.expected_failure_time}",
            (
                f"按故障类型在“{guidance.fault_type}”故障现象下匹配维护知识，"
                f"所需资源 {len(guidance.resources)} 项、注意事项 {len(guidance.cautions)} 项"
                if guidance.fault_type
                else "未在图谱中匹配到该故障类型的故障现象"
            ),
        ],
    )

    readout_call = context_model_call(context, max_tokens=STATUS_READOUT_MAX_TOKENS)
    status_readout, readout_generated = await generate_status_readout(evidence, model_call=readout_call)
    result = produce_prediction_answer(
        evidence,
        status_readout=status_readout,
        status_readout_generated=readout_generated,
        elapsed_seconds=round(time.monotonic() - start, 1),
    )
    if not isinstance(result, dict) or result.get("answer_verified") is not True:
        logger.warning("设备预测报告校验未通过，只返回固定文案，不返回未校验的报告")
        emitter.emit(
            "maintenance_plan",
            "failed",
            content=REPORT_VERIFICATION_FAILED_MESSAGE,
            error={"code": "verification_failed", "message": REPORT_VERIFICATION_FAILED_MESSAGE},
        )
        return _remember(context, cache_key, REPORT_VERIFICATION_FAILED_MESSAGE)
    emitter.emit(
        "maintenance_plan",
        "completed",
        content=(
            f"维护计划建议已生成：维护时机“{prediction.maintenance_time}”，"
            f"所需资源 {len(guidance.resources)} 项、注意事项 {len(guidance.cautions)} 项。"
        ),
        data={"answer_verified": True, "answer_source": "device_prediction"},
    )
    result["thinking_steps"] = emitter.snapshot()
    return _remember(context, cache_key, result)


# ---------------------------------------------------------------------------
# 外部服务调用
# ---------------------------------------------------------------------------
async def _call_services(device_id: str, *, emitter: ThinkingEmitter) -> tuple[HealthAssessment, FaultPrediction] | str:
    """并行调用健康评估与故障预测服务；任一失败时返回固定文案，其余步骤标记 skipped。"""

    emitter.emit("health_assessment", "started", content="正在调用设备健康评估服务。")
    emitter.emit("fault_prediction", "started", content="正在调用 LSTM 故障预测服务。")
    results = await asyncio.gather(
        fetch_health_assessment(device_id),
        fetch_fault_prediction(device_id),
        return_exceptions=True,
    )

    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        # 非服务类异常属于实现缺陷，按 fail-fast 直接抛出，不伪装成服务失败文案。
        unexpected = next((error for error in errors if not isinstance(error, PredictionServiceError)), None)
        if unexpected is not None:
            raise unexpected
        for step_id, result in zip(SERVICE_STEPS, results, strict=True):
            if isinstance(result, PredictionServiceError):
                emitter.emit(
                    step_id,
                    "failed",
                    content=result.message,
                    error={"code": result.code, "message": result.message},
                )
            else:
                emitter.emit(step_id, "skipped", content="另一项服务未返回结果，本步骤结果不单独发布。")
        return next(error for error in errors if isinstance(error, PredictionServiceError)).message

    health, prediction = results
    emitter.emit(
        "health_assessment",
        "completed",
        content=(
            f"健康评分 {health.health_score} 分，健康等级“{health.health_level}”，"
            f"监测指标 {len(health.indicators)} 项。"
        ),
        data={
            "health_score": health.health_score,
            "health_level": health.health_level,
            "indicator_count": len(health.indicators),
        },
    )
    emitter.emit(
        "fault_prediction",
        "completed",
        content=(
            f"预测故障类型“{prediction.predicted_fault_type}”，发生概率 {prediction.probability}，"
            f"预计发生时间 {prediction.expected_failure_time}。"
        ),
        data={
            "predicted_fault_type": prediction.predicted_fault_type,
            "probability": prediction.probability,
            "expected_failure_time": prediction.expected_failure_time,
        },
    )
    return health, prediction


# ---------------------------------------------------------------------------
# 设备识别：候选不唯一时让用户选择
# ---------------------------------------------------------------------------
async def _locate_device(
    client: TuGraphClient,
    query: PredictionQuery,
    *,
    model_call: PredictionModelCall | None,
    emitter: ThinkingEmitter,
) -> tuple[PredictionQuery, DeviceCandidate] | str:
    """检索设备；候选不唯一时引导用户选择，用户补充文字后带上上一轮结果重新检索。"""

    rounds = [0]
    while True:
        candidates = await asyncio.to_thread(kg5.search_devices, client, query)
        if not candidates:
            return NO_DEVICE_MESSAGE
        picked = _pick_device(candidates, query, rounds, emitter=emitter)
        if isinstance(picked, DeviceCandidate):
            return query, picked
        if picked:
            query = await extract_prediction_intent(picked, model_call=model_call, previous=query)
            continue
        if rounds[0] >= MAX_CHOICE_ROUNDS:
            return CHOICE_ABORTED_MESSAGE


def _pick_device(
    candidates: list[DeviceCandidate], query: PredictionQuery, rounds: list[int], *, emitter: ThinkingEmitter
) -> DeviceCandidate | str | None:
    """从候选中定设备：唯一候选、或唯一最高分且名称与用户说法一致时直接采用。"""

    if len(candidates) == 1:
        return candidates[0]

    top = candidates[0]
    wanted = _normalize_text(query.device_name)
    if wanted and top.score > candidates[1].score and _normalize_text(top.name) == wanted:
        return top

    emitter.emit(
        "load_device",
        "waiting",
        content="等待用户选择目标设备。",
        data={"source": DEVICE_CHOICE_SOURCE, "candidate_count": len(candidates)},
    )
    answer = _ask_choice(_device_payload(query, candidates), rounds)
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


def _device_payload(query: PredictionQuery, candidates: list[DeviceCandidate]) -> dict[str, Any]:
    """设备选择卡片：展示前 MAX_DEVICE_CANDIDATES 个候选，value 用设备节点 id 便于回填。"""

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


def _ask_choice(payload: dict[str, Any], rounds: list[int]) -> tuple[str | None, str | None] | None:
    """发一次引导选择中断并解析恢复值；整次请求的轮次预算已用尽时返回 None（不发中断）。"""

    if rounds[0] >= MAX_CHOICE_ROUNDS:
        return None
    rounds[0] += 1
    return _parse_choice(interrupt(payload))


def _parse_choice(value: Any) -> tuple[str | None, str | None]:
    """解析 interrupt 恢复值，返回 ``(选中值, 自由文字)``。

    兼容 ``{question_id: ...}`` 嵌套、``{"selected": [...]}``、``{"value": ...}``、
    ``{"text": ...}``、列表与纯字符串；``"__other__" / "other" / "其他"`` 视为未选中。
    """

    if isinstance(value, dict):
        if DEVICE_QUESTION_ID in value:
            return _parse_choice(value[DEVICE_QUESTION_ID])
        text = str(value.get("text") or "").strip()
        if text:
            return (None, None) if _is_other(text) else (None, text)
        selected = value.get("selected")
        if isinstance(selected, (list, tuple)) and selected:
            return _parse_choice(selected[0])
        picked = value.get("value")
        if picked is not None and str(picked).strip():
            picked_text = str(picked).strip()
            return (None, None) if _is_other(picked_text) else (picked_text, None)
        return None, None
    if isinstance(value, (list, tuple)):
        for item in value:
            selected, text = _parse_choice(item)
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


def _normalize_text(value: str | None) -> str:
    """规范化比较文本：全角转半角、去空白、忽略大小写。"""

    if not value:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


# ---------------------------------------------------------------------------
# 配置与缓存
# ---------------------------------------------------------------------------
async def _resolve_kg_config() -> TuGraphConfig | None:
    """读取管理员配置的 TuGraph 连接；预测维护图默认 kg5，关键字段缺失时返回 None。"""

    try:
        resolved = await fault_trace_opts.get()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取设备预测维护知识图谱配置失败: {exc}")
        return None
    uri = str(resolved.get("server_url") or "").strip()
    graph = str(resolved.get("device_prediction_graph") or os.getenv("DEVICE_PREDICTION_GRAPH") or "kg5").strip()
    username = str(resolved.get("username") or "").strip()
    password = str(resolved.get("password") or "").strip()
    if not uri or not graph:
        return None
    if not username or not password:
        return None
    return TuGraphConfig(uri=uri, graph=graph, username=username, password=password)


def _cached(context: Any, key: str) -> Any:
    """按问题文本读取本轮缓存；没有缓存时返回 None。"""

    cache = getattr(context, RESULT_ATTR, None)
    return cache.get(key) if isinstance(cache, dict) else None


def _remember(context: Any, key: str, result: Any) -> Any:
    """把最终结果按问题文本缓存到对话上下文，避免模型在同一轮内重复调用工具。"""

    cache = getattr(context, RESULT_ATTR, None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(context, RESULT_ATTR, cache)
    cache[key] = result
    return result
