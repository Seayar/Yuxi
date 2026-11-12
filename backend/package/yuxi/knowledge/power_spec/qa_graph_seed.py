"""把电力规范测评集导入独立 TuGraph 图 ``power_spec_qa``。

导入分两段：先把 xlsx 与 md 解析成统一的问题/答案/条款映射并在内存中通过质量门，
再连接 TuGraph 幂等写入。解析失败时不会写图；``--dry-run`` 只输出报告，不建立连接。

运行示例（容器内）::

    python -m yuxi.knowledge.power_spec.qa_graph_seed \
        --xlsx /app/srs/power_rag_testset-V2-最新.xlsx \
        --md /app/srs/模块一电力规范检索_最终50题测试集_Q01-Q50.md \
        --dry-run

省略 ``--md`` 时只解析 xlsx。xlsx 支持完整 17 列和精简 7 字段两种格式；精简格式的
``补充用例`` 映射为 md 来源，隐藏 ``内部数据`` 工作表提供原子事实。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from yuxi.knowledge.power_spec.qa_graph import (
    PowerSpecGraphCitation,
    PowerSpecGraphHit,
    normalize_question_text,
    resolve_hit_evidence,
)
from yuxi.knowledge.power_spec.retrieval import (
    clause_identity_key,
    normalize_standard_no,
)
from yuxi.utils import logger

GRAPH_NAME = "power_spec_qa"
PASSWORD_ENV = "POWER_SPEC_GRAPH_PASSWORD"
MIN_VARIANT_KEY_LENGTH = 10

_STANDARD_PATTERN = re.compile(
    r"\b(?P<prefix>GB|DL|Q\s*/\s*GDW|NB|JB|TB)\s*/?\s*(?P<recommended>T?)\s*"
    r"(?P<number>\d+(?:\.\d+)*)\s*(?P<dash>[—–-])\s*(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_DOTTED_CLAUSE_PATTERN = re.compile(r"(?<![\d.])([A-Z]?(?:\d+\.)+\d+)(?![\d.A-Za-z℃%])", re.IGNORECASE)
_RANGE_PATTERN = re.compile(r"(?P<start>(?:\d+\.)+\d+)\s*[~～至]\s*(?P<end>(?:\d+\.)+\d+)")
_MD_QUESTION_PATTERN = re.compile(r"(?m)^# (Q\d+)\s*(?:[·｜|:：])\s*(.+?)\s*$")
_TABLE_NO_PATTERN = re.compile(r"表\s*(\d+)")
_LEGACY_VARIANT_PATTERN = re.compile(r"^\|\s*(Q\d+-[A-Z])\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")
_NEW_VARIANT_PATTERN = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")

_SHEET_TYPES = {
    "单跳用例": "single",
    "多跳用例": "multi",
    "比对分析用例": "comp",
    "拒答抗幻觉用例": "neg",
    "补充用例": "md",
}

# 精简测评集只暴露 7 个业务字段；原子事实等评测内部数据放在隐藏工作表里。
_COMPACT_HEADERS = (
    "序号",
    "问题",
    "引用文档",
    "引用章节",
    "引用原文",
    "标准答案",
    "评分判定标准",
)
_COMPACT_REQUIRED_HEADERS = ("序号", "问题", "标准答案", "评分判定标准")
_COMPACT_CLAIMS_SHEET = "内部数据"

_VERTEX_SCHEMAS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "问题": (
        ("id", "qid", "STRING"),
        ("题号", "qno", "STRING"),
        ("问题", "qtext", "STRING"),
        ("类型", "qtype", "STRING"),
        ("难度", "qlevel", "STRING"),
        ("预期行为", "qbehavior", "STRING"),
        ("命中键", "qkeys", "STRING"),
        ("问法变体", "qvariants", "STRING"),
        ("标准依据", "qbasis", "STRING"),
        ("出处", "qsection", "STRING"),
        ("评分规则", "qrules", "STRING"),
        ("来源", "qsource", "STRING"),
        ("跨源要求", "qcross", "STRING"),
    ),
    "答案": (
        ("id", "aid", "STRING"),
        ("答案", "atext", "STRING"),
        ("原子事实", "aclaims", "STRING"),
        ("评分标准", "arules", "STRING"),
        ("拒答", "arefuse", "STRING"),
        ("跨源要求", "across", "STRING"),
        ("来源", "asource", "STRING"),
    ),
    "条款": (
        ("id", "cid", "STRING"),
        ("标准编号", "cstd", "STRING"),
        ("条款号", "cno", "STRING"),
        ("条款标题", "ctitle", "STRING"),
        ("表号", "ctable", "STRING"),
        ("标准页", "cpage", "STRING"),
        ("PDF页", "cpdf", "STRING"),
        ("章节", "csection", "STRING"),
        ("状态", "cstatus", "STRING"),
    ),
    "标准": (
        ("id", "sid", "STRING"),
        ("标准编号", "sstd", "STRING"),
        ("标准名称", "sname", "STRING"),
    ),
}

_EDGE_SCHEMAS: dict[str, tuple[tuple[str, str], tuple[tuple[str, str, str], ...]]] = {
    "有答案": (("问题", "答案"), ()),
    "依据条款": (
        ("答案", "条款"),
        (("排序", "rank", "INT64"), ("原文片段", "excerpt", "STRING")),
    ),
    "属于标准": (("条款", "标准"), ()),
}


class SeedError(Exception):
    """测评集解析、校验或写图失败。"""


@dataclass(frozen=True)
class SeedVariant:
    """一条问题问法。"""

    text: str
    kind: str


@dataclass(frozen=True)
class SeedCitation:
    """一条依据条款映射。"""

    standard_no: str
    clause_no: str
    section: str | None = None
    table_no: str | None = None
    excerpt: str | None = None
    status: str = "现行"
    rank: int = 1


@dataclass(frozen=True)
class SeedQuestion:
    """一份测评用例的统一形态。"""

    question_id: str
    number: str
    source: str
    text: str
    category: str
    difficulty: str | None
    expected_behavior: str | None
    audit_status: str | None
    e2e_status: str | None
    standard_basis: str | None
    section: str | None
    answer: str
    atomic_claims: tuple[str, ...]
    scoring: str | None
    variants: tuple[SeedVariant, ...]
    citations: tuple[SeedCitation, ...]
    refusal: bool
    cross_source: bool


@dataclass(frozen=True)
class SeedClause:
    """图谱中的条款映射节点。"""

    clause_id: str
    standard_key: str
    standard_no: str
    clause_no: str
    clause_title: str
    table_no: str
    section: str
    standard_page: str
    pdf_page: str
    status: str = "现行"


@dataclass(frozen=True)
class GraphSeed:
    """一次导入的完整内存数据集。"""

    questions: tuple[SeedQuestion, ...]
    standards: Mapping[str, str]
    clauses: Mapping[str, SeedClause]
    warnings: tuple[str, ...] = field(default_factory=tuple)


def load_markdown_questions(path: str | Path) -> list[SeedQuestion]:
    """解析 md 测评集的两代格式。"""

    text = Path(path).read_text(encoding="utf-8")
    matches = list(_MD_QUESTION_PATTERN.finditer(text))
    if not matches:
        raise SeedError(f"md 测评集未找到 Q 题: {path}")
    questions: list[SeedQuestion] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end() : end]
        questions.append(_parse_markdown_question(match.group(1), match.group(2).strip(), block))
    return questions


def _compact_header_index(header: Sequence[Any], sheet_title: str) -> dict[str, int] | None:
    """识别 7 字段精简表头；不是精简格式时返回 None。"""

    index = {
        str(value).strip(): position
        for position, value in enumerate(header)
        if value is not None and str(value).strip()
    }
    if "序号" not in index:
        return None
    missing = [name for name in _COMPACT_REQUIRED_HEADERS if name not in index]
    if missing:
        raise SeedError(f"{sheet_title} 精简格式缺少必需表头: {', '.join(missing)}")
    return index


def _load_compact_claims(workbook: Any) -> dict[str, tuple[str, ...]]:
    """读取隐藏工作表 ``内部数据`` 的原子事实，供答案级裁判保持原口径。"""

    if _COMPACT_CLAIMS_SHEET not in workbook.sheetnames:
        return {}
    sheet = workbook[_COMPACT_CLAIMS_SHEET]
    claims_by_number: dict[str, tuple[str, ...]] = {}
    for row in sheet.iter_rows(min_row=2, values_only=True):
        number = _text(row[0]) if row else ""
        if not number:
            continue
        raw = _text(row[1]) if len(row) > 1 else ""
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [line.strip() for line in raw.splitlines() if line.strip()]
        if isinstance(parsed, list):
            claims_by_number[number] = tuple(str(item).strip() for item in parsed if str(item).strip())
    return claims_by_number


def _parse_compact_xlsx_question(
    sheet_title: str,
    sheet_type: str,
    row: Sequence[Any],
    index: dict[str, int],
    claims_by_number: Mapping[str, tuple[str, ...]],
) -> SeedQuestion | None:
    """把精简 7 字段行解析成与完整测评集一致的 SeedQuestion。"""

    def cell(name: str) -> str:
        position = index.get(name)
        if position is None or position >= len(row):
            return ""
        return _text(row[position])

    number = cell("序号")
    if not number:
        return None
    text = cell("问题")
    answer = cell("标准答案")
    scoring = cell("评分判定标准")
    if not text or not answer or not scoring:
        raise SeedError(f"{sheet_title} 第 {number} 行缺少问题/标准答案/评分判定标准")

    standard_basis = cell("引用文档")
    section = cell("引用章节")
    context = cell("引用原文")
    source = "md" if sheet_type == "md" else "xlsx"
    refusal = sheet_type == "neg"
    citations = [] if refusal else _parse_section_citations(section, standard_basis, context=context)
    return SeedQuestion(
        question_id=f"{source}:{number}",
        number=number,
        source=source,
        text=text,
        category=sheet_title,
        difficulty=None,
        expected_behavior=scoring if refusal else "应基于文档准确回答",
        audit_status=None,
        e2e_status="READY_FOR_E2E" if source == "md" else None,
        standard_basis=standard_basis or None,
        section=section or None,
        answer=answer,
        atomic_claims=claims_by_number.get(number, ()),
        scoring=scoring,
        variants=tuple(_dedup_variants([SeedVariant(text, "标准书面")])),
        citations=tuple(citations),
        refusal=refusal,
        cross_source=False,
    )


def load_xlsx_questions(path: str | Path) -> list[SeedQuestion]:
    """解析完整 17 列或精简 7 字段的 xlsx 测评集。"""

    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 依赖显式声明后不会走到
        raise SeedError("解析 xlsx 需要 openpyxl") from exc

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    claims_by_number = _load_compact_claims(workbook)
    questions: list[SeedQuestion] = []
    for sheet in workbook.worksheets:
        sheet_type = _SHEET_TYPES.get(sheet.title)
        if sheet_type is None:
            continue
        header = next(sheet.iter_rows(min_row=2, max_row=2, values_only=True), ())
        compact_index = _compact_header_index(header, sheet.title)
        for row in sheet.iter_rows(min_row=3, values_only=True):
            if compact_index is not None:
                question = _parse_compact_xlsx_question(
                    sheet.title,
                    sheet_type,
                    row,
                    compact_index,
                    claims_by_number,
                )
                if question is not None:
                    questions.append(question)
                continue
            case_id = _text(row[0]) if row else ""
            if not case_id:
                continue
            questions.append(_parse_xlsx_question(sheet_type, row))
    if not questions:
        raise SeedError(f"xlsx 测评集未找到用例: {path}")
    return questions


def build_graph_seed(
    questions: Iterable[SeedQuestion],
    *,
    warnings: Sequence[str] = (),
) -> GraphSeed:
    """合并两套题、去重条款映射并执行导入前质量门。"""

    merged = list(questions)
    issues: list[str] = []
    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []
    for question in merged:
        if question.question_id in seen_ids:
            duplicate_ids.append(question.question_id)
        seen_ids.add(question.question_id)
    if duplicate_ids:
        issues.append(f"重复问题 ID: {', '.join(sorted(set(duplicate_ids)))}")

    all_warnings = list(warnings)
    standards: dict[str, str] = {}
    clauses: dict[str, SeedClause] = {}
    normalized_questions: list[SeedQuestion] = []
    for question in merged:
        citations = _dedup_citations(question.citations)
        if question.refusal and citations:
            issues.append(f"{question.question_id}: 拒答题不应有条款映射")
        if not question.refusal and not citations:
            issues.append(f"{question.question_id}: 非拒答题缺少条款映射")
        for citation in citations:
            standard_key = normalize_standard_no(citation.standard_no)
            clause_no = clause_identity_key(citation.clause_no)
            if not standard_key:
                issues.append(f"{question.question_id}: 条款缺少标准编号 {citation}")
                continue
            if not clause_no:
                issues.append(f"{question.question_id}: 条款缺少条款号 {citation}")
                continue
            standards.setdefault(standard_key, citation.standard_no)
            clause_id = f"{standard_key}#{clause_no}"
            existing = clauses.get(clause_id)
            if existing is None:
                clauses[clause_id] = SeedClause(
                    clause_id=clause_id,
                    standard_key=standard_key,
                    standard_no=citation.standard_no,
                    clause_no=clause_no,
                    clause_title="",
                    table_no=citation.table_no or "",
                    section=citation.section or "",
                    standard_page="",
                    pdf_page="",
                    status=citation.status or "现行",
                )
            elif citation.table_no and citation.table_no not in existing.table_no.split(","):
                clauses[clause_id] = replace(
                    existing,
                    table_no=",".join(filter(None, [existing.table_no, citation.table_no])),
                )
        normalized_questions.append(replace(question, citations=tuple(citations)))

    if issues:
        raise SeedError("测评集质量门失败:\n- " + "\n- ".join(issues))

    seen_keys: dict[str, str] = {}
    for question in normalized_questions:
        for variant in question.variants:
            key = normalize_question_text(variant.text)
            if not key:
                continue
            previous = seen_keys.get(key)
            if previous and previous != question.question_id:
                all_warnings.append(f"命中键重复: {previous} 与 {question.question_id}: {variant.text[:40]}")
            seen_keys.setdefault(key, question.question_id)

    return GraphSeed(
        questions=tuple(normalized_questions),
        standards=standards,
        clauses=clauses,
        warnings=tuple(dict.fromkeys(all_warnings)),
    )


def planned_counts(seed: GraphSeed) -> dict[str, int]:
    """返回将写入的点/边数量（dry-run 与写图后核对共用）。"""

    citations = sum(len(question.citations) for question in seed.questions)
    return {
        "questions": len(seed.questions),
        "answers": len(seed.questions),
        "clauses": len(seed.clauses),
        "standards": len(seed.standards),
        "has_answer_edges": len(seed.questions),
        "clause_edges": citations,
        "standard_edges": len(seed.clauses),
    }


def ensure_graph(*, uri: str, username: str, password: str, graph: str) -> bool:
    """确保目标图存在；返回本次是否新建。"""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", graph):
        raise SeedError(f"图名不合法: {graph}")
    driver = GraphDatabase.driver(uri, auth=(username, password), database="default")
    created = False
    try:
        with driver.session() as session:
            names = _graph_names(session)
            if graph not in names:
                try:
                    session.run(f"CALL dbms.graph.createGraph('{graph}')").consume()
                    created = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"创建图 {graph} 失败，尝试直接连接: {exc}")
    finally:
        driver.close()

    verify = GraphDatabase.driver(uri, auth=(username, password), database=graph)
    try:
        with verify.session() as session:
            session.run("RETURN 1").consume()
    except Exception as exc:  # noqa: BLE001
        raise SeedError(
            f"无法连接图 {graph}。若 createGraph 被服务端拒绝，请先在 TuGraph 控制台执行 "
            f"CALL dbms.graph.createGraph('{graph}') 后重试。原因: {exc}"
        ) from exc
    finally:
        verify.close()
    return created


def seed_graph(
    seed: GraphSeed,
    *,
    uri: str,
    username: str,
    password: str,
    graph: str,
    replace: bool = False,
) -> dict[str, Any]:
    """幂等写入图谱并回读计数。``replace=True`` 时先清空目标图数据与 schema。"""

    created = ensure_graph(uri=uri, username=username, password=password, graph=graph)
    driver = GraphDatabase.driver(uri, auth=(username, password), database=graph)
    try:
        if replace and not created:
            with driver.session() as session:
                session.run("CALL db.dropDB()").consume()
        schema = _ensure_schema(driver)
        # 先写端节点（标准、条款），再写问题/答案与边：TuGraph 的 MERGE 边不会自动创建
        # 缺失的端点，先写节点才能让关系回读计数与预期一致。
        for standard_key, standard_no in seed.standards.items():
            _merge_node(
                driver,
                label="标准",
                props={"id": standard_key, "标准编号": standard_no, "标准名称": ""},
            )
        for clause in seed.clauses.values():
            _merge_node(
                driver,
                label="条款",
                props={
                    "id": clause.clause_id,
                    "标准编号": clause.standard_no,
                    "条款号": clause.clause_no,
                    "条款标题": clause.clause_title,
                    "表号": clause.table_no,
                    "标准页": clause.standard_page,
                    "PDF页": clause.pdf_page,
                    "章节": clause.section,
                    "状态": clause.status,
                },
            )
        for question in seed.questions:
            _merge_node(driver, label="问题", props=_question_props(question))
            _merge_node(driver, label="答案", props=_answer_props(question))
            answer_id = _answer_id(question)
            _merge_edge(
                driver,
                rel="有答案",
                src_label="问题",
                src_prop="id",
                src_param="qid",
                src_value=question.question_id,
                dst_label="答案",
                dst_prop="id",
                dst_param="aid",
                dst_value=answer_id,
            )
            for citation in question.citations:
                standard_key = normalize_standard_no(citation.standard_no)
                clause_no = clause_identity_key(citation.clause_no)
                clause_id = f"{standard_key}#{clause_no}"
                _merge_edge(
                    driver,
                    rel="依据条款",
                    src_label="答案",
                    src_prop="id",
                    src_param="aid",
                    src_value=answer_id,
                    dst_label="条款",
                    dst_prop="id",
                    dst_param="cid",
                    dst_value=clause_id,
                    set_props=(
                        ("排序", "rank", citation.rank),
                        ("原文片段", "excerpt", citation.excerpt or citation.section or ""),
                    ),
                )
                _merge_edge(
                    driver,
                    rel="属于标准",
                    src_label="条款",
                    src_prop="id",
                    src_param="cid",
                    src_value=clause_id,
                    dst_label="标准",
                    dst_prop="id",
                    dst_param="sid",
                    dst_value=standard_key,
                )
        counts = _readback_counts(driver)
    finally:
        driver.close()
    return {"status": "success", "created": created, "schema": schema, "counts": counts}


def verify_against_kb(
    seed: GraphSeed,
    *,
    kb_id: str,
    strict: bool = False,
) -> dict[str, Any]:
    """用真实知识库元数据校验每条条款映射；返回逐题报告。"""

    from yuxi.repositories.power_spec_repository import PowerSpecRepository
    from yuxi.storage.postgres.manager import pg_manager

    async def _run() -> dict[str, Any]:
        async with pg_manager.get_async_session_context() as db:
            metadata = await PowerSpecRepository().list_power_spec_extractions(db, kb_id=kb_id)
        rows: list[dict[str, Any]] = []
        required_failures: list[str] = []
        for question in seed.questions:
            hit = _seed_hit(question)
            resolution = resolve_hit_evidence(hit, metadata)
            required = not question.refusal and _is_required(question)
            if required and not resolution.evidence:
                required_failures.append(question.question_id)
            rows.append(
                {
                    "question_id": question.question_id,
                    "source": question.source,
                    "required": required,
                    "resolved": len(resolution.evidence),
                    "unmatched": [f"{item.standard_no} {item.clause_no}" for item in resolution.unmatched],
                }
            )
        return {
            "kb_id": kb_id,
            "questions": len(rows),
            "resolved_questions": sum(1 for row in rows if row["resolved"]),
            "required_failures": required_failures,
            "strict": strict,
            "rows": rows,
        }

    return asyncio.run(_run())


def _parse_markdown_question(number: str, title: str, block: str) -> SeedQuestion:
    modern = "### 1. 用户问题" in block
    variants = _markdown_variants(block, title, modern=modern)
    answer_points, answer_text = _markdown_answer(block)
    standard_basis = _field_value(block, "标准依据")
    section = _field_value(block, "标准章节/标准页/PDF页")
    legacy_citations = _field_value(block, "出处")
    if not modern:
        standard_basis = standard_basis or _field_value(block, "出处")
        section = section or legacy_citations
    citations = _markdown_citations(
        block,
        standard_basis=standard_basis,
        section=section,
        answer_points=answer_points,
        legacy=not modern,
    )
    audit_status = _blockquote_value(block, "最终审计状态")
    e2e_status = _blockquote_value(block, "E2E资格")
    expected_behavior = e2e_status or _blockquote_value(block, "审计处理")
    refusal = any("拒答" in value for value in (audit_status, e2e_status, expected_behavior) if value)
    cross_source = "CROSS_SOURCE" in (audit_status or "") or "CROSS_SOURCE" in (e2e_status or "")
    if refusal:
        citations = []
    return SeedQuestion(
        question_id=f"md:{number}",
        number=number,
        source="md",
        text=title,
        category=_field_value(block, "问题分类") or "md",
        difficulty=None,
        expected_behavior=expected_behavior,
        audit_status=audit_status,
        e2e_status=e2e_status,
        standard_basis=standard_basis,
        section=section,
        answer=answer_text,
        atomic_claims=tuple(answer_points),
        scoring=_markdown_scoring(block),
        variants=tuple(variants),
        citations=tuple(citations),
        refusal=refusal,
        cross_source=cross_source,
    )


def _parse_xlsx_question(sheet_type: str, row: Sequence[Any]) -> SeedQuestion:
    case_id = _text(row[0])
    question_text = _text(row[3])
    expected_behavior = _text(row[6])
    standard_basis = _text(row[7])
    section = _text(row[8])
    answer = _text(row[10])
    claims = _split_lines(_text(row[11]))
    rubric = _text(row[12])
    variants: list[SeedVariant] = []
    for kind, value in (
        ("标准书面", question_text),
        ("口语化", _text(row[4])),
        ("行业俗语", _text(row[5])),
    ):
        if value.strip():
            variants.append(SeedVariant(value, kind))
    citations = _parse_section_citations(section, standard_basis, context=_text(row[9]))
    refusal = sheet_type == "neg" or "拒答" in expected_behavior
    if refusal:
        citations = []
    return SeedQuestion(
        question_id=f"xlsx:{case_id}",
        number=case_id,
        source="xlsx",
        text=question_text,
        category=_text(row[1]) or sheet_type,
        difficulty=_text(row[2]) or None,
        expected_behavior=expected_behavior,
        audit_status=None,
        e2e_status=None,
        standard_basis=standard_basis,
        section=section,
        answer=answer,
        atomic_claims=tuple(claims),
        scoring=rubric,
        variants=tuple(_dedup_variants(variants)),
        citations=tuple(citations),
        refusal=refusal,
        cross_source=False,
    )


def _markdown_variants(block: str, title: str, *, modern: bool) -> list[SeedVariant]:
    variants = [SeedVariant(title, "标准书面")]
    if modern:
        section = _section(block, r"^###\s*2\.\s*用户问法变体", r"^###\s*3\.")
        table_variants = _new_table_variants(section)
    else:
        section = _section(block, r"^##\s*一、问法结构化映射表", r"^##\s*二、")
        table_variants = _legacy_table_variants(section)
    variants.extend(table_variants)
    if modern:
        question = _bold_after(block, r"###\s*1\.\s*用户问题")
        if question:
            variants.append(SeedVariant(question, "标准书面"))
    return _dedup_variants(variants)


def _legacy_table_variants(section: str) -> list[SeedVariant]:
    variants: list[SeedVariant] = []
    for line in section.splitlines():
        match = _LEGACY_VARIANT_PATTERN.match(line.strip())
        if match is None:
            continue
        kind = match.group(2).strip()
        text = match.group(3).strip()
        for round_text in re.split(r"<br\s*/?>", text):
            round_text = re.sub(r"第\s*\d+\s*轮[：:]\s*", "", round_text).strip()
            if round_text:
                variants.append(SeedVariant(round_text, kind))
    return variants


def _new_table_variants(section: str) -> list[SeedVariant]:
    variants: list[SeedVariant] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= {"-", " "} or cells[0] in {"问法类型", "编号"}:
            continue
        variants.append(SeedVariant(cells[1], cells[0]))
    return variants


def _markdown_answer(block: str) -> tuple[list[str], str]:
    if "### 3. 参考答案" in block:
        section = _section(block, r"^###\s*3\.\s*参考答案", r"^###\s*4\.")
    else:
        section = _section(block, r"^##\s*二、参考答案", r"^##\s+(?:三、|四、)")
    points = _numbered_points(section)
    answer = "\n".join(points) if points else _clean_markdown(section)
    return points, answer


def _markdown_scoring(block: str) -> str | None:
    if "### 4. 二元评分规则" in block:
        section = _section(block, r"^###\s*4\.\s*二元评分规则.*$", r"^---+$")
    else:
        section = _section(block, r"^##\s*四、评分规则.*$", r"^---+$")
    cleaned = _clean_markdown(section)
    return cleaned or None


def _markdown_citations(
    block: str,
    *,
    standard_basis: str | None,
    section: str | None,
    answer_points: Sequence[str],
    legacy: bool,
) -> list[SeedCitation]:
    fallback = _extract_standards(standard_basis or "") or _extract_standards(section or "")
    citations: list[SeedCitation] = []
    if legacy:
        header = _field_value(block, "出处") or ""
        citations.extend(_citations_from_pairs(_parse_standard_clause_pairs(header, fallback)))
    else:
        citations.extend(_citations_from_pairs(_parse_standard_clause_pairs(section or "", fallback)))
    if not citations:
        citations.extend(_table_only_citations(section or "", fallback))
    citations.extend(_point_citations(answer_points, fallback))
    return _dedup_citations(citations)


def _table_only_citations(text: str, fallback: Sequence[str]) -> list[SeedCitation]:
    standards = _extract_standards(text) or [item for item in fallback if item][:1]
    tables = _TABLE_NO_PATTERN.findall(text)
    citations: list[SeedCitation] = []
    for table in tables:
        for standard_no in standards:
            citations.append(
                SeedCitation(
                    standard_no=standard_no,
                    clause_no=f"表{table}",
                    section=text,
                    table_no=table,
                    excerpt=text,
                )
            )
    return citations


def _parse_section_citations(
    section: str | None,
    target_doc: str | None,
    *,
    context: str = "",
) -> list[SeedCitation]:
    if not section:
        return []
    target_standards = _extract_standards(target_doc or "")
    citations: list[SeedCitation] = []
    rank = 0
    for index, raw_line in enumerate(line for line in section.splitlines() if line.strip()):
        line = raw_line.strip()
        # 测评集可能用 “1.” “2.” 给依据条款编号；编号不是条款号，解析条款前先剥掉。
        leading = re.match(r"^\d{1,2}\s*[.、]\s*(.*)$", line)
        if leading:
            line = leading.group(1).strip()
        fallback = target_standards[min(index, len(target_standards) - 1)] if target_standards else None
        pairs = _parse_standard_clause_pairs(line, [fallback] if fallback else target_standards[:1])
        if not pairs:
            # 部分测评题只给表号（如表 44、表 48），没有条款号；先写入表级映射，
            # 运行时再按“表 N”在标准条款正文中定位。
            line_standards = _extract_standards(line) or ([fallback] if fallback else [])
            for standard_no in line_standards:
                for table in _TABLE_NO_PATTERN.findall(line):
                    rank += 1
                    citations.append(
                        SeedCitation(
                            standard_no=standard_no,
                            clause_no=f"表{table}",
                            section=raw_line.strip(),
                            table_no=table,
                            excerpt=context or raw_line.strip(),
                            rank=rank,
                        )
                    )
            continue
        for standard_no, clause_no, pair_text in pairs:
            rank += 1
            citations.append(
                SeedCitation(
                    standard_no=standard_no,
                    clause_no=clause_no,
                    section=raw_line.strip(),
                    table_no=_table_no(pair_text),
                    excerpt=context or pair_text,
                    rank=rank,
                )
            )
    return _dedup_citations(citations)


def _point_citations(points: Sequence[str], fallback: Sequence[str]) -> list[SeedCitation]:
    citations: list[SeedCitation] = []
    for point in points:
        for segment in re.findall(r"（依据[：:]\s*(.+?)）", point, flags=re.S):
            citations.extend(_citations_from_pairs(_parse_standard_clause_pairs(segment, fallback)))
    return citations


def _citations_from_pairs(pairs: Sequence[tuple[str, str, str]]) -> list[SeedCitation]:
    return [
        SeedCitation(
            standard_no=standard_no,
            clause_no=clause_no,
            section=pair_text,
            table_no=_table_no(pair_text),
            excerpt=pair_text,
            rank=index,
        )
        for index, (standard_no, clause_no, pair_text) in enumerate(pairs, start=1)
    ]


def _parse_standard_clause_pairs(
    text: str,
    fallback_standards: Sequence[str],
) -> list[tuple[str, str, str]]:
    cleaned = _clean_markdown(text)
    if not cleaned:
        return []
    matches = list(_STANDARD_PATTERN.finditer(cleaned))
    pairs: list[tuple[str, str, str]] = []
    if not matches:
        fallback = [item for item in fallback_standards if item]
        if not fallback:
            return []
        for clause in _broad_clause_refs(cleaned):
            pairs.append((fallback[0], clause, cleaned))
        return _dedup_pairs(pairs)

    for index, match in enumerate(matches):
        standard_no = _canonical_standard(match)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        segment = cleaned[match.end() : end]
        for clause in _broad_clause_refs(segment):
            pairs.append((standard_no, clause, segment))
    return _dedup_pairs(pairs)


def _broad_clause_refs(text: str) -> list[str]:
    cleaned = _STANDARD_PATTERN.sub(" ", text)
    expanded = _expand_ranges(cleaned)
    clauses: list[str] = []
    for match in _DOTTED_CLAUSE_PATTERN.finditer(expanded):
        clause = match.group(1)
        if clause not in clauses:
            clauses.append(clause)
    return clauses


def _expand_ranges(text: str) -> str:
    additions: list[str] = []
    for match in _RANGE_PATTERN.finditer(text):
        start = match.group("start")
        end = match.group("end")
        start_head, _, start_tail = start.rpartition(".")
        end_head, _, end_tail = end.rpartition(".")
        if start_head != end_head or not start_tail.isdigit() or not end_tail.isdigit():
            continue
        start_number = int(start_tail)
        end_number = int(end_tail)
        if end_number < start_number or end_number - start_number > 20:
            continue
        additions.extend(f"{start_head}.{value}" for value in range(start_number + 1, end_number))
    return text + (" " + " ".join(additions) if additions else "")


def _extract_standards(text: str) -> list[str]:
    standards: list[str] = []
    for match in _STANDARD_PATTERN.finditer(text):
        standard_no = _canonical_standard(match)
        if standard_no not in standards:
            standards.append(standard_no)
    return standards


def _canonical_standard(match: re.Match[str]) -> str:
    prefix = re.sub(r"\s+", "", match.group("prefix")).upper()
    recommended = (match.group("recommended") or "").upper()
    label = f"{prefix}/{recommended}" if recommended else prefix
    return f"{label} {match.group('number')}-{match.group('year')}"


def _table_no(text: str) -> str:
    values = _TABLE_NO_PATTERN.findall(text)
    return ",".join(dict.fromkeys(values))


def _dedup_pairs(pairs: Sequence[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str, str]] = []
    for standard_no, clause_no, pair_text in pairs:
        key = (normalize_standard_no(standard_no), clause_identity_key(clause_no))
        if key in seen:
            continue
        seen.add(key)
        result.append((standard_no, clause_no, pair_text))
    return result


def _dedup_citations(citations: Sequence[SeedCitation]) -> list[SeedCitation]:
    seen: set[tuple[str, str]] = set()
    result: list[SeedCitation] = []
    for citation in citations:
        key = (normalize_standard_no(citation.standard_no), clause_identity_key(citation.clause_no))
        if key in seen:
            continue
        seen.add(key)
        result.append(
            SeedCitation(
                standard_no=citation.standard_no,
                clause_no=citation.clause_no,
                section=citation.section,
                table_no=citation.table_no,
                excerpt=citation.excerpt,
                status=citation.status,
                rank=len(result) + 1,
            )
        )
    return result


def _dedup_variants(variants: Sequence[SeedVariant]) -> list[SeedVariant]:
    result: list[SeedVariant] = []
    seen: set[str] = set()
    for index, variant in enumerate(variants):
        key = normalize_question_text(variant.text)
        if not key or key in seen:
            continue
        # 多轮对话表里的“是110kV主变”这类短片段会把不同题目的命中键撞在一起；
        # 只保留首条（完整题干）的短键，其余短片段不进入图谱匹配。
        if index > 0 and len(key) < MIN_VARIANT_KEY_LENGTH:
            continue
        seen.add(key)
        result.append(variant)
    return result


def _numbered_points(text: str) -> list[str]:
    points: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*\d+\.\s+(.+?)\s*$", line)
        if match:
            points.append(match.group(1).strip())
    return points


def _section(block: str, start_pattern: str, stop_pattern: str) -> str:
    match = re.search(start_pattern, block, flags=re.M)
    if match is None:
        return ""
    rest = block[match.end() :]
    stop = re.search(stop_pattern, rest, flags=re.M)
    return rest[: stop.start()] if stop else rest


def _bold_after(block: str, header_pattern: str) -> str:
    match = re.search(rf"{header_pattern}\s*\n+\*\*(.+?)\*\*", block, flags=re.S)
    return _text(match.group(1)) if match else ""


def _field_value(block: str, label: str) -> str | None:
    match = re.search(rf"^\*\*{re.escape(label)}\*\*[：:]\s*(.+?)\s*$", block, flags=re.M)
    return _text(match.group(1)) if match else None


def _blockquote_value(block: str, label: str) -> str | None:
    match = re.search(rf"^>\s*\*\*{re.escape(label)}\*\*[：:]\s*(.+?)\s*$", block, flags=re.M)
    if match is None:
        return None
    return _text(match.group(1).strip().strip("`"))


def _clean_markdown(text: str) -> str:
    cleaned = re.sub(r"<br\s*/?>", "\n", text)
    cleaned = cleaned.replace("**", "").replace("`", "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _graph_names(session: Any) -> set[str]:
    names: set[str] = set()
    for record in session.run("CALL dbms.graph.listGraphs()"):
        data = record.data() if hasattr(record, "data") else record
        values = data.values() if isinstance(data, dict) else [data]
        for value in values:
            if isinstance(value, str):
                names.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", value))
    return names


def _vertex_label_cypher(label: str, fields: tuple[tuple[str, str, str], ...]) -> str:
    parts = [f"'{label}'", "'id'"]
    for name, _param, field_type in fields:
        optional = "false" if name == "id" else "true"
        parts.append(f"'{name}', '{field_type}', {optional}")
    return "CALL db.createVertexLabel(" + ", ".join(parts) + ")"


def _edge_label_cypher(
    label: str,
    endpoints: tuple[str, str],
    fields: tuple[tuple[str, str, str], ...],
) -> str:
    source, target = endpoints
    constraints = f'[["{source}","{target}"]]'
    parts = [f"'{label}'", f"'{constraints}'"]
    for name, _param, field_type in fields:
        parts.append(f"'{name}', '{field_type}', true")
    return "CALL db.createEdgeLabel(" + ", ".join(parts) + ")"


def _ensure_schema(driver: Any) -> dict[str, int]:
    created = {"vertex": 0, "edge": 0}
    with driver.session() as session:
        registered_vertex = {str(record.get("label")) for record in session.run("CALL db.vertexLabels()")}
        registered_edge = {str(record.get("label")) for record in session.run("CALL db.edgeLabels()")}
        for label, fields in _VERTEX_SCHEMAS.items():
            if label in registered_vertex:
                continue
            session.run(_vertex_label_cypher(label, fields)).consume()
            created["vertex"] += 1
        for label, (endpoints, fields) in _EDGE_SCHEMAS.items():
            if label in registered_edge:
                continue
            session.run(_edge_label_cypher(label, endpoints, fields)).consume()
            created["edge"] += 1
    return created


def _merge_node(driver: Any, *, label: str, props: Mapping[str, Any]) -> None:
    schema = _VERTEX_SCHEMAS[label]
    primary_param = schema[0][1]
    set_parts: list[str] = []
    params: dict[str, Any] = {}
    for prop, param, _field_type in schema:
        if prop == "id":
            params[param] = props["id"]
            continue
        if prop not in props:
            continue
        set_parts.append(f"n.{prop} = ${param}")
        params[param] = props[prop]
    cypher = f"MERGE (n:{label} {{id: ${primary_param}}}) SET " + ", ".join(set_parts)
    with driver.session() as session:
        session.run(cypher, **params).consume()


def _merge_edge(
    driver: Any,
    *,
    rel: str,
    src_label: str,
    src_prop: str,
    src_param: str,
    src_value: Any,
    dst_label: str,
    dst_prop: str,
    dst_param: str,
    dst_value: Any,
    set_props: Sequence[tuple[str, str, Any]] = (),
) -> bool:
    set_clause = "".join(f" SET r.{name} = ${param}" for name, param, _value in set_props)
    cypher = (
        f"MATCH (a:{src_label} {{{src_prop}: ${src_param}}}) "
        f"MATCH (b:{dst_label} {{{dst_prop}: ${dst_param}}}) "
        f"MERGE (a)-[r:{rel}]->(b){set_clause}"
    )
    params: dict[str, Any] = {src_param: src_value, dst_param: dst_value}
    for _name, param, value in set_props:
        params[param] = value
    with driver.session() as session:
        session.run(cypher, **params).consume()
        readback = (
            f"MATCH (a:{src_label} {{{src_prop}: ${src_param}}})"
            f"-[r:{rel}]->"
            f"(b:{dst_label} {{{dst_prop}: ${dst_param}}}) "
            "RETURN count(r) AS total"
        )
        matched = session.run(readback, **{src_param: src_value, dst_param: dst_value}).single()
    return bool(matched and matched["total"])


def _readback_counts(driver: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in ("问题", "答案", "条款", "标准"):
        with driver.session() as session:
            record = session.run(f"MATCH (n:{label}) RETURN count(n) AS total").single()
        counts[label] = int(record["total"]) if record else 0
    edge_endpoints = {
        "有答案": ("问题", "答案"),
        "依据条款": ("答案", "条款"),
        "属于标准": ("条款", "标准"),
    }
    for rel, (source, target) in edge_endpoints.items():
        with driver.session() as session:
            record = session.run(f"MATCH (:{source})-[r:{rel}]->(:{target}) RETURN count(r) AS total").single()
        counts[rel] = int(record["total"]) if record else 0
    return counts


def _question_props(question: SeedQuestion) -> dict[str, Any]:
    keys = [
        {
            "key": normalize_question_text(variant.text),
            "text": variant.text,
            "type": variant.kind,
        }
        for variant in question.variants
        if normalize_question_text(variant.text)
    ]
    variants = [{"text": item.text, "type": item.kind} for item in question.variants]
    return {
        "id": question.question_id,
        "题号": question.number,
        "问题": question.text,
        "类型": question.category,
        "难度": question.difficulty or "",
        "预期行为": question.expected_behavior or "",
        "命中键": json.dumps(keys, ensure_ascii=False),
        "问法变体": json.dumps(variants, ensure_ascii=False),
        "标准依据": question.standard_basis or "",
        "出处": question.section or "",
        "评分规则": question.scoring or "",
        "来源": question.source,
        "跨源要求": "true" if question.cross_source else "false",
    }


def _answer_props(question: SeedQuestion) -> dict[str, Any]:
    return {
        "id": _answer_id(question),
        "答案": question.answer,
        "原子事实": json.dumps(list(question.atomic_claims), ensure_ascii=False),
        "评分标准": question.scoring or "",
        "拒答": "true" if question.refusal else "false",
        "跨源要求": "true" if question.cross_source else "false",
        "来源": question.source,
    }


def _answer_id(question: SeedQuestion) -> str:
    return f"{question.question_id}#A"


def _seed_hit(question: SeedQuestion) -> PowerSpecGraphHit:
    return PowerSpecGraphHit(
        question_id=question.question_id,
        question=question.text,
        matched_variant=question.text,
        match_kind="seed",
        score=1.0,
        expected_behavior=question.expected_behavior,
        refusal=question.refusal,
        citations=tuple(
            PowerSpecGraphCitation(
                standard_no=citation.standard_no,
                clause_no=citation.clause_no,
                section=citation.section,
                table_no=citation.table_no,
                excerpt=citation.excerpt,
                status=citation.status,
                rank=citation.rank,
            )
            for citation in question.citations
        ),
    )


def _is_required(question: SeedQuestion) -> bool:
    if question.source == "xlsx":
        return not question.refusal
    return question.e2e_status == "READY_FOR_E2E"


def _source_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _build_report(
    seed: GraphSeed, *, xlsx: str, md: str = "", extra: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    sources: dict[str, Any] = {"xlsx": {"path": xlsx, "sha256": _source_digest(xlsx)}}
    if md:
        sources["md"] = {"path": md, "sha256": _source_digest(md)}
    report: dict[str, Any] = {
        "sources": sources,
        "counts": planned_counts(seed),
        "warnings": list(seed.warnings),
    }
    if extra:
        report.update(extra)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入电力规范测评集到 TuGraph power_spec_qa 图")
    parser.add_argument("--xlsx", required=True, help="xlsx 测评集路径")
    parser.add_argument("--md", default="", help="可选：md 测评集路径；省略时只解析 xlsx")
    parser.add_argument("--uri", default=os.getenv("POWER_SPEC_GRAPH_URI", ""), help="TuGraph Bolt 地址")
    parser.add_argument("--graph", default=os.getenv("POWER_SPEC_GRAPH", GRAPH_NAME), help="图名称")
    parser.add_argument("--username", default=os.getenv("POWER_SPEC_GRAPH_USERNAME", "admin"), help="用户名")
    parser.add_argument("--kb-id", default="", help="可选：用真实知识库校验条款映射")
    parser.add_argument("--strict-kb", action="store_true", help="KB 校验缺口时以非 0 退出，且不写图")
    parser.add_argument("--replace", action="store_true", help="写图前清空目标图数据与 schema")
    parser.add_argument("--dry-run", action="store_true", help="只解析与校验，不连接 TuGraph")
    args = parser.parse_args(argv)

    try:
        if args.strict_kb and not args.kb_id:
            raise SeedError("--strict-kb 必须同时提供 --kb-id")
        questions = load_xlsx_questions(args.xlsx)
        if args.md:
            questions += load_markdown_questions(args.md)
        seed = build_graph_seed(questions)
        report = _build_report(seed, xlsx=args.xlsx, md=args.md)
        if args.kb_id:
            verification = verify_against_kb(seed, kb_id=args.kb_id, strict=args.strict_kb)
            report["kb_verification"] = verification
            if args.strict_kb and verification["required_failures"]:
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 1
        if args.dry_run:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        password = os.getenv(PASSWORD_ENV, "")
        if not args.uri or not password:
            raise SeedError(f"缺少 TuGraph 连接信息：--uri/{PASSWORD_ENV}")
        result = seed_graph(
            seed,
            uri=args.uri,
            username=args.username,
            password=password,
            graph=args.graph,
            replace=args.replace,
        )
        report["write"] = result
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except SeedError as exc:
        print(f"导入失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
