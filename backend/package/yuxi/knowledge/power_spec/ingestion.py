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
    r"\b(?P<number>(?:GB|DL|Q/GDW|Q\s*/\s*GDW|NB|JB|TB)\s*/?\s*T?\s*[-/]?\s*\d+\s*[—–-]\s*\d{4})\b",
    re.IGNORECASE,
)
_CLAUSE_PATTERN = re.compile(r"^\s*(?P<number>(?:[1-9]\d*\.)+\d+)\s*(?P<title>[^。；;\n]*)\s*$")
_SCOPE_PATTERN = re.compile(r"^\s*(?:适用范围|范围)\s*[:：]\s*(?P<scope>.+?)\s*$")
_OCR_CELL_PATTERN = re.compile(r"</?(?:fcel|lcel|ecel|ucel|xcel|tcel|bcel|rcel)>")
_OCR_LINE_BREAK_PATTERN = re.compile(r"<nl\s*/?>")
_OCR_TABLE_GAP_PATTERN = re.compile(r"(?:\s*\|\s*){2,}")
_FORMAT_TRANSLATION = str.maketrans({"／": "/", "∕": "/", "＋": "+", "–": "—"})


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

    standard_match = _STANDARD_PATTERN.search(normalized_text)
    if standard_match is None:
        raise ValueError("未识别到规范编号")

    standard_no = re.sub(r"\s+", "", standard_match.group("number")).replace("–", "—")
    version = standard_no[-4:]
    lines = [line.strip() for line in normalized_text.splitlines()]
    document_scope = _find_scope(lines)
    resolved_name = standard_name or _derive_standard_name(lines, standard_match.group("number"))
    status = "废止" if re.search(r"(?:废止|作废|失效)", normalized_text) else "现行"
    clauses = _extract_clauses(lines, document_scope)

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
        if line.startswith("本标准适用于"):
            return line
    return None


def _derive_standard_name(lines: list[str], matched_standard_no: str) -> str:
    """从含标准编号的行中提取名称，跳过只剩页码、表格标记的候选行。"""

    for line in lines:
        if matched_standard_no not in line:
            continue
        candidate = _OCR_CELL_PATTERN.sub("", line.replace(matched_standard_no, "", 1)).strip(" ：:—-|")
        if _is_standard_name_candidate(candidate):
            return candidate
    return matched_standard_no


def _is_standard_name_candidate(candidate: str) -> bool:
    """判断候选是否像规范名称，而不是页码或整行表格内容。"""

    if len(candidate) > 40 or "|" in candidate:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]{2,}", candidate)) or bool(re.search(r"[A-Za-z]{3,}", candidate))


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


def build_clause_tags(document: PowerSpecDocument, clause: PowerSpecClause) -> list[str]:
    """构建知识 Chunk 的稳定规范标签。"""

    build_clause_extraction(document, clause)
    return [
        "power_spec",
        f"power_spec:standard:{document.standard_no}",
        f"power_spec:status:{document.status}",
        f"power_spec:clause:{clause.clause_no}",
    ]


# ---------------------------------------------------------------------------
# 提取流水线（原 pipeline.py）
# ---------------------------------------------------------------------------


class PowerSpecExtractionStore(Protocol):
    """定义领域提取结果的持久化边界。"""

    async def save_power_spec_extraction(
        self,
        *,
        kb_id: str,
        file_id: str,
        document: PowerSpecDocument,
        metadata: dict,
    ) -> None:
        """保存一份知识库文件的电力规范派生结果。"""


class PowerSpecIngestionService:
    """解析知识库文本并交给领域存储边界保存。"""

    def __init__(self, store: PowerSpecExtractionStore):
        self._store = store

    async def extract_and_store(
        self,
        *,
        kb_id: str,
        file_id: str,
        text: str,
        source_file: str | None = None,
        standard_name: str | None = None,
    ) -> PowerSpecDocument:
        """解析文本并在成功后一次性提交领域派生结果。"""

        document = parse_power_spec_text(
            text,
            document_id=file_id,
            source_file=source_file,
            standard_name=standard_name,
        )
        await self._store.save_power_spec_extraction(
            kb_id=kb_id,
            file_id=file_id,
            document=document,
            metadata=build_document_extraction(document),
        )
        return document


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


class AsyncPowerSpecTextSource(Protocol):
    """定义从 Yuxi 知识库读取已解析文本的边界。"""

    def __call__(self, *, kb_id: str, file_id: str) -> Awaitable[str]:
        """读取经过权限校验的知识库解析文本。"""


async def extract_from_source(
    *,
    source: AsyncPowerSpecTextSource,
    ingestion: PowerSpecIngestionService,
    kb_id: str,
    file_id: str,
    source_file: str | None = None,
) -> PowerSpecDocument:
    """从外部知识库文本读取边界调用规范提取流水线。"""

    text = await source(kb_id=kb_id, file_id=file_id)
    return await ingestion.extract_and_store(
        kb_id=kb_id,
        file_id=file_id,
        text=text,
        source_file=source_file,
    )


# ---------------------------------------------------------------------------
# 元数据重建（原 rebuild.py）
# ---------------------------------------------------------------------------


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
    clauses = document.clauses
    if not clauses:
        return "未提取到条款"
    numeric_titles = sum(1 for clause in clauses if clause.clause_title == clause.clause_no)
    short_bodies = sum(1 for clause in clauses if len(clause.clause_body.strip()) < 20)
    if numeric_titles / len(clauses) > 0.5 and short_bodies / len(clauses) > 0.2:
        return "超过一半条款标题只有章节编号且正文质量不足"
    if short_bodies / len(clauses) > 0.2:
        return "超过五分之一条款正文过短"
    return None


def is_power_spec_metadata_ready(metadata: dict[str, Any]) -> bool:
    """判断持久化派生元数据是否达到可回答质量门。"""

    clauses = metadata.get("clauses") or []
    if not clauses:
        return False
    numeric_titles = sum(
        1 for clause in clauses if str(clause.get("clause_title") or "") == str(clause.get("clause_no") or "")
    )
    short_bodies = sum(len(str(clause.get("clause_body") or "").strip()) < 20 for clause in clauses)
    return numeric_titles / len(clauses) <= 0.5 and short_bodies / len(clauses) <= 0.2
