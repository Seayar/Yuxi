"""故障溯源的数据契约。

``contracts`` 拥有故障溯源在工具、图谱与发布链路上流动的统一数据结构：
五段式报告、检索事实、用户可见文案与关系类型常量。其他模块只消费这里的形状。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 用户可见文案：工具返回的固定消息会被 ``chat_service`` 发布门原样透传。
FAULT_GRAPH_NOT_CONFIGURED_MESSAGE = "故障溯源知识库未配置，无法进行故障溯源"
NO_FAULT_GRAPH_MESSAGE = "未在故障知识图谱中检索到与该故障现象匹配的数据，无法生成溯源报告"
KG_GRAPH_UNAVAILABLE_MESSAGE = "故障溯源知识图谱暂时不可用，请稍后重试；若持续失败请联系管理员检查图谱服务"
PLAIN_TOOL_MESSAGES = frozenset(
    {FAULT_GRAPH_NOT_CONFIGURED_MESSAGE, NO_FAULT_GRAPH_MESSAGE, KG_GRAPH_UNAVAILABLE_MESSAGE}
)

# 知识图谱关系类型（方案5.3.2：has_phenomenon / has_cause / has_trigger_condition / has_measure）
REL_CAUSAL_TYPES = frozenset({"has_phenomenon", "has_cause", "has_trigger_condition", "has_measure"})
REL_FAULT_MODE_TYPE = "has_fault_mode"
REL_TOPOLOGY_TYPE = "electrically_connected_to"

# 人工录入允许的实体标签
ENTITY_DEVICE = "device"
ENTITY_FAULT_MODE = "fault_mode"
ENTITY_PHENOMENON = "phenomenon"
ENTITY_CAUSE = "cause"
ENTITY_MEASURE = "measure"
ENTITY_TRIGGER = "trigger"
ENTITY_LABELS = frozenset(
    {ENTITY_DEVICE, ENTITY_FAULT_MODE, ENTITY_PHENOMENON, ENTITY_CAUSE, ENTITY_MEASURE, ENTITY_TRIGGER}
)

# ---------------------------------------------------------------------------
# 独立 TuGraph `kg` 图的中文标签/关系 schema（方案A：人工录入到 kg 图）
#
# 故障溯源数据录入到外部 TuGraph 图 `kg`（连接信息由管理员经 fault_trace_opts 配置，
# 独立于 Yuxi 自管的 default 图）。图谱使用与帐台一致的中文标签；适配器把中文类型映射为上面的
# 内部英文类型，装配逻辑只消费英文类型，不感知双语差异。
# ---------------------------------------------------------------------------

# 中文节点标签 → 内部实体类型
KG_ENTITY_LABELS = {
    "设备实例": ENTITY_DEVICE,
    "故障模式": ENTITY_FAULT_MODE,
    "故障现象": ENTITY_PHENOMENON,
    "故障原因": ENTITY_CAUSE,
    "处置措施": ENTITY_MEASURE,
    "诱发因素": ENTITY_TRIGGER,
}

# 中文关系类型 → 内部关系类型
KG_RELATION_TYPES = {
    "存在故障模式": REL_FAULT_MODE_TYPE,
    "表现为": "has_phenomenon",
    "起因": "has_cause",
    "处理": "has_measure",
    "诱发条件": "has_trigger_condition",
    "电连接": REL_TOPOLOGY_TYPE,
}

# kg 图中设备节点优先使用的名称属性（帐台风格：名称/显示名/name）
KG_DEVICE_NAME_PROPS = ("名称", "显示名", "name")

# kg 图设备名称匹配的空间分隔符（名称可能含 # / 空格）
KG_DEVICE_LABEL = "设备实例"


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


@dataclass(frozen=True)
class FaultReasoningRow:
    """② 推理过程中一行「现象 → 可能原因 → 处置建议」的矩阵条目。"""

    phenomenon: str
    causes: list[str] = field(default_factory=list)
    measures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FaultTraceReport:
    """五段式溯源报告（SRS 表2-1）。"""

    summary: str  # ① 故障现象简述
    device_names: list[str]  # ① 涉及设备（只显示名称）
    reasoning: list[FaultReasoningRow]  # ② 推理过程
    located_cause: str  # ③ 定位原因
    rule: str | None  # ③ 命中规则 / 推理依据
    evidence_note: str  # ③ 定位原因的推理说明
    upstream: list[str]  # ④ 上游根源设备
    downstream: list[str]  # ④ 下游受影响设备
    topology_paths: list[str]  # ④ 拓扑关系（每分支一条路径）
    suggestions: list[dict[str, Any]]  # ⑤ 处置建议：只列定位原因及其措施


def report_payload(report: FaultTraceReport, *, answer_markdown: str, answer_verified: bool) -> dict[str, Any]:
    """工具返回的最终载荷：报告 + 发布门所需的 answer_markdown。"""

    return {
        "status": "ok",
        "answer_source": "fault_trace",
        "report": report_to_dict(report),
        "answer_markdown": answer_markdown,
        "answer_verified": answer_verified,
    }


def report_to_dict(report: FaultTraceReport) -> dict[str, Any]:
    return {
        "summary": report.summary,
        "device_names": list(report.device_names),
        "reasoning": [
            {
                "phenomenon": row.phenomenon,
                "causes": list(row.causes),
                "measures": list(row.measures),
            }
            for row in report.reasoning
        ],
        "located_cause": report.located_cause,
        "rule": report.rule,
        "evidence_note": report.evidence_note,
        "upstream": list(report.upstream),
        "downstream": list(report.downstream),
        "topology_paths": list(report.topology_paths),
        "suggestions": list(report.suggestions),
    }
