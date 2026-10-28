"""智能体拥有的思考过程步骤、注册表与发射器。

平台只负责传输与校验；步骤数量、标题和顺序由各智能体模块注册。
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal

from langgraph.config import get_stream_writer

from yuxi.thinking import (
    ThinkingStepSpec,
    get_thinking_steps,
    register_thinking_steps,
    thinking_steps_payload,
)
from yuxi.utils import logger

THINKING_EVENT_TYPE = "yuxi.thinking_step"
THINKING_STATES = frozenset({"started", "waiting", "completed", "failed", "skipped"})
THINKING_TERMINAL_STATES = frozenset({"completed", "failed", "skipped"})
ThinkingState = Literal["started", "waiting", "completed", "failed", "skipped"]


__all__ = [
    "THINKING_EVENT_TYPE",
    "THINKING_STATES",
    "ThinkingEmitter",
    "ThinkingStepSpec",
    "get_thinking_steps",
    "normalize_thinking_event",
    "parse_thinking_event",
    "register_thinking_steps",
    "thinking_steps_payload",
    "upsert_thinking_step",
]


def parse_thinking_event(payload: Any) -> dict[str, Any] | None:
    """从工具 custom 事件中取出思考步骤载荷。"""

    if not isinstance(payload, dict) or payload.get("type") != THINKING_EVENT_TYPE:
        return None
    thinking = payload.get("thinking")
    return dict(thinking) if isinstance(thinking, dict) else None


def normalize_thinking_event(thinking: dict[str, Any], steps: tuple[ThinkingStepSpec, ...]) -> dict[str, Any] | None:
    """校验 step/顺序/状态，并用注册表标题覆盖事件标题。"""

    if not steps:
        return None
    step_id = str(thinking.get("step_id") or "")
    state = str(thinking.get("state") or "")
    if not step_id or state not in THINKING_STATES:
        return None
    indexed = {step.step_id: (index, step) for index, step in enumerate(steps, start=1)}
    if step_id not in indexed:
        return None
    expected_index, spec = indexed[step_id]
    if thinking.get("step_index") != expected_index:
        return None
    normalized = dict(thinking)
    normalized["step_index"] = expected_index
    normalized["title"] = spec.title
    normalized["agent"] = str(thinking.get("agent") or "")
    normalized["version"] = str(thinking.get("version") or "v1")
    normalized["content"] = str(thinking.get("content") or "")
    normalized["data"] = thinking.get("data") if isinstance(thinking.get("data"), dict) else {}
    normalized["error"] = thinking.get("error") if isinstance(thinking.get("error"), dict) else None
    return normalized


def upsert_thinking_step(steps: list[dict[str, Any]], thinking: dict[str, Any]) -> None:
    """按 step_id 覆盖旧状态，并按 step_index 排序。"""

    steps[:] = [item for item in steps if item.get("step_id") != thinking.get("step_id")]
    steps.append(thinking)
    steps.sort(key=lambda item: int(item.get("step_index") or 0))


class ThinkingEmitter:
    """在工具内部发射智能体自己的四步思考过程。"""

    def __init__(
        self,
        *,
        agent_slug: str,
        steps: tuple[ThinkingStepSpec, ...] | None = None,
    ) -> None:
        self.agent_slug = agent_slug
        self.steps = tuple(steps if steps is not None else get_thinking_steps(agent_slug))
        self._events: dict[str, dict[str, Any]] = {}
        self._started_at: dict[str, float] = {}

    def emit(
        self,
        step_id: str,
        state: ThinkingState = "completed",
        *,
        content: str = "",
        data: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if not self.steps:
            return
        spec_by_id = {step.step_id: step for step in self.steps}
        spec = spec_by_id.get(step_id)
        if spec is None:
            raise ValueError(f"未知思考步骤: {step_id}")
        if state not in THINKING_STATES:
            raise ValueError(f"非法思考状态: {state}")
        if state == "waiting" and not (data or content):
            raise ValueError("waiting 状态必须提供 data 或 content")

        now = datetime.now(UTC)
        started_at = self._started_at.setdefault(step_id, monotonic())
        previous = self._events.get(step_id) or {}
        event: dict[str, Any] = {
            "version": "v1",
            "agent": self.agent_slug,
            "step_id": step_id,
            "step_index": next(index for index, step in enumerate(self.steps, start=1) if step.step_id == step_id),
            "title": spec.title,
            "state": state,
            "content": content,
            "data": data or {},
            "started_at": previous.get("started_at") or now.isoformat(),
            "finished_at": now.isoformat() if state in THINKING_TERMINAL_STATES else None,
            "elapsed_ms": int((monotonic() - started_at) * 1000),
            "error": error,
        }
        self._events[step_id] = event
        self._write(event)

    def snapshot(self) -> list[dict[str, Any]]:
        return [self._events[step_id] for step_id in sorted(self._events, key=self._step_index)]

    def _step_index(self, step_id: str) -> int:
        return next(index for index, step in enumerate(self.steps, start=1) if step.step_id == step_id)

    def _write(self, event: dict[str, Any]) -> None:
        try:
            writer = get_stream_writer()
        except RuntimeError:
            # 不在图执行上下文（直接调用工具的单测或 CLI）时没有事件流。
            return
        try:
            writer({"type": THINKING_EVENT_TYPE, "thinking": event})
        except Exception as exc:  # noqa: BLE001 - 流已关闭时不能影响业务
            logger.warning(f"思考步骤事件写入失败: {exc}")
