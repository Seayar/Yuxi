"""设备预测维护的数据契约。

``contracts`` 拥有设备预测维护在设备识别、外部服务调用、维护知识匹配、报告装配与发布
链路上流动的统一数据结构，以及用户可见固定文案、思考过程注册和 ``kg5`` 图 schema 常量。
其他模块只消费这里的形状，不要在别处重复定义字段名、文案或图谱属性名。

图谱数据只来自设备预测维护自己的 ``kg5``：设备检索与维护知识匹配都连这一张图，
不读故障溯源 ``kg3`` 与处置方案 ``kg4``。

报告字段与 ``srs`` 模块五需求一一对应：

- 设备健康评估：健康评分、健康等级、状态解读、监测指标列表；
- 故障预测：预测故障类型、发生概率、预计发生时间、退化趋势；
- 可靠性与风险评估：故障率、平均停运时间；
- 维护计划建议：维护时机、所需资源、注意事项（匹配到的故障现象一并展示）。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from yuxi.thinking import ThinkingStepSpec, register_thinking_steps

DEVICE_PREDICTION_AGENT_SLUG = "device-prediction-agent"
DEVICE_PREDICTION_THINKING_STEPS = (
    ThinkingStepSpec("load_device", "加载设备档案", "按名称检索设备，解析出预测服务所需的设备 ID。"),
    ThinkingStepSpec("health_assessment", "健康评估计算", "调用设备健康评估服务，读取评分、等级、指标与可靠性指标。"),
    ThinkingStepSpec(
        "fault_prediction", "趋势预测", "调用 LSTM 故障预测服务，读取故障类型、概率、时间窗口与退化趋势。"
    ),
    ThinkingStepSpec("maintenance_plan", "生成维护建议", "按预测故障类型匹配所需资源与注意事项并生成报告。"),
)
register_thinking_steps(DEVICE_PREDICTION_AGENT_SLUG, DEVICE_PREDICTION_THINKING_STEPS)

# ---------------------------------------------------------------------------
# 用户可见文案（工具返回的固定消息会被 chat_service 发布门原样透传）
# ---------------------------------------------------------------------------
NO_PERMISSION_MESSAGE = "无法获取当前用户的知识库权限"
PREDICTION_GRAPH_NOT_CONFIGURED_MESSAGE = "设备预测维护知识图谱未配置或不可用，无法定位设备"
NO_DEVICE_MESSAGE = "未在设备台账中找到该设备，请核对设备名称后重新提交"
DEVICE_ID_UNRESOLVED_MESSAGE = "未找到该设备的台账 ID，无法执行故障预测与健康评估，请核对台账数据"
CLARIFY_DEVICE_MESSAGE = "未能识别完整的设备信息，请明确目标设备"
CHOICE_ABORTED_MESSAGE = "交互次数过多，未完成设备选择，请补充完整的设备名称后重新提交"
HEALTH_SERVICE_NOT_CONFIGURED_MESSAGE = "设备健康评估服务未配置，无法执行健康评估"
HEALTH_SERVICE_UNAVAILABLE_MESSAGE = "设备健康评估服务暂不可用，请稍后重试"
HEALTH_SERVICE_UNKNOWN_DEVICE_MESSAGE = "设备健康评估服务中没有该设备的数据，请核对设备台账 ID"
HEALTH_SERVICE_INVALID_MESSAGE = "设备健康评估服务返回的数据不完整，无法生成预测报告"
PREDICTION_SERVICE_NOT_CONFIGURED_MESSAGE = "故障预测服务未配置，无法执行故障预测"
PREDICTION_SERVICE_UNAVAILABLE_MESSAGE = "故障预测服务暂不可用，请稍后重试"
PREDICTION_SERVICE_UNKNOWN_DEVICE_MESSAGE = "故障预测服务中没有该设备的预测数据，请核对设备台账 ID"
PREDICTION_SERVICE_INVALID_MESSAGE = "故障预测服务返回的数据不完整，无法生成预测报告"
REPORT_VERIFICATION_FAILED_MESSAGE = "设备预测报告未通过证据校验，暂时无法发布"
PLAIN_TOOL_MESSAGES = frozenset(
    {
        NO_PERMISSION_MESSAGE,
        PREDICTION_GRAPH_NOT_CONFIGURED_MESSAGE,
        NO_DEVICE_MESSAGE,
        DEVICE_ID_UNRESOLVED_MESSAGE,
        CLARIFY_DEVICE_MESSAGE,
        CHOICE_ABORTED_MESSAGE,
        HEALTH_SERVICE_NOT_CONFIGURED_MESSAGE,
        HEALTH_SERVICE_UNAVAILABLE_MESSAGE,
        HEALTH_SERVICE_UNKNOWN_DEVICE_MESSAGE,
        HEALTH_SERVICE_INVALID_MESSAGE,
        PREDICTION_SERVICE_NOT_CONFIGURED_MESSAGE,
        PREDICTION_SERVICE_UNAVAILABLE_MESSAGE,
        PREDICTION_SERVICE_UNKNOWN_DEVICE_MESSAGE,
        PREDICTION_SERVICE_INVALID_MESSAGE,
        REPORT_VERIFICATION_FAILED_MESSAGE,
    }
)

# 写入报告的数据说明（不是失败文案）：缺失项按事实标注，不伪造内容
GUIDANCE_NOT_MATCHED_NOTE = "知识图谱中未匹配到该故障类型的故障现象，所需资源与注意事项未匹配"
GUIDANCE_NO_DEVICE_TYPE_NOTE = "设备缺少设备类型，维护知识按全图故障现象匹配"
GUIDANCE_QUERY_FAILED_NOTE = "知识图谱查询失败，所需资源与注意事项未匹配"
HEALTH_FAULT_RATE_MISSING_NOTE = "设备健康评估服务未返回故障率"
HEALTH_DOWNTIME_MISSING_NOTE = "设备健康评估服务未返回平均停运时间"
HEALTH_INDICATORS_MISSING_NOTE = "设备健康评估服务未返回可用的监测指标"
MAINTENANCE_TIME_DERIVED_NOTE = "故障预测服务未返回维护时机，按预计发生时间推导"
STATUS_READOUT_TEMPLATE_NOTE = "状态解读由评分、等级与监测指标确定性生成（模型解读不可用或未通过数值校验）"

# 中断（LangGraph interrupt）：设备候选不唯一时让用户选择。
# 载荷沿用 Yuxi 既有选择卡片格式（source + questions[]），前端无需新增中断渲染器。
DEVICE_CHOICE_SOURCE = "device_prediction_device_choice"
DEVICE_QUESTION_ID = "device_prediction_device"
DEVICE_CHOICE_QUESTION = "请选择目标设备："
# 设备候选展示上限与一次请求内最多的引导选择轮次
MAX_DEVICE_CANDIDATES = 8
MAX_CHOICE_ROUNDS = 4
# 卡片里代表“其他（自由输入）”的取值，视为用户没有选中任何选项
OTHER_VALUES = frozenset({"__other__", "other", "其他"})

# ---------------------------------------------------------------------------
# ``kg5`` 图 schema（设备预测维护独占的新图）
#
#   设备识别与处置方案生成同一套设备口径：``设备实例`` 节点的 名称/层级路径/电压等级/id/
#   台账ID列表，经 ``属于`` 边关联 ``设备类型``。
#   维护知识按预测故障类型索引到 ``故障现象``：
#   ``设备类型`` -[具有现象]-> ``故障现象``（属性：名称、``故障码``、``所需资源``、
#   ``注意事项``、说明）。图谱中没有对应现象时如实标注未匹配，不伪造内容。
# ---------------------------------------------------------------------------
LABEL_DEVICE = "设备实例"
LABEL_DEVICE_TYPE = "设备类型"
LABEL_PHENOMENON = "故障现象"
REL_BELONGS = "属于"
REL_HAS_PHENOMENON = "具有现象"
PROP_PHENOMENON_NAME = "名称"
PROP_FAULT_CODE = "故障码"
PROP_REQUIRED_RESOURCES = "所需资源"
PROP_CAUTIONS = "注意事项"
# 设备名称属性的读取优先级（与处置方案的设备口径一致）
DEVICE_NAME_PROPS = ("名称", "显示名", "name")
# 单次匹配返回上限
GUIDE_QUERY_LIMIT = 20
# 维护知识匹配打分：故障码精确 > 现象名称精确 > 名称包含 > 二元组重叠
FAULT_CODE_EXACT_SCORE = 100.0
PHENOMENON_NAME_EXACT_SCORE = 90.0
PHENOMENON_NAME_CONTAINS_SCORE = 60.0
PHENOMENON_BIGRAM_SCORE = 40.0
PHENOMENON_MATCH_THRESHOLD = 40.0
PHENOMENON_BIGRAM_MIN_RATIO = 0.5
# 预测故障类型可能是“开关柜绝缘退化/局放”这类复合写法，按这些分隔符拆分后逐个匹配
FAULT_TYPE_SEPARATORS = ("/", "／", "、", "，", ",", "；", ";")

# 台账 ID 占位值：出现在 ``台账ID列表`` 里时不能作为预测服务的设备 ID
LEDGER_ID_PLACEHOLDERS = frozenset({"", "补充数据", "无", "null", "None", "未知"})


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class PredictionQuery:
    """一次设备预测维护请求的语义解析结果（只关心设备相关字段）。"""

    raw_text: str = ""
    device_name: str | None = None
    device_type: str | None = None
    voltage_level: str | None = None
    location_hint: str | None = None
    clarification_required: bool = False
    clarification_message: str = ""

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DeviceCandidate:
    """kg5 中匹配到的一个设备实例。"""

    device_id: str  # 设备实例节点 id
    name: str
    device_type: str | None = None  # 来自 属于 边的设备类型名称；没有 属于 边时为 None
    voltage_level: str | None = None
    path: str = ""  # 层级路径，如“#3开闭所 > 3k18”
    ledger_ids: tuple[str, ...] = ()  # 台账ID列表，预测服务设备 ID 的唯一来源
    score: float = 0.0  # 名称匹配得分，越大越好

    def label(self) -> str:
        """给用户看的候选描述：名称（类型，电压）— 位置路径。"""

        meta = "，".join(item for item in (self.device_type, self.voltage_level) if item)
        if meta and self.path:
            return f"{self.name}（{meta}）— {self.path}"
        if meta:
            return f"{self.name}（{meta}）"
        return f"{self.name} — {self.path}" if self.path else self.name


@dataclass(frozen=True)
class HealthIndicator:
    """健康评估的一项监测指标；字段名沿用服务返回的下划线命名。"""

    indicator_name: str
    indicator_value: str
    indicator_unit: str
    weight_ratio: str  # 指标权重，如 30%
    indicator_score: str  # 指标所扣分数，如 -8 分
    criteria: str  # 判分标准

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class HealthAssessment:
    """设备健康评估服务返回的结构化结果。"""

    health_score: str
    health_level: str
    indicators: tuple[HealthIndicator, ...] = ()
    fault_rate: str = ""  # 故障率，如 0.8 次/年
    average_downtime_hours: str = ""  # 平均停运时间，如 6.5 小时/次


@dataclass(frozen=True)
class FaultPrediction:
    """LSTM 故障预测服务返回的结构化结果。"""

    predicted_fault_type: str
    probability: str  # 发生概率，如 67%
    expected_failure_time: str  # 预计发生时间窗口，如 未来 30–60 天
    degradation_trend: str
    maintenance_time: str  # 维护时机；服务未返回时按预计发生时间推导
    maintenance_time_derived: bool = False  # 维护时机是否为推导值


@dataclass(frozen=True)
class MaintenanceGuidance:
    """按预测故障类型从维护知识图谱匹配到的维护建议内容。"""

    fault_type: str = ""
    resources: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.resources or self.cautions)


@dataclass
class PredictionEvidence:
    """一次预测维护请求消费的全部证据；装配只能使用这里的内容，不得编造。"""

    device_name: str
    device_label: str
    device_type: str
    voltage_level: str
    device_id: str  # 发送给预测服务的台账 ID
    device_id_source: str  # 设备 ID 的来源（当前只有 kg5 节点的台账ID列表）
    health: HealthAssessment
    prediction: FaultPrediction
    guidance: MaintenanceGuidance
    retrieval_log: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PredictionReport:
    """设备预测维护报告（四个部分与需求字段一一对应）。"""

    title: str
    device_label: str
    device_id: str
    health_score: str
    health_level: str
    status_readout: str
    indicators: list[dict[str, str]]
    fault_type: str
    probability: str
    expected_failure_time: str
    degradation_trend: str
    fault_rate: str
    average_downtime_hours: str
    matched_phenomenon: str  # 维护知识匹配到的故障现象名称；未匹配时为空
    maintenance_time: str
    resources: list[str]
    cautions: list[str]
    data_notes: list[str]
    thinking: str


def report_payload(report: PredictionReport, *, answer_markdown: str, answer_verified: bool) -> dict[str, Any]:
    """工具返回的最终载荷：结构化报告 + 发布门所需的 answer_markdown。"""

    return {
        "status": "ok",
        "answer_source": "device_prediction",
        "report": asdict(report),
        "answer_markdown": answer_markdown,
        "answer_verified": answer_verified,
    }


# 编号条目（“1. …”“2、…”）本身就是一条，内部的分号不拆；小数点后仍接数字的是小数不是编号。
NUMBERED_ITEM_PATTERN = re.compile(r"^\s*\d+\s*[.、．](?!\d)")


def dedup(values: Any) -> list[str]:
    """去重并保持顺序；跳过空白与非文本值。graph 与 answer 共用，不各自重写。"""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
