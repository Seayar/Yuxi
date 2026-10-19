from __future__ import annotations

import html
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yuxi.knowledge.power_spec.contracts import (
    EvidenceBundle,
    PowerSpecQuery,
    SpecClauseEvidence,
    VerificationIssue,
    citation_key_of,
)
from yuxi.knowledge.power_spec.model_output import parse_model_json
from yuxi.utils import logger


# ---------------------------------------------------------------------------
# 检索编排（原 retrieval.py）
# ---------------------------------------------------------------------------

GraphSearch = Callable[[PowerSpecQuery], Awaitable[Sequence[SpecClauseEvidence]]]
VectorSearch = Callable[[str], Awaitable[Sequence[SpecClauseEvidence]]]

# 进入整理阶段的条款数上限。实测召回池里已包含正确条款、但 top-5 会把它丢掉
# （50 题：召回池命中 96%，最终证据只有 72%）。条款级语料的正文很短（百字量级），
# 放宽到 12 条的 prompt 成本可控，能覆盖只差 0.007 分被截掉的边缘排序损失。
MAX_EVIDENCE_CLAUSES = 12


class PowerSpecSearchResult(BaseModel):
    """表示一次电力规范检索的结构化结果。"""

    model_config = ConfigDict(frozen=True)

    query: PowerSpecQuery
    evidence: list[SpecClauseEvidence] = Field(default_factory=list)
    evidence_bundle: EvidenceBundle | None = None
    clarification: str | None = None
    no_result: str | None = None


class PowerSpecSearchService:
    """编排图谱过滤和带 rerank 分数的向量检索。"""

    def __init__(self, graph_search: GraphSearch, vector_search: VectorSearch):
        self._graph_search = graph_search
        self._vector_search = vector_search

    async def search(self, query: PowerSpecQuery, *, limit: int = MAX_EVIDENCE_CLAUSES) -> PowerSpecSearchResult:
        """返回只经过图谱和向量共同确认的现行规范证据。"""

        if query.clarification_required:
            return PowerSpecSearchResult(query=query, clarification=query.clarification_message)

        graph_evidence = await self._graph_search(query)
        graph_keys = {item.citation_key for item in graph_evidence}
        vector_evidence = await self._vector_search(query.query)
        dimension_terms = query.answer_dimensions
        ranking_terms = query.normalized_terms or dimension_terms
        scored_evidence = []
        for item in vector_evidence:
            if item.rerank_score is None or item.status != "现行":
                continue
            matched_terms = [term for term in ranking_terms if term in item.clause_body or term in item.clause_title]
            if len(dimension_terms) >= 2 and not matched_terms:
                continue
            score = (item.rerank_score or 0.0) + min(len(matched_terms), 5) * 0.02
            scored_evidence.append((score, item, matched_terms))
        candidates = [
            (score, item, matched_terms)
            for score, item, matched_terms in scored_evidence
            if not graph_keys or item.citation_key in graph_keys
        ]
        candidates.sort(key=lambda item: item[0], reverse=True)
        # 同一条款在语料里可能对应多个分块：大表格会被切分成多块，条款级重建也可能
        # 产出多行碎片。这些分块必须合回一条证据再限量，否则同一条款的片段会互相挤占
        # 名额、或被去重逻辑直接丢弃，用户只能看到同一条款的任意几行。
        groups: dict[tuple[str, str], list[tuple[float, SpecClauseEvidence, list[str]]]] = {}
        for score, item, matched_terms in candidates:
            groups.setdefault((item.standard_no, item.clause_no), []).append((score, item, matched_terms))

        ordered_groups = sorted(
            (group for group in groups.values()),
            key=lambda group: max(entry[0] for entry in group),
            reverse=True,
        )
        selected = [_merge_clause_group(group, graph_matched=bool(graph_keys)) for group in ordered_groups[:limit]]
        if not selected:
            return PowerSpecSearchResult(query=query, no_result="未在现行规范中找到可验证的相关条款")

        bundle = EvidenceBundle(
            query=query.query,
            scenario_id="power_spec",
            intent=query.intent,
            entities={
                key: value
                for key, value in {
                    "device_name": query.device_name,
                    "device_type": query.device_type,
                    "voltage_level": query.voltage_level,
                }.items()
                if value
            },
            evidence=selected,
        )
        return PowerSpecSearchResult(query=query, evidence=selected, evidence_bundle=bundle)


def _merge_clause_group(
    group: Sequence[tuple[float, SpecClauseEvidence, list[str]]],
    *,
    graph_matched: bool,
) -> SpecClauseEvidence:
    """把同一条款的多个分块合成一条证据，正文按原文顺序拼接以保持可回溯。

    分块顺序先取 chunk_index，再取源文件名：条款级分块用 chunk_index 表达顺序，
    行级碎片语料用文件名里的表号/行号表达顺序。拼接后的正文同步写入 raw_clause_body，
    使 `verify_evidence_projection` 仍能逐段回溯到拼接后的原文。
    """

    best = max(group, key=lambda entry: entry[0])[1]
    if len(group) == 1:
        return best.model_copy(
            update={"metadata": {**best.metadata, "graph_matched": graph_matched, "matched_terms": group[0][2]}}
        )

    ordered = sorted(group, key=lambda entry: _chunk_order(entry[1]))
    # 引用身份取原文顺序第一块（即展示正文开头的那一段），避免引用键指向正文里排在后
    # 面的碎片文件；分数仍取整组最高分。
    base = ordered[0][1]
    bodies: list[str] = []
    raw_bodies: list[str] = []
    matched_terms: list[str] = []
    aggregated_from: list[str] = []
    for _score, item, terms in ordered:
        if item.document_id not in aggregated_from:
            aggregated_from.append(item.document_id)
        body = item.clause_body.strip()
        if body and body not in bodies:
            bodies.append(body)
        raw_body = str(item.metadata.get("raw_clause_body") or item.clause_body).strip()
        if raw_body and raw_body not in raw_bodies:
            raw_bodies.append(raw_body)
        matched_terms.extend(term for term in terms if term not in matched_terms)

    logger.debug(
        f"聚合条款 {base.standard_no} {base.clause_no}：{len(group)} 个分块，"
        f"{len(aggregated_from)} 个文档，{len(bodies)} 段正文"
    )
    return base.model_copy(
        update={
            "clause_body": "\n\n".join(bodies) or base.clause_body,
            # rerank_score 保持重排模型的原始输出语义，不带上本地排序加分。
            "rerank_score": max((item.rerank_score or 0.0) for _score, item, _terms in group),
            "metadata": {
                **base.metadata,
                "raw_clause_body": "\n\n".join(raw_bodies) or base.clause_body,
                "aggregated_from": aggregated_from,
                "graph_matched": graph_matched,
                "matched_terms": matched_terms,
            },
        }
    )


def _chunk_order(item: SpecClauseEvidence) -> tuple[int, str]:
    """返回分块在原文中的顺序键，用于聚合时还原原文顺序。"""

    try:
        index = int(item.metadata.get("chunk_index"))
    except (TypeError, ValueError):
        index = 0
    return (index, str(item.source_file or ""))


# ---------------------------------------------------------------------------
# 证据适配（原 adapters.py）
# ---------------------------------------------------------------------------


def adapt_vector_results(
    results: Iterable[Mapping[str, Any]],
    metadata_by_file_id: Mapping[str, Mapping[str, Any]],
    query_text: str | None = None,
) -> list[SpecClauseEvidence]:
    """将 MilvusKB.aquery 的结果转换为规范条款证据。"""

    evidence: list[SpecClauseEvidence] = []
    for result in results:
        metadata = result.get("metadata") or {}
        file_id = str(metadata.get("file_id") or "")
        document_metadata = metadata_by_file_id.get(file_id)
        if not document_metadata:
            continue
        clause_metadata = _resolve_clause_metadata(metadata, document_metadata, str(result.get("content") or ""))
        if clause_metadata is None:
            continue
        evidence.append(
            _build_evidence(
                document_id=file_id,
                document_metadata=document_metadata,
                clause_metadata=clause_metadata,
                clause_body=str(result.get("content") or ""),
                rerank_score=_as_float(result.get("rerank_score")),
                metadata={
                    "chunk_id": metadata.get("chunk_id"),
                    "graph_score": _as_float(result.get("graph_score")),
                    "raw_clause_body": str(result.get("content") or ""),
                    "parsed_clause_body": str(clause_metadata.get("clause_body") or ""),
                    "raw_clause_no": str(clause_metadata.get("clause_no") or ""),
                    "raw_clause_title": str(clause_metadata.get("clause_title") or ""),
                    "keyword_only": bool(metadata.get("keyword_only")),
                },
                query_text=query_text,
            )
        )
    return evidence


def _resolve_clause_metadata(
    result_metadata: Mapping[str, Any],
    document_metadata: Mapping[str, Any],
    clause_body: str = "",
) -> Mapping[str, Any] | None:
    clause_no = str(result_metadata.get("clause_no") or "")
    clauses = document_metadata.get("clauses") or []
    if clause_no:
        for clause in clauses:
            if str(clause.get("clause_no") or "") == clause_no:
                return clause
        return None
    if len(clauses) == 1:
        return clauses[0]

    heading_matches = re.findall(r"(?<![\d.])\d{1,2}(?:\.\d{1,2}){1,3}(?![\d.])", clause_body)
    heading_candidates = [clause for clause in clauses if str(clause.get("clause_no") or "") in heading_matches]
    if len(heading_candidates) == 1:
        return heading_candidates[0]

    matched_clauses = [
        clause
        for clause in clauses
        if str(clause.get("clause_title") or "") and str(clause["clause_title"]) in clause_body
    ]
    if len(matched_clauses) == 1:
        return matched_clauses[0]

    normalized_body = _normalize_clause_text(clause_body)
    scored_clauses = sorted(
        (
            (
                _shared_trigram_count(normalized_body, _normalize_clause_text(str(clause.get("clause_body") or ""))),
                clause,
            )
            for clause in clauses
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if scored_clauses and scored_clauses[0][0] >= 2:
        if len(scored_clauses) == 1 or scored_clauses[0][0] > scored_clauses[1][0]:
            return scored_clauses[0][1]
    return None


def _normalize_clause_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _shared_trigram_count(left: str, right: str) -> int:
    if len(left) < 3 or len(right) < 3:
        return 0
    left_trigrams = {left[index : index + 3] for index in range(len(left) - 2)}
    right_trigrams = {right[index : index + 3] for index in range(len(right) - 2)}
    return len(left_trigrams & right_trigrams)


def _build_evidence(
    *,
    document_id: str,
    document_metadata: Mapping[str, Any],
    clause_metadata: Mapping[str, Any],
    clause_body: str,
    rerank_score: float | None,
    metadata: dict[str, Any],
    query_text: str | None = None,
) -> SpecClauseEvidence:
    clause_no = str(clause_metadata.get("clause_no") or "未知条款")
    resolved_body = _resolve_clause_body(clause_metadata, clause_body)
    matched = query_text and any(term in clause_body for term in _line_match_terms(query_text))
    display_source = clause_body if matched else resolved_body
    display_body = _select_relevant_clause_text(display_source, query_text)
    clause_title = _resolve_clause_title(clause_metadata, clause_body, clause_no)
    return SpecClauseEvidence(
        document_id=document_id,
        standard_name=str(document_metadata.get("standard_name") or "未命名规范"),
        standard_no=str(document_metadata.get("standard_no") or "未知标准"),
        version=document_metadata.get("version"),
        status=str(document_metadata.get("status") or "未知"),
        replaced_by=document_metadata.get("replaced_by"),
        clause_no=clause_no,
        clause_title=clause_title,
        clause_body=display_body or "条款正文缺失",
        scope=clause_metadata.get("scope") or document_metadata.get("scope"),
        rerank_score=rerank_score,
        source_file=document_metadata.get("source_file"),
        source_page=clause_metadata.get("source_page"),
        source_page_ref=clause_metadata.get("source_page_ref"),
        metadata=metadata,
    )


def _resolve_clause_body(clause_metadata: Mapping[str, Any], result_content: str) -> str:
    """优先返回已解析条款正文，避免把相邻章节混入卡片。"""

    extracted_body = str(clause_metadata.get("clause_body") or "").strip()
    return extracted_body or result_content.strip()


def _select_relevant_clause_text(body: str, query_text: str | None) -> str:
    """只展示命中问题维度的原文行；命中表格时保留整张表，原文全文保存在 metadata。"""

    if not body or not query_text:
        return body
    terms = _line_match_terms(query_text)
    if not terms:
        return body
    lines = body.splitlines()
    keep = [any(term in line for term in terms) for line in lines]
    if not any(keep):
        return body
    for block in _table_blocks(lines):
        if any(keep[index] for index in block):
            for index in block:
                keep[index] = True
    return "\n".join(line for line, keep_line in zip(lines, keep) if keep_line)


def _table_blocks(lines: list[str]) -> list[list[int]]:
    """返回连续表格行的索引分组，保证命中时表头与分隔行不被裁掉。"""

    blocks: list[list[int]] = []
    current: list[int] = []
    for index, line in enumerate(lines):
        if "|" in line:
            current.append(index)
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _line_match_terms(query: str) -> list[str]:
    """返回用于行级命中判定的词元：完整词元加上 2~6 字的子串，尽量提高召回。"""

    terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|\d+(?:\.\d+)?kV|[A-Za-z0-9]+", query))
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", query):
        for size in range(2, min(len(phrase), 6) + 1):
            terms.update(phrase[index : index + size] for index in range(len(phrase) - size + 1))
    return sorted(terms, key=len, reverse=True)


def _resolve_clause_title(clause_metadata: Mapping[str, Any], clause_body: str, clause_no: str) -> str:
    """标题只有在原文出现时才提取，无法确认时保留条款编号。"""

    title = str(clause_metadata.get("clause_title") or "").strip()
    if re.fullmatch(r"\d+\.\d+", title) and title != clause_no:
        title = ""
    if title and title != clause_no and title in clause_body:
        return title
    pattern = re.compile(rf"(?:^|\n)\s*{re.escape(clause_no)}\s+([^\n。；;]+)")
    match = pattern.search(clause_body)
    if match:
        return match.group(1).strip(" ：:、") or clause_no
    return title or clause_no


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 证据投影校验（原 verification.py）
# ---------------------------------------------------------------------------

# 只覆盖 OCR 常见的字符写法差异，不改变编号语义。
_CLAUSE_NO_TRANSLATION = str.maketrans(
    {
        "０": "0",
        "１": "1",
        "２": "2",
        "３": "3",
        "４": "4",
        "５": "5",
        "６": "6",
        "７": "7",
        "８": "8",
        "９": "9",
        "．": ".",
        "。": ".",
        "：": ":",
        "／": "/",
        "－": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        "＿": "_",
    }
)


class PowerSpecEvidenceVerification(BaseModel):
    """表示一组规范卡片是否仍可回溯到原始证据。"""

    model_config = ConfigDict(frozen=True)

    valid: bool
    issues: list[VerificationIssue] = Field(default_factory=list)


def verify_evidence_projection(evidence: Sequence[SpecClauseEvidence]) -> PowerSpecEvidenceVerification:
    """验证展示正文、条款号和标题没有超出原始证据。"""

    issues: list[VerificationIssue] = []
    for item in evidence:
        raw_bodies = evidence_raw_bodies(item)
        if not is_supported_display_body(item.clause_body, raw_bodies):
            issues.append(
                VerificationIssue(
                    code="body_not_supported",
                    message="展示条款正文不是原始检索证据的内容",
                    citation=item.citation_key,
                )
            )
        raw_clause_no = str(item.metadata.get("raw_clause_no") or item.clause_no)
        if not is_supported_clause_no(item.clause_no, raw_clause_no):
            issues.append(
                VerificationIssue(
                    code="clause_no_changed",
                    message="条款编号只允许修复 OCR 字符，不能改变编号本身",
                    citation=item.citation_key,
                )
            )
        raw_title = str(item.metadata.get("raw_clause_title") or item.clause_title)
        if not is_supported_clause_title(item.clause_title, raw_title, raw_bodies):
            issues.append(
                VerificationIssue(
                    code="clause_title_not_supported",
                    message="条款标题无法在原始证据中确认",
                    citation=item.citation_key,
                )
            )
    return PowerSpecEvidenceVerification(valid=not issues, issues=issues)


def evidence_raw_bodies(item: SpecClauseEvidence) -> list[str]:
    """返回一条证据可用于回溯的原始正文候选，按可信度排序。"""

    raw_bodies = [
        str(item.metadata.get("raw_clause_body") or ""),
        str(item.metadata.get("parsed_clause_body") or ""),
    ]
    if not any(raw_bodies):
        raw_bodies.append(item.clause_body)
    return [body for body in raw_bodies if body]


def is_supported_display_body(display_body: str, raw_bodies: Sequence[str]) -> bool:
    """判断展示正文是否逐段、按序来自原始正文。

    允许挑选不连续片段、跨行合并，以及列表符号等纯格式标记；不允许改写、插入或
    重排原文文字。
    """

    segments = [normalize_evidence_text(_strip_list_marker(segment)) for segment in display_body.splitlines()]
    segments = [segment for segment in segments if segment]
    if not segments:
        return True
    normalized_raw = [normalize_evidence_text(body) for body in raw_bodies if body]
    for raw in normalized_raw:
        position = 0
        for segment in segments:
            found = raw.find(segment, position)
            if found < 0:
                break
            position = found + len(segment)
        else:
            return True
    return False


def is_supported_clause_no(display_clause_no: str, raw_clause_no: str) -> bool:
    """判断展示编号是否只是对原始编号的字符级修复，而不是重新编号。"""

    if display_clause_no == raw_clause_no:
        return True
    return normalize_clause_no(display_clause_no) == normalize_clause_no(raw_clause_no)


def is_supported_clause_title(title: str, raw_title: str, raw_bodies: Sequence[str]) -> bool:
    """判断展示标题是否等于原始标题，或在原始正文中出现过。"""

    if not title or title == raw_title:
        return True
    return any(title in body for body in raw_bodies)


def normalize_clause_no(value: str) -> str:
    """归一化条款编号的字符写法，用于判断是否发生重新编号。"""

    normalized = html.unescape(str(value)).strip()
    normalized = normalized.translate(_CLAUSE_NO_TRANSLATION)
    normalized = re.sub(r"[\s　]+", "", normalized)
    return normalized.strip(".-—–_")


def normalize_evidence_text(value: str) -> str:
    """去除空白和展示高亮，用于正文回溯比较。"""

    value = html.unescape(value)
    value = re.sub(r"<mark>(.*?)</mark>", r"\1", value, flags=re.DOTALL)
    return re.sub(r"\s+", "", value)


def _strip_list_marker(segment: str) -> str:
    """去掉模型为可读性添加的列表符号，避免把格式当成文字改动。"""

    return re.sub(r"^\s*(?:[-*•·]|\d+\s*[.、)）]|\d+\s*[）)])\s*", "", segment)


# ---------------------------------------------------------------------------
# 条款字段校对（原 clause_review.py）
# ---------------------------------------------------------------------------

DISPLAY_CLAUSE_NO_KEY = "display_clause_no"
DISPLAY_CLAUSE_TITLE_KEY = "display_clause_title"
MAX_BODY_CHARS = 2000
MAX_TITLE_CHARS = 60

ClauseReviewModelCall = Callable[[str], Awaitable[str | dict[str, Any]]]

REVIEW_PROMPT = """你是电力规范条款字段校对助手。知识库解析可能带有 OCR 噪声，请判断每条条款的编号与标题是否与原文一致。

对每条条款只输出一个 JSON 对象，不要输出解释或 Markdown：
{"items":[{"citation_key":"原样返回","clause_no":"修复后的编号，无需修复时原样返回","clause_title":"修复后的标题，无需修复时原样返回","note":"一句话说明修复了什么"}]}

规则：
1. 编号只允许修复字符级错误：全角数字、多余空格、错把 `．` 或 `。` 当小数点、破折号写法不统一。
禁止重新编号，禁止改变数字含义。
2. 标题必须是原文出现过的文字。原文标题缺失、或标题实际是页码、表格标记、列名堆叠时，
从原文中挑出真正的章节标题；确实没有可用标题时原样返回。
3. 不能确定时原样返回，不要猜测，不要补充原文没有的设备名或数值。
4. 只输出 JSON。"""


class ClauseCorrection(BaseModel):
    """模型对单条条款字段的校对结果。"""

    model_config = ConfigDict(frozen=True)

    citation_key: str = Field(min_length=1)
    clause_no: str | None = None
    clause_title: str | None = None
    note: str | None = None


class ClauseReviewOutcome(BaseModel):
    """一次条款字段校对的结果。"""

    model_config = ConfigDict(frozen=True)

    evidence: list[Any] = Field(default_factory=list)
    applied: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)


def build_correction_prompt(evidences: Sequence[Any]) -> str:
    """构造条款字段校对提示词，只给出字段与用于判断的原文片段。"""

    lines = [REVIEW_PROMPT, "", "## 待校对条款"]
    for evidence in evidences:
        body = str(getattr(evidence, "clause_body", "") or "")[:MAX_BODY_CHARS]
        lines.extend(
            [
                f"### citation_key: {citation_key_of(evidence)}",
                f"clause_no: {getattr(evidence, 'clause_no', '')}",
                f"clause_title: {getattr(evidence, 'clause_title', '')}",
                f"原文片段：\n{body}",
                "",
            ]
        )
    return "\n".join(lines)


def parse_corrections(raw: str | dict[str, Any]) -> dict[str, ClauseCorrection]:
    """解析模型校对结果，无法解析时返回空映射（等同于不做修复）。"""

    try:
        payload = parse_model_json(raw)
    except ValueError as exc:
        logger.warning(f"条款字段校对结果无法解析: {exc}")
        return {}
    items = payload.get("items")
    if not isinstance(items, list):
        return {}
    corrections: dict[str, ClauseCorrection] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            correction = ClauseCorrection.model_validate(item)
        except ValidationError:
            continue
        corrections[correction.citation_key] = correction
    return corrections


def apply_corrections(evidences: Sequence[Any], corrections: dict[str, ClauseCorrection]) -> ClauseReviewOutcome:
    """校验并应用校对结果；不合法的修复被丢弃并记录。"""

    updated: list[Any] = []
    applied: list[str] = []
    rejected: list[str] = []
    for evidence in evidences:
        correction = corrections.get(citation_key_of(evidence))
        if correction is None:
            updated.append(evidence)
            continue
        metadata = dict(getattr(evidence, "metadata", None) or {})
        raw_clause_no = str(metadata.get("raw_clause_no") or evidence.clause_no)
        raw_title = str(metadata.get("raw_clause_title") or evidence.clause_title)
        raw_bodies = evidence_raw_bodies(evidence)

        display_no = _clean(correction.clause_no, limit=MAX_TITLE_CHARS)
        if display_no and display_no != evidence.clause_no:
            if is_supported_clause_no(display_no, raw_clause_no):
                metadata[DISPLAY_CLAUSE_NO_KEY] = display_no
                applied.append(f"{citation_key_of(evidence)}:编号 {evidence.clause_no} → {display_no}")
            else:
                rejected.append(f"{citation_key_of(evidence)}:编号修复被拒绝「{display_no}」")

        display_title = _clean(correction.clause_title, limit=MAX_TITLE_CHARS)
        if display_title and display_title != evidence.clause_title:
            if is_supported_clause_title(display_title, raw_title, raw_bodies):
                metadata[DISPLAY_CLAUSE_TITLE_KEY] = display_title
                applied.append(f"{citation_key_of(evidence)}:标题 → {display_title}")
            else:
                rejected.append(f"{citation_key_of(evidence)}:标题修复被拒绝「{display_title}」")

        updated.append(
            evidence.model_copy(update={"metadata": metadata})
            if metadata != getattr(evidence, "metadata", None)
            else evidence
        )
    return ClauseReviewOutcome(evidence=updated, applied=applied, rejected=rejected)


async def review_clause_metadata(
    evidences: Sequence[Any],
    *,
    model_call: ClauseReviewModelCall | None,
) -> ClauseReviewOutcome:
    """由模型校对条款编号与标题；模型不可用时原样返回。"""

    if model_call is None or not evidences:
        return ClauseReviewOutcome(evidence=list(evidences))
    try:
        raw = await model_call(build_correction_prompt(evidences))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"条款字段校对失败，保留原始字段: {exc}")
        return ClauseReviewOutcome(evidence=list(evidences))
    outcome = apply_corrections(evidences, parse_corrections(raw))
    if outcome.applied:
        logger.info(f"条款字段校对修正 {len(outcome.applied)} 处: {'; '.join(outcome.applied)}")
    if outcome.rejected:
        logger.warning(f"条款字段校对拒绝 {len(outcome.rejected)} 处: {'; '.join(outcome.rejected)}")
    return outcome


def display_clause_no(evidence: Any) -> str:
    """返回展示用编号，优先使用校对结果。"""

    metadata = getattr(evidence, "metadata", None) or {}
    return str(metadata.get(DISPLAY_CLAUSE_NO_KEY) or getattr(evidence, "clause_no", ""))


def display_clause_title(evidence: Any) -> str:
    """返回展示用标题，优先使用校对结果。"""

    metadata = getattr(evidence, "metadata", None) or {}
    return str(metadata.get(DISPLAY_CLAUSE_TITLE_KEY) or getattr(evidence, "clause_title", ""))


def _clean(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "无", "无需修复"}:
        return None
    return text[:limit]
