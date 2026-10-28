"""故障溯源显式工作流：按解析、因果链、拓扑、报告四步固定执行。

模型只在意图解析与文本生成节点介入；控制流、证据门、实时值求值与报告装配
由本模块和各自的能力模块完成，工作流状态见 ``FaultTraceState``。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from langchain.messages import HumanMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import interrupt

from yuxi.agents.thinking import ThinkingEmitter
from yuxi.knowledge.fault_trace.answer import (
    build_fault_trace_payload,
    build_summary_text,
    factor_phenomenon,
    supporting_phenomena,
)
from yuxi.knowledge.fault_trace.contracts import (
    DEVICE_AMBIGUOUS_MESSAGE,
    DEVICE_NOT_FOUND_MESSAGE,
    DEVICE_TYPE_MISSING_MESSAGE,
    ENTITY_CAUSE,
    ENTITY_FAULT_MODE,
    ENTITY_PHENOMENON,
    ENTITY_TRIGGER,
    FAULT_GRAPH_UNAVAILABLE_MESSAGE,
    IMAGE_NOT_FOUND_MESSAGE,
    IMAGE_NO_FAULT_MESSAGE,
    IMAGE_SERVICE_UNAVAILABLE_MESSAGE,
    INTENT_PLAN_CHANGED_MESSAGE,
    NO_FAULT_GRAPH_MESSAGE,
    REALTIME_UNAVAILABLE_STATUSES,
    REL_CAUSE_TYPE,
    REL_MEASURE_TYPE,
    FaultTraceQuery,
)
from yuxi.knowledge.fault_trace.graph import (
    FaultTopology,
    trace_topology_evidence,
)
from yuxi.agents.toolkits.tugraph import (
    TuGraphClient,
    TuGraphConfig,
    TuGraphError,
)
from yuxi.knowledge.fault_trace.graph_kg3 import (
    collect_kg3_evidence,
    device_ancestors,
    find_devices,
    resolve_device_type,
)
from yuxi.knowledge.fault_trace.images import DeviceImageError, resolve_device_image
from yuxi.knowledge.fault_trace.intent import (
    MAX_INTENT_ATTEMPTS,
    build_confirmation_payload,
    build_correction_payload,
    build_intent_plan,
    extract_correction_text,
    extract_fault_trace_intent,
    parse_confirmation_token,
    parse_intent_confirmation,
    plan_token,
)
from yuxi.knowledge.fault_trace.ledger import LedgerDevice, ledger_repository
from yuxi.knowledge.fault_trace.realtime import read_realtime_values
from yuxi.knowledge.fault_trace.report_text import compose_location_reasoning, compose_summary_text
from yuxi.knowledge.fault_trace.rules import (
    evaluate_causes,
    located_causes,
    missing_attributes,
    referenced_attributes,
    trigger_summary,
)
from yuxi.services.yolo_service import YoloServiceError, detect_fault_image
from yuxi.utils import logger

FAULT_TRACE_INTENT_ENABLED = os.getenv("FAULT_TRACE_INTENT_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
FAULT_TRACE_INTENT_TIMEOUT_SECONDS = float(os.getenv("FAULT_TRACE_INTENT_TIMEOUT", "300"))
# 输出额度与提示词共享服务端 max_model_len（当前 20480），需为提示词留出余量。
FAULT_TRACE_INTENT_MAX_TOKENS = int(os.getenv("FAULT_TRACE_INTENT_MAX_TOKENS", "4096"))

_IMAGE_WARNING_TEXTS = {
    "storage_unavailable": "设备图片存储暂时不可用，未使用设备图片",
    "yolo_unavailable": "故障图片识别服务暂时不可用，未使用设备图片",
    "image_too_large": "设备图片超过大小上限，未使用设备图片",
}


@dataclass
class _PreparedDevice:
    """设备已通过图谱校验、可用于后续溯源步骤的上下文。"""

    query: FaultTraceQuery
    device: dict[str, Any]
    device_type_name: str
    device_type_node: dict[str, Any]
    ledger_device: LedgerDevice | None


@dataclass
class FaultTraceState:
    """一次故障溯源工作流的显式状态；四个步骤只读写这里。"""

    query_text: str
    confirmed_intent: bool
    context: Any
    runtime: ToolRuntime | None
    config: TuGraphConfig
    started_at: float
    emitter: ThinkingEmitter
    image_payload: dict[str, Any] | None = None
    image_source: str = "none"
    image_object_key: str | None = None
    image_warning: str | None = None
    model_call: Callable[[str], Awaitable[str]] | None = None
    query: FaultTraceQuery | None = None
    device: dict[str, Any] | None = None
    device_type_name: str | None = None
    device_type_node: dict[str, Any] | None = None
    ledger_device: LedgerDevice | None = None
    device_data: dict[str, Any] | None = None
    summary_text: str | None = None
    evidence: Any = None
    values: dict[str, Any] = field(default_factory=dict)
    cause_evaluations: list[Any] = field(default_factory=list)
    matched_phenomena: list[dict[str, Any]] = field(default_factory=list)
    reasoning_rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    located_all: list[Any] = field(default_factory=list)
    located: Any = None
    supporting: list[str] = field(default_factory=list)
    cause_measures: dict[str, list[str]] = field(default_factory=dict)
    topology: FaultTopology | None = None
    payload: dict[str, Any] | None = None


class _WorkflowExit(Exception):
    """某一步产生终态结果（澄清载荷或固定文案）时结束工作流。"""

    def __init__(self, result: Any) -> None:
        super().__init__("workflow exited")
        self.result = result


async def run_fault_trace(state: FaultTraceState) -> Any:
    """按固定四步执行：解析、因果链、拓扑、报告。"""

    client = TuGraphClient(state.config)
    try:
        await _parse_fault(state, client)
        await _match_causal_chain(state, client)
    except _WorkflowExit as exc:
        return exc.result
    finally:
        client.close()

    _trace_topology(state)
    return await _generate_report(state)


async def _parse_fault(state: FaultTraceState, client: TuGraphClient) -> None:
    state.model_call = _context_model_call(state.context)

    # 上传图片先由服务端识别，再进入意图识别；模型自始至终拿不到图片结果参数。
    uploaded_payload, uploaded_warning, uploaded_attempted = await _detect_uploaded_image(state.runtime)
    if uploaded_payload is not None:
        state.image_payload = uploaded_payload
        state.image_source = "user_upload"
    state.image_warning = uploaded_warning

    prepared = await _prepare_device(
        client,
        query_text=state.query_text,
        image_payload=state.image_payload,
        context=state.context,
        emitter=state.emitter,
        confirmed_intent=state.confirmed_intent,
    )
    if isinstance(prepared, str):
        raise _WorkflowExit(prepared)
    if isinstance(prepared, dict):
        raise _WorkflowExit(prepared)

    state.query = prepared.query
    state.device = prepared.device
    state.device_type_name = prepared.device_type_name
    state.device_type_node = prepared.device_type_node
    state.ledger_device = prepared.ledger_device

    if not uploaded_attempted:
        ledger_payload, object_key, ledger_warning = await _resolve_ledger_image(state.device, state.ledger_device)
        if ledger_payload is not None:
            state.image_payload = ledger_payload
            state.image_source = "minio"
            state.image_object_key = object_key
        state.image_warning = ledger_warning or state.image_warning
    if state.image_payload and state.image_payload.get("fault_name"):
        state.query.image_fault_name = str(state.image_payload["fault_name"]).strip() or None
        state.query.image_fault_type = str(state.image_payload.get("fault_type") or "").strip() or None
    elif state.image_payload:
        state.query.image_fault_name = None
        state.query.image_fault_type = None

    state.device_data = _device_data(state.device, state.device_type_name, state.ledger_device)
    if not state.query.phenomena and not state.query.image_fault_name:
        message = _image_missing_message(state.image_payload, state.image_warning)
        state.emitter.emit(
            "parse_fault",
            "waiting",
            content=message,
            data={
                "reason": "phenomenon_missing",
                "question": message,
                "needs_clarification": True,
                "device": state.device_data,
                "image": _image_data(state.image_payload, source=state.image_source, object_key=state.image_object_key),
            },
        )
        raise _WorkflowExit(
            {
                "query": state.query.model_dump(),
                "clarification": message,
                "needs_clarification": True,
                "thinking_steps": state.emitter.snapshot(),
            }
        )

    state.summary_text = await compose_summary_text(
        query_text=state.query_text,
        device_name=str(state.device.get("name") or state.query.device_name or ""),
        device_type=state.device_type_name,
        phenomena=list(state.query.phenomena),
        image_fault_name=state.query.image_fault_name,
        model_call=state.model_call,
    )
    state.emitter.emit(
        "parse_fault",
        "completed",
        content=_fault_intent_summary(state.query, state.device_data, state.image_payload),
        data={
            "device": state.device_data,
            "candidates": [],
            "image": _image_data(state.image_payload, source=state.image_source, object_key=state.image_object_key),
            "phenomena": list(state.query.phenomena),
            "summary": state.summary_text
            or build_summary_text(state.query, state.device_data, factor_phenomenon(state.query, None)),
            "needs_clarification": False,
            "clarification": "",
        },
    )


async def _match_causal_chain(state: FaultTraceState, client: TuGraphClient) -> None:
    state.emitter.emit("match_causal_chain", "started", content="正在匹配故障因果链并求值实时判据。")
    try:
        state.evidence = await asyncio.to_thread(
            collect_kg3_evidence,
            client,
            devices=[state.device],
            device_type=state.device_type_node,
        )
    except TuGraphError as exc:
        logger.warning(f"故障溯源因果链查询失败: {exc}")
        state.emitter.emit(
            "match_causal_chain",
            "failed",
            content=FAULT_GRAPH_UNAVAILABLE_MESSAGE,
            error={"code": "graph_unavailable", "message": FAULT_GRAPH_UNAVAILABLE_MESSAGE},
        )
        raise _WorkflowExit(FAULT_GRAPH_UNAVAILABLE_MESSAGE) from exc

    evidence = state.evidence
    modes = evidence.nodes_with_label(ENTITY_FAULT_MODE)
    phenomena = evidence.nodes_with_label(ENTITY_PHENOMENON)
    trigger_nodes = evidence.nodes_with_label(ENTITY_TRIGGER)
    causes = evidence.nodes_with_label(ENTITY_CAUSE)
    if not phenomena or not causes:
        state.emitter.emit(
            "match_causal_chain",
            "failed",
            content=NO_FAULT_GRAPH_MESSAGE,
            error={"code": "no_fault_evidence", "message": NO_FAULT_GRAPH_MESSAGE},
        )
        raise _WorkflowExit(NO_FAULT_GRAPH_MESSAGE)

    attributes = _referenced_attributes(trigger_nodes)
    state.values = await _read_device_values(
        state.device,
        attributes,
        ledger_id=str(state.ledger_device.device_id) if state.ledger_device is not None else "",
    )
    state.cause_evaluations = evaluate_causes(evidence, state.values)
    state.matched_phenomena = _match_phenomena(phenomena, state.query, state.image_payload)
    state.reasoning_rows = _reasoning_rows(evidence, phenomena, state.cause_evaluations)
    state.warnings = _step_warnings(evidence, state.matched_phenomena, state.values)
    if state.image_warning in _IMAGE_WARNING_TEXTS:
        state.warnings.append(_IMAGE_WARNING_TEXTS[state.image_warning])
    state.located_all = located_causes(state.cause_evaluations)
    state.located = state.located_all[0] if state.located_all else None
    state.supporting = supporting_phenomena(state.reasoning_rows, state.located_all)
    state.cause_measures = _cause_measures(evidence)
    state.emitter.emit(
        "match_causal_chain",
        "completed",
        content=_causal_chain_summary(modes, phenomena, state.cause_evaluations),
        data={
            "device_type": state.device_type_name,
            "fault_modes": [{"mode_id": str(node["id"]), "name": str(node.get("name") or "")} for node in modes],
            "phenomena": [
                {
                    "phenomenon_id": str(node["id"]),
                    "name": str(node.get("name") or ""),
                    "matched": node in state.matched_phenomena,
                }
                for node in phenomena
            ],
            "causes": [
                {
                    "cause_id": cause.cause_id,
                    "name": cause.name,
                    "status": cause.status,
                    "true_count": cause.true_count,
                    "trigger_summary": trigger_summary(cause),
                }
                for cause in state.cause_evaluations
            ],
            "triggers": [
                {
                    "trigger_id": trigger.trigger_id,
                    "name": trigger.name,
                    "expression": trigger.expression,
                    "result": trigger.result,
                    "reason": trigger.reason,
                }
                for cause in state.cause_evaluations
                for trigger in cause.triggers
            ],
            "missing_data": missing_attributes(state.cause_evaluations),
            "warnings": state.warnings,
        },
    )


def _trace_topology(state: FaultTraceState) -> None:
    state.topology = trace_topology_evidence(state.evidence, [str(state.device.get("id") or "")])
    state.emitter.emit("trace_topology", "started", content="正在追溯设备上下游拓扑。")
    state.emitter.emit(
        "trace_topology",
        "completed",
        content=_topology_summary(state.topology),
        data=_topology_data(state.topology),
    )


async def _generate_report(state: FaultTraceState) -> dict[str, Any]:
    state.emitter.emit("generate_report", "started", content="正在生成故障溯源报告。")
    location_reasoning = await compose_location_reasoning(
        query_text=state.query_text,
        device_name=str(state.device.get("name") or ""),
        located_causes=state.located_all,
        causes=state.cause_evaluations,
        phenomena=state.supporting,
        model_call=state.model_call,
    )
    payload = build_fault_trace_payload(
        query=state.query,
        device=state.device_data,
        device_type=state.device_type_name,
        reasoning_rows=state.reasoning_rows,
        causes=state.cause_evaluations,
        topology=state.topology,
        cause_links=state.evidence.cause_links,
        image_used=bool(state.image_payload and state.image_payload.get("fault_name")),
        elapsed_ms=int((monotonic() - state.started_at) * 1000),
        warnings=state.warnings,
        summary_text=state.summary_text,
        location_reasoning=location_reasoning,
        cause_measures=state.cause_measures,
        graph=state.config.graph,
    )
    state.emitter.emit(
        "generate_report",
        "completed",
        content="已生成故障溯源报告。",
        data={
            "located_cause": (
                {"cause_id": state.located.cause_id, "name": state.located.name} if state.located is not None else None
            ),
            "located_causes": [
                {"cause_id": cause.cause_id, "name": cause.name, "true_count": cause.true_count}
                for cause in state.located_all
            ],
            "verdict": payload["report"]["location"]["verdict"],
            "reasoning": payload["report"]["location"]["reasoning"],
            "report": payload["report"],
            "answer_markdown": payload["answer_markdown"],
        },
    )
    payload["thinking_steps"] = state.emitter.snapshot()
    state.payload = payload
    return payload


def _clarify(
    emitter: ThinkingEmitter,
    query: FaultTraceQuery,
    fallback: str,
    image_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message = query.clarification_message or fallback
    emitter.emit(
        "parse_fault",
        "waiting",
        content=message,
        data={
            "reason": "needs_clarification",
            "question": message,
            "clarification": message,
            "needs_clarification": True,
            "device": _fault_device_data(query),
            "image": _intent_image_data(image_payload),
        },
    )
    return {
        "query": query.model_dump(),
        "clarification": message,
        "needs_clarification": True,
        "thinking_steps": emitter.snapshot(),
    }


def _fault_device_data(query: FaultTraceQuery) -> dict[str, Any]:
    return {
        "name": query.device_name,
        "device_type": query.device_type,
        "voltage_level": query.voltage_level,
    }


async def _disambiguate_devices(
    client: TuGraphClient,
    devices: list[dict[str, Any]],
    query_text: str,
) -> list[dict[str, Any]]:
    """同名设备按上级变电站名称在问题中的出现消歧。"""

    text = "".join(str(query_text).split())
    matched: list[dict[str, Any]] = []
    for device in devices:
        ancestors = await asyncio.to_thread(device_ancestors, client, device)
        if any(ancestor and "".join(ancestor.split()) in text for ancestor in ancestors):
            matched.append(device)
    return matched if len(matched) == 1 else devices


def _ledger_ids(device: dict[str, Any]) -> list[str]:
    """设备可能对应多个台账 ID（``台账ID列表`` 以顿号/逗号分隔），逐个作为 Redis key 前缀。"""

    properties = device.get("properties") or {}
    raw = str(properties.get("台账ID列表") or "")
    ids = [item.strip() for item in re.split(r"[,，、;；\s]+", raw) if item.strip()]
    graph_id = str(device.get("id") or "").removeprefix("LGR#")
    if graph_id and graph_id not in ids:
        ids.append(graph_id)
    return ids


def _ledger_id(device: dict[str, Any]) -> str:
    """Redis key 使用台账 ID；kg3 设备节点把它放在 ``台账ID列表``。"""

    ids = _ledger_ids(device)
    return ids[0] if ids else ""


async def _read_device_values(
    device: dict[str, Any],
    attributes: list[str],
    *,
    ledger_id: str = "",
) -> dict[str, Any]:
    """按台账设备 ID（优先）与图谱记录的所有 ID 读取实时值；ok 值合并。"""

    ids = [item for item in (ledger_id, *_ledger_ids(device)) if item]
    merged: dict[str, Any] = {}
    for current_id in list(dict.fromkeys(ids))[:6]:
        current = await read_realtime_values(current_id, attributes)
        for attribute, value in current.items():
            if attribute not in merged or (not merged[attribute].ok and value.ok):
                merged[attribute] = value
        if merged and all(value.ok for value in merged.values()):
            break
    return merged


def _uploaded_image_bytes(runtime: ToolRuntime | None) -> tuple[bytes, str] | None:
    """从图状态最近一条用户消息里取出上传图片（字节与 MIME 类型）。"""

    state = getattr(runtime, "state", None)
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    for message in reversed(list(messages or [])):
        if not isinstance(message, HumanMessage):
            continue
        return _message_image_payload(message)
    return None


def _message_image_payload(message: Any) -> tuple[bytes, str] | None:
    for block in getattr(message, "content_blocks", None) or []:
        if not isinstance(block, dict) or block.get("type") not in {"image", "image_url", "input_image"}:
            continue
        mime_type = str(block.get("mime_type") or "image/jpeg")
        encoded = block.get("base64")
        if isinstance(encoded, str) and encoded:
            try:
                return base64.b64decode(encoded, validate=False), mime_type
            except (binascii.Error, ValueError) as exc:
                logger.warning(f"上传图片 base64 解析失败: {exc}")
                return None
        url = block.get("url")
        if not isinstance(url, str) and isinstance(block.get("image_url"), dict):
            url = block["image_url"].get("url")
        if isinstance(url, str) and url.startswith("data:"):
            header, _, payload = url.partition(",")
            if "base64" not in header or not payload:
                return None
            data_mime = header[5:].split(";", 1)[0] or mime_type
            try:
                return base64.b64decode(payload, validate=False), data_mime
            except (binascii.Error, ValueError) as exc:
                logger.warning(f"上传图片 base64 解析失败: {exc}")
                return None
    return None


def _image_missing_message(image_payload: dict[str, Any] | None, image_warning: str | None) -> str:
    """按图片处理结果选择现象缺失的固定文案。"""

    if image_warning in {"storage_unavailable", "yolo_unavailable"}:
        return IMAGE_SERVICE_UNAVAILABLE_MESSAGE
    if image_payload is not None:
        return IMAGE_NO_FAULT_MESSAGE
    return IMAGE_NOT_FOUND_MESSAGE


def _device_data(
    device: dict[str, Any],
    device_type: str | None,
    ledger_device: Any | None = None,
) -> dict[str, Any]:
    properties = device.get("properties") or {}
    ledger_id = str(getattr(ledger_device, "device_id", "") or "")
    return {
        "device_id": ledger_id or _ledger_id(device),
        "graph_id": str(device.get("id") or ""),
        "ledger_id": ledger_id or None,
        "name": str(device.get("name") or ""),
        "device_type": device_type,
        "device_category": str(properties.get("设备分类") or properties.get("台账分类") or "") or None,
        "voltage_level": str(properties.get("电压等级") or getattr(ledger_device, "voltage_level", "") or "") or None,
    }


def _image_data(
    image_payload: dict[str, Any] | None,
    *,
    source: str = "none",
    object_key: str | None = None,
) -> dict[str, Any]:
    """把图片识别结果投影为思考步骤里的图片证据。"""

    if not image_payload:
        return {
            "source": source,
            "object_key": object_key,
            "yolo": None,
            "skipped_reason": "no_image" if source == "none" else None,
        }
    yolo = {
        key: image_payload.get(key)
        for key in ("fault", "fault_type", "fault_name", "confidence")
        if key in image_payload
    }
    return {"source": source, "object_key": object_key, "yolo": yolo, "skipped_reason": None}


def _intent_image_data(image_payload: dict[str, Any] | None) -> dict[str, Any]:
    """意图阶段的图片证据：此时来源只可能是会话上传图片。"""

    return _image_data(image_payload, source="user_upload" if image_payload else "none")


def _fault_intent_summary(
    query: FaultTraceQuery,
    device: dict[str, Any],
    image_payload: dict[str, Any] | None,
) -> str:
    name = str(device.get("name") or query.device_name or "相关设备")
    phenomena = "、".join(query.phenomena) if query.phenomena else "未提供故障现象"
    image_name = str((image_payload or {}).get("fault_name") or "").strip()
    suffix = f"，图片识别到{image_name}" if image_name else ""
    return f"识别到 {name}：{phenomena}{suffix}。"


def _referenced_attributes(trigger_nodes: list[dict[str, Any]]) -> list[str]:
    attributes: list[str] = []
    for node in trigger_nodes:
        expression = str((node.get("properties") or {}).get("表达式") or "")
        for name in referenced_attributes(expression):
            if name not in attributes:
                attributes.append(name)
    return attributes


def _match_phenomena(
    phenomena: list[dict[str, Any]],
    query: FaultTraceQuery,
    image_payload: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    texts = [item.strip() for item in query.phenomena if item.strip()]
    image_name = str((image_payload or {}).get("fault_name") or "").strip()
    if image_name:
        texts.append(image_name)
    matched: list[dict[str, Any]] = []
    for node in phenomena:
        name = str(node.get("name") or "")
        if any(text and (text in name or name in text) for text in texts):
            matched.append(node)
    return matched


def _reasoning_rows(
    evidence: Any,
    phenomena: list[dict[str, Any]],
    evaluations: list[Any],
) -> list[dict[str, Any]]:
    """按现象分组图谱中的原因；每个现象都成行，没有原因时保留空行。"""

    by_id = {cause.cause_id: cause for cause in evaluations}
    if evidence.cause_links == "fault_phenomenon":
        links: dict[str, list[str]] = {}
        for edge in evidence.edges_with_type(REL_CAUSE_TYPE):
            links.setdefault(str(edge.get("source_id")), []).append(str(edge.get("target_id")))
        rows = []
        for node in phenomena:
            linked = [by_id[cause_id] for cause_id in links.get(str(node["id"]), []) if cause_id in by_id]
            rows.append(
                {
                    "phenomenon_id": str(node["id"]),
                    "phenomenon": str(node.get("name") or ""),
                    "link_level": "fault_phenomenon",
                    "causes": linked,
                }
            )
        return rows
    representative = phenomena[0] if phenomena else None
    return [
        {
            "phenomenon_id": str(representative["id"]) if representative is not None else None,
            "phenomenon": (str(representative.get("name") or "") if representative is not None else "故障模式汇总"),
            "link_level": "fault_mode",
            "causes": list(evaluations),
        }
    ]


def _cause_measures(evidence: Any) -> dict[str, list[str]]:
    """按原因收集图谱处置措施的具体说明（``说明``，缺失时退回名称）。"""

    measures: dict[str, list[str]] = {}
    for edge in evidence.edges_with_type(REL_MEASURE_TYPE):
        cause_id = str(edge.get("source_id") or "")
        node = evidence.node_by_id(str(edge.get("target_id") or ""))
        if not cause_id or node is None:
            continue
        properties = node.get("properties") or {}
        description = str(properties.get("说明") or properties.get("措施内容") or node.get("name") or "").strip()
        if description and description not in measures.setdefault(cause_id, []):
            measures[cause_id].append(description)
    return measures


def _step_warnings(
    evidence: Any,
    matched_phenomena: list[dict[str, Any]],
    values: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if evidence.cause_links == "fault_mode":
        warnings.append("故障原因挂在故障模式上（缺少现象→原因边），按模式汇总可能原因")
    if not matched_phenomena:
        if evidence.cause_links == "fault_phenomenon":
            warnings.append("未匹配到用户描述中的故障现象，推理过程列出该设备类型的全部现象")
        else:
            warnings.append("未匹配到具体故障现象，按故障模式汇总可能原因")
    if any(value.status in REALTIME_UNAVAILABLE_STATUSES for value in values.values()):
        warnings.append("实时数据服务暂时不可用，部分原因无法判定")
    return warnings


def _causal_chain_summary(
    modes: list[dict[str, Any]],
    phenomena: list[dict[str, Any]],
    evaluations: list[Any],
) -> str:
    established = sum(1 for cause in evaluations if cause.status == "established")
    unknown = sum(1 for cause in evaluations if cause.status == "unknown")
    mode_part = f"{len(modes)} 个故障模式、" if modes else ""
    return (
        f"匹配到 {mode_part}{len(phenomena)} 个故障现象、{len(evaluations)} 个可能原因；"
        f"{established} 个成立、{unknown} 个待确认。"
    )


def _topology_summary(topology: FaultTopology) -> str:
    upstream = "、".join(str(device.get("name") or "") for device in topology.upstream) or "无"
    downstream = "、".join(str(device.get("name") or "") for device in topology.downstream) or "无"
    return f"上游：{upstream}；下游：{downstream}。"


def _topology_data(topology: FaultTopology) -> dict[str, Any]:
    return {
        "upstream": topology.upstream,
        "downstream": topology.downstream,
        "paths": topology.paths,
        "consequences": topology.consequences,
        "warnings": topology.warnings,
    }


async def _prepare_device(
    client: TuGraphClient,
    *,
    query_text: str,
    image_payload: dict[str, Any] | None,
    context: Any,
    emitter: ThinkingEmitter,
    confirmed_intent: bool,
) -> _PreparedDevice | dict[str, Any] | str:
    """先校验设备存在且可解析类型，再请用户通过选项卡确认；返回澄清载荷或固定文案表示未通过。"""

    model_call = _context_model_call(context) if FAULT_TRACE_INTENT_ENABLED else None
    feedback: str | None = None
    previous: FaultTraceQuery | None = None
    for _ in range(MAX_INTENT_ATTEMPTS):
        query = await extract_fault_trace_intent(
            query_text,
            image_result=image_payload,
            model_call=model_call,
            previous=previous,
            feedback=feedback,
        )
        if query.clarification_required:
            return _clarify(emitter, query, DEVICE_NOT_FOUND_MESSAGE, image_payload)
        try:
            devices = await asyncio.to_thread(find_devices, client, [query.device_name or ""])
        except TuGraphError as exc:
            logger.warning(f"故障溯源设备查询失败: {exc}")
            emitter.emit(
                "parse_fault",
                "failed",
                content=FAULT_GRAPH_UNAVAILABLE_MESSAGE,
                error={"code": "graph_unavailable", "message": FAULT_GRAPH_UNAVAILABLE_MESSAGE},
            )
            return FAULT_GRAPH_UNAVAILABLE_MESSAGE
        if not devices:
            emitter.emit(
                "parse_fault",
                "waiting",
                content=DEVICE_NOT_FOUND_MESSAGE,
                data={
                    "reason": "device_not_found",
                    "question": DEVICE_NOT_FOUND_MESSAGE,
                    "needs_clarification": True,
                    "device": _fault_device_data(query),
                    "image": _intent_image_data(image_payload),
                },
            )
            return {
                "query": query.model_dump(),
                "clarification": DEVICE_NOT_FOUND_MESSAGE,
                "needs_clarification": True,
                "thinking_steps": emitter.snapshot(),
            }
        if len(devices) > 1:
            devices = await _disambiguate_devices(client, devices, query_text)
        if len(devices) > 1:
            candidates = [
                {
                    "device_id": _ledger_id(item),
                    "name": str(item.get("name") or ""),
                    "device_type": str((item.get("properties") or {}).get("设备分类") or "") or None,
                    "voltage_level": str((item.get("properties") or {}).get("电压等级") or "") or None,
                }
                for item in devices[:10]
            ]
            emitter.emit(
                "parse_fault",
                "waiting",
                content=DEVICE_AMBIGUOUS_MESSAGE,
                data={
                    "reason": "device_ambiguous",
                    "question": DEVICE_AMBIGUOUS_MESSAGE,
                    "needs_clarification": True,
                    "candidates": candidates,
                    "device": _fault_device_data(query),
                    "image": _intent_image_data(image_payload),
                },
            )
            return {
                "query": query.model_dump(),
                "clarification": DEVICE_AMBIGUOUS_MESSAGE,
                "candidates": candidates,
                "needs_clarification": True,
                "thinking_steps": emitter.snapshot(),
            }

        device = devices[0]
        try:
            device_type_name, device_type_node = await asyncio.to_thread(resolve_device_type, client, device)
            ancestors = await asyncio.to_thread(device_ancestors, client, device)
        except TuGraphError as exc:
            logger.warning(f"故障溯源设备类型查询失败: {exc}")
            emitter.emit(
                "parse_fault",
                "failed",
                content=FAULT_GRAPH_UNAVAILABLE_MESSAGE,
                error={"code": "graph_unavailable", "message": FAULT_GRAPH_UNAVAILABLE_MESSAGE},
            )
            return FAULT_GRAPH_UNAVAILABLE_MESSAGE
        ledger_device = await ledger_repository.find_device(str(device.get("name") or ""), ancestors=ancestors)
        device_data = _device_data(device, device_type_name, ledger_device)
        if device_type_name is None:
            emitter.emit(
                "parse_fault",
                "failed",
                content=DEVICE_TYPE_MISSING_MESSAGE,
                data={"device": device_data, "image": _intent_image_data(image_payload)},
                error={"code": "device_type_missing", "message": DEVICE_TYPE_MISSING_MESSAGE},
            )
            return DEVICE_TYPE_MISSING_MESSAGE
        prepared = _PreparedDevice(
            query=query,
            device=device,
            device_type_name=device_type_name,
            device_type_node=device_type_node,
            ledger_device=ledger_device,
        )

        intent_plan = build_intent_plan(query)
        intent_plan.update(
            {
                "device_name": str(device.get("name") or query.device_name or ""),
                "device_type": device_type_name,
                "voltage_level": query.voltage_level or str(getattr(ledger_device, "voltage_level", "") or "") or None,
            }
        )
        if confirmed_intent:
            return prepared

        emitter.emit(
            "parse_fault",
            "waiting",
            content="等待用户确认故障设备与现象。",
            data={
                "reason": "needs_confirmation",
                "question": "请确认故障设备与故障现象。",
                "needs_confirmation": True,
                "device": device_data,
                "image": _intent_image_data(image_payload),
                "phenomena": list(query.phenomena),
            },
        )
        answer = interrupt(build_confirmation_payload(intent_plan))
        decision, correction = parse_intent_confirmation(answer)
        if decision == "confirm":
            # resume 会重新识别意图；用户确认的计划指纹随选项值持久化在 checkpoint 里，
            # 重算结果与它不一致时不能静默执行另一份计划。
            confirmed_token = parse_confirmation_token(answer)
            if confirmed_token is not None and confirmed_token != plan_token(intent_plan):
                emitter.emit(
                    "parse_fault",
                    "failed",
                    content=INTENT_PLAN_CHANGED_MESSAGE,
                    error={"code": "intent_plan_changed", "message": INTENT_PLAN_CHANGED_MESSAGE},
                )
                return INTENT_PLAN_CHANGED_MESSAGE
            return prepared
        if decision == "invalid" or not correction:
            correction = extract_correction_text(interrupt(build_correction_payload(intent_plan)))
        if not correction:
            return "意图确认交互过多，未完成故障溯源"
        previous = query
        feedback = correction
    return "意图确认交互过多，未完成故障溯源"


async def _detect_uploaded_image(runtime: ToolRuntime | None) -> tuple[dict[str, Any] | None, str | None, bool]:
    """识别会话上传图片；返回 ``(识别结果, 告警码, 是否尝试过)``。

    智能体配置为服务端处理图片时，上传图片仍保留在会话状态里，这里直接读取并交
    YOLO；模型从未收到 base64，也不能传入识别结果。
    """

    uploaded = _uploaded_image_bytes(runtime)
    if uploaded is None:
        return None, None, False
    data, mime_type = uploaded
    content_type = mime_type if mime_type.startswith("image/") else "image/jpeg"
    extension = "png" if content_type == "image/png" else "webp" if content_type == "image/webp" else "jpg"
    try:
        result = await detect_fault_image(
            image=data,
            filename=f"fault-image.{extension}",
            content_type=content_type,
        )
    except YoloServiceError as exc:
        logger.warning(f"上传图片识别失败: {exc}")
        return None, "yolo_unavailable", True
    return (
        {"fault": result.fault, "fault_type": result.fault_type, "fault_name": result.fault_name},
        None,
        True,
    )


async def _resolve_ledger_image(
    device: dict[str, Any],
    ledger_device: LedgerDevice | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """按台账设备名从 MinIO 取图并识别；返回 ``(识别结果, 对象 key, 告警码)``。"""

    name = str(getattr(ledger_device, "name", "") or device.get("name") or "").strip()
    if not name:
        return None, None, None
    try:
        image = await resolve_device_image(name)
    except DeviceImageError as exc:
        logger.warning(f"读取设备图片失败: {exc}")
        return None, None, exc.code
    if image is None:
        return None, None, None
    try:
        result = await detect_fault_image(
            image=image.data,
            filename=image.object_key.rsplit("/", 1)[-1],
            content_type=image.content_type,
        )
    except YoloServiceError as exc:
        logger.warning(f"设备图片识别失败: {exc}")
        return None, image.object_key, "yolo_unavailable"
    return (
        {"fault": result.fault, "fault_type": result.fault_type, "fault_name": result.fault_name},
        image.object_key,
        None,
    )


def _context_model_call(context: Any) -> Callable[[str], Awaitable[str]] | None:
    """构造按当前 Agent 模型执行的非流式单次调用；上下文没有模型标识时返回 None。"""

    if not getattr(context, "model", None):
        return None

    async def model_call(prompt: str) -> str:
        model = _select_context_model(context, max_tokens=FAULT_TRACE_INTENT_MAX_TOKENS)
        response = await model.ainvoke(
            prompt,
            config={
                "metadata": {"yuxi_internal_model": True, "purpose": "fault_trace_internal"},
                "tags": ["yuxi_internal_model"],
            },
        )
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
