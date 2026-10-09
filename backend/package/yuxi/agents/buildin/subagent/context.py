from dataclasses import dataclass, field

from yuxi.agents.context import BaseContext


@dataclass(kw_only=True)
class SubAgentContext(BaseContext):
    parent_thread_id: str | None = field(
        default=None,
        metadata={"name": "父线程ID", "configurable": False, "hide": True},
    )
    is_subagent_runtime: bool = field(
        default=False,
        metadata={"name": "子智能体运行态", "configurable": False, "hide": True},
    )
    temperature: float | None = field(
        default=None,
        metadata={
            "name": "模型采样温度",
            "description": (
                "子智能体模型的采样温度，留空时使用服务默认值。整理、核验这类需要稳定输出的子智能体应设为 0。"
            ),
            "type": "number",
            "auth": "admin",
        },
    )
