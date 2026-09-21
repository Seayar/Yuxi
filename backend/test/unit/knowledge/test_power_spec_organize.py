import json

import pytest

from yuxi.knowledge.power_spec.answer import (
    ORGANIZER_SLUG,
    REVIEWER_SLUG,
    SubagentReply,
    missing_citation_keys,
    organize_and_review,
    parse_organized_draft,
    parse_review_verdict,
    render_draft_for_review,
    ungrounded_numbers,
)


class Evidence:
    """最小证据替身，只需提供整理与审查需要的字段。"""

    def __init__(self, body: str, *, clause_no: str = "6.1", standard_no: str = "DL/T 596-2021"):
        self.document_id = "file-1"
        self.clause_no = clause_no
        self.clause_title = "油浸式电力变压器"
        self.clause_body = body
        self.standard_no = standard_no
        self.version = "2021"

    @property
    def citation_key(self) -> str:
        return f"{self.document_id}#{self.clause_no}"


BODY = "220kV:6个月；总烃：150；H2：150"


def _draft(body: str, **overrides) -> str:
    """构造整理子智能体应当返回的结构化 JSON。"""

    clause = {"citation_key": "file-1#6.1", "body": body}
    clause.update(overrides)
    return json.dumps({"clauses": [clause]}, ensure_ascii=False)


def _empty_draft(reason: str = "证据未覆盖该问题") -> str:
    return json.dumps({"clauses": [], "notes": reason}, ensure_ascii=False)


def _reply(output: str, thread_id: str | None = "thread-1") -> SubagentReply:
    return SubagentReply(output=output, thread_id=thread_id, run_id="run-1")


class Caller:
    """按 slug 顺序回放子智能体输出并记录收到的输入。"""

    def __init__(self, organizer_outputs, reviewer_outputs):
        self._organizer = list(organizer_outputs)
        self._reviewer = list(reviewer_outputs)
        self.calls = []

    async def __call__(self, *, slug, message, tool_call_id, continue_thread_id=None):
        self.calls.append(
            {
                "slug": slug,
                "message": message,
                "tool_call_id": tool_call_id,
                "continue_thread_id": continue_thread_id,
            }
        )
        if slug == ORGANIZER_SLUG:
            return _reply(self._organizer.pop(0), "organizer-thread")
        return _reply(self._reviewer.pop(0), "reviewer-thread")


def _verdict(passed: bool, detail: str = "") -> str:
    issues = [] if passed else [{"kind": "事实错误", "detail": detail or "把上限说成下限"}]
    return json.dumps({"verdict": "pass" if passed else "fail", "issues": issues}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_passing_review_publishes_organized_answer():
    caller = Caller([_draft("220kV 变压器油中溶解气体分析周期为 6 个月。")], [_verdict(True)])

    result = await organize_and_review(
        question="220kV变压器油中溶解气体分析的周期",
        evidences=[Evidence(BODY)],
        call_subagent=caller,
        tool_call_id="call-1",
    )

    assert result.status == "published"
    assert result.draft is not None
    assert result.draft.clauses[0].body == "220kV 变压器油中溶解气体分析周期为 6 个月。"
    assert result.attempts == 1
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG, REVIEWER_SLUG]


@pytest.mark.asyncio
async def test_review_failure_retries_with_feedback_on_same_thread():
    caller = Caller(
        [_draft("所有变压器都应执行该试验。")],
        [_verdict(False, "原文按电压等级区分周期"), _verdict(True)],
    )
    caller._organizer.append(_draft("220kV 变压器每 6 个月执行一次。"))

    result = await organize_and_review(
        question="220kV变压器油中溶解气体分析的周期",
        evidences=[Evidence(BODY)],
        call_subagent=caller,
        tool_call_id="call-1",
    )

    assert result.status == "published"
    assert result.attempts == 2
    second_organizer = [call for call in caller.calls if call["slug"] == ORGANIZER_SLUG][1]
    assert "原文按电压等级区分周期" in second_organizer["message"]
    assert second_organizer["continue_thread_id"] == "organizer-thread"


@pytest.mark.asyncio
async def test_ungrounded_number_skips_review_and_retries():
    caller = Caller(
        [_draft("总烃限值为 999。"), _draft("总烃限值为 150。")],
        [_verdict(True)],
    )

    result = await organize_and_review(
        question="总烃限值",
        evidences=[Evidence(BODY)],
        call_subagent=caller,
        tool_call_id="call-1",
    )

    assert result.status == "published"
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG, ORGANIZER_SLUG, REVIEWER_SLUG]
    assert "999" in [call for call in caller.calls if call["slug"] == ORGANIZER_SLUG][1]["message"]


@pytest.mark.asyncio
async def test_exhausted_attempts_request_user_confirmation():
    caller = Caller([_draft("周期为 12 个月。")] * 3, [_verdict(False, "原文是 6 个月")] * 3)

    result = await organize_and_review(
        question="220kV变压器油中溶解气体分析的周期",
        evidences=[Evidence(BODY)],
        call_subagent=caller,
        tool_call_id="call-1",
        max_attempts=3,
    )

    assert result.status == "needs_user_confirmation"
    assert result.attempts == 3
    assert result.last_verdict is not None and result.last_verdict.passed is False
    assert result.draft is not None and result.draft.clauses[0].body == "周期为 12 个月。"


@pytest.mark.asyncio
async def test_organizer_failure_falls_back_to_rejected():
    async def failing(*, slug, message, tool_call_id, continue_thread_id=None):
        raise RuntimeError("子智能体不可用")

    result = await organize_and_review(
        question="周期",
        evidences=[Evidence(BODY)],
        call_subagent=failing,
        tool_call_id="call-1",
    )

    assert result.status == "rejected"
    assert result.draft is None


@pytest.mark.asyncio
async def test_invalid_organizer_output_retries_without_review():
    caller = Caller(["整理结果不是 JSON", _draft("220kV:6个月")], [_verdict(True)])

    result = await organize_and_review(
        question="周期",
        evidences=[Evidence(BODY)],
        call_subagent=caller,
        tool_call_id="call-1",
    )

    assert result.status == "published"
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG, ORGANIZER_SLUG, REVIEWER_SLUG]


@pytest.mark.asyncio
async def test_empty_clauses_are_rejected_without_review():
    caller = Caller([_empty_draft()], [_verdict(True)])

    result = await organize_and_review(
        question="周期",
        evidences=[Evidence(BODY)],
        call_subagent=caller,
        tool_call_id="call-1",
    )

    assert result.status == "rejected"
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG]


def test_parse_organized_draft_rejects_missing_body():
    with pytest.raises(ValueError):
        parse_organized_draft(json.dumps({"clauses": [{"citation_key": "file-1#6.1"}]}))


def test_parse_organized_draft_accepts_optional_fields():
    draft = parse_organized_draft(
        json.dumps({"clauses": [{"citation_key": "file-1#6.1", "body": "内容", "clause_title": "标题"}]})
    )

    assert draft.clauses[0].clause_title == "标题"
    assert draft.clauses[0].clause_no is None


def test_render_draft_for_review_contains_fields_and_notes():
    draft = parse_organized_draft(
        json.dumps({"clauses": [{"citation_key": "file-1#6.1", "body": "内容", "scope": "适用范围"}], "notes": "说明"})
    )

    text = render_draft_for_review(draft)

    assert "file-1#6.1" in text
    assert "整理内容：内容" in text
    assert "适用范围：适用范围" in text
    assert "说明：说明" in text


def test_missing_citation_keys_reports_uncovered_clauses():
    draft = parse_organized_draft(_draft("内容"))

    assert missing_citation_keys(draft, [Evidence(BODY)]) == []
    assert missing_citation_keys(draft, [Evidence(BODY), Evidence(BODY, clause_no="9.8")]) == ["file-1#9.8"]


@pytest.mark.asyncio
async def test_missing_clause_triggers_retry_with_feedback():
    caller = Caller([_draft("仅第一条")], [_verdict(True)])
    caller._organizer.append(
        json.dumps(
            {
                "clauses": [
                    {"citation_key": "file-1#6.1", "body": "第一条"},
                    {"citation_key": "file-1#9.8", "body": "第二条"},
                ]
            },
            ensure_ascii=False,
        )
    )

    result = await organize_and_review(
        question="周期",
        evidences=[Evidence(BODY), Evidence(BODY, clause_no="9.8")],
        call_subagent=caller,
        tool_call_id="call-1",
    )

    assert result.status == "published"
    assert result.attempts == 2
    second = [call for call in caller.calls if call["slug"] == ORGANIZER_SLUG][1]
    assert "file-1#9.8" in second["message"]
    assert [clause.citation_key for clause in result.draft.clauses] == ["file-1#6.1", "file-1#9.8"]


def test_parse_review_verdict_requires_pass_and_no_issues():
    assert parse_review_verdict('{"verdict":"pass","issues":[]}').passed is True
    assert parse_review_verdict('{"verdict":"pass","issues":[{"kind":"遗漏","detail":"x"}]}').passed is False
    assert parse_review_verdict('{"verdict":"fail","issues":[]}').passed is False
    assert parse_review_verdict("```json\n{\"verdict\":\"pass\",\"issues\":[]}\n```").passed is True


def test_parse_review_verdict_treats_invalid_output_as_failure():
    result = parse_review_verdict("审查完成，没有问题")

    assert result.passed is False
    assert result.issues[0].kind == "格式"


def test_ungrounded_numbers_ignores_citation_and_whitespace_differences():
    sources = [BODY, "DL/T 596-2021", "2021"]

    assert ungrounded_numbers("周期：6 个月，总烃：150 [source:file-1#6.1]", sources) == []
    assert ungrounded_numbers("周期：12 个月", sources) == ["12"]


def test_ungrounded_numbers_does_not_merge_across_identifier_digits():
    sources = ["新装变压器总烃20；H2 30；C2H2 0.1"]

    assert ungrounded_numbers("总烃 20；H2 30；C2H2 0.1", sources) == []


def test_ungrounded_numbers_still_flags_invented_values():
    sources = ["总烃：150；H2：150"]

    assert ungrounded_numbers("总烃：999", sources) == ["999"]


def test_ungrounded_numbers_normalizes_fullwidth_digits():
    assert ungrounded_numbers("周期 １２ 个月", ["周期 12 个月"]) == []


def test_ungrounded_numbers_allows_numbers_from_the_question():
    assert ungrounded_numbers("220kV 变压器适用", ["220 kV 变压器适用"]) == []
