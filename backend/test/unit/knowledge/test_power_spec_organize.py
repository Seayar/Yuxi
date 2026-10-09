"""电力规范答案的「整理 — 审查 — 合并」编排。"""

import inspect
import json
from types import SimpleNamespace

import pytest

from yuxi.knowledge.power_spec.answer import (
    ORGANIZER_SLUG,
    REVIEWER_SLUG,
    SubagentReply,
    build_subagent_caller,
    organize_review_compose,
    parse_organized_draft,
    parse_review_issues,
    placeholder_clause_keys,
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
QUESTION = "220kV变压器油中溶解气体分析的周期"
FINAL_BODY = "220kV 变压器每 6 个月执行一次。"


def _draft(body: str, **overrides) -> str:
    """构造整理子智能体应当返回的结构化 JSON。"""

    clause = {"citation_key": "file-1#6.1", "body": body}
    clause.update(overrides)
    return json.dumps({"clauses": [clause]}, ensure_ascii=False)


def _empty_draft(reason: str = "证据未覆盖该问题") -> str:
    return json.dumps({"clauses": [], "notes": reason}, ensure_ascii=False)


def _opinions(*issues: dict) -> str:
    """构造审查子智能体应当返回的意见列表。"""

    return json.dumps({"issues": list(issues)}, ensure_ascii=False)


def _issue(kind: str = "事实错误", detail: str = "原文按电压等级区分周期") -> dict:
    return {"kind": kind, "detail": detail}


class Caller:
    """按 slug 回放子智能体输出并记录收到的输入。"""

    def __init__(self, organizer_outputs, reviewer_outputs):
        self._organizer = list(organizer_outputs)
        self._reviewer = list(reviewer_outputs)
        self.calls = []

    async def __call__(self, *, slug, message, tool_call_id):
        self.calls.append({"slug": slug, "message": message, "tool_call_id": tool_call_id})
        if slug == ORGANIZER_SLUG:
            return SubagentReply(output=self._organizer.pop(0), thread_id="organizer-thread")
        return SubagentReply(output=self._reviewer.pop(0), thread_id="reviewer-thread")


class FailingCaller:
    """让指定 slug 的子智能体调用抛错。"""

    def __init__(self, failed_slug: str):
        self.failed_slug = failed_slug

    async def __call__(self, *, slug, message, tool_call_id):
        if slug == self.failed_slug:
            raise RuntimeError("子智能体不可用")
        return SubagentReply(output=_draft(FINAL_BODY), thread_id="thread")


class Composer:
    """记录最终整理收到的提示词，并按顺序回放模型输出。"""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._outputs.pop(0)


class BrokenComposer:
    """最终整理不可用。"""

    async def __call__(self, _prompt: str) -> str:
        raise RuntimeError("模型不可用")


def _run(caller, composer, *, dimensions=("周期",), evidences=None):
    return organize_review_compose(
        question=QUESTION,
        dimensions=dimensions,
        evidences=evidences if evidences is not None else [Evidence(BODY)],
        call_subagent=caller,
        call_compose=composer,
        tool_call_id="call-1",
    )


@pytest.mark.asyncio
async def test_each_subagent_runs_once_and_final_body_comes_from_compose():
    """整理、审查、合并各执行一次，发布的是合并后的正文。"""

    caller = Caller([_draft("220kV 变压器周期为 6 个月。")], [_opinions()])
    composer = Composer([_draft("220kV 变压器每 6 个月执行一次，总烃注意值 150。")])

    answer = await _run(caller, composer)

    assert answer is not None
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG, REVIEWER_SLUG]
    assert len(composer.prompts) == 1
    assert answer.draft.clauses[0].body == "220kV 变压器每 6 个月执行一次，总烃注意值 150。"
    assert answer.issues == []


@pytest.mark.asyncio
async def test_organizer_message_carries_dimensions_from_intent_recognition():
    """关注维度由上游意图识别带入，不再让整理子智能体自行判断。"""

    caller = Caller([_draft(FINAL_BODY)], [_opinions()])

    await _run(caller, Composer([_draft(FINAL_BODY)]), dimensions=["周期", "总烃"])

    message = caller.calls[0]["message"]
    assert "## 本次问题的关注维度（由上游意图识别给出，不要自行判断）" in message
    assert "周期、总烃" in message
    assert "先判断用户问的是哪个维度" not in message


@pytest.mark.asyncio
async def test_review_opinions_are_passed_into_final_compose():
    """审查意见进入最终整理提示词，结论不再被判定通过与否。"""

    caller = Caller([_draft("所有变压器都应执行该试验。")], [_opinions(_issue())])
    composer = Composer([_draft(FINAL_BODY)])

    answer = await _run(caller, composer)

    assert answer is not None
    assert [issue.kind for issue in answer.issues] == ["事实错误"]
    assert "原文按电压等级区分周期" in composer.prompts[0]
    assert answer.draft.clauses[0].body == FINAL_BODY


@pytest.mark.asyncio
async def test_invalid_review_output_becomes_format_opinion_without_blocking():
    """负向：审查输出不可解析时只记一条格式意见并继续，不再当成审查失败。"""

    caller = Caller([_draft(FINAL_BODY)], ["审查完成，没有问题"])
    composer = Composer([_draft(FINAL_BODY)])

    answer = await _run(caller, composer)

    assert answer is not None
    assert [issue.kind for issue in answer.issues] == ["格式"]
    assert "审查输出不是合法 JSON" in composer.prompts[0]
    assert len(composer.prompts) == 1


@pytest.mark.asyncio
async def test_ungrounded_number_becomes_review_opinion():
    """数字前置门降级为一条审查意见交给最终整理，不再阻断或重试。"""

    caller = Caller([_draft("总烃限值为 999。")], [_opinions()])
    composer = Composer([_draft("总烃限值为 150。")])

    answer = await _run(caller, composer)

    assert answer is not None
    assert [issue.kind for issue in answer.issues] == ["编造"]
    assert "999" in composer.prompts[0]
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG, REVIEWER_SLUG]


@pytest.mark.asyncio
async def test_placeholder_body_becomes_review_opinion():
    """占位正文降级为一条审查意见，不再要求重试。"""

    caller = Caller([_draft("本条款未涉及该问题。")], [_opinions()])
    composer = Composer([_draft(FINAL_BODY)])

    answer = await _run(caller, composer)

    assert answer is not None
    assert [issue.kind for issue in answer.issues] == ["空内容"]
    assert "占位说明" in composer.prompts[0]
    assert len(caller.calls) == 2


@pytest.mark.asyncio
async def test_organizer_failure_returns_none():
    composer = Composer([])

    assert await _run(FailingCaller(ORGANIZER_SLUG), composer) is None
    assert composer.prompts == []


@pytest.mark.asyncio
async def test_reviewer_failure_returns_none():
    composer = Composer([])

    assert await _run(FailingCaller(REVIEWER_SLUG), composer) is None
    assert composer.prompts == []


@pytest.mark.asyncio
async def test_invalid_organizer_output_returns_none():
    assert await _run(Caller(["整理结果不是 JSON"], []), Composer([])) is None


@pytest.mark.asyncio
async def test_empty_clauses_return_none():
    caller = Caller([_empty_draft()], [])

    assert await _run(caller, Composer([])) is None
    assert [call["slug"] for call in caller.calls] == [ORGANIZER_SLUG]


@pytest.mark.asyncio
async def test_compose_failure_or_invalid_output_returns_none():
    """负向：最终整理不可用或输出非法时整条链路失败，由调用方回退固定卡片。"""

    assert await _run(Caller([_draft(FINAL_BODY)], [_opinions()]), BrokenComposer()) is None
    assert await _run(Caller([_draft(FINAL_BODY)], [_opinions()]), Composer(["不是 JSON"])) is None
    assert await _run(Caller([_draft(FINAL_BODY)], [_opinions()]), Composer([_empty_draft()])) is None


@pytest.mark.asyncio
async def test_subagent_caller_arguments_are_accepted_by_run_service(monkeypatch):
    """负向：调用方实参必须被 SubagentRunService.start 接受，否则只在真实运行期报错。"""

    from yuxi.repositories import agent_repository
    from yuxi.services import subagent_run_service
    from yuxi.storage.postgres import manager as postgres_manager

    accepted = set(inspect.signature(subagent_run_service.SubagentRunService.start).parameters)
    captured: dict = {}

    class StopCall(Exception):
        pass

    class Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    class Repository:
        def __init__(self, _db):
            pass

        async def get_by_slug(self, slug):
            return SimpleNamespace(slug=slug)

    class Service:
        def __init__(self, _db):
            pass

        async def start(self, **kwargs):
            captured.update(kwargs)
            raise StopCall

    monkeypatch.setattr(postgres_manager.pg_manager, "get_async_session_context", lambda: Session())
    monkeypatch.setattr(agent_repository, "AgentRepository", Repository)
    monkeypatch.setattr(subagent_run_service, "SubagentRunService", Service)

    call_subagent = build_subagent_caller(SimpleNamespace(uid="u1", run_id="r1"))

    with pytest.raises(StopCall):
        await call_subagent(slug=ORGANIZER_SLUG, message="m", tool_call_id="call-1")

    assert captured
    assert set(captured) <= accepted


def test_placeholder_clause_keys_only_matches_standalone_placeholder_text():
    """占位判定只匹配整句占位说明，不误伤以「无」开头的真实正文。"""

    placeholder = parse_organized_draft(_draft("本条款未涉及该问题。"))
    assert placeholder_clause_keys(placeholder) == ["file-1#6.1"]

    real_content = parse_organized_draft(_draft("无励磁调压开关的试验周期为 6 个月。"))
    assert placeholder_clause_keys(real_content) == []

    missing_body = parse_organized_draft(_draft("   "))
    assert placeholder_clause_keys(missing_body) == ["file-1#6.1"]


def test_parse_organized_draft_rejects_missing_body():
    with pytest.raises(ValueError):
        parse_organized_draft(json.dumps({"clauses": [{"citation_key": "file-1#6.1"}]}))


def test_parse_organized_draft_extracts_json_after_analysis_prose():
    """回归：模型先写一段分析、再用代码块给出 JSON 时，合法结论不能被丢弃。"""

    raw = (
        "The user wants to extract information about rated capacity.\n"
        "I need to check each clause:\n"
        "1. 6.3 SF6 Gas Insulated Transformer: no rated capacity.\n\n"
        "```json\n" + _draft(FINAL_BODY) + "\n```\n"
    )

    draft = parse_organized_draft(raw)

    assert draft.clauses[0].body == FINAL_BODY


def test_parse_organized_draft_extracts_bare_json_after_prose():
    raw = "分析：原文按电压等级分档，第 6.1 条给出周期。\n" + _draft(FINAL_BODY)

    assert parse_organized_draft(raw).clauses[0].body == FINAL_BODY


def test_parse_organized_draft_keeps_braces_inside_body():
    """正文里的花括号不能让提取提前收尾。"""

    body = '判据：{"上限": 1} 与单独的 } 都属于原文。'
    raw = "分析在前。\n```json\n" + _draft(body) + "\n```\n"

    assert parse_organized_draft(raw).clauses[0].body == body


def test_parse_organized_draft_rejects_output_without_json_object():
    """负向：整段既没有 JSON 也没有 JSON 块时，必须判定失败而不是猜内容。"""

    with pytest.raises(ValueError):
        parse_organized_draft("我无法整理这份原文，因为证据不足，建议人工确认。")


def test_parse_review_issues_extracts_json_after_prose():
    raw = "我逐条比对后发现一处问题。\n```json\n" + _opinions(_issue()) + "\n```\n"

    assert [issue.kind for issue in parse_review_issues(raw)] == ["事实错误"]


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


def test_parse_review_issues_reads_opinions_regardless_of_verdict():
    """审查意见按 issues 读取；即使模型仍带 verdict 字段也不影响解析。"""

    issues = parse_review_issues('```json\n{"verdict":"fail","issues":[{"kind":"遗漏","detail":"x"}]}\n```')

    assert [issue.kind for issue in issues] == ["遗漏"]
    assert parse_review_issues('{"issues":[]}') == []
    assert parse_review_issues('{"issues":["缺少引用"]}')[0].detail == "缺少引用"


def test_parse_review_issues_flags_parsed_object_without_issues():
    """解析出对象但缺少 issues 数组时按格式意见处理，不能当作审查通过。"""

    issues = parse_review_issues('见表 {1} 的内容，另外 {"x": 1} 是无关的。')

    assert [issue.kind for issue in issues] == ["格式"]
    assert "issues" in issues[0].detail
    assert parse_review_issues('{"x": 1}')[0].kind == "格式"


def test_parse_review_issues_treats_invalid_output_as_format_opinion():
    issues = parse_review_issues("审查完成，没有问题")

    assert [issue.kind for issue in issues] == ["格式"]
    assert "合法 JSON" in issues[0].detail


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
