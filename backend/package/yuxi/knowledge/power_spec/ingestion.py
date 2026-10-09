from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from yuxi.repositories.power_spec_repository import PowerSpecRepository


# ---------------------------------------------------------------------------
# 条款解析（原 parser.py）
# ---------------------------------------------------------------------------

_STANDARD_PATTERN = re.compile(
    r"\b(?P<base>(?:GB|DL|Q/GDW|Q\s*/\s*GDW|NB|JB|TB)\s*/?\s*T?\s*[-/]?\s*\d+)\s*(?P<dash>[—–-])\s*(?P<year>\d{4})\b",
    re.IGNORECASE,
)
# 人工整理的标准文档把条款标题写成 Markdown 标题，如 `## 6.1 油浸式电力变压器和电抗器`。
# 部分文档的条款标题只有编号（`## 6.4.1.1`），标题写在下一级标题或正文里；
# 单级编号仍需自带标题，否则 `## 6` 这类章节标题会把整章切成一条条款。
_CLAUSE_HEADING_PATTERN = re.compile(r"^#{1,6}\s+(?P<number>\d+(?:\.\d+)*)(?:\s+(?P<title>.+?))?\s*$")
_HEADING_PATTERN = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>\S.*?)\s*$")
_TITLE_HEADING_PATTERN = re.compile(r"^#\s+(?P<text>\S.*?)\s*$")
_TOC_HEADING_PATTERN = re.compile(r"^#{1,6}\s*(?:目\s*次|目\s*录|contents)\s*$", re.IGNORECASE)
_PAGE_MARKER_PATTERN = re.compile(r"^<!--\s*source_pdf_page:\s*(?P<page>\d+)\s*-->$")
# PDF 转 Markdown 时常见的旧式条款标题（无 Markdown 前缀）。
_CLAUSE_PATTERN = re.compile(r"^\s*(?P<number>(?:[1-9]\d*\.)+\d+)\s*(?P<title>[^。；;\n]*)\s*$")
_SCOPE_PATTERN = re.compile(r"^\s*(?:适用范围|范围)\s*[:：]\s*(?P<scope>.+?)\s*$")
_REPLACED_STANDARD_NAME_PATTERN = re.compile(r"代替\s*[A-Za-z／/\s]*\d+\s*[—–-]\s*\d{4}\s*《(?P<name>[^》]+)》")
_PUBLISH_YEAR_PATTERN = re.compile(r"(?P<year>(?:19|20)\d{2})\s*[-年]\s*\d{1,2}\s*[-月]\s*\d{1,2}\s*日?\s*发布")
# 这些词出现在编号之前时，该编号属于被代替的标准，不是本文件编号。
_SUPERSEDED_MARKERS = ("代替", "被代替", "作废", "废止")
_OCR_CELL_PATTERN = re.compile(r"</?(?:fcel|lcel|ecel|ucel|xcel|tcel|bcel|rcel)>")
_OCR_LINE_BREAK_PATTERN = re.compile(r"<nl\s*/?>")
_OCR_TABLE_GAP_PATTERN = re.compile(r"(?:\s*\|\s*){2,}")
_FORMAT_TRANSLATION = str.maketrans({"／": "/", "∕": "/", "＋": "+", "–": "—"})
_PAGE_HEADER_PATTERN = re.compile(r"中华人民共和国|^(?:国家|行业|地方|团体)标准$")


class PowerSpecClause(BaseModel):
    """表示从规范文本中提取的一条条款。"""

    model_config = ConfigDict(frozen=True)

    clause_no: str = Field(min_length=1)
    clause_title: str = Field(min_length=1)
    clause_body: str = Field(min_length=1)
    scope: str | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_page_ref: str | None = None


class PowerSpecDocument(BaseModel):
    """表示一份完成基础条款提取的电力规范文档。"""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    standard_name: str = Field(min_length=1)
    standard_no: str = Field(min_length=1)
    version: str | None = None
    status: str = Field(min_length=1)
    replaced_by: str | None = None
    source_file: str | None = None
    scope: str | None = None
    clauses: list[PowerSpecClause] = Field(min_length=1)


def parse_power_spec_text(
    text: str,
    *,
    document_id: str,
    source_file: str | None = None,
    standard_name: str | None = None,
) -> PowerSpecDocument:
    """从规范纯文本中提取标准信息和数字条款。"""

    normalized_text = text.translate(_FORMAT_TRANSLATION)
    normalized_text = normalized_text.replace("+", " ").replace("\r\n", "\n").replace("\r", "\n")
    normalized_text = _normalize_ocr_table_markers(normalized_text).strip()
    if not normalized_text:
        raise ValueError("规范文本不能为空")

    standard_no, version = _resolve_standard_number(normalized_text)
    lines = [line.strip() for line in normalized_text.splitlines()]
    document_scope = _find_scope(lines)
    resolved_name = standard_name or _derive_standard_name(normalized_text, lines, standard_no)
    status = "废止" if re.search(r"(?:废止|作废|失效)", normalized_text) else "现行"
    # 人工整理的标准文档按条款标题切分；PDF 直出的旧式文本回退到行首编号规则。
    # 注意这是「标题优先、行号兜底」的二选一：同一文档混排（部分 Markdown 标题 +
    # 部分行首编号）时只产出标题类条款，行号类条目被丢弃；目录块 skipped 只抑制
    # 标题识别，若目录块夹在两条条款标题之间会漏进前一条正文。边界场景，按现状取舍。
    clauses = _extract_clause_headings(lines, document_scope) or _extract_clauses(lines, document_scope)

    if not clauses:
        raise ValueError("未识别到数字条款")

    return PowerSpecDocument(
        document_id=document_id,
        standard_name=resolved_name,
        standard_no=standard_no,
        version=version,
        status=status,
        source_file=source_file,
        scope=document_scope,
        clauses=clauses,
    )


def _normalize_ocr_table_markers(text: str) -> str:
    """把 OCR 表格单元格标记换成可读分隔，避免标记泄漏进条款正文。"""

    text = _OCR_LINE_BREAK_PATTERN.sub("\n", text)
    text = _OCR_CELL_PATTERN.sub(" | ", text)
    return _OCR_TABLE_GAP_PATTERN.sub(" | ", text)


def _find_scope(lines: list[str]) -> str | None:
    for line in lines:
        match = _SCOPE_PATTERN.match(line)
        if match:
            return match.group("scope").strip()
        if line.startswith(("本标准适用于", "本文件适用于")):
            return line
    return None


def _resolve_standard_number(text: str) -> tuple[str, str]:
    """推导本文件的标准编号与年份。

    标准封面经常只写被代替的编号（`代替 DL/T 393—2010`），直接采用会把卡片上的编号
    写成上一版；这种情况用发布日期年份作为本版年份。
    """

    matches = list(_STANDARD_PATTERN.finditer(text))
    if not matches:
        raise ValueError("未识别到规范编号")
    for match in matches:
        prefix = text[max(0, match.start() - 8) : match.start()]
        if not any(marker in prefix for marker in _SUPERSEDED_MARKERS):
            return _format_standard_no(match), match.group("year")
    published = _PUBLISH_YEAR_PATTERN.search(text)
    year = published.group("year") if published else matches[0].group("year")
    return _format_standard_no(matches[0], year=year), year


def _format_standard_no(match: re.Match[str], *, year: str | None = None) -> str:
    """拼接标准编号，只统一短横线写法，保留原文用的是连字符还是破折号。"""

    base = re.sub(r"\s+", "", match.group("base"))
    dash = "—" if match.group("dash") == "–" else match.group("dash")
    return f"{base}{dash}{year or match.group('year')}"


def _derive_standard_name(text: str, lines: list[str], fallback: str) -> str:
    """按标准文档的写法推导规范名称：优先「代替…《名称》」，其次文档标题。"""

    replaced = _REPLACED_STANDARD_NAME_PATTERN.search(text)
    if replaced:
        candidate = replaced.group("name").strip()
        if _is_standard_name_candidate(candidate):
            return candidate
    for line in lines:
        heading = _TITLE_HEADING_PATTERN.match(line)
        if heading is None:
            continue
        candidate = heading.group("text").strip()
        if _PAGE_HEADER_PATTERN.search(candidate):
            continue
        if _is_standard_name_candidate(candidate):
            return candidate
    return _standard_name_from_number_line(lines, fallback)


def _standard_name_from_number_line(lines: list[str], fallback: str) -> str:
    """从含标准编号的行中提取名称，跳过只剩页码、表格标记的候选行。

    没有可用名称时退回编号原文，不拿 OCR 标记或表格碎片充当名称。
    """

    raw_number: str | None = None
    for line in lines:
        match = _STANDARD_PATTERN.search(line)
        if match is None:
            continue
        if raw_number is None:
            raw_number = match.group(0).strip()
        candidate = _OCR_CELL_PATTERN.sub("", line[: match.start()] + line[match.end() :]).strip(" ：:—-|")
        if _is_standard_name_candidate(candidate):
            return candidate
    return raw_number or fallback


def _is_standard_name_candidate(candidate: str) -> bool:
    """判断候选是否像规范名称，而不是页码或整行表格内容。"""

    if len(candidate) > 40 or "|" in candidate:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]{2,}", candidate)) or bool(re.search(r"[A-Za-z]{3,}", candidate))


def _extract_clause_headings(lines: list[str], document_scope: str | None) -> list[PowerSpecClause]:
    """按 Markdown 条款标题切分标准正文；没有这种标题时返回空列表。

    标准文档的条款标题形如 `## 6.1 油浸式电力变压器和电抗器`，目录里同一条款不带
    Markdown 前缀；只把标题行当条款边界，正文就不会跨章节累积成巨块。部分文档的条款
    标题只写编号，标题落在紧邻的下一级标题里，此时按下一级标题取标题。
    """

    skipped = _toc_line_indexes(lines)
    headings: list[tuple[int, int, str, str]] = []
    for index, line in enumerate(lines):
        if index in skipped:
            continue
        match = _CLAUSE_HEADING_PATTERN.match(line)
        if match is None:
            continue
        number = match.group("number")
        title = (match.group("title") or "").strip(" ：:、")
        body_start = index + 1
        if not title:
            if "." not in number:
                # `## 6` 这类单级标题不带标题时与章节无法区分，仍按章节处理。
                continue
            title, body_start = _title_from_subheading(lines, index, skipped)
        if _is_numeric_table_line(title):
            continue
        headings.append((index, body_start, number, title))
    if not headings:
        return []

    pages = _page_markers(lines)
    clauses: list[PowerSpecClause] = []
    for order, (index, body_start, number, title) in enumerate(headings):
        end = headings[order + 1][0] if order + 1 < len(headings) else len(lines)
        body = _clause_body(lines, body_start, end)
        if not body:
            # 只有标题、没有自身正文的章节（如仅用于分层的 6.1）不构成证据。
            continue
        clauses.append(
            PowerSpecClause(
                clause_no=number,
                clause_title=title or number,
                clause_body=body,
                scope=document_scope,
                source_page=_page_before(pages, index),
            )
        )
    return clauses


def _title_from_subheading(lines: list[str], heading_index: int, skipped: set[int]) -> tuple[str, int]:
    """纯编号条款标题没有自带标题时，取紧邻的下一级标题作为条款标题。

    返回（标题, 正文起始行）；只有下一行是更深层级的、本身不是条款标题的标题才取值，
    避免把正文段落或子条款编号当成标题。
    """

    body_start = heading_index + 1
    level = len(lines[heading_index]) - len(lines[heading_index].lstrip("#"))
    for index in range(body_start, len(lines)):
        if index in skipped or not lines[index].strip():
            continue
        match = _HEADING_PATTERN.match(lines[index])
        if match is None or len(match.group("hashes")) <= level:
            return "", body_start
        if _CLAUSE_HEADING_PATTERN.match(lines[index]) is not None:
            return "", body_start
        return match.group("text").strip(" ：:、"), index + 1
    return "", body_start


def _toc_line_indexes(lines: list[str]) -> set[int]:
    """返回目录区块的行号；从目录标题到同级或更高级的 Markdown 标题都算目录。"""

    skipped: set[int] = set()
    index = 0
    while index < len(lines):
        match = _TOC_HEADING_PATTERN.match(lines[index])
        if match is None:
            index += 1
            continue
        level = len(match.group(0)) - len(match.group(0).lstrip("#"))
        skipped.add(index)
        index += 1
        while index < len(lines):
            heading = _HEADING_PATTERN.match(lines[index])
            if heading is not None and len(heading.group("hashes")) <= level:
                break
            skipped.add(index)
            index += 1
    return skipped


def _page_markers(lines: list[str]) -> list[tuple[int, int]]:
    """返回 (行号, 源 PDF 页码) 列表。"""

    markers: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = _PAGE_MARKER_PATTERN.match(line)
        if match:
            markers.append((index, int(match.group("page"))))
    return markers


def _page_before(markers: list[tuple[int, int]], index: int) -> int | None:
    """返回标题之前最近一个页码标记的页码。"""

    page = None
    for marker_index, marker_page in markers:
        if marker_index >= index:
            break
        page = marker_page
    return page


def _clause_body(lines: list[str], start: int, end: int) -> str:
    """拼接条款正文，去掉页码标记这类非正文行。"""

    body_lines = [line for line in lines[start:end] if not _PAGE_MARKER_PATTERN.match(line)]
    return _collapse_blank_lines("\n".join(body_lines)).strip()


def _extract_clauses(lines: list[str], document_scope: str | None) -> list[PowerSpecClause]:
    clauses: list[PowerSpecClause] = []
    current_number: str | None = None
    current_title = ""
    body_lines: list[str] = []
    current_scope = document_scope

    def flush_clause() -> None:
        if current_number is None:
            return
        body = _collapse_blank_lines("\n".join(body_lines)).strip()
        if len(body) < 20 and current_title == current_number:
            return
        if body:
            clauses.append(
                PowerSpecClause(
                    clause_no=current_number,
                    clause_title=current_title or current_number,
                    clause_body=body,
                    scope=current_scope,
                )
            )

    for line in lines:
        match = _CLAUSE_PATTERN.match(line)
        if match:
            candidate_title = match.group("title").strip(" ：:、")
            if _is_numeric_table_line(candidate_title):
                if current_number is not None and line:
                    body_lines.append(line)
                continue
            flush_clause()
            current_number = match.group("number")
            current_title = candidate_title or current_number
            body_lines = []
            current_scope = document_scope
            continue
        if current_number is None:
            continue
        scope_match = _SCOPE_PATTERN.match(line)
        if scope_match:
            current_scope = scope_match.group("scope").strip()
            continue
        if line:
            body_lines.append(line)
        elif body_lines:
            body_lines.append("")

    flush_clause()
    return clauses


def _collapse_blank_lines(text: str) -> str:
    """压缩连续空行，保留段落与表格之间的单个空行。"""

    return re.sub(r"\n{3,}", "\n\n", text)


def _is_numeric_table_line(title: str) -> bool:
    """过滤 OCR 表格中的数值行，避免其伪装成章节。"""

    normalized = title.strip()
    return bool(normalized) and bool(re.fullmatch(r"[\d./%~—+±-]+", normalized))


# ---------------------------------------------------------------------------
# 派生元数据（原 ingestion.py）
# ---------------------------------------------------------------------------

POWER_SPEC_EXTRACTION_SCHEMA = "power_spec.v1"


def build_document_extraction(document: PowerSpecDocument) -> dict[str, Any]:
    """构建可写入知识库文件派生字段的规范文档元数据。"""

    return {
        "schema": POWER_SPEC_EXTRACTION_SCHEMA,
        "document_id": document.document_id,
        "standard_name": document.standard_name,
        "standard_no": document.standard_no,
        "version": document.version,
        "status": document.status,
        "replaced_by": document.replaced_by,
        "source_file": document.source_file,
        "scope": document.scope,
        "clause_count": len(document.clauses),
    }


def build_clause_extraction(document: PowerSpecDocument, clause: PowerSpecClause) -> dict[str, Any]:
    """构建可写入知识 Chunk extraction_result 的条款元数据。"""

    if clause not in document.clauses:
        raise ValueError(f"条款 {clause.clause_no} 不属于文档 {document.document_id}")

    return {
        "schema": POWER_SPEC_EXTRACTION_SCHEMA,
        "document_id": document.document_id,
        "standard_name": document.standard_name,
        "standard_no": document.standard_no,
        "version": document.version,
        "status": document.status,
        "replaced_by": document.replaced_by,
        "clause_no": clause.clause_no,
        "clause_title": clause.clause_title,
        "clause_body": clause.clause_body,
        "scope": clause.scope or document.scope,
        "source_page": clause.source_page,
        "source_page_ref": clause.source_page_ref,
    }


class PostgresPowerSpecExtractionStore:
    """将领域流水线连接到调用方显式提供的 PostgreSQL 会话。"""

    def __init__(self, db: AsyncSession, repository: PowerSpecRepository | None = None):
        self._db = db
        if repository is None:
            from yuxi.repositories.power_spec_repository import PowerSpecRepository

            repository = PowerSpecRepository()
        self._repository = repository

    async def save_power_spec_extraction(
        self,
        *,
        kb_id: str,
        file_id: str,
        document: PowerSpecDocument,
        metadata: dict,
    ) -> None:
        """在当前事务中保存电力规范派生结果。"""

        await self._repository.save_power_spec_extraction(
            self._db,
            kb_id=kb_id,
            file_id=file_id,
            document=document,
            metadata=metadata,
        )


class PowerSpecRebuildFile(BaseModel):
    """描述待重建的知识库文件。"""

    model_config = ConfigDict(frozen=True)

    kb_id: str = Field(min_length=1)
    file_id: str = Field(min_length=1)
    source_file: str | None = None


class PowerSpecRebuildItem(BaseModel):
    """记录单个文件的重建结果。"""

    model_config = ConfigDict(frozen=True)

    file_id: str
    source_file: str | None = None
    status: str
    clause_count: int = 0
    reason: str | None = None


class PowerSpecRebuildReport(BaseModel):
    """表示一次 dry-run 或提交重建的汇总结果。"""

    model_config = ConfigDict(frozen=True)

    dry_run: bool
    items: list[PowerSpecRebuildItem] = Field(default_factory=list)

    @property
    def accepted(self) -> list[PowerSpecRebuildItem]:
        return [item for item in self.items if item.status == "accepted"]

    @property
    def rejected(self) -> list[PowerSpecRebuildItem]:
        return [item for item in self.items if item.status == "rejected"]


class PowerSpecRebuildStore(Protocol):
    """定义规范重建结果的持久化边界。"""

    async def save_power_spec_extraction(
        self,
        *,
        kb_id: str,
        file_id: str,
        document: PowerSpecDocument,
        metadata: dict[str, Any],
    ) -> None:
        """保存通过质量门的规范文档。"""


PowerSpecTextSource = Callable[[PowerSpecRebuildFile], Awaitable[str]]


class PowerSpecRebuildService:
    """执行不依赖对话请求的规范文件重建。"""

    def __init__(self, store: PowerSpecRebuildStore):
        self._store = store

    async def rebuild(
        self,
        files: Sequence[PowerSpecRebuildFile],
        *,
        source: PowerSpecTextSource,
        dry_run: bool = True,
    ) -> PowerSpecRebuildReport:
        """解析全部文件；dry-run 只报告，正式模式只写入通过质量门的文件。"""

        items: list[PowerSpecRebuildItem] = []
        for file in files:
            try:
                text = await source(file)
                document = parse_power_spec_text(
                    text,
                    document_id=file.file_id,
                    source_file=file.source_file,
                )
                reason = _quality_failure_reason(document)
                if reason:
                    items.append(
                        PowerSpecRebuildItem(
                            file_id=file.file_id,
                            source_file=file.source_file,
                            status="rejected",
                            clause_count=len(document.clauses),
                            reason=reason,
                        )
                    )
                    continue
                if not dry_run:
                    await self._store.save_power_spec_extraction(
                        kb_id=file.kb_id,
                        file_id=file.file_id,
                        document=document,
                        metadata=build_document_extraction(document),
                    )
                items.append(
                    PowerSpecRebuildItem(
                        file_id=file.file_id,
                        source_file=file.source_file,
                        status="accepted",
                        clause_count=len(document.clauses),
                    )
                )
            except (ValueError, TypeError) as exc:
                items.append(
                    PowerSpecRebuildItem(
                        file_id=file.file_id,
                        source_file=file.source_file,
                        status="rejected",
                        reason=str(exc),
                    )
                )
        return PowerSpecRebuildReport(dry_run=dry_run, items=items)


def _quality_failure_reason(document: PowerSpecDocument) -> str | None:
    """判断解析出的条款是否达到可入库质量。"""

    return _clause_quality_failure(
        [(clause.clause_no, clause.clause_title, clause.clause_body) for clause in document.clauses]
    )


# 条款正文短于该长度视为「没拿到内容」；占比超过上限即判为切分质量不足。
_SHORT_CLAUSE_BODY_LENGTH = 20
_MAX_SHORT_BODY_RATIO = 0.2
_MAX_NUMERIC_TITLE_RATIO = 0.5


def _clause_quality_failure(clauses: Sequence[tuple[str, str, str]]) -> str | None:
    """判断条款（编号, 标题, 正文）是否达到可回答质量；None 表示通过。

    正文过短说明切分没有真正拿到条款内容；标题只剩编号且正文也短，说明标题抽取失败。
    重建写入与检索读取共用这一条规则，避免写入通过、读取又被拒的落差。
    """

    if not clauses:
        return "未提取到条款"
    total = len(clauses)
    numeric_titles = sum(1 for number, title, _ in clauses if title == number)
    short_bodies = sum(1 for _, _, body in clauses if len(body.strip()) < _SHORT_CLAUSE_BODY_LENGTH)
    if numeric_titles / total > _MAX_NUMERIC_TITLE_RATIO and short_bodies / total > _MAX_SHORT_BODY_RATIO:
        return "超过一半条款标题只有章节编号且正文质量不足"
    if short_bodies / total > _MAX_SHORT_BODY_RATIO:
        return "超过五分之一条款正文过短"
    return None


def is_power_spec_metadata_ready(metadata: dict[str, Any]) -> bool:
    """判断持久化派生元数据是否达到可回答质量门，与重建质量门同一规则。"""

    clauses = metadata.get("clauses") or []
    return (
        _clause_quality_failure(
            [
                (
                    str(clause.get("clause_no") or ""),
                    str(clause.get("clause_title") or ""),
                    str(clause.get("clause_body") or ""),
                )
                for clause in clauses
            ]
        )
        is None
    )
