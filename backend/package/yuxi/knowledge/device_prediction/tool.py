"""设备预测维护工具：设备识别、两个预测服务调用与维护计划装配。

工具只做权限校验、结果缓存、图配置读取与流程编排；设备检索与维护知识匹配在 ``graph``，
外部服务调用在 ``services``，报告装配与校验在 ``answer``，跨模块流动的数据形状由
``contracts`` 拥有。

每次请求都通过 LangGraph ``interrupt`` 确认目标设备（载荷沿用 Yuxi 既有选择卡片格式
``source + questions[]``），零候选时允许自由输入。``interrupt`` 会让工具从头重放，
因此中断之前只做确定性只读查询，不写图、不写外部服务。所有同步图查询一律放到
``asyncio.to_thread`` 执行。
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
from yuxi.knowledge.device_index import graph_key as device_graph_key
from yuxi.knowledge.device_selection import (
    DeviceOption,
    build_device_choice_payload,
    parse_device_choice,
    select_device,
)
from yuxi.knowledge.fault_trace.ledger import ledger_repository
from yuxi.knowledge.device_prediction import graph as kg5
from yuxi.knowledge.device_prediction.answer import generate_status_readout, produce_prediction_answer
from yuxi.knowledge.device_prediction.contracts import (
    CHOICE_ABORTED_MESSAGE,
    CHOICE_CANCELLED_MESSAGE,
    CLARIFY_DEVICE_MESSAGE,
    DEVICE_CHOICE_QUESTION,
    DEVICE_CHOICE_SOURCE,
    DEVICE_ID_UNRESOLVED_MESSAGE,
    DEVICE_PREDICTION_AGENT_SLUG,
    DEVICE_PREDICTION_THINKING_STEPS,
    DEVICE_QUESTION_ID,
    MAX_CHOICE_ROUNDS,
    MAX_DEVICE_CANDIDATES,
    NO_PERMISSION_MESSAGE,
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

    图谱数据来自管理员配置的 TuGraph 设备预测维护图（``fault_trace_opts`` 的
    ``device_prediction_graph``，默认图名由配置默认值决定）；健康评估与故障预测两个
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

    client = TuGraphClient(config)
    try:
        located = await _locate_device(
            client,
            query,
            model_call=model_call,
            emitter=emitter,
            graph_key=device_graph_key(config),
        )
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
        error_code = _device_choice_error_code(located)
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
    graph_key: str = "",
) -> tuple[PredictionQuery, DeviceCandidate] | str:
    """检索设备；候选不唯一时由模型在候选内给出推荐项，再由用户选择确认。"""

    rounds = [0]
    while True:
        recall_text = query.device_name or query.location_hint or query.raw_text
        ledger_candidates = await ledger_repository.search_devices(
            recall_text,
            device_type=query.device_type,
            voltage_level=query.voltage_level,
            location_hint=query.location_hint,
            limit=MAX_DEVICE_CANDIDATES,
            minimum_score=0.0 if (query.device_name or query.location_hint or query.device_type) else 60.0,
        )
        if query.clarification_required and not ledger_candidates:
            candidates = []
        else:
            candidates = await asyncio.to_thread(
                kg5.search_devices,
                client,
                query,
                graph_key=graph_key,
                ledger_candidates=ledger_candidates,
            )
        if not candidates:
            logger.warning(
                f"设备预测维护设备召回为空: device_name={query.device_name!r} "
                f"device_type={query.device_type!r} location={query.location_hint!r} "
                f"ledger_candidates={len(ledger_candidates)}"
            )
        recommended_id: str | None = None
        selection: dict[str, Any] | None = None
        if len(candidates) > 1:
            recommended_id, selection = await _select_device_candidate(
                candidates,
                query=query,
                model_call=model_call,
            )
        if recommended_id:
            candidates = _promote_candidate(candidates, recommended_id)
        picked = _pick_device(candidates, query, rounds, emitter=emitter, selection=selection)
        if picked is CHOICE_CANCELLED:
            return CHOICE_CANCELLED_MESSAGE
        if isinstance(picked, DeviceCandidate):
            return query, picked
        if picked:
            logger.info(f"设备预测用户改填设备，清除上一轮设备身份: {picked!r}")
            query = await extract_prediction_intent(
                picked,
                model_call=model_call,
                previous=query,
                device_override=picked,
            )
            continue
        if rounds[0] >= MAX_CHOICE_ROUNDS:
            return CHOICE_ABORTED_MESSAGE


async def _select_device_candidate(
    candidates: list[DeviceCandidate],
    *,
    query: PredictionQuery,
    model_call: PredictionModelCall | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """模型在候选内选择设备；返回 (推荐候选 id, 选择状态信息)。"""

    options = [
        DeviceOption(
            device_id=candidate.device_id,
            name=candidate.name,
            device_type=candidate.device_type,
            voltage_level=candidate.voltage_level,
            path=candidate.path,
            category=None,
        )
        for candidate in candidates
    ]
    selected_id, state = await select_device(
        options,
        user_text=query.raw_text,
        intent={
            "device_name": query.device_name,
            "device_type": query.device_type,
            "voltage_level": query.voltage_level,
            "location_hint": query.location_hint,
        },
        model_call=model_call,
    )
    info = {"state": state, "candidate_count": len(options), "selected_id": selected_id}
    return selected_id, info


def _promote_candidate(candidates: list[DeviceCandidate], selected_id: str) -> list[DeviceCandidate]:
    """把模型选中的候选挪到第一位，保证推荐项出现在卡片可见范围内。"""

    picked = [candidate for candidate in candidates if candidate.device_id == selected_id]
    if not picked:
        return candidates
    return picked + [candidate for candidate in candidates if candidate.device_id != selected_id]


def _pick_device(
    candidates: list[DeviceCandidate],
    query: PredictionQuery,
    rounds: list[int],
    *,
    emitter: ThinkingEmitter,
    selection: dict[str, Any] | None = None,
) -> DeviceCandidate | str | None | _ChoiceCancelled:
    """每次都让用户确认设备；零候选也发卡片允许直接填写新设备名。"""

    emitter.emit(
        "load_device",
        "waiting",
        content="等待用户选择目标设备。",
        data={
            "source": DEVICE_CHOICE_SOURCE,
            "candidate_count": len(candidates),
            "device_selection": selection,
        },
    )
    if rounds[0] >= MAX_CHOICE_ROUNDS:
        return None
    rounds[0] += 1
    answer = interrupt(_device_payload(query, candidates, recommended_id=_recommended_id(selection)))
    choice = parse_device_choice(answer, DEVICE_QUESTION_ID)
    if choice.cancelled:
        return CHOICE_CANCELLED
    if choice.selected_id:
        matched = next((candidate for candidate in candidates if candidate.device_id == choice.selected_id), None)
        if matched is not None:
            return matched
    if choice.free_text:
        return choice.free_text
    return choice.selected_id


def _device_choice_error_code(message: str) -> str:
    """设备选择终止文案 → 思考步骤错误码：区分用户取消、轮次用尽与真的没找到。"""

    if message == CHOICE_CANCELLED_MESSAGE:
        return "device_choice_cancelled"
    if message == CHOICE_ABORTED_MESSAGE:
        return "choice_aborted"
    return "device_not_found"


def _recommended_id(selection: dict[str, Any] | None) -> str | None:
    if not isinstance(selection, dict):
        return None
    value = selection.get("selected_id")
    return str(value) if value else None


def _device_payload(
    query: PredictionQuery,
    candidates: list[DeviceCandidate],
    *,
    recommended_id: str | None = None,
) -> dict[str, Any]:
    """设备选择卡片：展示前 MAX_DEVICE_CANDIDATES 个候选，value 用设备节点 id 便于回填。

    模型选中的候选在 label 末尾标记“（推荐）”，仍由用户点击确认。
    """

    payload = build_device_choice_payload(
        source=DEVICE_CHOICE_SOURCE,
        question_id=DEVICE_QUESTION_ID,
        question=DEVICE_CHOICE_QUESTION if candidates else "未召回到候选设备，请直接填写正确的设备名称。",
        options=[
            DeviceOption(
                device_id=candidate.device_id,
                name=candidate.name,
                device_type=candidate.device_type,
                voltage_level=candidate.voltage_level,
                path=candidate.path,
            )
            for candidate in candidates
        ],
        recommended_id=recommended_id,
        limit=MAX_DEVICE_CANDIDATES,
    )
    payload["query"] = query.model_dump()
    return payload


class _ChoiceCancelled:
    """用户关闭选择卡片的取消结果；与“轮次预算用尽”的 None 区分。"""


CHOICE_CANCELLED = _ChoiceCancelled()


def _normalize_text(value: str | None) -> str:
    """规范化比较文本：全角转半角、去空白、忽略大小写。"""

    if not value:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


# ---------------------------------------------------------------------------
# 配置与缓存
# ---------------------------------------------------------------------------
async def _resolve_kg_config() -> TuGraphConfig | None:
    """读取管理员配置的 TuGraph 连接；预测维护图默认图名由配置决定，关键字段缺失时返回 None。"""

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
