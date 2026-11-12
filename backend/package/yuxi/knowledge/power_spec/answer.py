from __future__ import annotations

import html
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from yuxi.knowledge.power_spec.contracts import PowerSpecQuery, SpecClauseEvidence, citation_key_of
from yuxi.knowledge.power_spec.model_output import parse_model_json
from yuxi.knowledge.power_spec.retrieval import (
    display_clause_no,
    display_clause_title,
    order_graph_evidence_first,
)
from yuxi.utils import logger


ORGANIZER_SLUG = "power-spec-organizer"
REVIEWER_SLUG = "power-spec-reviewer"
MAX_EVIDENCE_BODY_CHARS = 6000
CITATION_PATTERN = re.compile(r"\[source:[^\]]+\]")
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９．", "0123456789.")

ANSWER_SOURCE_MODEL = "model"
ANSWER_SOURCE_FIXED_CARD = "fixed_card"

POWER_SPEC_ORGANIZE_ENABLED = os.getenv("POWER_SPEC_ORGANIZE_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
}


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
    ) -> Awaitable[SubagentReply]: ...


ComposeCall = Callable[[str], Awaitable[str]]


class ReviewIssue(BaseModel):
    """审查子智能体给出的一条意见。"""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(default="未分类")
    detail: str = Field(default="")
    citation: str | None = None


class OrganizedClause(BaseModel):
    """整理子智能体给出的单条条款字段值。格式由代码决定，这里只提供值。"""

    model_config = ConfigDict(frozen=True)

    citation_key: str = Field(min_length=1)
    body: str = Field(min_length=1)
    clause_no: str | None = None


class QuestionAnswer(BaseModel):
    """整理子智能体针对一个子问题给出的简短结论。"""

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    citation_keys: list[str] = Field(default_factory=list)


class OrganizedDraft(BaseModel):
    """整理子智能体的结构化输出。"""

    model_config = ConfigDict(frozen=True)

    clauses: list[OrganizedClause] = Field(default_factory=list)
    answers: list[QuestionAnswer] = Field(default_factory=list)
    notes: str | None = None


class OrganizedAnswer(BaseModel):
    """整理、审查与最终合并的结论。"""

    model_config = ConfigDict(frozen=True)

    draft: OrganizedDraft
    issues: list[ReviewIssue] = Field(default_factory=list)


ORGANIZER_INSTRUCTION = """你是电力规范内容整理子智能体。
调用方会把用户问题、本次问题的关注维度和检索到的条款原文交给你。

你的任务：把条款原文中与用户问题相关的内容整理出来，只输出 JSON。

针对每一条输入条款各输出一个条目，逐条整理，不要合并、不要遗漏。
调用方已经完成相关性筛选，你的职责是把每一条都整理成面向用户问题的内容。

输出格式：整段回答必须是且只是一个 JSON 对象，以 { 开头、以 } 结尾。
不要输出分析、推理、说明或英文叙述，不要用 Markdown 代码块包裹。
{"clauses":[{"citation_key":"原样返回","clause_no":"本条款展示编号","body":"整理后的条款内容"}],"answers":[{"question":"子问题原文","answer":"仅依据原文的简短结论","citation_keys":["file-1#6.1"]}],"notes":"可选的一句话说明"}

整理规则：
1. 关注维度由调用方给出，直接使用，不要自行判断、收窄或扩大维度。
2. 只依据给定原文。body 要列出该条款中与用户问题和关注维度有关的内容；
优先保留原文表述、数值、单位、分档和表格结构；表格命中时连表头一起保留。
3. 拿不准是否相关时保留而不是删除。与问题相近、能帮助用户判断的内容也要列出；
不能因为「不完全对应问题」就把条款整理成空内容。
4. body 不得为空，也不得只写「本条款未涉及该问题」这类占位说明；
确实找不到直接相关内容时，列出该条款中与问题最接近的原文内容。
5. 同一项目内的多个条件分支必须完整保留。例如某个判据按容量分档
（8MVA 以下、8MVA 及以上、120MVA 及以上）或按电压等级分档时，
与用户问题相关的每个分档都要写出，不能只留其中一档。
6. 允许为可读性改写、分条和重新组织语言，但不得改变数值、单位、条件、
期限、判据的含义，不得新增原文没有的数字、页码或结论。
7. clause_no 只允许修复字符级错误（全角数字、多余空格、错误的标点）。
禁止重新编号，禁止改变数字含义。
8. 每条输入条款都必须出现在输出中，citation_key 必须与输入完全一致。
缺少任何一条都视为输出不完整。
9. body 中不要出现省略号（如 ... 或 ……），省略内容通过少写内容表达。
10. 调用方给出多个子问题时，answers 必须为每个子问题各输出一条；
answer 只能使用给定原文中的数值、条件和结论，不得新增原文没有的内容；
确实无法从给定条款回答时写「未在给定条款中找到依据」。
只有一个问题时 answers 为空数组；不要凭空补写问题或结论。"""

REVIEWER_INSTRUCTION = """你是电力规范内容的忠实性审查子智能体，负责核对另一智能体的整理内容是否忠实于给定原文。
调用方会同时给你原始条款原文和待审结论。

你的唯一职责：逐条核对整理内容中的每一句话是否能在对应原文中找到依据。
你不判断条款是否切题、是否满足用户问题、是否覆盖足够多的条款；这些不属于你的职责。

审查项：
1. 编造：结论中的数字、单位、条件、期限、判据、设备名称、电压等级是否在对应原文中出现。
2. 语义改变：结论是否改变了原文含义，例如把上限说成下限、把条件当成结论、把可选说成必须、
   把「不宜」说成「必须」，或删掉了会改变结论的适用条件、例外和单位。
3. 引用：每条结论的 citation_key 是否对应输入中真实存在的原文条款。

禁止：
- 不要因为某条款「与用户问题无关」「没有直接回答问题」「不是最相关」而提出删除或弱化意见。
- 不要评价条款取舍、覆盖范围或答案完整性。
- 不要要求补充原文没有的内容。

输出要求：只输出一个 JSON 对象，不要输出 Markdown 或解释：
{"issues":[{"kind":"编造|语义改变|引用","detail":"问题、涉及的文字与原文的正确说法","citation":"相关条款号"}]}

要求：
- 只提出你能在对应原文中找到依据的问题；没有问题时 issues 输出空数组。
- detail 要写清「哪里错了、原文的正确说法是什么」。
- 不确定时不要提出，避免把原文的合理概括当成错误。"""

# 审查子智能体的职责是忠实性核验，不判断条款是否切题。模型仍可能输出这些
# 超出职责的意见；在进入最终合并前丢弃，避免它们被当作删除条款的依据。
OUT_OF_SCOPE_REVIEW_KINDS = frozenset({"无关内容", "相关性", "切题", "覆盖不足"})

COMPOSE_INSTRUCTION = """你是电力规范答案的最终合并者。调用方会给你用户问题、关注维度、
整理者已整理好的条款内容，以及审查者对这份内容的忠实性意见。

你的任务：以整理内容为基础，只修正审查意见指出、且你判断确实成立的原文不一致；只输出 JSON。

输出格式：整段回答必须是且只是一个 JSON 对象，以 { 开头、以 } 结尾。
不要输出分析、推理、说明或英文叙述，不要用 Markdown 代码块包裹。
{"clauses":[{"citation_key":"原样返回","clause_no":"展示编号","body":"最终条款内容"}],"answers":[{"question":"子问题原文","answer":"最终简短结论","citation_keys":["file-1#6.1"]}],"notes":"可选的一句话说明"}

规则：
1. 逐条输出，citation_key 必须与输入完全一致，不要新增或删除条款。
2. 只修改审查意见指出、且你判断确实成立的忠实性问题（数值、单位、条件、期限、判据、编号、语义）。
3. 审查意见没提到的内容保持原样。
4. 不得因为某条款「与用户问题无关」「没有直接回答问题」「不是最相关」而删除该条款或删减其正文；
条款取舍由上游检索决定，不在本步骤处理。
5. 审查意见与整理内容冲突、或看起来是审查自身理解有误时，保留整理内容，不要跟着改错。
6. 不得新增整理内容和审查意见都没有的数字、页码或结论，不得为了「看起来完整」补写内容。
7. body 不得为空，也不得只写「本条款未涉及该问题」这类占位说明。
8. body 中不要出现省略号（如 ... 或 ……）。
9. answers 必须保留输入中的每个子问题，顺序不变；
只允许按审查意见修正引用原文的数值、条件或表述，
不得删除问题、合并问题或补写原文没有的结论。"""

# 最新需求规格说明书规定的卡片字段与顺序，代码拥有格式，模型只提供字段值。
SRS_CARD_FIELDS: tuple[tuple[str, str], ...] = (
    ("standard_name", "规范名称"),
    ("standard_no", "标准编号"),
    ("clause_no", "条款编号"),
    ("clause_body", "条款正文"),
)
# 前端按该标记在每张卡片末尾定位原始 PDF；URL 来自知识库文件的 MinIO 地址。
PDF_MARKER = "[[PDF:{url}]]"


def build_organizer_message(
    question: str,
    evidences: Sequence[Any],
    dimensions: Sequence[str] = (),
    sub_questions: Sequence[str] = (),
) -> str:
    """构造整理任务的输入，只提供问题、上游已识别的维度与受限证据。"""

    dimension_text = "、".join(item for item in dimensions if str(item).strip()) or "未识别到明确维度"
    lines = [
        ORGANIZER_INSTRUCTION,
        "",
        "## 用户问题",
        question,
        "",
    ]
    clean_sub_questions = [str(item).strip() for item in sub_questions if str(item).strip()]
    if clean_sub_questions:
        lines.extend(["## 用户一次提出的多个问题（逐条覆盖，不要只回答第一个）"])
        lines.extend(f"{index}. {item}" for index, item in enumerate(clean_sub_questions, start=1))
        lines.append("")
    lines.extend(
        [
            "## 本次问题的关注维度（由上游意图识别给出，不要自行判断）",
            dimension_text,
            "",
            "## 检索到的规范条款原文",
        ]
    )
    for evidence in evidences:
        body = str(getattr(evidence, "clause_body", "") or "")[:MAX_EVIDENCE_BODY_CHARS]
        lines.extend(
            [
                f"### citation_key: {citation_key_of(evidence)}",
                f"标准：{getattr(evidence, 'standard_no', '')}",
                # 标准名称必须给出：DL/T 408（发电厂和变电站电气部分）与 DL/T 409（电力线路部分）
                # 的验电/接地条款几乎同文，只给编号时模型无法判断该引哪一部。
                f"标准名称：{getattr(evidence, 'standard_name', '')}",
                f"当前条款编号：{display_clause_no(evidence)}",
                f"当前条款标题：{display_clause_title(evidence)}",
                f"原文：\n{body}",
                "",
            ]
        )
    return "\n".join(lines)


def build_reviewer_message(question: str, evidences: Sequence[Any], answer: str) -> str:
    """构造审查任务的输入，同时给出原文与待审结论。"""

    lines = [REVIEWER_INSTRUCTION, "", "## 用户问题", question, "", "## 原始条款原文"]
    for evidence in evidences:
        body = str(getattr(evidence, "clause_body", "") or "")[:MAX_EVIDENCE_BODY_CHARS]
        lines.extend(
            [
                f"### citation_key: {citation_key_of(evidence)}",
                f"出处：{getattr(evidence, 'standard_no', '')} "
                f"{getattr(evidence, 'standard_name', '')} {display_clause_no(evidence)}",
                body,
                "",
            ]
        )
    lines.extend(["## 待审结论", answer, "", "请按你的审查规则输出审查意见 JSON。"])
    return "\n".join(lines)


def build_compose_message(
    question: str,
    dimensions: Sequence[str],
    draft: OrganizedDraft,
    issues: Sequence[ReviewIssue],
    sub_questions: Sequence[str] = (),
) -> str:
    """构造最终整理任务的输入：整理内容与审查意见。"""

    dimension_text = "、".join(item for item in dimensions if str(item).strip()) or "未识别到明确维度"
    lines = [
        COMPOSE_INSTRUCTION,
        "",
        "## 用户问题",
        question,
        "",
    ]
    clean_sub_questions = [str(item).strip() for item in sub_questions if str(item).strip()]
    if clean_sub_questions:
        lines.extend(["## 用户一次提出的多个问题（必须逐条保留 answers）"])
        lines.extend(f"{index}. {item}" for index, item in enumerate(clean_sub_questions, start=1))
        lines.append("")
    lines.extend(
        [
            "## 本次问题的关注维度",
            dimension_text,
            "",
            "## 整理者输出的条款内容",
        ]
    )
    for clause in draft.clauses:
        lines.append(f"### citation_key: {clause.citation_key}")
        if clause.clause_no:
            lines.append(f"条款编号：{clause.clause_no}")
        lines.append(f"整理内容：{clause.body}")
        lines.append("")
    if draft.answers:
        lines.append("## 整理者给出的问题回答")
        for item in draft.answers:
            lines.append(f"### 问题：{item.question}")
            lines.append(f"回答：{item.answer}")
            if item.citation_keys:
                lines.append(f"引用：{', '.join(item.citation_keys)}")
            lines.append("")
    lines.append("## 审查意见")
    if issues:
        for index, issue in enumerate(issues, start=1):
            citation = f"（{issue.citation}）" if issue.citation else ""
            lines.append(f"{index}. [{issue.kind}] {issue.detail}{citation}")
    else:
        lines.append("审查未提出问题。")
    if draft.notes:
        lines.extend(["", f"整理者说明：{draft.notes}"])
    return "\n".join(lines)


def render_draft_for_review(draft: OrganizedDraft) -> str:
    """把结构化草稿渲染成审查者容易比对的文本。"""

    lines: list[str] = []
    for clause in draft.clauses:
        lines.append(f"### {clause.citation_key}")
        if clause.clause_no:
            lines.append(f"条款编号：{clause.clause_no}")
        lines.append(f"整理内容：{clause.body}")
        lines.append("")
    if draft.answers:
        lines.append("## 问题回答")
        for item in draft.answers:
            lines.append(f"问题：{item.question}")
            lines.append(f"回答：{item.answer}")
            if item.citation_keys:
                lines.append(f"引用：{', '.join(item.citation_keys)}")
            lines.append("")
    if draft.notes:
        lines.extend([f"说明：{draft.notes}", ""])
    return "\n".join(lines).strip()


def draft_content_text(draft: OrganizedDraft) -> str:
    """返回草稿的内容字段文本，用于数字落地检查。

    引用键和条款编号属于标识符，其中的数字不参与编造判定。
    """

    parts: list[str] = []
    for clause in draft.clauses:
        parts.append(clause.body)
    for item in draft.answers:
        parts.append(item.answer)
    if draft.notes:
        parts.append(draft.notes)
    return "\n".join(part for part in parts if part)


# 提示词禁止的占位说明，等价写法视为没有内容。
_PLACEHOLDER_BODIES = frozenset(
    {
        "本条款未涉及该问题",
        "未涉及该问题",
        "本条款不涉及该问题",
        "不涉及该问题",
        "无相关内容",
        "无相关信息",
        "未提及",
        "不涉及",
        "无",
    }
)


def placeholder_clause_keys(draft: OrganizedDraft) -> list[str]:
    """返回只写了占位说明、没有实际内容的条款键。

    整理者被要求列出与问题相关或最接近的原文内容，占位说明会让规范卡片没有可读内容，
    因此作为一条审查意见交给最终整理者改写。
    """

    keys: list[str] = []
    for clause in draft.clauses:
        normalized = re.sub(r"[\s。.]", "", clause.body)
        if not normalized or normalized in _PLACEHOLDER_BODIES:
            keys.append(clause.citation_key)
    return keys


def placeholder_issues(draft: OrganizedDraft) -> list[ReviewIssue]:
    """把只写了占位说明的条款转成一条审查意见。"""

    keys = placeholder_clause_keys(draft)
    if not keys:
        return []
    detail = f"以下条款的正文只有占位说明或空内容，请改写为列出原文内容：{', '.join(keys)}"
    return [ReviewIssue(kind="空内容", detail=detail)]


def parse_organized_draft(raw: str | dict[str, Any]) -> OrganizedDraft:
    """解析整理子智能体的结构化输出；非法输出抛错，由调用方回退固定卡片。"""

    payload = parse_model_json(raw)
    try:
        return OrganizedDraft.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("整理结果不符合结构化契约") from exc


def parse_review_issues(raw: str | dict[str, Any]) -> list[ReviewIssue]:
    """解析审查子智能体提出的意见；无法解析时返回一条格式意见，不阻断流程。"""

    try:
        payload = parse_model_json(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"电力规范审查结果无法解析: {exc}")
        return [ReviewIssue(kind="格式", detail=f"审查输出不是合法 JSON：{str(raw)[:200]}")]
    issues: list[ReviewIssue] = []
    raw_issues = payload.get("issues")
    if not isinstance(raw_issues, list):
        # 能解析出对象但缺少 issues 数组时，属于格式不符而不是「未提出问题」；
        # 必须回退成一条格式意见，避免脏输出被当作审查通过（answer_verified=True）。
        logger.warning("电力规范审查输出缺少 issues 数组: %s", str(raw)[:200])
        return [ReviewIssue(kind="格式", detail="审查输出缺少 issues 数组")]
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
    return issues


def ungrounded_numbers(answer: str, sources: Sequence[str]) -> list[str]:
    """返回结论中无法在来源里找到的数字，用于找出缺少原文依据的编造。

    按完整数字词元比较，避免 `12` 被 `2021` 误判为有依据。数字之间的空白不参与归一化，
    否则 `H2 30` 会被拼成 `230` 而产生假报。
    """

    grounded = {number for source in sources for number in NUMBER_PATTERN.findall(_normalize_digits(source))}
    ungrounded: list[str] = []
    for number in NUMBER_PATTERN.findall(_normalize_digits(CITATION_PATTERN.sub("", answer))):
        if number not in grounded and number not in ungrounded:
            ungrounded.append(number)
    return ungrounded


def ungrounded_number_issues(draft: OrganizedDraft, sources: Sequence[str]) -> list[ReviewIssue]:
    """把代码发现的、缺少原文依据的数字转成一条审查意见。"""

    numbers = ungrounded_numbers(draft_content_text(draft), sources)
    if not numbers:
        return []
    return [ReviewIssue(kind="编造", detail=f"下列数字在原文和问题中都没有依据，请删除或改正：{', '.join(numbers)}")]


async def organize_review_compose(
    *,
    question: str,
    dimensions: Sequence[str],
    evidences: Sequence[Any],
    call_subagent: SubagentCaller,
    call_compose: ComposeCall,
    tool_call_id: str,
    sub_questions: Sequence[str] = (),
) -> OrganizedAnswer | None:
    """整理一次、审查一次，再由父 Agent 的模型合并成最终条款内容。

    任何一步失败都返回 None，由调用方回退程序生成的固定卡片。流程不重试、也不询问
    用户，因此恢复执行不会重复整理。
    """

    organizer = await _call_subagent(
        call_subagent,
        slug=ORGANIZER_SLUG,
        message=build_organizer_message(question, evidences, dimensions, sub_questions),
        tool_call_id=f"{tool_call_id}#organize",
    )
    if organizer is None:
        return None
    try:
        draft = parse_organized_draft(organizer.output)
    except ValueError as exc:
        logger.warning(f"电力规范整理结果无法解析，回退固定卡片: {exc}")
        return None
    if not draft.clauses:
        logger.warning("电力规范整理结果没有条款，回退固定卡片")
        return None

    reviewer = await _call_subagent(
        call_subagent,
        slug=REVIEWER_SLUG,
        message=build_reviewer_message(question, evidences, render_draft_for_review(draft)),
        tool_call_id=f"{tool_call_id}#review",
    )
    if reviewer is None:
        return None

    issues = parse_review_issues(reviewer.output)
    issues.extend(ungrounded_number_issues(draft, _grounding_sources(question, evidences)))
    issues.extend(placeholder_issues(draft))
    out_of_scope = [issue for issue in issues if issue.kind in OUT_OF_SCOPE_REVIEW_KINDS]
    if out_of_scope:
        # 审查只负责忠实性；即使模型仍输出相关性判断，也不能让它成为删除条款的依据。
        logger.warning(
            f"审查提出 {len(out_of_scope)} 条超出忠实性范围的判断，已忽略: "
            + "; ".join(issue.detail for issue in out_of_scope)
        )
        issues = [issue for issue in issues if issue.kind not in OUT_OF_SCOPE_REVIEW_KINDS]
    if issues:
        logger.info(f"电力规范审查意见 {len(issues)} 条，交给最终整理合并")
    else:
        logger.info("电力规范审查未提出问题")

    try:
        composed = await call_compose(build_compose_message(question, dimensions, draft, issues, sub_questions))
        final = parse_organized_draft(composed)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"电力规范最终整理失败，回退固定卡片: {exc}")
        return None
    if not final.clauses:
        logger.warning("电力规范最终整理没有条款，回退固定卡片")
        return None
    return OrganizedAnswer(draft=final, issues=issues)


async def _call_subagent(
    call_subagent: SubagentCaller,
    *,
    slug: str,
    message: str,
    tool_call_id: str,
) -> SubagentReply | None:
    """调用一次子智能体；不可用或报错时返回 None，由调用方回退固定卡片。"""

    try:
        return await call_subagent(slug=slug, message=message, tool_call_id=tool_call_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"电力规范子智能体 {slug} 调用失败: {exc}")
        return None


def build_subagent_caller(context: Any) -> SubagentCaller:
    """构造在当前工具调用内启动并等待子智能体的入口。"""

    uid = str(getattr(context, "uid", "") or "").strip()
    parent_run_id = str(getattr(context, "run_id", "") or "").strip()

    async def call_subagent(
        *,
        slug: str,
        message: str,
        tool_call_id: str,
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
            # 子 run 的模型由父 run 的模型快照解析，调用方不额外传入。
            # tool_call_id 含 #organize/#review 后缀，子线程由它稳定派生。
            started = await SubagentRunService(db).start(
                uid=uid,
                created_by_run_id=parent_run_id,
                agent_item=agent_item,
                input_message=build_chat_input_message(message),
                tool_call_id=tool_call_id,
                internal=True,
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
    call_compose: ComposeCall | None,
    tool_call_id: str | None,
) -> dict[str, Any]:
    """产出最终答案载荷：模型整理结果或程序生成的固定卡片。

    模型不可用、整理/审查/最终合并任一步失败时回退固定卡片。答案来源判断只存在于
    这一处，调用方只负责转发结果。
    """

    evidences = order_graph_evidence_first(list(evidences))
    fallback = format_power_spec_results(query.query, evidences)
    if not POWER_SPEC_ORGANIZE_ENABLED or not tool_call_id or call_compose is None:
        return _fixed_card_payload(fallback)

    answer = await organize_review_compose(
        question=query.query,
        dimensions=query.answer_dimensions,
        evidences=evidences,
        call_subagent=call_subagent,
        call_compose=call_compose,
        tool_call_id=tool_call_id,
        sub_questions=[sub.query for sub in query.sub_queries],
    )
    if answer is None:
        return _fixed_card_payload(fallback)

    rendered = apply_organized_clauses(
        query.query,
        evidences,
        answer.draft.clauses,
        answers=answer.draft.answers,
    )
    if not rendered["items"]:
        logger.warning("电力规范最终整理没有可用条款，回退固定卡片")
        return _fixed_card_payload(fallback)
    return {
        "items": rendered["items"],
        "answer_markdown": rendered["answer_markdown"],
        "answer_source": ANSWER_SOURCE_MODEL,
        # 语义：审查未提出问题。有意见时已经由最终整理合并，不再阻断发布。
        "answer_verified": not answer.issues,
    }


def format_power_spec_results(query: str, evidence: Sequence[SpecClauseEvidence]) -> dict:
    """返回规范卡片数据和按最新需求书字段顺序展示的 Markdown。"""

    items = [_format_item(query, item) for item in evidence]
    source_urls = [item.source_url for item in evidence]
    return {"items": items, "answer_markdown": render_srs_cards(items, source_urls)}


def render_srs_cards(items: Sequence[dict], source_urls: Sequence[str | None] = ()) -> str:
    """按最新需求书规定的四个字段与顺序渲染卡片，正文独立成块以便表格显示。

    ``source_urls`` 与 ``items`` 同序；有原始文档地址时在卡片末尾追加 PDF 标记，
    供前端解析后请求并打开对应文档。
    """

    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        lines.append(f"### 规范卡片 {index}")
        lines.append("")
        for key, label in SRS_CARD_FIELDS:
            value = item.get(key)
            if key == "clause_body":
                # 标签写成独立列表项、与正文之间保留空行：缺前者会被 Markdown 懒续
                # 行并进上一条「条款编号」，缺后者则正文并进列表项、表格无法渲染。
                lines.extend([f"- **{label}**", "", str(value or "未提供"), ""])
                continue
            lines.append(f"- **{label}**：{value if value else '未提供'}")
        marker = _pdf_marker(source_urls[index - 1] if index <= len(source_urls) else None)
        if marker:
            lines.extend([marker, ""])
        else:
            lines.append("")
    return "\n".join(lines).rstrip()


def _pdf_marker(source_url: str | None) -> str:
    """返回卡片末尾的 PDF 标记；没有地址、地址不是可请求 URL 或会破坏标记边界时返回空串。"""

    url = str(source_url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://", "/")):
        logger.warning(f"电力规范卡片的源文档地址不是可请求 URL，已跳过: {url[:200]}")
        return ""
    if "\n" in url or "\r" in url or "]]" in url:
        logger.warning(f"电力规范卡片的源文档地址无法写入 PDF 标记，已跳过: {url[:200]}")
        return ""
    return PDF_MARKER.format(url=url)


def apply_organized_clauses(
    query: str,
    evidence: Sequence[SpecClauseEvidence],
    clauses: Sequence[Any],
    answers: Sequence[QuestionAnswer] = (),
) -> dict:
    """把整理子智能体给出的字段值套进代码渲染的规范卡片。

    图谱命中的条款是测评集确认过的高置信映射：它的正文字段直接取知识库原文，不采用
    模型改写；即使整理模型重排或遗漏，图谱卡片仍按图谱顺序排在最前。其余卡片保持
    「只发布整理者保留的条款」的既有行为。
    """

    by_citation = {item.citation_key: item for item in evidence}
    graph_evidence = order_graph_evidence_first([item for item in evidence if item.metadata.get("qa_graph_hit")])
    graph_keys = {item.citation_key for item in graph_evidence}
    rendered: list[tuple[dict, str | None]] = []
    used: set[str] = set()
    for clause in clauses:
        citation_key = str(getattr(clause, "citation_key", "") or "")
        source = by_citation.get(citation_key)
        if source is None or citation_key in used or citation_key in graph_keys:
            continue
        used.add(citation_key)
        item = _format_item(query, source)
        body = str(getattr(clause, "body", "") or "").strip()
        if body:
            item["clause_body"] = _highlight(body, _highlight_terms(query))
        clause_no = str(getattr(clause, "clause_no", "") or "").strip()
        if clause_no:
            item["clause_no"] = clause_no
        rendered.append((item, source.source_url))

    graph_rendered: list[tuple[dict, str | None]] = []
    for source in graph_evidence:
        if source.citation_key in used:
            continue
        used.add(source.citation_key)
        graph_rendered.append((_format_item(query, source), source.source_url))
    rendered = graph_rendered + rendered
    items = [item for item, _source_url in rendered]
    cards_markdown = render_srs_cards(items, [source_url for _item, source_url in rendered])
    if answers:
        cards_markdown = f"{render_question_answers(answers)}\n\n{cards_markdown}"
    return {"items": items, "answer_markdown": cards_markdown}


def render_question_answers(answers: Sequence[QuestionAnswer]) -> str:
    """把多问题的简短结论渲染在卡片之前，先回答每个子问题。"""

    lines = ["### 问题回答", ""]
    for index, item in enumerate(answers, start=1):
        lines.extend([f"**{index}. {item.question}**", "", item.answer, ""])
    return "\n".join(lines).rstrip()


def _format_item(query: str, item: SpecClauseEvidence) -> dict:
    """渲染单张卡片，只保留最新需求书规定的四个字段。"""

    return {
        "standard_name": item.standard_name,
        "standard_no": item.standard_no,
        "clause_no": display_clause_no(item),
        "clause_body": _highlight(item.clause_body, _highlight_terms(query)),
    }


def _fixed_card_payload(formatted: dict[str, Any]) -> dict[str, Any]:
    """构造固定卡片结果，作为模型不可用或校验失败时的降级输出。"""

    return {
        "items": formatted["items"],
        "answer_markdown": formatted["answer_markdown"],
        "answer_source": ANSWER_SOURCE_FIXED_CARD,
        "answer_verified": True,
    }


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


def _highlight_terms(query: str) -> list[str]:
    """返回用于正文高亮的词元；与检索层的行级匹配词元不同，不做子串展开。"""

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
