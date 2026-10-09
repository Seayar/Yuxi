from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


PowerSpecIntent = Literal["设备参数查询", "检修标准查询", "安全规范查询", "其他类型"]

# 工具直接返回给用户的纯文本结果。发布门需要原样透传这些句子，不再做模型改写，
# 因此文案与集合定义在同一处，避免多处硬编码漏改。
NO_EVIDENCE_MESSAGE = "未检索到资料"
KNOWLEDGE_BASE_NOT_READY_MESSAGE = "电力规范知识库尚未完成条款级重建"
RETRIEVAL_NOT_CONFIGURED_MESSAGE = "该知识库未配置重排模型，无法检索电力规范"
INTENT_ATTEMPTS_EXHAUSTED_MESSAGE = "多次未确认问题意图，请重新描述您的电力规范问题"
PLAIN_TOOL_MESSAGES = frozenset(
    {
        NO_EVIDENCE_MESSAGE,
        KNOWLEDGE_BASE_NOT_READY_MESSAGE,
        RETRIEVAL_NOT_CONFIGURED_MESSAGE,
        INTENT_ATTEMPTS_EXHAUSTED_MESSAGE,
    }
)


class PowerSpecQuery(BaseModel):
    """表示一次意图分析后的电力规范查询。"""

    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1)
    intent: PowerSpecIntent
    device_name: str | None = None
    device_type: str | None = None
    voltage_level: str | None = None
    answer_dimensions: list[str] = Field(default_factory=list)
    normalized_terms: list[str] = Field(default_factory=list)
    unknown_terms: list[str] = Field(default_factory=list)
    clarification_required: bool = False
    clarification_message: str | None = None


class SpecClauseEvidence(BaseModel):
    """表示一条可被回答引用的规范条款证据。"""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    standard_name: str = Field(min_length=1)
    standard_no: str = Field(min_length=1)
    version: str | None = None
    status: str = Field(min_length=1)
    replaced_by: str | None = None
    clause_no: str = Field(min_length=1)
    clause_title: str = Field(min_length=1)
    clause_body: str = Field(min_length=1)
    scope: str | None = None
    rerank_score: float | None = None
    source_file: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_page_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("status")
    @classmethod
    def normalize_status(cls, value: str) -> str:
        return value.strip()

    @property
    def citation_key(self) -> str:
        """返回稳定的文档条款引用键。"""

        return f"{self.document_id}#{self.clause_no}"


def citation_key_of(evidence: Any) -> str:
    """返回证据对象的引用键。

    `SpecClauseEvidence.citation_key` 是模型属性，这里额外兼容缺少该属性的替身对象
    （测试替身、图谱节点），避免各调用点重复实现同一兜底。
    """

    key = getattr(evidence, "citation_key", None)
    if key:
        return str(key)
    return f"{getattr(evidence, 'document_id', '')}#{getattr(evidence, 'clause_no', '')}"


class EvidenceBundle(BaseModel):
    """表示一次场景检索提供给生成器的受限证据集合。"""

    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    intent: str | None = None
    entities: dict[str, str] = Field(default_factory=dict)
    evidence: list[SpecClauseEvidence] = Field(default_factory=list)
    current_only: bool = True


class VerificationIssue(BaseModel):
    """表示一次证据校验失败的具体原因。"""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    citation: str | None = None
