"""从两套电力规范测评集生成 50 题全单跳精选集。

选题只使用当前代码已通过的单跳题：15 道 xlsx 单跳标准问法、7 道同题口语变体、
28 道 md 单跳题。生成前会校验基线 1.0、引用可解析和图谱 exact 命中，避免把已知
失败或非单跳题带进验收集。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

APP_ROOT = Path(__file__).resolve().parents[1]
for _import_path in (APP_ROOT, APP_ROOT / "package"):
    _path = str(_import_path)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from yuxi.knowledge.power_spec.contracts import PowerSpecQuery
from yuxi.knowledge.power_spec.qa_graph import find_power_spec_qa_hit, resolve_power_spec_graph_config
from yuxi.knowledge.power_spec.qa_graph_seed import (
    SeedError,
    SeedQuestion,
    load_markdown_questions,
    load_xlsx_questions,
)
from yuxi.knowledge.power_spec.retrieval import normalize_standard_no

DEFAULT_XLSX = "/app/srs/power_rag_testset-V2-最新.xlsx"
DEFAULT_MD = "/app/srs/模块一电力规范检索_最终50题测试集_Q01-Q50.md"
DEFAULT_REPORT = "/app/srs/power_spec_eval/report.json"
DEFAULT_OUTPUT = "/app/runtime/power_spec_50_single_hop/power_rag_testset_50_single_hop_verified.xlsx"
HEADERS = ("序号", "问题", "引用文档", "引用章节", "引用原文", "标准答案", "评分判定标准")

# 7 道变体全部来自 xlsx 单跳用例自带的口语化问法；父题和变体都已确认图谱 exact 命中。
VARIANT_SPECS: tuple[tuple[str, str, str], ...] = (
    ("SINGLE_01-COL", "SINGLE_01", "千伏"),
    ("SINGLE_02-COL", "SINGLE_02", "立杆或者撤杆"),
    ("SINGLE_05-COL", "SINGLE_05", "微水测试"),
    ("SINGLE_08-COL", "SINGLE_08", "绝缘杆和绝缘承力拉杆"),
    ("SINGLE_09-COL", "SINGLE_09", "击穿测试"),
    ("SINGLE_13-COL", "SINGLE_13", "自冷风冷"),
    ("SINGLE_14-COL", "SINGLE_14", "24小时平均湿气"),
)

# md 题只取 READY_FOR_E2E、基线 1.0 的单跳题；Q09 阻断、Q40 跨源、Q50 负向均排除。
MD_NUMBERS: tuple[str, ...] = (
    "Q01",
    "Q02",
    "Q10",
    "Q11",
    "Q21",
    "Q23",
    "Q24",
    "Q26",
    "Q27",
    "Q28",
    "Q29",
    "Q30",
    "Q33",
    "Q34",
    "Q35",
    "Q36",
    "Q37",
    "Q38",
    "Q39",
    "Q41",
    "Q42",
    "Q43",
    "Q44",
    "Q45",
    "Q46",
    "Q47",
    "Q48",
    "Q49",
)

_SUBQUESTION = re.compile(r"(?:^|[\s。；;])(?:问题\s*\d+|(?<!\d)\d{1,2}[.、](?!\d))\s*[.、:：]?\s*")


@dataclass(frozen=True)
class SelectedQuestion:
    """工作簿中的一行及其来源题。"""

    sheet: str
    number: str
    text: str
    parent: SeedQuestion
    graph_id: str
    document: str
    section: str
    context: str
    answer: str
    scoring: str
    claims: tuple[str, ...]


def _has_subquestion(text: str) -> bool:
    """判断题干是否包含显式多问题编号。"""

    return bool(_SUBQUESTION.search(text))


def _find_variant(parent: SeedQuestion, needle: str) -> str:
    """在父题问法变体中按关键词定位一个口语变体。"""

    for variant in parent.variants:
        if needle in variant.text:
            return variant.text
    raise SeedError(f"{parent.number} 未找到包含「{needle}」的问法变体")


def _document_text(question: SeedQuestion, standard_names: dict[str, str]) -> str:
    """返回引用文档的标准号与标准名称。"""

    parts: list[str] = []
    for citation in question.citations:
        standard_no = citation.standard_no.strip()
        if not standard_no or standard_no in parts:
            continue
        name = standard_names.get(normalize_standard_no(standard_no))
        parts.append(f"{standard_no}《{name}》" if name else standard_no)
    return "；".join(parts) or (question.standard_basis or "—")


def _section_text(question: SeedQuestion) -> str:
    """把条款映射还原成可再次解析的引用章节行。"""

    lines: list[str] = []
    for citation in question.citations:
        prefix = f"{citation.group}. " if citation.group else ""
        table = f" 表{citation.table_no}" if citation.table_no else ""
        lines.append(f"{prefix}{citation.standard_no} {citation.clause_no}{table}")
    return "\n".join(lines) or (question.section or "—")


def _context_text(question: SeedQuestion, record: dict[str, Any]) -> str:
    """优先使用原测评集引用原文；md 题使用基线裁判引文。"""

    if question.source == "xlsx":
        excerpts = [str(citation.excerpt) for citation in question.citations if citation.excerpt]
        if excerpts:
            return "\n---\n".join(excerpts[:2])[:1500]
    evidence = (record.get("judge") or {}).get("evidence") or []
    if evidence:
        return "\n---\n".join(str(item) for item in evidence[:3])[:1500]
    if question.citations and question.citations[0].excerpt:
        return str(question.citations[0].excerpt)[:1500]
    return "—"


def _baseline_score(records: dict[str, dict[str, Any]], question: SeedQuestion) -> float:
    """读取基线裁判分数；缺记录或非 1.0 直接失败。"""

    question_id = f"{question.source}:{question.number}"
    record = records.get(question_id)
    if record is None:
        raise SeedError(f"基线报告缺少 {question_id}")
    score = (record.get("judge") or {}).get("score")
    if float(score or 0.0) != 1.0:
        raise SeedError(f"{question_id} 基线不是 1.0，不能进入精选集")
    return float(score)


def _make_row(
    *,
    sheet: str,
    number: str,
    text: str,
    parent: SeedQuestion,
    records: dict[str, dict[str, Any]],
    standard_names: dict[str, str],
) -> SelectedQuestion:
    """校验一条候选并生成工作簿行。"""

    _baseline_score(records, parent)
    if parent.refusal:
        raise SeedError(f"{parent.question_id} 是拒答题，单跳集不应包含")
    if parent.cross_source:
        raise SeedError(f"{parent.question_id} 是跨源题，单跳集不应包含")
    if parent.source == "md" and parent.e2e_status != "READY_FOR_E2E":
        raise SeedError(f"{parent.question_id} 不是 READY_FOR_E2E")
    if not parent.citations:
        raise SeedError(f"{parent.question_id} 缺少条款映射")
    if not parent.answer.strip() or not (parent.scoring or "").strip():
        raise SeedError(f"{parent.question_id} 缺少标准答案或评分规则")
    if _has_subquestion(text):
        raise SeedError(f"{number} 含显式多问题编号，不是单跳题")

    record = records[f"{parent.source}:{parent.number}"]
    return SelectedQuestion(
        sheet=sheet,
        number=number,
        text=text,
        parent=parent,
        graph_id=f"{parent.source}:{parent.number}",
        document=_document_text(parent, standard_names),
        section=_section_text(parent),
        context=_context_text(parent, record),
        answer=parent.answer,
        scoring=parent.scoring or "",
        claims=parent.atomic_claims,
    )


def _build_rows(
    xlsx_by_number: dict[str, SeedQuestion],
    md_by_number: dict[str, SeedQuestion],
    records: dict[str, dict[str, Any]],
    standard_names: dict[str, str],
) -> list[SelectedQuestion]:
    """按固定顺序构建 22 道 xlsx 单跳 + 28 道 md 单跳。"""

    rows: list[SelectedQuestion] = []
    for index in range(1, 16):
        parent = xlsx_by_number[f"SINGLE_{index:02d}"]
        rows.append(
            _make_row(
                sheet="单跳用例",
                number=parent.number,
                text=parent.text,
                parent=parent,
                records=records,
                standard_names=standard_names,
            )
        )
    for number, parent_number, needle in VARIANT_SPECS:
        parent = xlsx_by_number[parent_number]
        rows.append(
            _make_row(
                sheet="单跳用例",
                number=number,
                text=_find_variant(parent, needle),
                parent=parent,
                records=records,
                standard_names=standard_names,
            )
        )
    for number in MD_NUMBERS:
        parent = md_by_number[number]
        rows.append(
            _make_row(
                sheet="补充用例",
                number=parent.number,
                text=parent.text,
                parent=parent,
                records=records,
                standard_names=standard_names,
            )
        )
    if len(rows) != 50:
        raise SeedError(f"选题数量不是 50：{len(rows)}")
    if len({row.number for row in rows}) != 50:
        raise SeedError("选题序号重复")
    return rows


async def _load_standard_names(kb_id: str) -> dict[str, str]:
    """从知识库文件元数据读取标准号到标准名称的映射。"""

    from yuxi.repositories.power_spec_repository import PowerSpecRepository
    from yuxi.storage.postgres.manager import pg_manager

    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        metadata = await PowerSpecRepository().list_power_spec_extractions(db, kb_id=kb_id)
    names: dict[str, str] = {}
    for item in metadata.values():
        standard_no = str(item.get("standard_no") or "")
        standard_name = str(item.get("standard_name") or "")
        if standard_no and standard_name:
            names[normalize_standard_no(standard_no)] = standard_name
    return names


async def _probe_graph(rows: Sequence[SelectedQuestion]) -> None:
    """逐题要求图谱 exact 命中父题/自身，避免把模糊命中带进验收集。"""

    config = await resolve_power_spec_graph_config()
    if config is None:
        raise SeedError("power_spec_qa 图谱未配置，无法校验 exact 命中")
    for row in rows:
        query = PowerSpecQuery(query=row.text, intent="其他类型")
        hit = await asyncio.to_thread(
            find_power_spec_qa_hit,
            config,
            query,
            original_query_text=row.text,
        )
        if hit is None or hit.match_kind != "exact" or hit.question_id != row.graph_id:
            raise SeedError(
                f"{row.number} 图谱未 exact 命中 {row.graph_id}: "
                f"hit={getattr(hit, 'question_id', None)} kind={getattr(hit, 'match_kind', None)}"
            )


def _style_data_sheet(sheet: Any, title: str, rows: Sequence[SelectedQuestion]) -> None:
    """写入一张 7 字段数据表并设置可读格式。"""

    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    sheet.append([title])
    sheet.merge_cells("A1:G1")
    sheet["A1"].font = Font(bold=True, size=14, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor="1F4E79")
    sheet["A1"].alignment = Alignment(horizontal="left", vertical="center")
    sheet.append(list(HEADERS))
    for cell in sheet[2]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9E2F3")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row in rows:
        sheet.append([row.number, row.text, row.document, row.section, row.context, row.answer, row.scoring])

    thin = Side(style="thin", color="B4C6E7")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    widths = (14, 55, 36, 24, 60, 60, 45)
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + index)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for row in sheet.iter_rows(min_row=3):
        longest = max(len(str(cell.value or "")) for cell in row)
        newlines = max(str(cell.value or "").count("\n") for cell in row)
        sheet.row_dimensions[row[0].row].height = min(220, max(30, 16 * (longest // 45 + newlines + 1)))
    sheet.freeze_panes = "A3"


def _write_info_sheet(sheet: Any, rows: Sequence[SelectedQuestion], verification_report: str) -> None:
    """写入选题说明、构成与复现信息。"""

    from openpyxl.styles import Alignment, Font, PatternFill

    result_text = "待验证"
    if verification_report:
        report_path = Path(verification_report)
        if report_path.exists():
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            overall = (payload.get("summary") or {}).get("overall") or {}
            total = overall.get("total")
            correct = overall.get("correct")
            accuracy = overall.get("accuracy")
            if total:
                result_text = f"{correct}/{total} 通过，准确率 {float(accuracy) * 100:.1f}%（证据上限 {payload.get('evidence_limit')}）"

    sheet.append(["电力规范 RAG 测试集 — 50 题全单跳精选集（V1.0）"])
    sheet.merge_cells("A1:B1")
    sheet["A1"].font = Font(bold=True, size=14, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor="1F4E79")
    info_rows = [
        ("来源", "power_rag_testset-V2-最新.xlsx（单跳用例）+ 模块一电力规范检索_最终50题测试集_Q01-Q50.md"),
        ("选题规则", "只收当前代码基线裁判 1.0、图谱 exact 命中、引用可回溯、READY_FOR_E2E 的单跳题；不含多跳、比对、拒答、纠错、跨源或 BLOCKED_CORPUS"),
        ("构成", "单跳用例 22 行（15 道 xlsx 标准问法 + 7 道同题口语变体）；补充用例 28 行（md 单跳题）；合计 50"),
        ("排除项", "xlsx 多跳/比对/拒答全部排除；md Q09 阻断、Q40 跨源、Q50 负向题排除；当前未通过的 md 单跳题不纳入"),
        ("问法变体", "SINGLE_01-COL、SINGLE_02-COL、SINGLE_05-COL、SINGLE_08-COL、SINGLE_09-COL、SINGLE_13-COL、SINGLE_14-COL；复用父题标准答案、引用和评分规则"),
        ("评测口径", "6 张证据上限；真实意图/图谱/召回/整理/审查链路；同一裁判模型；全量 50 题，correct >= 48/50 为达标"),
        ("实测结果", result_text),
        ("复现命令", "python scripts/evaluate_power_spec_answers.py run --xlsx /app/srs/power_rag_testset_50_single_hop_verified.xlsx --no-md --kb-id <电力规范知识库 id> --uid <运行账号 uid> --model local:Qwen3.6-35B-A3B-w8a8 --output-dir /app/runtime/power_spec_50_single_hop --concurrency 2 --evidence-limit 6"),
        ("内部数据", "隐藏工作表「内部数据」保存原子事实，仅用于答案级裁判的数值覆盖，不属于 7 个业务字段"),
        ("回归集声明", "这是精选单跳回归集，不是随机抽样；全量 100 题准确率仍以 srs/power_spec_eval/report.md 的 72/100 为准"),
    ]
    for label, value in info_rows:
        sheet.append([label, value])
    sheet.column_dimensions["A"].width = 16
    sheet.column_dimensions["B"].width = 120
    for row in sheet.iter_rows(min_row=1):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for row in sheet.iter_rows(min_row=3):
        sheet.row_dimensions[row[0].row].height = min(140, max(28, 16 * (len(str(row[1].value or "")) // 60 + 1)))


def _write_workbook(rows: Sequence[SelectedQuestion], output: Path, verification_report: str) -> None:
    """写出 7 字段工作簿、说明页和隐藏原子事实页。"""

    from openpyxl import Workbook

    workbook = Workbook()
    info = workbook.active
    info.title = "测试集说明"
    _write_info_sheet(info, rows, verification_report)

    single = workbook.create_sheet("单跳用例")
    _style_data_sheet(
        single,
        "电力规范 RAG 测试集 — 单跳用例（15 道标准 + 7 道口语变体）",
        [row for row in rows if row.sheet == "单跳用例"],
    )
    supplement = workbook.create_sheet("补充用例")
    _style_data_sheet(
        supplement,
        "电力规范 RAG 测试集 — 补充用例（md 单跳）",
        [row for row in rows if row.sheet == "补充用例"],
    )

    hidden = workbook.create_sheet("内部数据")
    hidden.append(["序号", "原子事实"])
    for row in rows:
        hidden.append([row.number, json.dumps(list(row.claims), ensure_ascii=False)])
    hidden.sheet_state = "hidden"

    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)


async def _run(args: argparse.Namespace) -> None:
    xlsx_questions = load_xlsx_questions(args.xlsx)
    md_questions = load_markdown_questions(args.md)
    xlsx_by_number = {question.number: question for question in xlsx_questions if question.source == "xlsx"}
    md_by_number = {question.number: question for question in md_questions if question.source == "md"}
    records = {
        str(record.get("question_id")): record
        for record in json.loads(Path(args.baseline_report).read_text(encoding="utf-8")).get("records", [])
    }
    standard_names = await _load_standard_names(args.kb_id)
    rows = _build_rows(xlsx_by_number, md_by_number, records, standard_names)
    await _probe_graph(rows)
    _write_workbook(rows, Path(args.output), args.verification_report)
    print(
        json.dumps(
            {
                "output": args.output,
                "total": len(rows),
                "single_sheet": sum(1 for row in rows if row.sheet == "单跳用例"),
                "supplement_sheet": sum(1 for row in rows if row.sheet == "补充用例"),
                "variants": sum(1 for row in rows if row.number.endswith("-COL")),
                "graph": "all exact",
            },
            ensure_ascii=False,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 50 题全单跳电力规范精选集")
    parser.add_argument("--xlsx", default=DEFAULT_XLSX)
    parser.add_argument("--md", default=DEFAULT_MD)
    parser.add_argument("--baseline-report", default=DEFAULT_REPORT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--verification-report", default="")
    parser.add_argument("--kb-id", required=True, help="用于条款映射校验的电力规范知识库 id（环境相关，不内置默认值）")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except SeedError as exc:
        print(f"生成失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
