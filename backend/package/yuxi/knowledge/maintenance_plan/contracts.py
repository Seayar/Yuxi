"""处置方案生成的数据契约。

``contracts`` 拥有处置方案在意图解析、图谱检索、方案装配与发布链路上流动的统一数据结构：
六段式方案、图谱证据、用户可见文案与 kg3 图的中文标签/关系常量。其他模块只消费这里的形状，
不要在别处重复定义标签名、关系名或报告字段。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 用户可见文案（工具返回的固定消息会被 chat_service 发布门原样透传）
# ---------------------------------------------------------------------------
PLAN_GRAPH_NOT_CONFIGURED_MESSAGE = "处置方案知识库未配置，无法生成处置方案"
NO_DEVICE_MESSAGE = "未在设备台账中找到该设备，请核对设备名称后重新提交"
NO_TEMPLATE_MESSAGE = "知识图谱中暂无该设备类型的此类作业规程，无法生成标准化方案"
PLAN_VERIFICATION_FAILED_MESSAGE = "处置方案证据校验未通过，无法生成可发布的标准化方案"
# 需求说明“方案三（反问澄清）”的固定文案
CLARIFY_ALL_MESSAGE = "未能识别完整的任务信息，请明确任务类型（更换/检修/试验/巡视）和目标设备"
CLARIFY_TASK_TYPE_MESSAGE = "请明确要对该设备执行的任务类型：更换、检修、试验或巡视"
CLARIFY_DEVICE_MESSAGE = "请明确目标设备（设备名称），例如：#3开闭所 3k18、35kV#1变电站 991SVG开关"
CHOICE_ABORTED_MESSAGE = "交互次数过多，未完成处置方案生成，请补充完整信息后重新提交"
PLAIN_TOOL_MESSAGES = frozenset(
    {
        PLAN_GRAPH_NOT_CONFIGURED_MESSAGE,
        NO_DEVICE_MESSAGE,
        NO_TEMPLATE_MESSAGE,
        PLAN_VERIFICATION_FAILED_MESSAGE,
        CLARIFY_ALL_MESSAGE,
        CLARIFY_TASK_TYPE_MESSAGE,
        CLARIFY_DEVICE_MESSAGE,
        CHOICE_ABORTED_MESSAGE,
    }
)

# 方案装配阶段注入的固定文案（answer 使用；前端/验收脚本如需识别也从这里取）
OUTAGE_DERIVATION_NOTE = "（依据设备台账层级推导，执行前须以调度下达的停电范围为准）"
NO_SAFETY_DISTANCE_NOTE = "未检索到该电压等级的安全距离数据，请按 DL/T 408/409 相应表格执行"
NO_TEAM_TEXT = "未在知识图谱中检索到管辖班组"
DEMO_SOURCE_MARK = "演示数据"
DEMO_RESOURCE_NOTE = "班组与备件为演示数据，待接入生产管理系统后替换"
TEST_ITEM_MARK = "（kg 试验项目）"
PATROL_ITEM_MARK = "（kg 巡视项目）"

# 中断（LangGraph interrupt）：设备不唯一或缺任务类型时让用户选择（方案二：引导选择）。
# 载荷沿用 Yuxi 既有选择卡片格式（与 fault_trace 确认卡片相同：source + questions[]），
# 前端无需新增中断渲染器；以下为 source 与 question_id 取值。
DEVICE_CHOICE_SOURCE = "maintenance_plan_device_choice"
DEVICE_QUESTION_ID = "maintenance_plan_device"
DEVICE_CHOICE_QUESTION = "请选择目标设备："
TASK_TYPE_CHOICE_SOURCE = "maintenance_plan_task_type_choice"
TASK_TYPE_QUESTION_ID = "maintenance_plan_task_type"
TASK_TYPE_CHOICE_QUESTION = "请选择任务类型："

# ---------------------------------------------------------------------------
# 任务类型（需求说明：更换/检修/试验/巡视 四类）
# ---------------------------------------------------------------------------
TASK_REPLACE = "更换"
TASK_REPAIR = "检修"
TASK_TEST = "试验"
TASK_PATROL = "巡视"
TASK_TYPES = (TASK_REPLACE, TASK_REPAIR, TASK_TEST, TASK_PATROL)

# kg3 图的设备类型节点全部名称（ref/kg_schema.md，19 个）
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

# 设备候选超过该数量时只展示前 N 个供用户选择（方案二：引导选择）
MAX_DEVICE_CANDIDATES = 8
# 一次请求内最多的引导选择/补充轮次，超过返回 CHOICE_ABORTED_MESSAGE
MAX_CHOICE_ROUNDS = 3

# ---------------------------------------------------------------------------
# TuGraph `kg3` 图的中文标签 / 关系
#
# 已有（同事录入的台账与规程，见 ref/kg_schema.md）：
#   设备实例 -[属于]-> 设备类型；设备实例 -[位于]-> 设备实例（子→父）；
#   设备实例 -[包含]-> 设备实例（父→子）；设备实例 -[电连接]-> 设备实例；
#   标准 -[包含条款]-> 标准条款；标准条款 -[规定]-> 设备类型；
#   设备类型 -[需要试验]-> 试验项目；设备类型 -[需要巡视]-> 巡视项目。
# 本模块新增（由 seed 写入，数据来源见各 JSON）：
#   设备类型 -[适用模板]-> 作业模板；作业模板 -[依据条款]-> 标准条款；
#   班组 -[管辖]-> 设备实例（挂在站/开闭所/箱变群/线路等区域节点，向下继承）；
#   设备类型 -[配备备件]-> 备品备件；安全距离（按电压等级的独立节点）。
# ---------------------------------------------------------------------------
LABEL_DEVICE = "设备实例"
LABEL_DEVICE_TYPE = "设备类型"
LABEL_CLAUSE = "标准条款"
LABEL_STANDARD = "标准"
LABEL_TEST_ITEM = "试验项目"
LABEL_PATROL_ITEM = "巡视项目"
LABEL_TEMPLATE = "作业模板"
LABEL_TEAM = "班组"
LABEL_SPARE = "备品备件"
LABEL_SAFETY_DISTANCE = "安全距离"

REL_BELONGS = "属于"
REL_LOCATED_IN = "位于"
REL_CONTAINS = "包含"
REL_ELECTRIC = "电连接"
REL_NEEDS_TEST = "需要试验"
REL_NEEDS_PATROL = "需要巡视"
REL_HAS_TEMPLATE = "适用模板"
REL_TEMPLATE_CLAUSE = "依据条款"
REL_MANAGES = "管辖"
REL_SPARE = "配备备件"

# 设备实例节点的名称属性优先级（与 fault_trace 一致）
DEVICE_NAME_PROPS = ("名称", "显示名", "name")

# 台账 Scheme1 标签 → kg 中“设备类型”节点名称（台账标签与规程设备类型不完全一致时在此归一）
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
    task_type: str | None = None  # 更换/检修/试验/巡视
    device_name: str | None = None  # 用户提到的设备名称（原文，如“3k18”“1号主变”）
    device_type: str | None = None  # 设备类型（归一到 kg3 设备类型名称，如“开关柜”）
    voltage_level: str | None = None  # 如“10kV”
    work_object: str | None = None  # 作业对象/部件，如“断路器”“冷却系统”“有载分接开关”
    location_hint: str | None = None  # 站/开闭所/线路等位置线索，如“#3开闭所”“35kV#1变电站”
    clarification_required: bool = False
    clarification_message: str = ""

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DeviceCandidate:
    """台账中匹配到的一个设备实例。"""

    device_id: str  # 设备实例 id，如 LGR#xxxx
    name: str
    device_type: str | None  # 归一后的设备类型（kg3 设备类型名称）
    voltage_level: str | None
    path: str  # 层级路径，如“#3开闭所 > 3k18”
    ledger_class: str | None = None  # 台账分类，如“中压断路器柜”
    score: float = 0.0  # 名称匹配得分，越大越好

    def label(self) -> str:
        """给用户看的候选描述：名称（类型，电压）— 位置路径。"""

        meta = "，".join(x for x in (self.device_type, self.voltage_level) if x)
        return f"{self.name}（{meta}）— {self.path}" if meta else f"{self.name} — {self.path}"


@dataclass(frozen=True)
class ClauseRef:
    """规程条款引用（用于每一段内容的出处标注）。"""

    standard_no: str  # 如“DL/T 408—2023”
    clause_no: str  # 如“7.3.1”
    title: str = ""
    excerpt: str = ""  # 条款原文节选（≤200 字）


@dataclass
class WorkTemplate:
    """kg 中的一个作业模板节点（字段与 seed JSON 一一对应）。"""

    template_id: str
    name: str
    task_type: str
    device_type: str
    work_object: str = ""
    outage_required: str = ""  # 需停电 / 不停电 / 视作业内容
    isolation_measures: str = ""  # 安全隔离措施（可含占位符）
    step_sequence: list[str] = field(default_factory=list)  # 工序顺序
    key_points: list[str] = field(default_factory=list)  # 操作要点
    tech_params: list[str] = field(default_factory=list)  # 技术参数要求
    personal_protection: str = ""  # 个人防护要求
    grounding_requirements: str = ""  # 验电接地要求
    work_safety: list[str] = field(default_factory=list)  # 其他作业安全要求
    tools: list[str] = field(default_factory=list)  # 工器具清单
    materials: list[str] = field(default_factory=list)  # 材料清单
    acceptance: list[str] = field(default_factory=list)  # 验收标准
    notes: list[str] = field(default_factory=list)  # 注意事项
    staffing: str = ""  # 人员配置建议，如“4人：工作负责人1、作业人员2、专责监护人1”
    clauses: list[ClauseRef] = field(default_factory=list)  # 依据条款
    source_note: str = ""  # 数据来源说明（规程/通用作业经验/演示数据）


@dataclass(frozen=True)
class SafetyDistance:
    """按电压等级的安全距离（DL/T 408/409 表格数据）。"""

    voltage_level: str
    no_outage_m: float | None  # 设备不停电时的安全距离
    work_range_m: float | None  # 工作人员工作中正常活动范围与设备带电部分的安全距离
    live_work_m: float | None  # 带电作业时人身与带电体的安全距离
    clause: ClauseRef | None = None
    remark: str = ""  # 原始表注和适用条件，须随安全距离一同展示


@dataclass(frozen=True)
class TeamInfo:
    """管辖班组（来自 管辖 关系，沿层级向上找到的第一个班组）。"""

    name: str
    member_count: int | None
    leader: str
    managed_area: str  # 挂接的区域节点名称，如“#3开闭所”
    source_note: str = ""


@dataclass(frozen=True)
class SpareInfo:
    """备品备件。"""

    name: str
    model: str = ""
    quantity: int | None = None
    location: str = ""
    source_note: str = ""


@dataclass
class PlanEvidence:
    """图谱检索得到的全部证据；装配只能使用这里的内容，不得编造。"""

    device: DeviceCandidate
    ancestors: list[str] = field(default_factory=list)  # 从近到远的上级节点名称（来自 位于/层级路径）
    outage_scope: list[str] = field(default_factory=list)  # 停电范围（设备本身 + 推导的上级电源/间隔）
    electrical_neighbors: list[str] = field(default_factory=list)  # 电连接的相邻设备
    template: WorkTemplate | None = None
    test_items: list[str] = field(default_factory=list)  # 设备类型关联的试验项目名称
    patrol_items: list[str] = field(default_factory=list)  # 设备类型关联的巡视项目名称
    safety_distance: SafetyDistance | None = None
    team: TeamInfo | None = None
    spares: list[SpareInfo] = field(default_factory=list)
    retrieval_log: list[str] = field(default_factory=list)  # 思考过程用的检索记录（人话）


@dataclass(frozen=True)
class PlanReport:
    """六段式处置方案（需求说明“处置方案字段说明”）。"""

    title: str  # 如“#3开闭所 3k18 开关柜更换方案”
    task_type: str
    device_label: str  # DeviceCandidate.label()
    # 一、检修前准备
    outage_scope: str
    isolation_measures: str
    # 二、检修步骤
    step_sequence: list[str]
    key_points: list[str]
    tech_params: list[str]
    # 三、安全措施
    personal_protection: str
    safety_distance: dict[str, Any] | None  # {"voltage_level":..,"no_outage_m":..,"work_range_m":..,"live_work_m":..}
    grounding_requirements: str
    work_safety: list[str]
    # 四、所需工器具及材料
    tools: list[str]
    materials: list[str]
    # 五、责任班组与资源安排
    team: str  # 如“#3开闭所运维班（4人），负责人：李工”
    spares: list[str]
    resource_note: str  # 演示数据/外部接口未接入等说明
    # 六、验收标准
    acceptance: list[str]
    notes: list[str]
    # 出处
    references: list[dict[str, str]]  # [{"standard_no":..,"clause_no":..,"title":..}]
    thinking: str  # 需求说明“思考过程模版”文本
    staffing: str = ""  # 本次作业人员配置建议（来自作业模板 人员配置建议），如“4人：工作负责人1人……”


def report_to_dict(report: PlanReport) -> dict[str, Any]:
    return asdict(report)


def report_payload(report: PlanReport, *, answer_markdown: str, answer_verified: bool) -> dict[str, Any]:
    """工具返回的最终载荷：结构化方案 + 发布门所需的 answer_markdown。"""

    return {
        "status": "ok",
        "answer_source": "maintenance_plan",
        "report": report_to_dict(report),
        "answer_markdown": answer_markdown,
        "answer_verified": answer_verified,
    }
