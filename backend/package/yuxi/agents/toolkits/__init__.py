# toolkits 包

# 触发各模块的 @tool 装饰器执行，自动注册工具
from . import buildin, debug
from .kbs import get_common_kb_tools

# 电力规范检索工具（导入即注册 search_power_spec）
from yuxi.knowledge.power_spec import tool as _power_spec_tool  # noqa: F401

# 故障溯源工具（导入即注册 trace_fault）
from yuxi.knowledge.fault_trace import tool as _fault_trace_tool  # noqa: F401


# 工具获取函数
from .registry import (
    ToolExtraMetadata,
    get_all_extra_metadata,
    get_all_tool_instances,
    get_extra_metadata,
    tool,
)

__all__ = [
    "get_extra_metadata",
    "get_all_extra_metadata",
    "get_all_tool_instances",
    "ToolExtraMetadata",
    "tool",
    "get_common_kb_tools",
    # 触发各模块的 @tool 装饰器执行，自动注册工具
    "buildin",
    "debug",
]
