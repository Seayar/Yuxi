"""思考过程的通用类型与智能体步骤注册表。

放在顶层包，避免知识模块为了定义步骤而导入 ``yuxi.agents``，从而触发
agents/toolkits 的循环初始化；worker 只要导入工具所属的 contracts 就能完成注册。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThinkingStepSpec:
    """一个智能体拥有的顶层思考步骤。"""

    step_id: str
    title: str
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "description": self.description,
        }


_REGISTRY: dict[str, tuple[ThinkingStepSpec, ...]] = {}


def register_thinking_steps(agent_slug: str, steps: tuple[ThinkingStepSpec, ...] | list[ThinkingStepSpec]) -> None:
    """注册智能体的四步思考过程；同 slug 的重复注册必须一致。"""

    normalized = tuple(steps)
    if not normalized:
        raise ValueError("思考步骤不能为空")
    if len(normalized) > 4:
        raise ValueError("每个智能体最多注册 4 个思考步骤")
    step_ids = [step.step_id for step in normalized]
    if len(set(step_ids)) != len(step_ids):
        raise ValueError("思考步骤 step_id 不能重复")
    existing = _REGISTRY.get(agent_slug)
    if existing is not None and existing != normalized:
        raise ValueError(f"智能体 {agent_slug} 的思考步骤已注册且不一致")
    _REGISTRY[agent_slug] = normalized


def get_thinking_steps(agent_slug: str) -> tuple[ThinkingStepSpec, ...]:
    return _REGISTRY.get(agent_slug, ())


def thinking_steps_payload(agent_slug: str) -> list[dict[str, str]]:
    """给 agent 详情 API 与前端骨架使用的只读步骤清单。"""

    return [step.to_dict() for step in get_thinking_steps(agent_slug)]
