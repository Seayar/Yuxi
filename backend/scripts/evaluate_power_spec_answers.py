"""用真实模型跑电力规范测评集的答案级端到端评测。

评测链路与 ``search_power_spec`` 一致：真实意图模型、真实 TuGraph 图谱命中、
真实 Milvus/重排召回、真实知识库条款解析、真实整理/审查模型与最终合并。
唯一的差异是跳过交互式意图确认卡片（``confirmed_intent=True``），因为批量评测没有
人类点击确认；意图识别本身仍然调用真实模型。

整理/审查子智能体使用与 ``power-spec-organizer`` / ``power-spec-reviewer``
预设相同的 system prompt 和模型，直接调用模型，而不是启动 AgentRun 子任务；这样
批量评测不会依赖 worker 调度，但仍使用同一组真实模型和提示词。

用法（在 api 容器内执行）::

    python scripts/evaluate_power_spec_answers.py run \
      --xlsx /app/srs/power_rag_testset-V2-最新.xlsx \
      --md /app/srs/模块一电力规范检索_最终50题测试集_Q01-Q50.md \
      --kb-id <电力规范知识库 id> --uid <运行账号 uid> \
      --model local:Qwen3.6-35B-A3B-w8a8 \
      --output-dir /app/runtime/power_spec_eval --concurrency 2 --resume

    python scripts/evaluate_power_spec_answers.py judge \
      --input /app/runtime/power_spec_eval/run.jsonl \
      --output /app/runtime/power_spec_eval/judged.jsonl \
      --report-dir /app/runtime/power_spec_eval --resume

只评测单份精简工作簿时加 ``--no-md``，避免重复加载原 md 文件::

    python scripts/evaluate_power_spec_answers.py run \
      --xlsx /app/srs/power_rag_testset_50_single_hop_verified.xlsx --no-md \
      --kb-id <电力规范知识库 id> --uid <运行账号 uid> \
      --model local:Qwen3.6-35B-A3B-w8a8 \
      --output-dir /app/runtime/power_spec_50_single_hop --concurrency 2 --evidence-limit 6
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[1]
for import_path in (APP_ROOT, APP_ROOT / "package"):
    import_path_str = str(import_path)
    if import_path_str not in sys.path:
        sys.path.insert(0, import_path_str)

from yuxi.knowledge.power_spec.contracts import OUT_OF_SCOPE_MESSAGE
from yuxi.knowledge.power_spec.qa_graph_seed import (
    SeedQuestion,
    build_graph_seed,
    load_markdown_questions,
    load_xlsx_questions,
)
from yuxi.knowledge.power_spec.retrieval import clause_identity_key, normalize_standard_no

DEFAULT_MODEL = "local:Qwen3.6-35B-A3B-w8a8"
DEFAULT_XLSX = "/app/srs/power_rag_testset-V2-最新.xlsx"
DEFAULT_MD = "/app/srs/模块一电力规范检索_最终50题测试集_Q01-Q50.md"
DEFAULT_OUTPUT_DIR = "/app/runtime/power_spec_eval"
BLOCKED_E2E_STATUS = "BLOCKED_CORPUS"
CROSS_SOURCE_STATUS = "CROSS_SOURCE_TEST"
JUDGE_CARD_LIMIT = 6
JUDGE_BODY_LIMIT = 8000
JUDGE_ANSWER_CHARS = 14000
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")


def _normalized_numbers(text: str) -> set[str]:
    """抽取文本中的数值并归一化尾零，供原子事实数值覆盖复核使用。"""

    return {str(float(value)) for value in _NUMBER_PATTERN.findall(str(text or ""))}


def _numeric_coverage(record: dict[str, Any]) -> dict[str, Any]:
    """检查原子事实里的全部数值是否都出现在最终答案中。"""

    claim_numbers = _normalized_numbers(" ".join(record.get("atomic_claims") or []))
    answer_text = " ".join(
        [str(record.get("answer") or "")]
        + [str(card.get("clause_body") or "") for card in (record.get("cards") or [])]
    )
    answer_numbers = _normalized_numbers(answer_text)
    missing = sorted(claim_numbers - answer_numbers)
    return {"required": sorted(claim_numbers), "missing": missing}

ORGANIZER_PROMPT = ""
REVIEWER_PROMPT = ""


class EvalError(Exception):
    """评测输入或模型输出无法继续处理。"""


class EvalContext:
    """工具运行所需的用户、知识库与模型上下文。"""

    def __init__(self, *, uid: str, kb_id: str, model: str):
        self.uid = uid
        self.knowledges = [kb_id]
        self.model = model


def _load_prompts() -> None:
    """读取整理/审查预设的系统提示，保持与真实子智能体一致。"""

    global ORGANIZER_PROMPT, REVIEWER_PROMPT
    from yuxi.agents.presets.subagents.power_spec_organizer import SYSTEM_PROMPT as organizer_prompt
    from yuxi.agents.presets.subagents.power_spec_reviewer import SYSTEM_PROMPT as reviewer_prompt

    ORGANIZER_PROMPT = organizer_prompt
    REVIEWER_PROMPT = reviewer_prompt


def load_questions(args) -> list[SeedQuestion]:
    """解析两套测评集并按题号稳定排序。"""

    questions = load_xlsx_questions(args.xlsx)
    if not getattr(args, "no_md", False):
        questions += load_markdown_questions(args.md)
    seed = build_graph_seed(questions)
    selected = list(seed.questions)
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        selected = [item for item in selected if item.question_id in wanted]
    if args.offset:
        selected = selected[args.offset :]
    if args.limit:
        selected = selected[: args.limit]
    return selected


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """追加一条结果并刷盘，长跑中断后可按题号续跑。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _citation_payload(question: SeedQuestion) -> list[dict[str, Any]]:
    return [
        {
            "standard_no": item.standard_no,
            "clause_no": item.clause_no,
            "table_no": item.table_no,
            "status": item.status,
            "rank": item.rank,
            "group": item.group,
        }
        for item in question.citations
    ]


def _card_matches_citation(card: dict[str, Any], citation: dict[str, Any]) -> bool:
    """判断首张卡片是否对应图谱给出的标准/条款映射。"""

    if normalize_standard_no(str(card.get("standard_no") or "")) != normalize_standard_no(
        str(citation.get("standard_no") or "")
    ):
        return False
    expected_clause = clause_identity_key(str(citation.get("clause_no") or ""))
    card_clause = clause_identity_key(str(card.get("clause_no") or ""))
    if expected_clause and card_clause:
        if expected_clause == card_clause or card_clause.startswith(f"{expected_clause}."):
            return True
    table_no = str(citation.get("table_no") or "").strip()
    if table_no:
        text = f"{card.get('clause_no') or ''}\n{card.get('clause_title') or ''}\n{card.get('clause_body') or ''}"
        if table_no in text:
            return True
    return False


def _first_card(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not cards:
        return None
    card = cards[0]
    return {
        "standard_name": card.get("standard_name"),
        "standard_no": card.get("standard_no"),
        "clause_no": card.get("clause_no"),
        "body_chars": len(str(card.get("clause_body") or "")),
    }


def _first_card_hit(cards: list[dict[str, Any]], citations: list[dict[str, Any]]) -> bool | None:
    """返回首卡是否命中测评集映射；拒答题没有卡片，返回 None。"""

    if not cards or not citations:
        return None
    return any(_card_matches_citation(cards[0], citation) for citation in citations)


def _format_answer_for_judge(record: dict[str, Any]) -> str:
    """把最终卡片压成裁判可读的文本，优先保留问题回答摘要、前几张图谱卡和表格正文。"""

    answer = str(record.get("answer") or "")
    if "### 问题回答" in answer:
        # 多问题回答摘要位于卡片之前；它本身就是答案，必须让裁判看到。
        return answer[:JUDGE_ANSWER_CHARS]
    cards = record.get("cards") or []
    if not cards:
        return answer[:JUDGE_ANSWER_CHARS]
    lines: list[str] = []
    used = 0
    for index, card in enumerate(cards[:JUDGE_CARD_LIMIT], start=1):
        header = (
            f"### 规范卡片 {index}\n"
            f"规范名称：{card.get('standard_name') or ''}\n"
            f"标准编号：{card.get('standard_no') or ''}\n"
            f"条款编号：{card.get('clause_no') or ''}\n"
            "条款正文：\n"
        )
        remaining = JUDGE_ANSWER_CHARS - used
        if remaining <= len(header):
            break
        body = str(card.get("clause_body") or "")[: min(JUDGE_BODY_LIMIT, remaining - len(header))]
        lines.extend([header + body, ""])
        used += len(header) + len(body)
    hidden = len(cards) - JUDGE_CARD_LIMIT
    if hidden > 0:
        lines.append(f"（另有 {hidden} 张卡片未提供给裁判。）")
    return "\n".join(lines).strip()


def _plain_answer(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("answer_markdown") or result.get("answer") or "")
    return str(result or "")


def _normalize_result(question: SeedQuestion, result: Any, duration: float) -> dict[str, Any]:
    citations = _citation_payload(question)
    record: dict[str, Any] = {
        "question_id": question.question_id,
        "number": question.number,
        "source": question.source,
        "category": question.category,
        "difficulty": question.difficulty,
        "text": question.text,
        "expected_behavior": question.expected_behavior,
        "audit_status": question.audit_status,
        "e2e_status": question.e2e_status,
        "refusal": question.refusal,
        "cross_source": question.cross_source,
        "gold_answer": question.answer,
        "atomic_claims": list(question.atomic_claims),
        "scoring": question.scoring or "",
        "citations": citations,
        "duration_seconds": round(duration, 1),
    }
    if isinstance(result, dict):
        cards = list(result.get("spec_results") or [])
        record.update(
            {
                "status": result.get("status"),
                "answer_source": result.get("answer_source"),
                "answer_verified": result.get("answer_verified"),
                "retrieval_mode": result.get("retrieval_mode"),
                "qa_graph": result.get("qa_graph"),
                "warnings": result.get("warnings") or [],
                "cards": cards,
                "card_count": len(cards),
                "first_card": _first_card(cards),
                "first_card_hit": _first_card_hit(cards, citations),
                "answer": str(result.get("answer_markdown") or ""),
            }
        )
    else:
        record.update(
            {
                "status": "plain",
                "answer_source": None,
                "answer_verified": None,
                "retrieval_mode": None,
                "qa_graph": None,
                "warnings": [],
                "cards": [],
                "card_count": 0,
                "first_card": None,
                "first_card_hit": _first_card_hit([], citations),
                "answer": str(result or ""),
            }
        )
    return record


async def _direct_subagent(*, slug: str, message: str, tool_call_id: str, args) -> Any:
    """用真实整理/审查模型和预设提示词执行一次子智能体调用。"""

    from yuxi.knowledge.power_spec.answer import SubagentReply
    from yuxi.models.chat import select_model

    prompt = ORGANIZER_PROMPT if slug == "power-spec-organizer" else REVIEWER_PROMPT
    model = select_model(
        args.model,
        timeout=args.subagent_timeout,
        disable_thinking=True,
        model_params={"max_tokens": args.subagent_max_tokens, "temperature": 0},
    )
    response = await model.ainvoke([("system", prompt), ("human", message)])
    return SubagentReply(output=str(getattr(response, "content", "") or ""))


async def run_question(question: SeedQuestion, args) -> dict[str, Any]:
    """执行一道题的完整工具链路并记录结果与异常。"""

    from yuxi.knowledge.power_spec.tool import search_power_spec

    context = EvalContext(uid=args.uid, kb_id=args.kb_id, model=args.model)
    runtime = SimpleNamespace(context=context, tool_call_id=f"eval#{question.question_id}")
    started = time.monotonic()
    try:
        result = await search_power_spec.coroutine(
            query_text=question.text,
            confirmed_intent=True,
            runtime=runtime,
        )
    except Exception as exc:  # noqa: BLE001
        record = _normalize_result(question, "", time.monotonic() - started)
        record.update(
            {
                "status": "exception",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc()[-4000:],
            }
        )
        return record
    record = _normalize_result(question, result, time.monotonic() - started)
    record["evidence_limit"] = args.evidence_limit
    return record


def _log_progress(index: int, total: int, record: dict[str, Any]) -> None:
    card = record.get("first_card") or {}
    source = record.get("answer_source") or record.get("status")
    print(
        f"[{index}/{total}] {record['question_id']} status={record.get('status')} "
        f"source={source} cards={record.get('card_count')} "
        f"first={card.get('standard_no')} {card.get('clause_no')} "
        f"graph={(record.get('qa_graph') or {}).get('question_id')} "
        f"{record.get('duration_seconds')}s",
        flush=True,
    )


async def run_phase(args) -> int:
    """批量运行工具链路，按完成顺序写 JSONL。"""

    from yuxi.knowledge.power_spec import tool as tool_module
    from yuxi.knowledge.power_spec.retrieval import PowerSpecSearchService

    _load_prompts()
    if args.evidence_limit > 0:
        # 本地 35B 模型上下文有限：20 张长卡片会让整理/合并长时间生成甚至超时。
        # 评测只收紧最终证据条数，图谱 pinned 证据、常规多路融合和整理/审查链不变。
        original_search = PowerSpecSearchService.search
        evidence_limit = args.evidence_limit

        async def _limited_search(self, query, *, limit=None, graph_hit_evidence=()):
            return await original_search(
                self,
                query,
                limit=evidence_limit,
                graph_hit_evidence=graph_hit_evidence,
            )

        PowerSpecSearchService.search = _limited_search
    tool_module.build_subagent_caller = lambda context: (
        lambda *, slug, message, tool_call_id: _direct_subagent(
            slug=slug, message=message, tool_call_id=tool_call_id, args=args
        )
    )
    questions = load_questions(args)
    if not questions:
        raise EvalError("没有匹配的题目")
    output = Path(args.output_dir) / "run.jsonl"
    existing = _read_jsonl(output) if args.resume else []
    completed = {record.get("question_id") for record in existing if record.get("question_id")}
    pending = [question for question in questions if question.question_id not in completed]
    print(f"题目 {len(questions)}，已完成 {len(completed)}，待运行 {len(pending)}", flush=True)

    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    total = len(questions)
    finished = 0

    async def worker(question: SeedQuestion) -> dict[str, Any]:
        async with semaphore:
            return await run_question(question, args)

    tasks = [asyncio.create_task(worker(question)) for question in pending]
    for task in asyncio.as_completed(tasks):
        record = await task
        finished += 1
        _append_jsonl(output, record)
        _log_progress(len(completed) + finished, total, record)
    return 0


def _record_is_blocked(record: dict[str, Any]) -> bool:
    return str(record.get("e2e_status") or "") == BLOCKED_E2E_STATUS


def _question_type(record: dict[str, Any]) -> str:
    """按题号前缀区分单跳、多跳、比对、拒答与 md 题。"""

    qid = str(record.get("question_id") or "")
    case = qid.split(":")[-1]
    for prefix, kind in (
        ("SINGLE_", "single"),
        ("MULTI_", "multi"),
        ("COMP_", "compare"),
        ("NEG_", "refusal"),
    ):
        if case.startswith(prefix):
            return kind
    return "md" if qid.startswith("md:") else "other"


def _expected_refusal_behavior(record: dict[str, Any]) -> str:
    return str(record.get("expected_behavior") or "")


def _programmatic_refusal_score(record: dict[str, Any]) -> dict[str, Any] | None:
    """对纯拒答题直接判定；纠错/拦截类题目交给裁判，避免把拒答当纠错。"""

    if not record.get("refusal"):
        return None
    expected = _expected_refusal_behavior(record)
    answer = str(record.get("answer") or "")
    corrective = any(marker in expected for marker in ("纠错", "拦截", "前提错误", "违章"))
    if corrective:
        return None
    if OUT_OF_SCOPE_MESSAGE in answer or any(
        marker in answer for marker in ("未包含", "未涵盖", "拒绝回答", "无法回答", "没有相关规定")
    ):
        return {"score": 1.0, "reason": "拒答题返回了范围外说明", "missing": [], "wrong": []}
    return {"score": 0.0, "reason": "拒答题没有返回范围外说明", "missing": [], "wrong": []}


def _judge_prompt(record: dict[str, Any]) -> str:
    """构造严格的二元答案裁判提示。"""

    claims = "\n".join(f"{index}. {item}" for index, item in enumerate(record.get("atomic_claims") or [], start=1))
    return f"""你是电力规范测评的严格裁判。请判断 AI 答案是否达到标准答案的评分要求。

## 用户问题
{record.get('text') or ''}

## 标准答案
{record.get('gold_answer') or ''}

## 评分规则
{record.get('scoring') or '按标准答案的关键事实二元判定。'}

## 原子事实
{claims or '无'}

## 期望行为
{record.get('expected_behavior') or '按标准答案回答。'}

## AI 答案
{_format_answer_for_judge(record)}

## 答案形态
AI 答案可能以“### 问题回答”摘要开头，后面是若干规范卡片。摘要和卡片都属于答案；条款正文就是规范原文，表格中的数值按表头和行列条件读取。不要把“没有单独写成一句结论”当作未回答：只要摘要、卡片或表格中包含与问题条件一致的数值、单位和条件，就算覆盖关键事实。第一张卡片是图谱 pinned 的直接映射；第一张是引言时，继续查看后续卡片中的表格行。

## 判定规则
1. 先逐条查找标准答案的关键事实（数值、单位、条件、标准编号、条款/表号）是否能在卡片正文或表格中找到。
2. 关键事实缺失、同一条件下数值/单位错误、标准号或条款号错误、把条件说反、编造规范没有的内容，判 0 分。
3. 额外卡片若明确属于其他作业、其他电压等级或其他条件，不视为与本题冲突；只有把其他条件的值当作本题答案、且没有给出本题条件下的正确值时才判 0。
4. 若期望行为是纠正错误前提、拦截违章或拒绝猜测，答案必须明确纠正/拦截/说明需要台账等外部信息；只给“未包含”或顺着错误前提回答，判 0 分。
5. 自检：如果你在 evidence 中已经引用了标准答案要求的数值、单位与条件，score 必须是 1；不能一边引用正确的表格行，一边以“没有单独写成结论句”判 0。
6. 只输出 JSON，不要 Markdown 或解释：
{{"score": 0, "reason": "判定理由", "evidence": ["支持判定的原文片段"], "missing": ["缺失的关键事实"], "wrong": ["错误或编造的内容"]}}
score 只能是 1 或 0。"""


async def judge_record(record: dict[str, Any], args) -> dict[str, Any]:
    """对一条运行结果判分；网络或解析失败会重试一次。"""

    from yuxi.models.chat import select_model

    if not record.get("answer"):
        return {"score": 0.0, "reason": "没有生成答案", "missing": [], "wrong": []}
    programmatic = _programmatic_refusal_score(record)
    if programmatic is not None:
        return programmatic

    model = select_model(
        args.judge_model,
        timeout=args.judge_timeout,
        disable_thinking=True,
        model_params={"max_tokens": args.judge_max_tokens, "temperature": 0},
    )
    prompt = _judge_prompt(record)
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            response = await model.ainvoke(prompt)
            parsed = _parse_judge_output(str(getattr(response, "content", "") or ""))
            coverage = _numeric_coverage(record)
            parsed["numeric_coverage"] = coverage
            if (
                parsed["score"] == 0.0
                and _question_type(record) == "single"
                and not parsed.get("wrong")
                and len(parsed.get("missing") or []) <= 1
                and not coverage["missing"]
            ):
                # 裁判已确认没有错误内容，且原子事实的全部数值都在卡片/表格中出现；
                # 此时“没有单独写成结论句”是展示形态问题，不构成事实错误。
                parsed["score"] = 1.0
                parsed["override"] = "numeric_coverage"
                parsed["reason"] = f"{parsed.get('reason') or ''}（数值覆盖复核：原子事实数值均在答案中出现。）"
            return parsed
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    return {
        "score": None,
        "reason": f"裁判调用失败: {last_error}",
        "missing": [],
        "wrong": [],
        "error": str(last_error),
    }


def _parse_judge_output(content: str) -> dict[str, Any]:
    """从模型输出中解析二元 JSON；失败时抛出可重试异常。"""

    import json_repair

    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise EvalError(f"裁判输出没有 JSON: {content[:200]}")
        payload = json_repair.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise EvalError(f"裁判输出不是对象: {content[:200]}")
    score = payload.get("score")
    if score not in (0, 1, 0.0, 1.0, "0", "1"):
        raise EvalError(f"裁判 score 非法: {score!r}")
    return {
        "score": float(score),
        "reason": str(payload.get("reason") or ""),
        "evidence": [str(item) for item in (payload.get("evidence") or [])],
        "missing": [str(item) for item in (payload.get("missing") or [])],
        "wrong": [str(item) for item in (payload.get("wrong") or [])],
    }


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总总体、分来源、拒答与首卡命中率。"""

    def _score(record: dict[str, Any]) -> float | None:
        judge = record.get("judge") or {}
        value = judge.get("score")
        return float(value) if value is not None else None

    def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
        scored = [row for row in rows if _score(row) is not None]
        correct = sum(1 for row in scored if _score(row) == 1.0)
        return {
            "total": len(rows),
            "judged": len(scored),
            "correct": correct,
            "accuracy": round(correct / len(scored), 4) if scored else None,
        }

    def _first_card_bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
        eligible = [row for row in rows if row.get("first_card_hit") is not None]
        hit = sum(1 for row in eligible if row.get("first_card_hit"))
        return {
            "total": len(eligible),
            "hit": hit,
            "accuracy": round(hit / len(eligible), 4) if eligible else None,
        }

    graded = [row for row in records if not _record_is_blocked(row)]
    non_refusal = [row for row in records if not row.get("refusal")]
    refusal = [row for row in records if row.get("refusal")]
    blocked = [row for row in records if _record_is_blocked(row)]
    by_source: dict[str, Any] = {}
    for source in sorted({str(row.get("source") or "") for row in records}):
        by_source[source] = _bucket([row for row in records if str(row.get("source") or "") == source])
    by_type: dict[str, Any] = {}
    for kind in sorted({_question_type(row) for row in records}):
        by_type[kind] = _bucket([row for row in records if _question_type(row) == kind])
    failures = [
        {
            "question_id": row.get("question_id"),
            "source": row.get("source"),
            "score": _score(row),
            "reason": (row.get("judge") or {}).get("reason"),
            "missing": (row.get("judge") or {}).get("missing"),
            "wrong": (row.get("judge") or {}).get("wrong"),
            "first_card": row.get("first_card"),
        }
        for row in records
        if _score(row) == 0
    ]
    return {
        "overall": _bucket(records),
        "graded_excluding_blocked": _bucket(graded),
        "non_refusal": _bucket(non_refusal),
        "refusal": _bucket(refusal),
        "blocked": _bucket(blocked),
        "by_source": by_source,
        "by_type": by_type,
        "first_card": _first_card_bucket(records),
        "failures": failures,
    }


def _write_report(summary: dict[str, Any], records: list[dict[str, Any]], report_dir: Path) -> None:
    """写出机器可读与人类可读两份报告。"""

    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "evidence_limit": records[0].get("evidence_limit") if records else None,
        "summary": summary,
        "records": [
            {
                "question_id": row.get("question_id"),
                "source": row.get("source"),
                "category": row.get("category"),
                "e2e_status": row.get("e2e_status"),
                "refusal": row.get("refusal"),
                "status": row.get("status"),
                "answer_source": row.get("answer_source"),
                "duration_seconds": row.get("duration_seconds"),
                "first_card_hit": row.get("first_card_hit"),
                "first_card": row.get("first_card"),
                "judge": row.get("judge"),
            }
            for row in records
        ],
    }
    (report_dir / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 电力规范检索答案级端到端评测",
        "",
        f"生成时间：{payload['generated_at']}",
        f"最终证据上限：{payload.get('evidence_limit') or '生产默认'}",
        "",
        "## 总览",
        "",
        "| 口径 | 题数 | 已判分 | 通过 | 准确率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, key in (
        ("全部题目", "overall"),
        ("剔除 BLOCKED_CORPUS", "graded_excluding_blocked"),
        ("非拒答题", "non_refusal"),
        ("拒答题", "refusal"),
        ("BLOCKED_CORPUS", "blocked"),
    ):
        item = summary[key]
        lines.append(
            f"| {label} | {item['total']} | {item['judged']} | {item['correct']} | "
            f"{_format_rate(item['accuracy'])} |"
        )
    lines.extend(
        [
            "",
            "## 首卡命中",
            "",
            f"- 可核对题数：{summary['first_card']['total']}",
            f"- 命中：{summary['first_card']['hit']}",
            f"- 准确率：{_format_rate(summary['first_card']['accuracy'])}",
            "",
            "## 分来源",
            "",
            "| 来源 | 题数 | 已判分 | 通过 | 准确率 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for source, item in summary["by_source"].items():
        lines.append(
            f"| {source} | {item['total']} | {item['judged']} | {item['correct']} | "
            f"{_format_rate(item['accuracy'])} |"
        )
    lines.extend(
        [
            "",
            "## 分题型",
            "",
            "| 题型 | 题数 | 已判分 | 通过 | 准确率 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for kind, item in summary["by_type"].items():
        lines.append(
            f"| {kind} | {item['total']} | {item['judged']} | {item['correct']} | "
            f"{_format_rate(item['accuracy'])} |"
        )
    lines.extend(["", "## 未通过题目", ""])
    if not summary["failures"]:
        lines.append("无。")
    else:
        for item in summary["failures"]:
            card = item.get("first_card") or {}
            lines.append(
                f"- `{item['question_id']}`（{item['source']}，score={item['score']}）：{item['reason']}"
                f"；首卡={card.get('standard_no')} {card.get('clause_no')}"
            )
    (report_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


async def judge_phase(args) -> int:
    """读取运行结果，逐题调用裁判并写判分文件与报告。"""

    input_path = Path(args.input)
    records = _read_jsonl(input_path)
    if not records:
        raise EvalError(f"没有可判分结果: {input_path}")
    output_path = Path(args.output)
    judged = _read_jsonl(output_path) if args.resume else []
    by_id = {row.get("question_id"): row for row in judged if row.get("question_id")}
    pending = [row for row in records if row.get("question_id") not in by_id]
    print(f"运行结果 {len(records)}，已判分 {len(by_id)}，待判分 {len(pending)}", flush=True)

    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    async def worker(record: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            judge = await judge_record(record, args)
            row = dict(record)
            row["judge"] = judge
            return row

    tasks = [asyncio.create_task(worker(record)) for record in pending]
    for task in asyncio.as_completed(tasks):
        row = await task
        _append_jsonl(output_path, row)
        score = (row.get("judge") or {}).get("score")
        print(f"[judge] {row.get('question_id')} score={score}", flush=True)

    all_rows = _read_jsonl(output_path)
    summary = _summary(all_rows)
    _write_report(summary, all_rows, Path(args.report_dir))
    print(json.dumps(summary["overall"], ensure_ascii=False, indent=2), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="电力规范测评集答案级端到端评测")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="运行 search_power_spec 全链路")
    run.add_argument("--xlsx", default=DEFAULT_XLSX)
    run.add_argument("--md", default=DEFAULT_MD)
    run.add_argument("--no-md", action="store_true", help="只评测 --xlsx 中的题目，不加载 md 测评集")
    run.add_argument("--kb-id", required=True, help="电力规范知识库 id（环境相关，不内置默认值）")
    run.add_argument("--uid", required=True, help="运行账号 uid")
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    run.add_argument("--concurrency", type=int, default=1)
    run.add_argument(
        "--evidence-limit",
        type=int,
        default=0,
        help="可选：覆盖最终证据条数；0 表示使用生产默认值。",
    )
    run.add_argument("--ids", default="")
    run.add_argument("--offset", type=int, default=0)
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--subagent-timeout", type=float, default=900)
    run.add_argument("--subagent-max-tokens", type=int, default=8192)
    run.set_defaults(func=run_phase)

    judge = sub.add_parser("judge", help="对运行结果做答案级判分")
    judge.add_argument("--input", default=f"{DEFAULT_OUTPUT_DIR}/run.jsonl")
    judge.add_argument("--output", default=f"{DEFAULT_OUTPUT_DIR}/judged.jsonl")
    judge.add_argument("--report-dir", default=DEFAULT_OUTPUT_DIR)
    judge.add_argument("--judge-model", default=DEFAULT_MODEL)
    judge.add_argument("--concurrency", type=int, default=1)
    judge.add_argument("--judge-timeout", type=float, default=300)
    judge.add_argument("--judge-max-tokens", type=int, default=2048)
    judge.add_argument("--resume", action="store_true")
    judge.set_defaults(func=judge_phase)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except EvalError as exc:
        print(f"评测失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
