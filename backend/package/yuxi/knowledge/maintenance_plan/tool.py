"""处置方案生成工具：意图解析、设备/场景选择、分支判定、证据收集与方案装配。

工具只做权限校验、结果缓存、kg 配置读取与流程编排；意图解析（``intent``）、图谱检索
（``graph_plan``）、分支判定（``branch``）、外部数据（``sources``）与方案装配
（``answer``）各自拥有模块，跨模块流动的数据形状统一由 ``contracts`` 拥有。

设备/场景两类引导选择走 LangGraph ``interrupt``（载荷沿用 Yuxi 既有选择卡片格式
``source + questions[]``）：每次请求都确认目标设备，零候选时允许自由输入；场景候选
不唯一或并列时选场景。
分支只从 Redis 读取；未配置、无 key、读取失败或值非法时返回固定失败文案，不让人工选择，
也不默认任何分支。``interrupt`` 会让工具从头重放，因此中断之前的步骤必须确定性且不产生
副作用（不写图、不写外部日志）。所有同步图查询一律放到 ``asyncio.to_thread`` 执行。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from yuxi.agents.toolkits.registry import tool
from yuxi.agents.toolkits.tugraph import TuGraphClient, TuGraphConfig, TuGraphError
from yuxi.agents.thinking import ThinkingEmitter
from yuxi.config.options import fault_trace_opts
from yuxi.knowledge.device_index import graph_key as device_graph_key
from yuxi.knowledge.device_selection import (
    DeviceOption,
    SELECTED_DEVICE_UNAVAILABLE_MESSAGE,
    SelectedDeviceId,
    build_device_choice_payload,
    parse_device_choice,
    select_device,
)
from yuxi.knowledge.fault_trace.ledger import ledger_repository
from yuxi.knowledge.maintenance_plan.answer import produce_maintenance_plan_answer
from yuxi.knowledge.maintenance_plan.branch import lookup_branch
from yuxi.knowledge.maintenance_plan.contracts import (
    BRANCH_UNRESOLVED_MESSAGE,
    CHOICE_ABORTED_MESSAGE,
    CHOICE_CANCELLED_MESSAGE,
    CLARIFY_ALL_MESSAGE,
    DEVICE_CHOICE_QUESTION,
    DEVICE_CHOICE_SOURCE,
    DEVICE_QUESTION_ID,
    MAINTENANCE_PLAN_AGENT_SLUG,
    MAINTENANCE_PLAN_THINKING_STEPS,
    MAX_CHOICE_ROUNDS,
    MAX_DEVICE_CANDIDATES,
    MAX_SCENE_CANDIDATES,
    NO_PERMISSION_MESSAGE,
    NO_SCENE_MESSAGE,
    PLAN_GRAPH_NOT_CONFIGURED_MESSAGE,
    PLAN_VERIFICATION_FAILED_MESSAGE,
    SCENE_CHOICE_QUESTION,
    SCENE_CHOICE_SOURCE,
    SCENE_QUESTION_ID,
    BranchSelection,
    DeviceCandidate,
    PlanQuery,
    Scenario,
)
from yuxi.knowledge.maintenance_plan.intent import extract_plan_intent
from yuxi.knowledge.maintenance_plan.sources import (
    MaintenanceDataRepository,
    annotate_resources,
    assign_personnel,
)
from yuxi.utils import logger
from yuxi.utils.question_utils import is_question_cancel


# ``graph_plan`` 在调用时延迟导入：本模块由 ``yuxi.agents.toolkits`` 在包初始化时导入，
# 而 ``graph_plan`` 又依赖 ``yuxi.agents.toolkits``；模块级互相导入会让
# ``import yuxi.knowledge.maintenance_plan.graph_plan`` 先失败于循环导入。
def _graph_plan():
    """调用时解析 ``graph_plan`` 模块；测试与调用方对模块属性的替换因此仍然生效。"""

    from yuxi.knowledge.maintenance_plan import graph_plan

    return graph_plan


# 意图识别开关与模型调用参数（与故障溯源同款，只有开关名换成本模块的）
MAINTENANCE_PLAN_INTENT_ENABLED = os.getenv("MAINTENANCE_PLAN_INTENT_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
MAINTENANCE_PLAN_INTENT_TIMEOUT_SECONDS = float(os.getenv("MAINTENANCE_PLAN_INTENT_TIMEOUT", "300"))
# 输出额度与提示词共享服务端 max_model_len（当前 20480），需为提示词留出余量。
MAINTENANCE_PLAN_INTENT_MAX_TOKENS = int(os.getenv("MAINTENANCE_PLAN_INTENT_MAX_TOKENS", "4096"))

# 最终结果按 query_text 缓存在对话上下文上（与故障溯源同款）：同一描述重复调用返回缓存，
# 不同描述各自独立，不能共用一份结果。
RESULT_ATTR = "_maintenance_plan_result"


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
    """根据运维任务从配置的处置方案图检索设备、检修作业场景与四类证据，确定性装配处置方案。

    图谱数据来自管理员配置的 TuGraph 处置方案图（``fault_trace_opts`` 的
    ``maintenance_plan_graph``，默认图名由配置默认值决定）；分支只读故障溯源 Redis 的
    ``{台账ID}:{分支判定维度}``，读不到时返回固定失败文案；工器具/人员数据从 MinIO 读取，
    未接入时在方案中如实标注。
    """

    if not (query_text or "").strip():
        return CLARIFY_ALL_MESSAGE

    context = getattr(runtime, "context", None) if runtime is not None else None
    if context is None:
        return NO_PERMISSION_MESSAGE
    cache_key = (query_text or "").strip()
    cache = getattr(context, RESULT_ATTR, None)
    if isinstance(cache, dict) and cache_key in cache:
        return cache[cache_key]

    config = await _resolve_kg_config()
    if config is None:
        return PLAN_GRAPH_NOT_CONFIGURED_MESSAGE

    emitter = ThinkingEmitter(agent_slug=MAINTENANCE_PLAN_AGENT_SLUG, steps=MAINTENANCE_PLAN_THINKING_STEPS)
    emitter.emit("parse_task", "started", content="正在解析运维任务需求。")

    start = time.monotonic()
    model_call = _context_model_call(context) if MAINTENANCE_PLAN_INTENT_ENABLED else None
    query = await extract_plan_intent(query_text, model_call=model_call)
    emitter.emit(
        "parse_task",
        "completed",
        content=_task_summary(query),
        data={
            "device_name": query.device_name,
            "device_type": query.device_type,
            "location_hint": query.location_hint,
            "task_type": query.task_type,
            "maintenance_level": query.maintenance_level,
        },
    )

    # 整次请求共享的引导选择轮次预算：重放时工具从头再跑，已提交的 interrupt 依次返回值并各扣一轮。
    rounds = [0]
    emitter.emit("retrieve_graph", "started", content="正在检索目标设备与检修作业场景。")
    client = TuGraphClient(config)
    try:
        located = await _locate_device(
            client,
            query,
            model_call=model_call,
            rounds=rounds,
            emitter=emitter,
            graph_key=device_graph_key(config),
        )
        if isinstance(located, str):
            emitter.emit(
                "retrieve_graph",
                "failed",
                content=located,
                error={"code": _device_choice_error_code(located), "message": located},
            )
            return located
        query, device = located

        scenes = await asyncio.to_thread(_graph_plan().find_scenes, client, device)
        if not scenes:
            emitter.emit(
                "retrieve_graph",
                "failed",
                content=NO_SCENE_MESSAGE,
                error={"code": "scene_not_found", "message": NO_SCENE_MESSAGE},
            )
            return _remember(context, cache_key, NO_SCENE_MESSAGE)
        scene = _choose_scene(scenes, query, rounds, emitter=emitter)
        if isinstance(scene, str):
            emitter.emit(
                "retrieve_graph",
                "failed",
                content=scene,
                error={
                    "code": "scene_choice_cancelled" if scene == CHOICE_CANCELLED_MESSAGE else "scene_choice_aborted",
                    "message": scene,
                },
            )
            return _remember(context, cache_key, scene)

        repository = MaintenanceDataRepository()
        ledger_ids = await repository.resolve_ledger_ids(device)
        branch_device = replace(device, ledger_ids=ledger_ids) if ledger_ids != device.ledger_ids else device
        branch = await _resolve_branch(branch_device, scene)
        if isinstance(branch, str):
            emitter.emit(
                "retrieve_graph",
                "failed",
                content=branch,
                error={"code": "branch_unresolved", "message": branch},
            )
            return _remember(context, cache_key, branch)

        evidence = await asyncio.to_thread(_graph_plan().collect_scene_evidence, client, query, device, scene, branch)
        if not (evidence.steps or evidence.safety or evidence.resources or evidence.personnel):
            # 场景缺全部子节点时不得发布空方案：按“暂无场景”固定文案收口（fail-closed）。
            emitter.emit(
                "retrieve_graph",
                "failed",
                content=NO_SCENE_MESSAGE,
                error={"code": "scene_content_empty", "message": NO_SCENE_MESSAGE},
            )
            return _remember(context, cache_key, NO_SCENE_MESSAGE)
    except TuGraphError as exc:
        logger.warning(f"处置方案图谱查询失败: {exc}")
        emitter.emit(
            "retrieve_graph",
            "failed",
            content=PLAN_GRAPH_NOT_CONFIGURED_MESSAGE,
            error={"code": "graph_unavailable", "message": str(exc)},
        )
        return _remember(context, cache_key, PLAN_GRAPH_NOT_CONFIGURED_MESSAGE)
    finally:
        client.close()

    emitter.emit(
        "retrieve_graph",
        "completed",
        content=(
            f"已定位设备“{device.name}”，匹配检修作业场景“{scene.name}”，"
            f"读取 {len(evidence.steps)} 道步骤、{len(evidence.safety)} 条安全措施、"
            f"{len(evidence.resources)} 项资源、{sum(item.count for item in evidence.personnel)} 人。"
        ),
        data={
            "device": device.name,
            "device_id": device.device_id,
            "device_type": device.device_type,
            "scene_id": scene.scene_id,
            "scene": scene.name,
            "branch_dimension": scene.branch_dimension,
            "branch": branch.branch if branch is not None else "",
            "branch_value": branch.condition if branch is not None else "",
            "branch_source": branch.source if branch is not None else "",
        },
    )

    emitter.emit("assemble_plan", "started", content="正在按执行分支组装方案模板。")
    tools = await repository.load_tools()
    evidence.resources, tools_note = annotate_resources(evidence.resources, tools)
    personnel = await repository.load_teams()
    assignment = assign_personnel(evidence.personnel, personnel, evidence.device.path)
    emitter.emit(
        "assemble_plan",
        "completed",
        content=(f"已组装检修步骤、安全措施、资源清单与人员安排；人员合计 {assignment.total} 人。"),
        data={
            "step_count": len(evidence.steps),
            "safety_count": len(evidence.safety),
            "resource_count": len(evidence.resources),
            "personnel_total": assignment.total,
            "tools_data_note": tools_note,
        },
    )

    data_notes = [note for note in (tools_note, assignment.note) if note]
    emitter.emit("generate_plan", "started", content="正在校验证据并生成标准化方案。")
    result = produce_maintenance_plan_answer(
        evidence,
        assignment,
        data_notes=data_notes,
        elapsed_seconds=round(time.monotonic() - start, 1),
    )
    if not isinstance(result, dict) or result.get("answer_verified") is not True:
        logger.warning("处置方案证据校验未通过，只返回固定文案，不返回未校验的报告")
        emitter.emit(
            "generate_plan",
            "failed",
            content=PLAN_VERIFICATION_FAILED_MESSAGE,
            error={"code": "verification_failed", "message": PLAN_VERIFICATION_FAILED_MESSAGE},
        )
        return _remember(context, cache_key, PLAN_VERIFICATION_FAILED_MESSAGE)
    emitter.emit(
        "generate_plan",
        "completed",
        content="方案已通过分支、条目与数值校验。",
        data={"answer_verified": True, "answer_source": "maintenance_plan"},
    )
    result["thinking_steps"] = emitter.snapshot()
    return _remember(context, cache_key, result)


# ---------------------------------------------------------------------------
# 引导选择：设备 / 场景 / 分支
# ---------------------------------------------------------------------------
def _task_summary(query: PlanQuery) -> str:
    """解析步骤的完成文案：只陈述已识别字段，不推测未识别字段。"""

    parts: list[str] = []
    if query.device_name:
        parts.append(f"设备“{query.device_name}”")
    elif query.device_type:
        parts.append(f"设备类型“{query.device_type}”")
    if query.location_hint:
        parts.append(f"位置“{query.location_hint}”")
    if query.maintenance_level:
        parts.append(f"{query.maintenance_level} 级检修")
    if query.task_type:
        parts.append(f"{query.task_type}任务")
    return "已识别" + "、".join(parts) + "。" if parts else "任务信息不完整，等待补充。"


class _ChoiceCancelled:
    """用户关闭选择卡片的取消结果；与“轮次预算用尽”的 None 区分。"""


CHOICE_CANCELLED = _ChoiceCancelled()


def _ask_choice(
    payload: dict[str, Any], question_id: str, rounds: list[int]
) -> tuple[str | None, str | None] | None | _ChoiceCancelled:
    """发一次引导选择中断并解析恢复值；整次请求的轮次预算已用尽时返回 None（不发中断）。

    关闭卡片提交的是 ``reject`` 这类取消值，先判取消再解析：否则会被当成用户补充的设备名，
    带着 ``reject`` 重新识别并再次弹卡片。
    """

    if rounds[0] >= MAX_CHOICE_ROUNDS:
        return None
    rounds[0] += 1
    raw = interrupt(payload)
    if is_question_cancel(raw):
        return CHOICE_CANCELLED
    return _parse_choice(raw, question_id)


async def _locate_device(
    client: TuGraphClient,
    query: PlanQuery,
    *,
    model_call: Callable[[str], Awaitable[str]] | None,
    rounds: list[int],
    emitter: ThinkingEmitter,
    graph_key: str = "",
) -> tuple[PlanQuery, DeviceCandidate] | str:
    """检索设备并由用户确认；用户补充文字后带上上一轮结果重新检索。

    多候选（含词元/索引兜底召回）时先用模型在候选内选一次，选中项排到第一位并标记推荐；
    无论候选多少都由用户确认，模型选择不替代人工确认。
    """

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
                _graph_plan().search_devices,
                client,
                query,
                graph_key=graph_key,
                ledger_candidates=ledger_candidates,
            )
        if not candidates:
            logger.warning(
                f"处置方案设备召回为空: device_name={query.device_name!r} "
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
        picked = _pick_device(
            candidates,
            query,
            rounds,
            recommended_id=recommended_id,
            selection=selection,
        )
        if picked is CHOICE_CANCELLED:
            return CHOICE_CANCELLED_MESSAGE
        if isinstance(picked, DeviceCandidate):
            return query, picked
        if isinstance(picked, SelectedDeviceId):
            selected = await asyncio.to_thread(
                _graph_plan().find_device_by_id,
                client,
                picked.device_id,
                query=query,
                ledger_candidates=ledger_candidates,
            )
            if selected is None:
                emitter.emit(
                    "retrieve_graph",
                    "failed",
                    content=SELECTED_DEVICE_UNAVAILABLE_MESSAGE,
                    error={"code": "selected_device_unavailable", "message": SELECTED_DEVICE_UNAVAILABLE_MESSAGE},
                )
                return SELECTED_DEVICE_UNAVAILABLE_MESSAGE
            return query, selected
        if picked:
            logger.info(f"处置方案用户改填设备，清除上一轮设备身份: {picked!r}")
            query = await extract_plan_intent(
                picked,
                model_call=model_call,
                previous=query,
                feedback=picked,
                device_override=picked,
            )
            continue
        if rounds[0] >= MAX_CHOICE_ROUNDS:
            return CHOICE_ABORTED_MESSAGE


async def _select_device_candidate(
    candidates: list[DeviceCandidate],
    *,
    query: PlanQuery,
    model_call: Callable[[str], Awaitable[str]] | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """模型在候选内选择设备；返回 (推荐候选 id, 选择状态信息)。"""

    options = [
        DeviceOption(
            device_id=candidate.device_id,
            name=candidate.name,
            device_type=candidate.device_type,
            voltage_level=candidate.voltage_level,
            path=candidate.path,
            category=candidate.ledger_class,
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
    query: PlanQuery,
    rounds: list[int],
    *,
    recommended_id: str | None = None,
    selection: dict[str, Any] | None = None,
) -> DeviceCandidate | SelectedDeviceId | str | None | _ChoiceCancelled:
    """每次都由用户确认目标设备，唯一候选或名称正好命中也不自动采用。

    图谱里存在同名/同前缀设备，自动确认一旦选错，后续场景、分支和人员都会按错误设备生成，
    用户很难从结果里发现。卡片允许用户改选候选或自行填写设备名。
    """

    if rounds[0] >= MAX_CHOICE_ROUNDS:
        return None
    rounds[0] += 1
    answer = interrupt(_device_payload(query, candidates, recommended_id=recommended_id))
    choice = parse_device_choice(answer, DEVICE_QUESTION_ID)
    if choice.cancelled:
        return CHOICE_CANCELLED
    if choice.selected_id:
        matched = next((candidate for candidate in candidates if candidate.device_id == choice.selected_id), None)
        if matched is not None:
            return matched
        return SelectedDeviceId(choice.selected_id)
    if choice.free_text:
        return choice.free_text
    return choice.selected_id


def _device_choice_error_code(message: str) -> str:
    """设备选择终止文案 → 思考步骤错误码：区分用户取消、轮次用尽与真的没找到。"""

    if message == CHOICE_CANCELLED_MESSAGE:
        return "device_choice_cancelled"
    if message == CHOICE_ABORTED_MESSAGE:
        return "choice_aborted"
    if message == SELECTED_DEVICE_UNAVAILABLE_MESSAGE:
        return "selected_device_unavailable"
    return "device_not_found"


def _device_payload(
    query: PlanQuery,
    candidates: list[DeviceCandidate],
    *,
    recommended_id: str | None = None,
) -> dict[str, Any]:
    """设备选择卡片：展示前 MAX_DEVICE_CANDIDATES 个候选，value 用设备 id 便于回填。

    选项 label 只放设备名/类型/电压，层级路径作为 description 供同名设备区分，避免把
    “目标设备”显示成一长串路径；模型选中的候选在 label 末尾标记“（推荐）”。
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
                path=candidate.location(),
                category=candidate.ledger_class,
            )
            for candidate in candidates
        ],
        recommended_id=recommended_id,
        limit=MAX_DEVICE_CANDIDATES,
    )
    payload["query"] = query.model_dump()
    return payload


def _choose_scene(
    scenes: list[Scenario], query: PlanQuery, rounds: list[int], *, emitter: ThinkingEmitter
) -> Scenario | str:
    """场景选择：唯一候选或唯一最高分直接采用；并列/0 分发中断让用户选。"""

    ranked = _graph_plan().rank_scenes(scenes, query)
    while True:
        if not ranked:
            return NO_SCENE_MESSAGE
        if len(ranked) == 1:
            return ranked[0]
        top = ranked[0]
        second = ranked[1]
        if top.score > 0 and top.score > second.score:
            return top

        emitter.emit(
            "retrieve_graph",
            "waiting",
            content="等待用户选择检修作业场景。",
            data={"source": SCENE_CHOICE_SOURCE, "candidate_count": len(ranked)},
        )
        answer = _ask_choice(_scene_payload(query, ranked), SCENE_QUESTION_ID, rounds)
        if answer is CHOICE_CANCELLED:
            return CHOICE_CANCELLED_MESSAGE
        if answer is None:
            return CHOICE_ABORTED_MESSAGE
        selected, free_text = answer
        if selected:
            matched = next((scene for scene in ranked if scene.scene_id == selected), None)
            if matched is not None:
                return matched
        if free_text:
            query.raw_text = f"{query.raw_text}；{free_text}" if query.raw_text else free_text
            ranked = _graph_plan().rank_scenes(scenes, query)
            continue


def _scene_payload(query: PlanQuery, scenes: list[Scenario]) -> dict[str, Any]:
    """场景选择卡片：名称（等级，分支维度），value 用场景 id。"""

    return {
        "source": SCENE_CHOICE_SOURCE,
        "questions": [
            {
                "question_id": SCENE_QUESTION_ID,
                "question": SCENE_CHOICE_QUESTION,
                "options": [
                    {"label": scene.label(), "value": scene.scene_id} for scene in scenes[:MAX_SCENE_CANDIDATES]
                ],
                "multi_select": False,
                "allow_other": True,
            }
        ],
        "query": query.model_dump(),
    }


async def _resolve_branch(device: DeviceCandidate, scenario: Scenario) -> BranchSelection | None | str:
    """分支判定：只读 Redis；未接入/无值/非法时返回固定失败文案，不默认分支。"""

    if not scenario.branch_dimension or not scenario.branch1_condition or not scenario.branch2_condition:
        return None

    lookup = await lookup_branch(device, scenario)
    if lookup.state == "ok":
        return BranchSelection(
            dimension=scenario.branch_dimension,
            condition=lookup.value,
            branch=lookup.branch,
            source="redis",
            ledger_id=lookup.ledger_id,
        )
    if lookup.state == "not_required":
        return None
    logger.warning(
        "分支判定未命中 Redis：设备=%s 台账ID=%s 维度=%s 状态=%s 原因=%s",
        device.name,
        device.ledger_ids,
        scenario.branch_dimension,
        lookup.state,
        lookup.reason,
    )
    return BRANCH_UNRESOLVED_MESSAGE


def _parse_choice(value: Any, question_id: str) -> tuple[str | None, str | None]:
    """解析 interrupt 恢复值，返回 ``(选中值, 自由文字)``。

    兼容 ``{question_id: ...}`` 嵌套、``{"selected": [...]}``、``{"value": ...}``、
    ``{"text": ...}``、列表与纯字符串；``"__other__" / "other" / "其他"`` 视为未选中。
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

    return text.strip().lower() in {"__other__", "other", "其他"}


# ---------------------------------------------------------------------------
# 配置、模型调用与缓存
# ---------------------------------------------------------------------------
async def _resolve_kg_config() -> TuGraphConfig | None:
    """读取管理员配置的 TuGraph 连接；处置方案图默认图名由配置决定，关键字段缺失时返回 None。"""

    try:
        resolved = await fault_trace_opts.get()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取处置方案知识图谱配置失败: {exc}")
        return None
    uri = str(resolved.get("server_url") or "").strip()
    graph = str(resolved.get("maintenance_plan_graph") or os.getenv("MAINTENANCE_PLAN_GRAPH") or "kg5").strip()
    username = str(resolved.get("username") or "").strip()
    password = str(resolved.get("password") or "").strip()
    if not uri or not graph:
        return None
    if not username or not password:
        return None
    return TuGraphConfig(uri=uri, graph=graph, username=username, password=password)


def _context_model_call(context: Any) -> Callable[[str], Awaitable[str]] | None:
    """构造按当前 Agent 模型执行的非流式单次调用；上下文没有模型标识时返回 None。"""

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


def _remember(context: Any, cache_key: str, result: Any) -> Any:
    """把最终结果按 query_text 缓存到对话上下文，避免模型在同一轮内对同一描述重复调用工具。"""

    cache = getattr(context, RESULT_ATTR, None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(context, RESULT_ATTR, cache)
    cache[cache_key] = result
    return result
