"""电力规范测评图谱的运行时只读查询与知识库条款解析。

图谱只保存测评集的“问题—答案—依据条款”映射，不保存条款正文。命中后按标准编号与
条款号从知识库 ``processing_params["power_spec"]["clauses"]`` 取正文，构造与其他召回
相同的 :class:`SpecClauseEvidence`。图谱是可选增强：调用方负责把连接失败降级为普通
检索，本模块不吞掉 ``TuGraphError``。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from yuxi.config.options import power_spec_graph_opts
from yuxi.knowledge.power_spec.contracts import PowerSpecQuery, SpecClauseEvidence
from yuxi.knowledge.power_spec.ingestion import is_power_spec_metadata_ready
from yuxi.knowledge.power_spec.retrieval import (
    build_clause_evidence,
    clause_identity_key,
    normalize_standard_no,
)
from yuxi.utils import logger

if TYPE_CHECKING:
    from yuxi.agents.toolkits.tugraph import TuGraphConfig

POWER_SPEC_QA_GRAPH_ENABLED = os.getenv("POWER_SPEC_QA_GRAPH_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
POWER_SPEC_QA_GRAPH_MATCH_THRESHOLD = float(os.getenv("POWER_SPEC_QA_GRAPH_MATCH_THRESHOLD", "0.85"))
POWER_SPEC_QA_GRAPH_QUESTION_LIMIT = int(os.getenv("POWER_SPEC_QA_GRAPH_QUESTION_LIMIT", "5000"))
POWER_SPEC_QA_GRAPH_CLAUSE_LIMIT = int(os.getenv("POWER_SPEC_QA_GRAPH_CLAUSE_LIMIT", "128"))
MAX_GRAPH_EVIDENCE_CLAUSES = 20
MAX_GRAPH_CHILDREN_PER_CITATION = 12
MAX_GRAPH_TABLE_CLAUSES = 8

# TuGraph 只读查询单语句最多一个 MATCH；三条查询按 问题 → 答案 → 条款 顺序执行。
_QUESTION_QUERY = """
MATCH (q:问题)
RETURN q.id AS qid, q.问题 AS qtext, q.命中键 AS qkeys,
       q.预期行为 AS qbehavior, q.来源 AS qsource, q.题号 AS qno
LIMIT $question_limit
"""
_ANSWER_QUERY = """
MATCH (q:问题 {id: $qid})-[:有答案]->(a:答案)
RETURN a.id AS aid, a.拒答 AS arefuse, a.跨源要求 AS across, a.答案 AS atext
LIMIT 1
"""
_CLAUSE_QUERY = """
MATCH (a:答案 {id: $aid})-[r:依据条款]->(c:条款)
RETURN c.标准编号 AS cstd, c.条款号 AS cno, c.条款标题 AS ctitle,
       c.表号 AS ctable, c.标准页 AS cpage, c.PDF页 AS cpdf, c.章节 AS csection,
       c.状态 AS cstatus, r.原文片段 AS cexcerpt, r.排序 AS crank, r.分组 AS cgroup
LIMIT $clause_limit
"""

_VOLTAGE_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*[kK][vV]")
_STANDARD_PATTERN = re.compile(
    r"\b(?P<prefix>GB|DL|Q\s*/\s*GDW|NB|JB|TB)\s*/?\s*(?P<recommended>T?)\s*"
    r"(?P<number>\d+(?:\.\d+)*)\s*(?P<dash>[—–-])\s*(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_NEGATION_MARKERS = ("非", "不", "无", "未")
_QUESTION_PUNCTUATION = re.compile(r"[\s\u3000，。；：？！、（）【】《》“”‘’·—–－\-_/?!,.;:()\[\]<>\"']+")


@dataclass(frozen=True)
class PowerSpecGraphCitation:
    """一条测评集给出的依据条款映射。"""

    standard_no: str
    clause_no: str
    clause_title: str | None = None
    table_no: str | None = None
    section: str | None = None
    excerpt: str | None = None
    status: str = "现行"
    rank: int = 1
    group: int = 0


@dataclass(frozen=True)
class PowerSpecGraphHit:
    """一次测评图谱命中。"""

    question_id: str
    question: str
    matched_variant: str
    match_kind: str
    score: float
    expected_behavior: str | None
    refusal: bool
    matched_group: int
    citations: tuple[PowerSpecGraphCitation, ...] = ()
    answer_text: str | None = None
    cross_source: bool = False

    @property
    def corrective(self) -> bool:
        """是否是应纠正错误前提或拦截违章的题，而不是范围外拒答题。"""

        behavior = str(self.expected_behavior or "")
        return self.refusal and any(marker in behavior for marker in ("纠错", "拦截", "前提错误", "违章"))


@dataclass(frozen=True)
class GraphResolution:
    """一次图谱命中在知识库中的解析结果。"""

    evidence: list[SpecClauseEvidence] = field(default_factory=list)
    unmatched: list[PowerSpecGraphCitation] = field(default_factory=list)


@dataclass(frozen=True)
class _QuestionKey:
    key: str
    text: str
    group: int
    kind: str
    index: int


@dataclass(frozen=True)
class _QuestionRow:
    question_id: str
    question: str
    keys: tuple[_QuestionKey, ...]
    expected_behavior: str | None
    source: str | None
    number: str | None


async def resolve_power_spec_graph_config() -> TuGraphConfig | None:
    """读取管理员配置的电力规范问答图谱连接；未启用或字段不完整时返回 None。"""

    # 延迟导入：本模块由 ``yuxi.agents.toolkits`` 在包初始化时间接导入，
    # 模块级导入 ``yuxi.agents`` 会形成循环。
    from yuxi.agents.toolkits.tugraph import TuGraphConfig

    if not POWER_SPEC_QA_GRAPH_ENABLED:
        return None
    try:
        resolved = await power_spec_graph_opts.get()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取电力规范问答图谱配置失败: {exc}")
        return None
    uri = str(resolved.get("server_url") or "").strip()
    graph = str(resolved.get("graph") or "").strip()
    username = str(resolved.get("username") or "").strip()
    password = str(resolved.get("password") or "").strip()
    if not uri or not graph or not username or not password:
        return None
    return TuGraphConfig(uri=uri, graph=graph, username=username, password=password)


def normalize_question_text(value: str) -> str:
    """规范化问题文本，使标点、空格、全半角和标准号写法不影响命中。"""

    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return _QUESTION_PUNCTUATION.sub("", text)


def question_similarity(left: str, right: str) -> float:
    """返回两个已规范化问题的字符 bigram Dice 相似度。"""

    if not left or not right:
        return 0.0
    left_bigrams = {left[index : index + 2] for index in range(len(left) - 1)}
    right_bigrams = {right[index : index + 2] for index in range(len(right) - 1)}
    if not left_bigrams or not right_bigrams:
        return 1.0 if left == right else 0.0
    return 2 * len(left_bigrams & right_bigrams) / (len(left_bigrams) + len(right_bigrams))


def match_question_rows(
    rows: Sequence[Mapping[str, Any]],
    texts: Sequence[str],
    *,
    threshold: float = POWER_SPEC_QA_GRAPH_MATCH_THRESHOLD,
) -> tuple[_QuestionRow, _QuestionKey, str, float] | None:
    """按规范化精确匹配优先、受 guard 约束的模糊匹配兜底选择最佳命中。

    三道 guard（否定词、电压等级、标准编号）必须在**未规范化**的原文上判断：规范化会剥掉
    ``/`` 与 ``-``，标准号正则对规范化文本永远匹配不上，guard 会退化为恒通过。
    """

    pairs = [(str(raw or ""), normalize_question_text(raw)) for raw in texts]
    pairs = [(raw, normalized) for raw, normalized in pairs if normalized]
    if not pairs:
        return None

    best: tuple[_QuestionRow, _QuestionKey, str, float] | None = None
    best_rank: tuple[int, float, int, int, str] | None = None
    for row in rows:
        parsed = _question_row(row)
        if parsed is None:
            continue
        for key in parsed.keys:
            for raw, normalized in pairs:
                if normalized == key.key:
                    candidate = (parsed, key, "exact", 1.0)
                else:
                    if not _guards_ok(raw, key.text):
                        continue
                    score = question_similarity(normalized, key.key)
                    if score < threshold:
                        continue
                    candidate = (parsed, key, "fuzzy", score)
                rank = (
                    0 if candidate[2] == "exact" else 1,
                    -candidate[3],
                    key.index,
                    0 if parsed.source == "md" else 1,
                    parsed.question_id,
                )
                if best_rank is None or rank < best_rank:
                    best = candidate
                    best_rank = rank
    return best


def find_power_spec_qa_hit(
    config: TuGraphConfig,
    query: PowerSpecQuery,
    *,
    original_query_text: str,
) -> PowerSpecGraphHit | None:
    """只读查询测评图谱并返回最佳命中；连接或查询失败由调用方降级。"""

    from yuxi.agents.toolkits.tugraph import TuGraphClient

    client = TuGraphClient(config)
    try:
        rows = client.run(_QUESTION_QUERY, question_limit=POWER_SPEC_QA_GRAPH_QUESTION_LIMIT)
        texts = [query.query, original_query_text, *[sub.query for sub in query.sub_queries]]
        matched = match_question_rows(rows, [text for text in dict.fromkeys(texts) if text])
        if matched is None:
            return None
        row, key, match_kind, score = matched
        answer_rows = client.run(_ANSWER_QUERY, qid=row.question_id)
        if not answer_rows:
            logger.warning(f"电力规范问答图谱命中问题缺少答案节点: {row.question_id}")
            return None
        answer = answer_rows[0]
        answer_id = str(answer.get("aid") or "").strip()
        if not answer_id:
            return None
        refusal = _truthy(answer.get("arefuse")) or "拒答" in (row.expected_behavior or "")
        citations: list[PowerSpecGraphCitation] = []
        if not refusal:
            citation_rows = client.run(
                _CLAUSE_QUERY,
                aid=answer_id,
                clause_limit=POWER_SPEC_QA_GRAPH_CLAUSE_LIMIT,
            )
            citations = _load_citations(citation_rows)
        citations.sort(key=lambda item: _citation_sort_key(item, key.group))
        return PowerSpecGraphHit(
            question_id=row.question_id,
            question=row.question,
            matched_variant=key.text,
            match_kind=match_kind,
            score=score,
            expected_behavior=row.expected_behavior,
            refusal=refusal,
            matched_group=key.group,
            citations=tuple(citations),
            answer_text=_optional_text(answer.get("atext")),
            cross_source=_truthy(answer.get("across")),
        )
    finally:
        client.close()


def resolve_hit_evidence(
    hit: PowerSpecGraphHit,
    metadata_by_file_id: Mapping[str, Mapping[str, Any]],
    *,
    limit: int = MAX_GRAPH_EVIDENCE_CLAUSES,
) -> GraphResolution:
    """把图谱条款映射解析为知识库条款证据；解析不到的映射单独返回。"""

    exact, by_standard = _build_clause_index(metadata_by_file_id)
    evidence: list[SpecClauseEvidence] = []
    unmatched: list[PowerSpecGraphCitation] = []
    seen: set[tuple[str, str]] = set()

    for citation in hit.citations:
        standard_key = normalize_standard_no(citation.standard_no)
        clause_key = clause_identity_key(citation.clause_no)
        if not standard_key or not clause_key:
            unmatched.append(citation)
            continue
        matched: list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]] = []
        table_matches = _table_matches(by_standard, standard_key, citation) if citation.table_no else []
        _append_unique(matched, table_matches)
        _append_unique(matched, list(exact.get((standard_key, clause_key), ())))
        children = [entry for entry in by_standard.get(standard_key, ()) if entry[3].startswith(f"{clause_key}.")]
        children.sort(key=lambda entry: _natural_clause_key(entry[3]))
        _append_unique(matched, children[:MAX_GRAPH_CHILDREN_PER_CITATION])
        if not matched:
            matched = _excerpt_matches(by_standard, standard_key, citation)
        if not matched:
            unmatched.append(citation)
            continue
        for file_id, document_metadata, clause_metadata, resolved_key in matched:
            group_key = (standard_key, resolved_key)
            if group_key in seen:
                continue
            seen.add(group_key)
            evidence.append(
                _build_graph_evidence(
                    file_id=file_id,
                    document_metadata=document_metadata,
                    clause_metadata=clause_metadata,
                    hit=hit,
                    citation=citation,
                )
            )
            if len(evidence) >= limit:
                return GraphResolution(evidence=evidence, unmatched=unmatched)
    return GraphResolution(evidence=evidence, unmatched=unmatched)


def _question_row(raw: Mapping[str, Any]) -> _QuestionRow | None:
    question_id = str(raw.get("qid") or "").strip()
    question = str(raw.get("qtext") or "").strip()
    if not question_id or not question:
        return None
    keys = _load_keys(raw.get("qkeys"))
    if not keys:
        keys = (_QuestionKey(normalize_question_text(question), question, 0, "标准书面", 0),)
    return _QuestionRow(
        question_id=question_id,
        question=question,
        keys=tuple(keys),
        expected_behavior=_optional_text(raw.get("qbehavior")),
        source=_optional_text(raw.get("qsource")),
        number=_optional_text(raw.get("qno")),
    )


def _load_keys(raw: Any) -> tuple[_QuestionKey, ...]:
    payload = _load_json(raw)
    if not isinstance(payload, list):
        return ()
    keys: list[_QuestionKey] = []
    for index, item in enumerate(payload):
        if isinstance(item, str):
            text = item
            group = 0
            kind = "标准书面"
        elif isinstance(item, Mapping):
            text = str(item.get("text") or item.get("key") or "").strip()
            group = _as_int(item.get("group"), 0)
            kind = str(item.get("type") or "标准书面")
        else:
            continue
        key = normalize_question_text(text)
        if key and all(existing.key != key for existing in keys):
            keys.append(_QuestionKey(key=key, text=text, group=group, kind=kind, index=index))
    return tuple(keys)


def _load_citations(rows: Sequence[Mapping[str, Any]]) -> list[PowerSpecGraphCitation]:
    citations: list[PowerSpecGraphCitation] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        standard_no = str(row.get("cstd") or "").strip()
        clause_no = str(row.get("cno") or "").strip()
        if not standard_no or not clause_no:
            continue
        key = (normalize_standard_no(standard_no), clause_identity_key(clause_no))
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            PowerSpecGraphCitation(
                standard_no=standard_no,
                clause_no=clause_no,
                clause_title=_optional_text(row.get("ctitle")),
                table_no=_optional_text(row.get("ctable")),
                section=_optional_text(row.get("csection")),
                excerpt=_optional_text(row.get("cexcerpt")),
                status=_optional_text(row.get("cstatus")) or "现行",
                rank=max(_as_int(row.get("crank"), 1), 1),
                group=max(_as_int(row.get("cgroup"), 0), 0),
            )
        )
    return citations


def _build_clause_index(
    metadata_by_file_id: Mapping[str, Mapping[str, Any]],
) -> tuple[
    dict[tuple[str, str], list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]]],
    dict[str, list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]]],
]:
    exact: dict[tuple[str, str], list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]]] = {}
    by_standard: dict[str, list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]]] = {}
    for file_id in sorted(metadata_by_file_id):
        document = metadata_by_file_id[file_id]
        if not isinstance(document, Mapping) or not is_power_spec_metadata_ready(dict(document)):
            continue
        # 图谱所在批次的标准状态由测评图谱决定；知识库解析把部分 2023 版标准误标为
        # “废止”时，不能因此丢掉图谱已经确认的映射。证据状态在构造时取图谱条款状态。
        standard_key = normalize_standard_no(str(document.get("standard_no") or ""))
        if not standard_key:
            continue
        clauses = document.get("clauses") or []
        if not isinstance(clauses, list):
            continue
        for clause in clauses:
            if not isinstance(clause, Mapping):
                continue
            clause_key = clause_identity_key(str(clause.get("clause_no") or ""))
            if not clause_key:
                continue
            entry = (file_id, document, clause, clause_key)
            exact.setdefault((standard_key, clause_key), []).append(entry)
            by_standard.setdefault(standard_key, []).append(entry)
    for entries in by_standard.values():
        entries.sort(key=lambda item: (_natural_clause_key(item[3]), item[0]))
    return exact, by_standard


def _append_unique(
    target: list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]],
    entries: Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any], str]],
) -> None:
    """按文件和条款号去重追加，保持调用方给出的优先顺序。"""

    seen = {(entry[0], entry[3]) for entry in target}
    for entry in entries:
        key = (entry[0], entry[3])
        if key not in seen:
            target.append(entry)
            seen.add(key)


def _table_matches(
    by_standard: Mapping[str, Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any], str]]],
    standard_key: str,
    citation: PowerSpecGraphCitation,
) -> list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]]:
    """按“表 N”在标准内找条款；用于测评集只给表号、不给条款号的引用。"""

    table_numbers = _table_numbers(citation)
    if not table_numbers:
        return []
    matched: list[tuple[int, tuple[str, Mapping[str, Any], Mapping[str, Any], str]]] = []
    for entry in by_standard.get(standard_key, ()):
        _file_id, _document, clause, clause_key = entry
        title = _compact_text(clause.get("clause_title"))
        body = _compact_text(clause.get("clause_body"))
        for table in table_numbers:
            # “见表 48”只是交叉引用；优先命中真正的表题“表 48 变压器油的试验项目和要求”。
            actual_title = re.compile(
                rf"(?<!见)表\s*{re.escape(table)}(?=\s*(?:[\u4e00-\u9fff]|[（(]))"
                r"(?!\s*(?:序号|中|的|所|规|\|))"
            )
            marker = f"表{table}"
            if actual_title.search(title):
                matched.append((0, entry))
                break
            if actual_title.search(body):
                matched.append((1, entry))
                break
            if marker in title or marker in body:
                matched.append((2, entry))
                break
    matched.sort(
        key=lambda item: (
            item[0],
            -len(_compact_text(item[1][2].get("clause_body"))),
            _natural_clause_key(item[1][3]),
        )
    )
    return [entry for _priority, entry in matched[:MAX_GRAPH_TABLE_CLAUSES]]


def _excerpt_matches(
    by_standard: Mapping[str, Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any], str]]],
    standard_key: str,
    citation: PowerSpecGraphCitation,
) -> list[tuple[str, Mapping[str, Any], Mapping[str, Any], str]]:
    """按测评集原文摘录回退定位条款，处理知识库把子条款并入父条款的情况。"""

    excerpt = _normalize_excerpt(citation.excerpt)
    if len(excerpt) < 8:
        return []
    grams = {excerpt[index : index + 8] for index in range(len(excerpt) - 7)}
    if not grams:
        return []
    scored: list[tuple[int, tuple[str, Mapping[str, Any], Mapping[str, Any], str]]] = []
    for entry in by_standard.get(standard_key, ()):
        _file_id, _document, clause, _clause_key = entry
        body = _compact_text(clause.get("clause_body")) + _compact_text(clause.get("clause_title"))
        hits = sum(1 for gram in grams if gram in body)
        if hits >= 4:
            scored.append((hits, entry))
    if not scored:
        return []
    scored.sort(key=lambda item: (-item[0], _natural_clause_key(item[1][3])))
    top_hits = scored[0][0]
    if len(scored) > 1 and scored[1][0] >= top_hits * 0.9:
        return []
    return [entry for hits, entry in scored[:3] if hits == top_hits]


def _normalize_excerpt(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[`*#>|]", "", text)
    text = re.sub(r"^\s*第?\s*[A-Z]?(?:\d+\.)+\d+\s*条?\s*", "", text, flags=re.IGNORECASE)
    return _compact_text(text)


def _table_numbers(citation: PowerSpecGraphCitation) -> list[str]:
    text = " ".join(part for part in (citation.clause_no, citation.section, citation.table_no) if part)
    return list(dict.fromkeys(re.findall(r"表\s*(\d+)", text)))


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _build_graph_evidence(
    *,
    file_id: str,
    document_metadata: Mapping[str, Any],
    clause_metadata: Mapping[str, Any],
    hit: PowerSpecGraphHit,
    citation: PowerSpecGraphCitation,
) -> SpecClauseEvidence:
    body = str(clause_metadata.get("clause_body") or "").strip()
    graph_document = dict(document_metadata)
    graph_document["status"] = citation.status or "现行"
    return build_clause_evidence(
        document_id=file_id,
        document_metadata=graph_document,
        clause_metadata=clause_metadata,
        clause_body=body,
        rerank_score=None,
        match_score=None,
        select_relevant=False,
        metadata={
            "qa_graph_hit": True,
            "graph_rank": citation.rank,
            "graph_group": citation.group,
            "graph_matched_group": hit.matched_group,
            "graph_question_id": hit.question_id,
            "graph_matched_variant": hit.matched_variant,
            "graph_match_kind": hit.match_kind,
            "raw_clause_body": body,
            "parsed_clause_body": body,
            "raw_clause_no": str(clause_metadata.get("clause_no") or ""),
            "raw_clause_title": str(clause_metadata.get("clause_title") or ""),
        },
    )


def _guards_ok(query: str, candidate: str) -> bool:
    query_voltage = _voltage_set(query)
    candidate_voltage = _voltage_set(candidate)
    if query_voltage and candidate_voltage and query_voltage != candidate_voltage:
        return False
    query_standards = _standard_set(query)
    candidate_standards = _standard_set(candidate)
    if query_standards and candidate_standards and query_standards != candidate_standards:
        return False
    return _negation_set(query) == _negation_set(candidate)


def _voltage_set(text: str) -> frozenset[str]:
    return frozenset(_normalize_voltage(match.group("value")) for match in _VOLTAGE_PATTERN.finditer(text))


def _normalize_voltage(value: str) -> str:
    try:
        number = float(value)
    except ValueError:
        return value
    return f"{number:g}"


def _standard_set(text: str) -> frozenset[str]:
    return frozenset(_format_standard_match(match) for match in _STANDARD_PATTERN.finditer(text))


def _format_standard_match(match: re.Match[str]) -> str:
    prefix = re.sub(r"\s+", "", match.group("prefix")).upper()
    recommended = (match.group("recommended") or "").upper()
    label = f"{prefix}/{recommended}" if recommended else prefix
    return normalize_standard_no(f"{label}{match.group('number')}-{match.group('year')}")


def _negation_set(text: str) -> frozenset[str]:
    return frozenset(marker for marker in _NEGATION_MARKERS if marker in text)


def _citation_sort_key(
    citation: PowerSpecGraphCitation,
    matched_group: int,
) -> tuple[int, int, tuple[tuple[int, int | str], ...]]:
    group_priority = 0 if matched_group == 0 or citation.group == matched_group else 1
    return (group_priority, citation.rank, _natural_clause_key(clause_identity_key(citation.clause_no)))


def _natural_clause_key(value: str) -> tuple[tuple[int, int | str], ...]:
    parts = re.findall(r"[A-Z]+|\d+", str(value or "").upper())
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def _load_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "是"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
