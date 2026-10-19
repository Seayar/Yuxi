"""处置方案生成模块。

仅重导出被生产消费的符号；公开面保持最小。

注意：**这里不导入 ``tool``**。工具由 ``yuxi.agents.toolkits`` 显式 import 注册
（``from yuxi.knowledge.maintenance_plan import tool``），若在包 ``__init__`` 中导入会与
``toolkits`` 形成循环导入。
"""

from yuxi.knowledge.maintenance_plan.answer import produce_maintenance_plan_answer
from yuxi.knowledge.maintenance_plan.contracts import (
    CHOICE_ABORTED_MESSAGE,
    CLARIFY_ALL_MESSAGE,
    CLARIFY_DEVICE_MESSAGE,
    CLARIFY_TASK_TYPE_MESSAGE,
    DEVICE_CHOICE_QUESTION,
    DEVICE_CHOICE_SOURCE,
    DEVICE_QUESTION_ID,
    MAX_CHOICE_ROUNDS,
    NO_DEVICE_MESSAGE,
    NO_TEMPLATE_MESSAGE,
    PLAIN_TOOL_MESSAGES,
    PLAN_GRAPH_NOT_CONFIGURED_MESSAGE,
    PLAN_VERIFICATION_FAILED_MESSAGE,
    TASK_TYPE_CHOICE_QUESTION,
    TASK_TYPE_CHOICE_SOURCE,
    TASK_TYPE_QUESTION_ID,
    DeviceCandidate,
    PlanEvidence,
    PlanQuery,
    PlanReport,
    report_payload,
)
from yuxi.knowledge.maintenance_plan.graph_plan import collect_plan_evidence, search_devices
from yuxi.knowledge.maintenance_plan.intent import extract_plan_intent
from yuxi.knowledge.maintenance_plan.seed import load_plan_seed, seed_plan_graph_kg

__all__ = [
    # 用户可见文案与发布门
    "PLAN_GRAPH_NOT_CONFIGURED_MESSAGE",
    "PLAN_VERIFICATION_FAILED_MESSAGE",
    "NO_DEVICE_MESSAGE",
    "NO_TEMPLATE_MESSAGE",
    "CLARIFY_ALL_MESSAGE",
    "CLARIFY_TASK_TYPE_MESSAGE",
    "CLARIFY_DEVICE_MESSAGE",
    "CHOICE_ABORTED_MESSAGE",
    "PLAIN_TOOL_MESSAGES",
    # 中断（选择卡片）
    "DEVICE_CHOICE_SOURCE",
    "DEVICE_QUESTION_ID",
    "DEVICE_CHOICE_QUESTION",
    "TASK_TYPE_CHOICE_SOURCE",
    "TASK_TYPE_QUESTION_ID",
    "TASK_TYPE_CHOICE_QUESTION",
    "MAX_CHOICE_ROUNDS",
    # 数据结构
    "PlanQuery",
    "DeviceCandidate",
    "PlanEvidence",
    "PlanReport",
    "report_payload",
    # 生产链路
    "extract_plan_intent",
    "search_devices",
    "collect_plan_evidence",
    "produce_maintenance_plan_answer",
    "load_plan_seed",
    "seed_plan_graph_kg",
]
