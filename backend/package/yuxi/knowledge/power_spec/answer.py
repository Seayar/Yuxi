from __future__ import annotations

import html
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import json_repair
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yuxi.knowledge.power_spec.contracts import PowerSpecQuery, SpecClauseEvidence
from yuxi.knowledge.power_spec.retrieval import display_clause_no, display_clause_title
from yuxi.utils import logger


ORGANIZER_SLUG = "power-spec-organizer"
REVIEWER_SLUG = "power-spec-reviewer"
REVIEW_PASS = "pass"
MAX_EVIDENCE_BODY_CHARS = 6000
CITATION_PATTERN = re.compile(r"\[source:[^\]]+\]")
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９．", "0123456789.")

ANSWER_SOURCE_MODEL = "model"
ANSWER_SOURCE_FIXED_CARD = "fixed_card"
ACCEPTANCE_QUESTION_ID = "power_spec_answer_acceptance"

POWER_SPEC_ORGANIZE_ENABLED = os.getenv("POWER_SPEC_ORGANIZE_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}
POWER_SPEC_ORGANIZE_MAX_ATTEMPTS = int(os.getenv("POWER_SPEC_ORGANIZE_MAX_ATTEMPTS", "3"))


@dataclass(frozen=True)
class SubagentReply:
    """一次子智能体运行的可见结果。"""

    output: str
    thread_id: str | None = None
    run_id: str | None = None


class SubagentCaller(Protocol):
    """启动子智能体并等待结果的边界。"""

    def __call__(
        self,
        *,
        slug: str,
        message: str,
        tool_call_id: str,
        continue_thread_id: str | None = None,
    ) -> Awaitable[SubagentReply]: ...


class ReviewIssue(BaseModel):
    """审查子智能体给出的一条问题。"""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(default="未分类")
    detail: str = Field(default="")
    citation: str | None = None


class ReviewVerdict(BaseModel):
    """审查子智能体的结构化判定。"""

    model_config = ConfigDict(frozen=True)

    passed: bool
    issues: list[ReviewIssue] = Field(default_factory=list)


class OrganizedClause(BaseModel):
    """整理子智能体给出的单条条款字段值。格式由代码决定，这里只提供值。"""

    model_config = ConfigDict(frozen=True)

    citation_key: str = Field(min_length=1)
    body: str = Field(min_length=1)
    clause_no: str | None = None
    clause_title: str | None = None
    scope: str | None = None


class OrganizedDraft(BaseModel):
    """整理子智能体的结构化输出。"""

    model_config = ConfigDict(frozen=True)

    clauses: list[OrganizedClause] = Field(default_factory=list)
    notes: str | None = None


class OrganizedAnswer(BaseModel):
    """整理与审查的最终结论。"""

    model_config = ConfigDict(frozen=True)

    status: Literal["published", "rejected", "needs_user_confirmation"]
    draft: OrganizedDraft | None = None
    attempts: int = Field(ge=1)
    last_verdict: ReviewVerdict | None = None
    ungrounded_numbers: list[str] = Field(default_factory=list)


@dataclass
class _Threads:
    """记录整理与审查各自的子线程，重试时续跑同一线程。"""

    organizer: str | None = None
    reviewer: str | None = None
    prompts: list[str] = field(default_factory=list)


ORGANIZER_INSTRUCTION = """你是电力规范内容整理子智能体。调用方会把用户问题和检索到的条款原文交给你。

你的任务：针对用户问题，把条款原文整理成面向该问题的简洁内容，只输出 JSON。

针对每一条输入条款各输出一个条目，逐条整理，不要合并、不要遗漏。调用方已经完成相关性筛选，你的职责是把每一条都整理成面向用户问题的内容。

输出格式（不要输出 Markdown 代码块，不要输出解释）：
{"clauses":[{"citation_key":"原样返回","clause_no":"本条款展示编号","clause_title":"本条款展示标题","body":"整理后的条款内容","scope":"适用范围，原文没有就留空"}],"notes":"可选的一句话说明"}

整理规则：
1. 只依据给定原文。body 写用户真正关心的内容，删除与问题无关的表格行、相邻章节和重复表述。
原文表格很长时只保留用户问题涉及的项目行与必要表头。
2. 同一项目内的多个条件分支必须完整保留。例如某个判据按容量分档
（8MVA 以下、8MVA 及以上、120MVA 及以上）或按电压等级分档时，
与用户问题相关的每个分档都要写出，不能只留其中一档。
3. 可以改写、合并、分条、重新组织语言，使内容简短易读；
但不得改变数值、单位、条件、期限、判据的含义，不得新增原文没有的数字、页码或结论。
4. clause_no 只允许修复字符级错误（全角数字、多余空格、错误的标点）。禁止重新编号，禁止改变数字含义。
5. clause_title 必须是原文出现过的文字；原文标题缺失或标题实际是页码、表格标记时，
从原文中挑出真正的章节标题；确实没有就留空。
6. 每条输入条款都必须出现在输出中，citation_key 必须与输入完全一致。缺少任何一条都视为输出不完整。
7. 某条条款确实不含用户问题的信息时，body 写该条款能提供的相关信息；
完全没有任何相关信息时，body 写「本条款未涉及该问题」。
8. body 中不要出现省略号（如 ... 或 ……），省略内容通过少写内容表达。"""

REVIEWER_INSTRUCTION = """你是电力规范内容审查子智能体，负责对抗式审查另一智能体整理的规范结论。
调用方会同时给你原始条款原文和待审结论。

审查项：
1. 编造：结论中的数字、单位、条件、期限、判据是否在原文中出现。原文没有依据的一律判定失败。
2. 事实错误：结论是否改变了原文含义，例如把上限说成下限、把条件当成结论、把可选说成必须、混淆电压等级或设备类型。
3. 遗漏：原文中直接回答用户问题的关键信息是否被漏掉或被弱化。
4. 无关内容：是否把与用户问题无关的内容放进结论。
5. 引用：每条结论的 citation_key 是否对应真实存在的原文条款。

只输出一个 JSON 对象，不要输出 Markdown 或解释：
{"verdict":"pass" 或 "fail","issues":[{"kind":"编造|事实错误|遗漏|无关内容|引用",
"detail":"具体问题与涉及的文字","citation":"相关条款号"}]}

判定规则：
- 结论忠实、没有漏掉直接回答问题的关键信息时 verdict 为 pass，此时 issues 为空数组。
- 只要有编造或事实错误，verdict 必须为 fail。
- 遗漏只有在关键信息确实存在于原文、且用户问题直接需要它时才判定 fail。
- 不确定时按 fail 处理，并在 detail 说明不确定的原因。"""

# 需求规格说明书 4.1 规定的卡片字段顺序，代码拥有格式，模型只提供字段值。
SRS_CARD_FIELDS: tuple[tuple[str, str], ...] = (
    ("standard_name", "规范名称"),
    ("standard_no", "标准编号"),
    ("clause_no", "条款编号"),
    ("clause_title", "条款标题"),
    ("clause_body", "条款正文"),
    ("scope", "适用范围"),
    ("standard_status", "规范状态"),
    ("replaced_by", "替代规范"),
    ("relevance", "相关度"),
    ("source_page", "源文件页码"),
    ("source_page_ref", "条款在源文件中的位置"),
)
CONDITIONAL_SRS_FIELDS = frozenset({"replaced_by"})

# 需求规格说明书 3.1 规定的思考链步骤。
SRS_THINKING_STEPS: tuple[str, ...] = (
    "解析自然语言查询",
    "检索规程标准知识库",
    "定位相关条款",
    "生成检索结果",
)


def build_organizer_message(question: str, evidences: Sequence[Any], feedback: list[str] | None = None) -> str:
    """构造整理任务的输入，只提供问题与受限证据。"""

    lines = [ORGANIZER_INSTRUCTION, "", "## 用户问题", question, "", "## 检索到的规范条款原文"]
    for evidence in evidences:
        body = str(getattr(evidence, "clause_body", "") or "")[:MAX_EVIDENCE_BODY_CHARS]
        lines.extend(
            [
                f"### citation_key: {_citation_key(evidence)}",
                f"标准：{getattr(evidence, 'standard_no', '')}",
                f"当前条款编号：{display_clause_no(evidence)}",
                f"当前条款标题：{display_clause_title(evidence)}",
                f"原文：\n{body}",
                "",
            ]
        )
    if feedback:
        lines.extend(["## 上一轮审查未通过的原因，请修正后重新输出 JSON", *[f"- {item}" for item in feedback], ""])
    return "\n".join(lines)


def build_reviewer_message(question: str, evidences: Sequence[Any], answer: str) -> str:
    """构造审查任务的输入，同时给出原文与待审结论。"""

    lines = [REVIEWER_INSTRUCTION, "", "## 用户问题", question, "", "## 原始条款原文"]
    for evidence in evidences:
        body = str(getattr(evidence, "clause_body", "") or "")[:MAX_EVIDENCE_BODY_CHARS]
        lines.extend([f"### citation_key: {_citation_key(evidence)}", body, ""])
    lines.extend(["## 待审结论", answer, "", "请按你的审查规则输出 JSON 判定。"])
    return "\n".join(lines)


def render_draft_for_review(draft: OrganizedDraft) -> str:
    """把结构化草稿渲染成审查者容易比对的文本。"""

    lines: list[str] = []
    for clause in draft.clauses:
        lines.append(f"### {clause.citation_key}")
        if clause.clause_no:
            lines.append(f"条款编号：{clause.clause_no}")
        if clause.clause_title:
            lines.append(f"条款标题：{clause.clause_title}")
        lines.append(f"整理内容：{clause.body}")
        if clause.scope:
            lines.append(f"适用范围：{clause.scope}")
        lines.append("")
    if draft.notes:
        lines.extend([f"说明：{draft.notes}", ""])
    return "\n".join(lines).strip()


def missing_citation_keys(draft: OrganizedDraft, evidences: Sequence[Any]) -> list[str]:
    """返回草稿中缺失的输入条款，用于要求整理者逐条覆盖。"""

    produced = {clause.citation_key for clause in draft.clauses}
    return [_citation_key(item) for item in evidences if _citation_key(item) not in produced]


def draft_content_text(draft: OrganizedDraft) -> str:
    """返回草稿的内容字段文本，用于数字前置门。

    引用键和条款编号属于标识符，其中的数字不参与编造判定。
    """

    parts: list[str] = []
    for clause in draft.clauses:
        parts.extend([clause.body, clause.clause_title or "", clause.scope or ""])
    if draft.notes:
        parts.append(draft.notes)
    return "\n".join(part for part in parts if part)


def parse_organized_draft(raw: str | dict[str, Any]) -> OrganizedDraft:
    """解析整理子智能体的结构化输出，非法输出抛错以触发重试。"""

    payload = raw if isinstance(raw, dict) else _loads(raw)
    try:
        return OrganizedDraft.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("整理结果不符合结构化契约") from exc


def parse_review_verdict(raw: str | dict[str, Any]) -> ReviewVerdict:
    """解析审查子智能体的判定，无法解析时按不通过处理。"""

    try:
        payload = raw if isinstance(raw, dict) else _loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"电力规范审查结果无法解析: {exc}")
        return ReviewVerdict(
            passed=False,
            issues=[ReviewIssue(kind="格式", detail=f"审查结果不是合法 JSON：{str(raw)[:200]}")],
        )
    verdict = str(payload.get("verdict") or "").strip().lower()
    issues: list[ReviewIssue] = []
    raw_issues = payload.get("issues")
    if isinstance(raw_issues, list):
        for item in raw_issues:
            if isinstance(item, dict):
                issues.append(
                    ReviewIssue(
                        kind=str(item.get("kind") or "未分类"),
                        detail=str(item.get("detail") or ""),
                        citation=item.get("citation"),
                    )
                )
            elif str(item).strip():
                issues.append(ReviewIssue(detail=str(item).strip()))
    passed = verdict == REVIEW_PASS and not issues
    return ReviewVerdict(passed=passed, issues=issues)


def ungrounded_numbers(answer: str, sources: Sequence[str]) -> list[str]:
    """返回结论中无法在来源里找到的数字，作为廉价的编造前置门。

    按完整数字词元比较，避免 `12` 被 `2021` 误判为有依据。数字之间的空白不参与归一化，
    否则 `H2 30` 会被拼成 `230` 而产生假报。
    """

    grounded = {number for source in sources for number in NUMBER_PATTERN.findall(_normalize_digits(source))}
    ungrounded: list[str] = []
    for number in NUMBER_PATTERN.findall(_normalize_digits(CITATION_PATTERN.sub("", answer))):
        if number not in grounded and number not in ungrounded:
            ungrounded.append(number)
    return ungrounded


async def organize_and_review(
    *,
    question: str,
    evidences: Sequence[Any],
    call_subagent: SubagentCaller,
    tool_call_id: str,
    max_attempts: int = 3,
) -> OrganizedAnswer:
    """整理答案并由审查子智能体复核；失败时带审查意见重试。"""

    threads = _Threads()
    sources = _grounding_sources(question, evidences)
    verdict: ReviewVerdict | None = None
    draft: OrganizedDraft | None = None
    ungrounded: list[str] = []
    attempts = 0
    feedback: list[str] | None = None

    for attempt in range(1, max(1, max_attempts) + 1):
        attempts = attempt
        try:
            organizer = await call_subagent(
                slug=ORGANIZER_SLUG,
                message=build_organizer_message(question, evidences, feedback),
                tool_call_id=f"{tool_call_id}#organize#{attempt}",
                continue_thread_id=threads.organizer,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"电力规范整理子智能体失败: {exc}")
            return OrganizedAnswer(status="rejected", attempts=attempts)
        threads.organizer = organizer.thread_id or threads.organizer

        try:
            draft = parse_organized_draft(organizer.output)
        except ValueError as exc:
            logger.warning(f"电力规范整理结果无法解析: {exc}")
            feedback = ["整理结果不是要求的 JSON 结构，请严格按 Outputs 格式重新输出。"]
            continue
        if not draft.clauses:
            return OrganizedAnswer(status="rejected", draft=draft, attempts=attempt)

        missing = missing_citation_keys(draft, evidences)
        if missing:
            logger.warning(f"电力规范整理结果缺少条款，要求重试: {missing}")
            feedback = [
                "输出不完整，缺少以下 citation_key 对应的条款，请为每一条输入条款各输出一个条目：" + ", ".join(missing)
            ]
            verdict = ReviewVerdict(
                passed=False,
                issues=[ReviewIssue(kind="遗漏", detail=f"缺少条款：{', '.join(missing)}")],
            )
            continue

        review_text = render_draft_for_review(draft)
        ungrounded = ungrounded_numbers(draft_content_text(draft), sources)
        if ungrounded:
            feedback = [f"整理内容中的数字 {', '.join(ungrounded)} 在原文中没有依据，请删除或改正。"]
            verdict = ReviewVerdict(
                passed=False,
                issues=[ReviewIssue(kind="编造", detail=f"数字缺少原文依据：{', '.join(ungrounded)}")],
            )
            continue

        try:
            reviewer = await call_subagent(
                slug=REVIEWER_SLUG,
                message=build_reviewer_message(question, evidences, review_text),
                tool_call_id=f"{tool_call_id}#review#{attempt}",
                continue_thread_id=threads.reviewer,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"电力规范审查子智能体失败: {exc}")
            return OrganizedAnswer(status="rejected", draft=draft, attempts=attempts)
        threads.reviewer = reviewer.thread_id or threads.reviewer
        verdict = parse_review_verdict(reviewer.output)

        if verdict.passed:
            return OrganizedAnswer(
                status="published",
                draft=draft,
                attempts=attempt,
                last_verdict=verdict,
            )
        feedback = [f"{issue.kind}：{issue.detail}" for issue in verdict.issues] or ["审查未通过，请依据原文重新整理。"]

    return OrganizedAnswer(
        status="needs_user_confirmation",
        draft=draft,
        attempts=attempts,
        last_verdict=verdict,
        ungrounded_numbers=ungrounded,
    )


def build_subagent_caller(context: Any) -> SubagentCaller:
    """构造在当前工具调用内启动并等待子智能体的入口。"""

    uid = str(getattr(context, "uid", "") or "").strip()
    parent_run_id = str(getattr(context, "run_id", "") or "").strip()
    model_spec = str(getattr(context, "model", "") or "").strip() or None

    async def call_subagent(
        *,
        slug: str,
        message: str,
        tool_call_id: str,
        continue_thread_id: str | None = None,
    ) -> SubagentReply:
        from yuxi.repositories.agent_repository import AgentRepository
        from yuxi.services.agent_run_service import AgentRunWaitTimeout, await_agent_run_result
        from yuxi.services.input_message_service import build_chat_input_message
        from yuxi.services.subagent_run_service import SubagentRunService
        from yuxi.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as db:
            repository = AgentRepository(db)
            agent_item = await repository.get_by_slug(slug)
            if agent_item is None:
                logger.warning(f"电力规范子智能体 {slug} 不存在，跳过整理")
                return SubagentReply(output="")
            started = await SubagentRunService(db).start(
                uid=uid,
                created_by_run_id=parent_run_id,
                agent_item=agent_item,
                input_message=build_chat_input_message(message),
                tool_call_id=tool_call_id,
                requested_thread_id=continue_thread_id,
                model_spec=model_spec,
            )
        run_id = started.run.id
        thread_id = started.run.conversation_thread_id
        try:
            result = await await_agent_run_result(run_id=run_id, current_uid=uid)
        except AgentRunWaitTimeout as exc:
            logger.warning(f"电力规范子智能体 {slug} 等待超时: {exc}")
            return SubagentReply(output="", thread_id=thread_id, run_id=run_id)
        return SubagentReply(output=str(result.get("output") or ""), thread_id=thread_id, run_id=run_id)

    return call_subagent


async def produce_power_spec_answer(
    *,
    query: PowerSpecQuery,
    evidences: Sequence[SpecClauseEvidence],
    call_subagent: SubagentCaller,
    tool_call_id: str | None,
    ask_acceptance: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """产出最终答案载荷：模型整理结果或程序生成的固定卡片。

    模型不可用、草稿缺失或用户不接受未通过审查的结论时，回退到固定卡片。答案来源
    判断只存在于这一处，调用方只负责转发结果。
    """

    fallback = format_power_spec_results(query.query, evidences)
    if not POWER_SPEC_ORGANIZE_ENABLED or not tool_call_id:
        return _fixed_card_payload(fallback)

    organized = await organize_and_review(
        question=query.query,
        evidences=evidences,
        call_subagent=call_subagent,
        tool_call_id=tool_call_id,
        max_attempts=POWER_SPEC_ORGANIZE_MAX_ATTEMPTS,
    )
    if organized.status == "rejected" or organized.draft is None:
        return _fixed_card_payload(fallback)

    rendered = apply_organized_clauses(query.query, evidences, organized.draft.clauses)
    if organized.status == "published":
        return {
            "items": rendered["items"],
            "answer_markdown": rendered["answer_markdown"],
            "answer_source": ANSWER_SOURCE_MODEL,
            "answer_verified": True,
        }

    if not parse_acceptance(ask_acceptance(build_acceptance_payload(query.query, organized))):
        logger.info("用户不接受未通过审查的电力规范答案，回退固定卡片")
        return _fixed_card_payload(fallback)
    return {
        "items": rendered["items"],
        "answer_markdown": rendered["answer_markdown"],
        "answer_source": ANSWER_SOURCE_MODEL,
        "answer_verified": False,
    }


def build_acceptance_payload(query: str, organized: OrganizedAnswer) -> dict[str, Any]:
    """构造接受询问卡片，展示审查未通过的具体原因。"""

    verdict = organized.last_verdict
    details = [f"{issue.kind}：{issue.detail}" for issue in (verdict.issues if verdict else [])][:3]
    if organized.ungrounded_numbers:
        details.append(f"数字缺少原文依据：{', '.join(organized.ungrounded_numbers)}")
    reasons = "；".join(details) or "审查子智能体未通过该结论"
    return {
        "source": "power_spec_answer_acceptance",
        "questions": [
            {
                "question_id": ACCEPTANCE_QUESTION_ID,
                "question": (
                    f"围绕「{query}」整理的答案连续 {organized.attempts} 次未通过审查（{reasons}）。"
                    "是否接受当前答案？不接受将改为输出程序生成的规范卡片。"
                ),
                "options": [
                    {"label": "接受当前答案", "value": "accept"},
                    {"label": "不接受，改为规范卡片", "value": "reject"},
                ],
                "multi_select": False,
                "allow_other": False,
            }
        ],
        "attempts": organized.attempts,
    }


def parse_acceptance(value: Any) -> bool:
    """解析接受询问结果，只有明确接受才发布未通过校验的答案。"""

    if isinstance(value, dict):
        if ACCEPTANCE_QUESTION_ID in value:
            return parse_acceptance(value[ACCEPTANCE_QUESTION_ID])
        selected = value.get("selected")
        if isinstance(selected, (list, tuple)) and selected:
            return parse_acceptance(selected[0])
        return False
    if isinstance(value, (list, tuple)):
        return any(parse_acceptance(item) for item in value)
    if isinstance(value, str):
        return value.strip().lower() in {"accept", "accepted"} or value.strip() in {"接受当前答案", "接受"}
    return False


def format_power_spec_results(query: str, evidence: Sequence[SpecClauseEvidence]) -> dict:
    """返回规范卡片数据和按需求书字段顺序展示的 Markdown。"""

    items = [_format_item(query, item) for item in evidence]
    return {"items": items, "answer_markdown": render_srs_cards(query, items)}


def render_srs_cards(query: str, items: Sequence[dict]) -> str:
    """按需求书 4.1 的字段与顺序渲染卡片，正文独立成块以便表格显示。"""

    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        lines.append(f"### 规范卡片 {index} · {item['standard_no']} 条款 {item['clause_no']}")
        lines.append("")
        for key, label in SRS_CARD_FIELDS:
            value = item.get(key)
            if key in CONDITIONAL_SRS_FIELDS and not value:
                continue
            if key == "clause_body":
                lines.extend([f"**{label}**", "", str(value or "未提供"), ""])
                continue
            lines.append(f"- **{label}**：{value if value else '未提供'}")
        lines.extend([f"- **引用**：[source:{item['citation_key']}]", ""])
        if item.get("relevance_label"):
            lines.append(f"> {item['relevance_label']}；查询：{query}")
            lines.append("")
    return "\n".join(lines).rstrip()


def render_thinking_steps(items: Sequence[dict], *, elapsed_seconds: float | None = None) -> str:
    """渲染需求书 3 规定的思考链，正文展示完成后折叠为一行。"""

    lines = ["已思考" + (f"（用时 {elapsed_seconds:.1f} 秒）" if elapsed_seconds is not None else "")]
    lines.append("")
    for step in SRS_THINKING_STEPS:
        lines.append(f"- ✓ {step}")
    if items:
        lines.extend(
            [
                "",
                f"正在生成规范卡片……共匹配到 {len(items)} 条相关条款，按相关度排序完成。"
                "准备展示规范名称、标准编号、条款编号、条款正文、适用范围等完整字段。",
            ]
        )
    return "\n".join(lines)


def apply_organized_clauses(
    query: str,
    evidence: Sequence[SpecClauseEvidence],
    clauses: Sequence[Any],
) -> dict:
    """把整理子智能体给出的字段值套进代码渲染的规范卡片。

    只发布整理者保留的条款，因此每张卡片的内容都经过同一套整理与审查流程，不会出现
    「部分卡片已整理、其余退回原文」的混合结果。模型只提供字段值；字段顺序、标签、
    高亮和引用由代码决定，格式不随模型输出漂移。
    """

    by_citation = {item.citation_key: item for item in evidence}
    items: list[dict] = []
    used: set[str] = set()
    for clause in clauses:
        citation_key = str(getattr(clause, "citation_key", "") or "")
        source = by_citation.get(citation_key)
        if source is None or citation_key in used:
            continue
        used.add(citation_key)
        item = _format_item(query, source)
        body = str(getattr(clause, "body", "") or "").strip()
        if body:
            item["clause_body"] = _highlight(body, _query_terms(query))
        title = str(getattr(clause, "clause_title", "") or "").strip()
        if title:
            item["clause_title"] = title
        clause_no = str(getattr(clause, "clause_no", "") or "").strip()
        if clause_no:
            item["clause_no"] = clause_no
        scope = str(getattr(clause, "scope", "") or "").strip()
        if scope:
            item["scope"] = scope
        items.append(item)
    return {"items": items, "answer_markdown": render_srs_cards(query, items)}


def _format_item(query: str, item: SpecClauseEvidence) -> dict:
    """渲染单张卡片，字段优先使用条款字段校对结果。"""

    score = round(max(float(item.rerank_score or 0.0), 0.0) * 100, 2)
    return {
        "standard_name": item.standard_name,
        "standard_no": item.standard_no,
        "standard_version": item.version,
        "standard_status": item.status,
        "replaced_by": item.replaced_by if item.status == "废止" else None,
        "clause_no": display_clause_no(item),
        "clause_title": display_clause_title(item),
        "clause_body": _highlight(item.clause_body, _query_terms(query)),
        "scope": item.scope,
        "relevance": f"{score}（{'高度相关' if score >= 80 else '相关'}）",
        "relevance_score": score,
        "relevance_label": "高度相关" if score >= 80 else "相关",
        "source_file": item.source_file,
        "source_page": item.source_page,
        "source_page_ref": item.source_page_ref,
        "source_page_url": item.metadata.get("source_page_url"),
        "matched_terms": [term for term in _query_terms(query) if term in item.clause_body],
        "citation_key": item.citation_key,
    }


def _fixed_card_payload(formatted: dict[str, Any]) -> dict[str, Any]:
    """构造固定卡片结果，作为模型不可用或校验失败时的降级输出。"""

    return {
        "items": formatted["items"],
        "answer_markdown": formatted["answer_markdown"],
        "answer_source": ANSWER_SOURCE_FIXED_CARD,
        "answer_verified": True,
    }


def _citation_key(evidence: Any) -> str:
    key = getattr(evidence, "citation_key", None)
    if key:
        return str(key)
    return f"{getattr(evidence, 'document_id', '')}#{getattr(evidence, 'clause_no', '')}"


def _grounding_sources(question: str, evidences: Sequence[Any]) -> list[str]:
    """返回允许结论引用的来源文本，包含原文、标准信息和用户问题。"""

    sources = [question]
    for evidence in evidences:
        sources.extend(
            [
                str(getattr(evidence, "clause_body", "") or ""),
                str(getattr(evidence, "clause_title", "") or ""),
                str(getattr(evidence, "standard_no", "") or ""),
                str(getattr(evidence, "version", "") or ""),
                str(getattr(evidence, "clause_no", "") or ""),
            ]
        )
    return [source for source in sources if source]


def _normalize_digits(value: str) -> str:
    """把全角数字归一化，使词元比较不受排版差异影响。"""

    return str(value).translate(_FULLWIDTH_DIGITS)


def _query_terms(query: str) -> list[str]:
    return sorted(
        {term for term in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?", query) if term.strip()},
        key=len,
        reverse=True,
    )


def _highlight(text: str, terms: list[str]) -> str:
    escaped = html.escape(text, quote=False)
    for term in terms:
        escaped_term = html.escape(term, quote=False)
        escaped = re.sub(re.escape(escaped_term), lambda match: f"<mark>{match.group(0)}</mark>", escaped)
    return escaped


def _loads(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    payload = json_repair.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("审查结果不是 JSON 对象")
    return payload
