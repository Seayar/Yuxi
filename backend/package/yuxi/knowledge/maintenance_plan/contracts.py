"""处置方案生成的数据契约（kg4 检修作业场景）。

``contracts`` 拥有处置方案在意图解析、图谱检索、分支判定、方案装配与发布链路上流动的统一
数据结构，以及 kg4 图的中文标签/关系和用户可见固定文案。其他模块只消费这里的形状，
不要在别处重复定义标签名、关系名或报告字段。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from yuxi.thinking import ThinkingStepSpec, register_thinking_steps

MAINTENANCE_PLAN_AGENT_SLUG = "maintenance-plan-agent"
MAINTENANCE_PLAN_THINKING_STEPS = (
    ThinkingStepSpec("parse_task", "解析任务需求", "识别目标设备、检修作业场景、检修等级与分支判定维度。"),
    ThinkingStepSpec("retrieve_graph", "检索知识图谱", "读取设备类型、场景步骤、安全措施、资源需求与人员需求。"),
    ThinkingStepSpec("assemble_plan", "组装方案模板", "按通用项与执行分支过滤，组装步骤、安全措施、资源与人员。"),
    ThinkingStepSpec("generate_plan", "生成标准化方案", "校验分支与证据一致性，生成可发布的 Markdown 方案。"),
)
register_thinking_steps(MAINTENANCE_PLAN_AGENT_SLUG, MAINTENANCE_PLAN_THINKING_STEPS)

# ---------------------------------------------------------------------------
# 用户可见文案（工具返回的固定消息会被 chat_service 发布门原样透传）
# ---------------------------------------------------------------------------
PLAN_GRAPH_NOT_CONFIGURED_MESSAGE = "处置方案知识库未配置，无法生成处置方案"
NO_DEVICE_MESSAGE = "未在设备台账中找到该设备，请核对设备名称后重新提交"
NO_SCENE_MESSAGE = "知识图谱中暂无该设备类型的检修作业场景，无法生成标准化方案"
PLAN_VERIFICATION_FAILED_MESSAGE = "处置方案证据校验未通过，无法生成可发布的标准化方案"
CLARIFY_ALL_MESSAGE = "未能识别完整的任务信息，请明确目标设备和检修作业场景"
CLARIFY_DEVICE_MESSAGE = "请明确目标设备（设备名称），例如：#3开闭所 3k18、35kV#1变电站 991SVG开关"
CHOICE_ABORTED_MESSAGE = "交互次数过多，未完成处置方案生成，请补充完整信息后重新提交"
PERSONNEL_SHORTAGE_MESSAGE = "人员数据不足，无法按所需人数生成方案，请补充人员数据后重试"
NO_PERMISSION_MESSAGE = "无法获取当前用户的知识库权限"
PLAIN_TOOL_MESSAGES = frozenset(
    {
        PLAN_GRAPH_NOT_CONFIGURED_MESSAGE,
        NO_DEVICE_MESSAGE,
        NO_SCENE_MESSAGE,
        PLAN_VERIFICATION_FAILED_MESSAGE,
        CLARIFY_ALL_MESSAGE,
        CLARIFY_DEVICE_MESSAGE,
        CHOICE_ABORTED_MESSAGE,
        PERSONNEL_SHORTAGE_MESSAGE,
        NO_PERMISSION_MESSAGE,
    }
)

# 装配阶段写入方案正文的数据口径说明（不是失败文案）
BRANCH_USER_CONFIRMED_NOTE = "分支判定 Redis 未返回可用值，本次执行分支由用户确认"
TOOLS_DATA_MISSING_NOTE = "工器具库存数据未接入 MinIO，资源清单未标注在库状态"
PERSONNEL_DATA_MISSING_NOTE = "人员数据未接入 MinIO，人员安排仅给出岗位与人数"

# 中断（LangGraph interrupt）：设备/场景/分支不唯一时让用户选择。
# 载荷沿用 Yuxi 既有选择卡片格式（source + questions[]），前端无需新增中断渲染器。
DEVICE_CHOICE_SOURCE = "maintenance_plan_device_choice"
DEVICE_QUESTION_ID = "maintenance_plan_device"
DEVICE_CHOICE_QUESTION = "请选择目标设备："
SCENE_CHOICE_SOURCE = "maintenance_plan_scene_choice"
SCENE_QUESTION_ID = "maintenance_plan_scene"
SCENE_CHOICE_QUESTION = "请选择检修作业场景："
BRANCH_CHOICE_SOURCE = "maintenance_plan_branch_choice"
BRANCH_QUESTION_ID = "maintenance_plan_branch"
BRANCH_CHOICE_QUESTION = "请确认现场分支条件："

# ---------------------------------------------------------------------------
# 任务类型（用户任务描述里的动词；用于展示与场景打分提示，不作为必填项）
# ---------------------------------------------------------------------------
TASK_REPLACE = "更换"
TASK_REPAIR = "检修"
TASK_TEST = "试验"
TASK_PATROL = "巡视"
TASK_TYPES = (TASK_REPLACE, TASK_REPAIR, TASK_TEST, TASK_PATROL)

# kg4 图的设备类型节点名称（与 kg3 的 19 个类型保持同名，便于口语归一）
DEVICE_TYPES = (
    "变压器",
    "开关柜",
    "开闭所",
    "接地装置",
    "断路器",
    "杆塔",
    "架空线路",
    "母线",
    "熔断器",
    "电力设备",
    "电压互感器",
    "电容器",
    "电抗器",
    "电流互感器",
    "电缆",
    "箱式变电站",
    "绝缘子",
    "避雷器",
    "隔离开关",
)

# 设备候选超过该数量时只展示前 N 个供用户选择
MAX_DEVICE_CANDIDATES = 8
# 场景候选超过该数量时只展示前 N 个供用户选择
MAX_SCENE_CANDIDATES = 8
# 一次请求内最多的引导选择/补充轮次（设备/场景/分支共用），超过返回 CHOICE_ABORTED_MESSAGE
MAX_CHOICE_ROUNDS = 4

# ---------------------------------------------------------------------------
# TuGraph `kg4` 图的中文标签 / 关系
#
#   设备实例 -[属于]-> 设备类型；设备类型 -[包含作业]-> 检修作业场景；
#   检修作业场景 -[包含步骤]-> 检修步骤；
#   检修作业场景 -[需要安全措施]-> 安全措施；
#   检修作业场景 -[需要资源]-> 资源需求；
#   检修作业场景 -[需要人员]-> 人员需求；
#   运维组织 -[管辖]-> 设备实例（区域节点，向下继承）。
# ---------------------------------------------------------------------------
LABEL_DEVICE = "设备实例"
LABEL_DEVICE_TYPE = "设备类型"
LABEL_SCENE = "检修作业场景"
LABEL_STEP = "检修步骤"
LABEL_SAFETY = "安全措施"
LABEL_RESOURCE = "资源需求"
LABEL_PERSONNEL = "人员需求"
LABEL_ORG = "运维组织"
LABEL_PERSON = "运维人员"

REL_BELONGS = "属于"
REL_HAS_SCENE = "包含作业"
REL_HAS_STEP = "包含步骤"
REL_NEEDS_SAFETY = "需要安全措施"
REL_NEEDS_RESOURCE = "需要资源"
REL_NEEDS_PERSONNEL = "需要人员"
REL_MANAGES = "管辖"
REL_LOCATED_IN = "位于"

# 分支取值（安全措施/资源需求/人员需求的 适用分支值）
BRANCH_1 = "分支1"
BRANCH_2 = "分支2"
BRANCH_VALUES = (BRANCH_1, BRANCH_2)
# 是否通用 的取值
COMMON_YES = "是"

# 设备实例节点的名称属性优先级
DEVICE_NAME_PROPS = ("名称", "显示名", "name")

# 台账 Scheme1 标签 → kg 中“设备类型”节点名称（口语归一用；kg4 的设备类型由 属于 边读取）
SCHEME1_TO_DEVICE_TYPE = {
    "开关柜": "开关柜",
    "断路器": "断路器",
    "变压器": "变压器",
    "箱式变电站": "箱式变电站",
    "杆塔": "杆塔",
    "配电线路": "架空线路",
    "输电线路": "架空线路",
    "电缆": "电缆",
    "母线": "母线",
    "绝缘子": "绝缘子",
    "开闭所": "开闭所",
    "变电站": "电力设备",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class PlanQuery:
    """一次处置方案请求的语义解析结果。"""

    raw_text: str = ""  # 用户原文；多轮补充时为“上一轮原文；本次补充”的拼接全文
    task_type: str | None = None  # 更换/检修/试验/巡视（可选，仅作展示与场景打分提示）
    device_name: str | None = None  # 用户提到的设备名称（原文，如“3k18”“1号主变”）
    device_type: str | None = None  # 设备类型（归一到 kg4 设备类型名称，如“开关柜”）
    voltage_level: str | None = None  # 如“10kV”
    work_object: str | None = None  # 作业对象/部件，如“断路器”“冷却系统”“有载分接开关”
    location_hint: str | None = None  # 站/开闭所/线路等位置线索，如“#3开闭所”“35kV#1变电站”
    maintenance_level: str | None = None  # 适用检修等级 A/B/C/D
    scene_hint: str | None = None  # 用户对检修场景的口语描述（可选）
    clarification_required: bool = False
    clarification_message: str = ""

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DeviceCandidate:
    """台账中匹配到的一个设备实例。"""

    device_id: str  # 设备实例 id，如 LGR#xxxx
    name: str
    device_type: str | None  # 来自 属于 边的设备类型名称
    voltage_level: str | None
    path: str  # 层级路径，如“#3开闭所 > 3k18”
    ledger_class: str | None = None  # 台账分类，如“中压断路器柜”
    ledger_ids: tuple[str, ...] = ()  # 台账ID列表，Redis 分支 key 的前缀
    score: float = 0.0  # 名称匹配得分，越大越好

    def label(self) -> str:
        """给用户看的候选描述：名称（类型，电压）— 位置路径。"""

        meta = "，".join(x for x in (self.device_type, self.voltage_level) if x)
        return f"{self.name}（{meta}）— {self.path}" if meta else f"{self.name} — {self.path}"


@dataclass(frozen=True)
class Scenario:
    """kg4 中的一个检修作业场景节点。"""

    scene_id: str
    name: str
    description: str = ""
    level: str = ""  # 适用检修等级，A/B/C/D
    branch_dimension: str = ""
    branch1_condition: str = ""
    branch2_condition: str = ""
    device_type: str = ""
    score: float = 0.0

    def label(self) -> str:
        """给用户看的场景描述：名称（等级，按维度分支）。"""

        meta = "，".join(
            x for x in (self.level, f"按{self.branch_dimension}分支" if self.branch_dimension else "") if x
        )
        return f"{self.name}（{meta}）" if meta else self.name


@dataclass(frozen=True)
class BranchSelection:
    """一次请求选定的执行分支。"""

    dimension: str
    condition: str  # 分支条件原文（Redis 值或用户选择）
    branch: str  # 分支1/分支2
    source: str = "redis"  # redis/user
    ledger_id: str = ""  # 命中的台账 ID（source=redis 时）


@dataclass(frozen=True)
class Step:
    """检修步骤（与分支无关）。"""

    node_id: str
    sequence: int
    content: str
    name: str = ""


@dataclass(frozen=True)
class SafetyMeasure:
    """安全措施（按通用/分支过滤后的节点）。"""

    node_id: str
    name: str
    content: str
    common: bool
    applicable_branch: str = ""
    category: str = ""


@dataclass(frozen=True)
class ResourceRequirement:
    """资源需求（按通用/分支过滤后的节点，可附库存判定）。"""

    node_id: str
    name: str
    quantity: int | None
    unit: str = ""
    quantity_requirement: str = ""
    common: bool = True
    applicable_branch: str = ""
    in_stock: bool | None = None
    stock_note: str = ""


@dataclass(frozen=True)
class PersonnelRequirement:
    """人员需求（按通用/分支过滤后的节点）。"""

    node_id: str
    role: str
    count: int
    common: bool = True
    applicable_branch: str = ""
    duty: str = ""
    qualification: str = ""


@dataclass(frozen=True)
class PersonnelAssignment:
    """从人员 JSON 抽样后的结果；``roles`` 为 岗位角色 → 姓名列表。"""

    roles: dict[str, tuple[str, ...]] = field(default_factory=dict)
    total: int = 0
    note: str = ""
    shortage: bool = False


@dataclass(frozen=True)
class TeamInfo:
    """责任班组（来自 运维组织 -[管辖]-> 设备实例）。"""

    name: str
    managed_area: str
    source_note: str = ""


@dataclass
class ScenarioEvidence:
    """kg4 检索得到的全部证据；装配只能使用这里的内容，不得编造。"""

    device: DeviceCandidate
    scenario: Scenario
    branch: BranchSelection | None = None
    steps: list[Step] = field(default_factory=list)
    safety: list[SafetyMeasure] = field(default_factory=list)
    resources: list[ResourceRequirement] = field(default_factory=list)
    personnel: list[PersonnelRequirement] = field(default_factory=list)
    team: TeamInfo | None = None
    retrieval_log: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScenarioPlanReport:
    """kg4 检修作业场景处置方案。"""

    title: str
    device_label: str
    scenario_name: str
    level: str
    branch_dimension: str
    branch_condition: str
    branch_value: str
    branch_source: str
    branch: str
    steps: list[str]
    safety: list[str]
    resources: list[str]
    personnel: list[str]
    team: str
    personnel_total: int
    data_notes: list[str]
    thinking: str


def report_to_dict(report: ScenarioPlanReport) -> dict[str, Any]:
    return asdict(report)


def report_payload(report: ScenarioPlanReport, *, answer_markdown: str, answer_verified: bool) -> dict[str, Any]:
    """工具返回的最终载荷：结构化方案 + 发布门所需的 answer_markdown。"""

    return {
        "status": "ok",
        "answer_source": "maintenance_plan",
        "report": report_to_dict(report),
        "answer_markdown": answer_markdown,
        "answer_verified": answer_verified,
    }
