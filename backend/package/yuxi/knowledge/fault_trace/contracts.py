"""故障溯源的数据契约。

``contracts`` 拥有故障溯源在工具、故障图谱、实时值、规则求值与发布链路上流动的
统一数据结构：五段式报告字段、设备/因果链实体与关系、实时属性类型、用户可见文案。
其他模块只消费这里的形状。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yuxi.thinking import ThinkingStepSpec, register_thinking_steps

FAULT_TRACE_THINKING_STEPS = (
    ThinkingStepSpec("parse_fault", "解析故障描述", "识别设备、故障现象与图片结果，生成故障摘要。"),
    ThinkingStepSpec("match_causal_chain", "匹配因果链", "沿知识图谱匹配故障模式、现象、原因并求值触发条件。"),
    ThinkingStepSpec("trace_topology", "拓扑关系追溯", "沿电气连接关系追溯直接上下游设备及故障后果。"),
    ThinkingStepSpec("generate_report", "生成溯源报告", "定位原因并生成溯源报告。"),
)
register_thinking_steps("fault-trace-agent", FAULT_TRACE_THINKING_STEPS)

REPORT_SCHEMA_VERSION = "fault-trace-report/v1"
GRAPH_SCHEMA_VERSION = "kg3/v1"
FAULT_TRACE_AGENT_SLUG = "fault-trace-agent"

# 用户可见文案：工具返回的固定消息会被 ``chat_service`` 发布门原样透传。
FAULT_GRAPH_NOT_CONFIGURED_MESSAGE = "故障溯源知识库未配置，无法进行故障溯源"
FAULT_GRAPH_UNAVAILABLE_MESSAGE = "故障溯源知识图谱暂时不可用，请稍后重试；若持续失败请联系管理员检查图谱服务"
DEVICE_NOT_FOUND_MESSAGE = "未能识别故障设备，请明确设备名称后重新提交"
DEVICE_AMBIGUOUS_MESSAGE = "找到多个同名设备，请选择具体设备"
DEVICE_TYPE_MISSING_MESSAGE = "未能确定设备类型，请联系管理员补全图谱"
NO_FAULT_GRAPH_MESSAGE = "未在故障知识图谱中检索到与该故障现象匹配的数据，无法生成溯源报告"
IMAGE_NOT_FOUND_MESSAGE = "未找到该设备的故障图片，且描述中缺少故障现象，请补充故障现象或上传故障图片"
IMAGE_NO_FAULT_MESSAGE = "图片中未识别到故障现象，且描述中缺少故障现象，请补充故障现象后重新提交"
IMAGE_SERVICE_UNAVAILABLE_MESSAGE = "故障图片识别服务暂时不可用，请补充故障现象或稍后重试"
REPORT_VERIFICATION_FAILED_MESSAGE = "溯源报告未通过证据校验，暂不发布；请补充实时数据或稍后重试"
# 意图识别阶段的澄清话术：与发布门共用同一份文案，避免工具与契约各写一句。
CLARIFY_INFO_MISSING_MESSAGE = "未能识别完整的故障信息，请明确故障设备和故障现象"
CLARIFY_PHENOMENON_MISSING_MESSAGE = (
    "已收到您的描述，但缺少故障现象，请补充具体故障现象（例如：冷却器风扇停转、触头烧蚀）。"
)
PLAIN_TOOL_MESSAGES = frozenset(
    {
        FAULT_GRAPH_NOT_CONFIGURED_MESSAGE,
        FAULT_GRAPH_UNAVAILABLE_MESSAGE,
        DEVICE_NOT_FOUND_MESSAGE,
        DEVICE_AMBIGUOUS_MESSAGE,
        DEVICE_TYPE_MISSING_MESSAGE,
        NO_FAULT_GRAPH_MESSAGE,
        IMAGE_NOT_FOUND_MESSAGE,
        IMAGE_NO_FAULT_MESSAGE,
        IMAGE_SERVICE_UNAVAILABLE_MESSAGE,
        REPORT_VERIFICATION_FAILED_MESSAGE,
        CLARIFY_INFO_MISSING_MESSAGE,
        CLARIFY_PHENOMENON_MISSING_MESSAGE,
    }
)

# ---------------------------------------------------------------------------
# 内部实体类型与关系类型（装配逻辑只消费英文类型）
# ---------------------------------------------------------------------------

ENTITY_DEVICE = "device"
ENTITY_DEVICE_TYPE = "device_type"
ENTITY_FAULT_MODE = "fault_mode"
ENTITY_PHENOMENON = "phenomenon"
ENTITY_CAUSE = "cause"
ENTITY_TRIGGER = "trigger"
ENTITY_MEASURE = "measure"
ENTITY_CONSEQUENCE = "consequence"

REL_FAULT_MODE_TYPE = "has_fault_mode"
REL_PHENOMENON_TYPE = "has_phenomenon"
REL_CAUSE_TYPE = "has_cause"
REL_TRIGGER_TYPE = "has_trigger_condition"
REL_MEASURE_TYPE = "has_measure"
REL_CONSEQUENCE_TYPE = "may_lead_to_consequence"
REL_DEVICE_TYPE = "belongs_to_device_type"
# 拓扑只读 ``电气连接``：出边为下游、入边为上游，一跳为限。
REL_ELECTRICAL_TYPE = "electrically_connected_to"

# ---------------------------------------------------------------------------
# 独立 TuGraph ``kg3`` 图的中文标签/关系 schema（人工录入）
#
# 历史 ``kg`` 图的同义关系名只保留映射用于旧数据读取；``kg``/``kg2`` 已废弃，运行时只读 kg3。
# ---------------------------------------------------------------------------

KG_ENTITY_LABELS = {
    "设备实例": ENTITY_DEVICE,
    "设备类型": ENTITY_DEVICE_TYPE,
    "故障模式": ENTITY_FAULT_MODE,
    "故障现象": ENTITY_PHENOMENON,
    "故障原因": ENTITY_CAUSE,
    "触发条件": ENTITY_TRIGGER,
    "诱发因素": ENTITY_TRIGGER,
    "处置措施": ENTITY_MEASURE,
    "故障后果": ENTITY_CONSEQUENCE,
}

KG_RELATION_TYPES = {
    "具有故障模式": REL_FAULT_MODE_TYPE,
    "存在故障模式": REL_FAULT_MODE_TYPE,
    "具有现象": REL_PHENOMENON_TYPE,
    "表现为": REL_PHENOMENON_TYPE,
    "具有原因": REL_CAUSE_TYPE,
    "起因": REL_CAUSE_TYPE,
    "具有触发条件": REL_TRIGGER_TYPE,
    "诱发条件": REL_TRIGGER_TYPE,
    "具有措施": REL_MEASURE_TYPE,
    "处理": REL_MEASURE_TYPE,
    "可能导致": REL_CONSEQUENCE_TYPE,
    "属于": REL_DEVICE_TYPE,
    "电气连接": REL_ELECTRICAL_TYPE,
    "电连接": REL_ELECTRICAL_TYPE,
}

KG_DEVICE_LABEL = "设备实例"
KG_DEVICE_TYPE_LABEL = "设备类型"
KG_DEVICE_NAME_PROPS = ("名称", "显示名", "name")

# ``设备分类`` → ``设备类型`` 的 v1 映射：没有 ``属于`` 边且台账没有设备类型时使用。
# 其他分类不猜测，返回 device_type_missing。
DEVICE_CLASS_TO_TYPE = {
    "输电线路": "架空线路",
    "配电线路": "架空线路",
    "中压线路": "架空线路",
    "线路": "架空线路",
    "杆塔单元": "杆塔",
    "箱式变电站": "箱式变电站",
}
# 台账分类里明确无对应设备类型的站/所，命中时返回 device_type_missing。
UNMAPPED_STATION_CLASSES = frozenset({"站", "变电站"})

# ---------------------------------------------------------------------------
# 实时值属性（触发条件 ``表达式`` 引用的标识符）
#
# 图像类属性不读 Redis，由 YOLO/人工输入提供；缺失时一律 unknown。
# ---------------------------------------------------------------------------

REALTIME_ATTRIBUTE_TYPES = {
    "crossarm_temp_rise": "float",
    "foreign_object_temp_rise": "float",
    "contact_point_temp_rise": "float",
    "leakage_current": "float",
    "uv_photon_rate": "float",
    "line_protection_trip": "bool",
}
IMAGE_ATTRIBUTE_TYPES = {
    "image_birdnest_recognized": "bool",
    "image_bird_dropping_detected": "bool",
    "image_kite_position_recognized": "bool",
    "image_tree_fall_detected": "bool",
}
REALTIME_ATTRIBUTE_UNITS = {
    "crossarm_temp_rise": "K",
    "foreign_object_temp_rise": "K",
    "contact_point_temp_rise": "K",
    "leakage_current": "mA",
    "uv_photon_rate": "光子/s",
}

# 单个设备最多读取的属性数与触发条件上限，避免无界查询。
MAX_REALTIME_ATTRIBUTES = 20
MAX_REPORT_DEVICES = 20
MAX_REPORT_PATHS = 20


@dataclass(frozen=True)
class RealtimeValue:
    """一个实时属性的读取结果；``status`` 为 ok/missing/invalid/unavailable/not_configured/not_collected。"""

    attribute: str
    status: str
    value: float | bool | None = None
    raw: str | None = None
    unit: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# 实时数据服务不可用（未配置或读取失败）：与 key 缺失、值非法区分，供告警与原因求值使用。
REALTIME_UNAVAILABLE_STATUSES = frozenset({"unavailable", "not_configured"})


@dataclass(frozen=True)
class TriggerResult:
    """一个触发条件的三值求值结果。``result`` 为 true/false/unknown。"""

    trigger_id: str
    name: str
    expression: str | None
    result: str
    reason: str
    values: dict[str, Any] = field(default_factory=dict)
    qualitative: bool = False


@dataclass
class CauseEvaluation:
    """一个故障原因的聚合结果：``established``/``rejected``/``unknown``。"""

    cause_id: str
    name: str
    status: str
    triggers: list[TriggerResult] = field(default_factory=list)
    true_count: int = 0
    missing: list[str] = field(default_factory=list)
    qualitative_only: bool = False


@dataclass
class FaultTraceQuery:
    """一次故障溯源请求的语义解析结果（含图片识别结果的融合）。"""

    device_name: str | None = None
    device_type: str | None = None
    voltage_level: str | None = None
    phenomena: list[str] = field(default_factory=list)
    image_fault_type: str | None = None
    image_fault_name: str | None = None
    clarification_required: bool = False
    clarification_message: str = ""

    def has_specific_device(self) -> bool:
        """SRS：设备名称应指向具体设备，仅设备类型视为不明确。"""

        return bool(self.device_name and self.device_name.strip())

    def has_phenomenon(self) -> bool:
        return bool(self.phenomena) or bool(self.image_fault_name and self.image_fault_name.strip())

    def model_dump(self) -> dict[str, Any]:
        return {
            "device_name": self.device_name,
            "device_type": self.device_type,
            "voltage_level": self.voltage_level,
            "phenomena": list(self.phenomena),
            "image_fault_type": self.image_fault_type,
            "image_fault_name": self.image_fault_name,
            "clarification_required": self.clarification_required,
            "clarification_message": self.clarification_message,
        }
