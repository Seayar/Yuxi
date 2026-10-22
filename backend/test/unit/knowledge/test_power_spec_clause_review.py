import json

import pytest

from yuxi.knowledge.power_spec.answer import format_power_spec_results
from yuxi.knowledge.power_spec.contracts import SpecClauseEvidence
from yuxi.knowledge.power_spec.retrieval import (
    DISPLAY_CLAUSE_NO_KEY,
    DISPLAY_CLAUSE_TITLE_KEY,
    apply_corrections,
    display_clause_no,
    display_clause_title,
    parse_corrections,
    review_clause_metadata,
)

RAW_BODY = "6.1 | 油浸式电力变压器\n油浸式电力变压器的试验项目见表5。"


def _evidence(clause_no: str = "6.1", clause_title: str = "| 油浸式电力变压器") -> SpecClauseEvidence:
    return SpecClauseEvidence(
        document_id="file-1",
        standard_name="DL/T 596—2021",
        standard_no="DL/T596—2021",
        version="2021",
        status="现行",
        clause_no=clause_no,
        clause_title=clause_title,
        clause_body=RAW_BODY,
        rerank_score=0.7,
        metadata={
            "raw_clause_body": RAW_BODY,
            "raw_clause_no": clause_no,
            "raw_clause_title": clause_title,
        },
    )


def _model_call(payload):
    async def call(_prompt):
        return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

    return call


def test_apply_corrections_accepts_ocr_level_clause_no_fix():
    correction = parse_corrections({"items": [{"citation_key": "file-1#6.1", "clause_no": "６．１"}]})

    outcome = apply_corrections([_evidence()], correction)

    assert display_clause_no(outcome.evidence[0]) == "６．１"
    assert outcome.rejected == []


def test_apply_corrections_rejects_renumbered_clause():
    correction = parse_corrections({"items": [{"citation_key": "file-1#6.1", "clause_no": "6.2"}]})

    outcome = apply_corrections([_evidence()], correction)

    assert display_clause_no(outcome.evidence[0]) == "6.1"
    assert len(outcome.rejected) == 1


def test_apply_corrections_accepts_title_present_in_source():
    correction = parse_corrections({"items": [{"citation_key": "file-1#6.1", "clause_title": "油浸式电力变压器"}]})

    outcome = apply_corrections([_evidence()], correction)

    assert display_clause_title(outcome.evidence[0]) == "油浸式电力变压器"


def test_apply_corrections_rejects_invented_title():
    correction = parse_corrections({"items": [{"citation_key": "file-1#6.1", "clause_title": "干式变压器温升"}]})

    outcome = apply_corrections([_evidence()], correction)

    assert display_clause_title(outcome.evidence[0]) == "| 油浸式电力变压器"
    assert len(outcome.rejected) == 1


def test_apply_corrections_keeps_citation_key_on_original_clause_no():
    correction = parse_corrections({"items": [{"citation_key": "file-1#6.1", "clause_no": "６．１"}]})

    outcome = apply_corrections([_evidence()], correction)

    assert outcome.evidence[0].citation_key == "file-1#6.1"


def test_applied_correction_changes_rendered_clause_no_only():
    correction = parse_corrections({"items": [{"citation_key": "file-1#6.1", "clause_no": "６．１"}]})
    outcome = apply_corrections([_evidence()], correction)

    item = format_power_spec_results("温升", outcome.evidence)["items"][0]

    assert item["clause_no"] == "６．１"
    assert set(item) == {"standard_name", "standard_no", "clause_no", "clause_body"}


@pytest.mark.asyncio
async def test_review_clause_metadata_applies_valid_items_only():
    payload = {
        "items": [
            {"citation_key": "file-1#6.1", "clause_title": "油浸式电力变压器"},
            {"citation_key": "file-9#9.9", "clause_title": "不存在"},
        ]
    }

    outcome = await review_clause_metadata([_evidence()], model_call=_model_call(payload))

    assert DISPLAY_CLAUSE_TITLE_KEY in outcome.evidence[0].metadata
    assert len(outcome.applied) == 1


@pytest.mark.asyncio
async def test_review_clause_metadata_without_model_keeps_fields():
    outcome = await review_clause_metadata([_evidence()], model_call=None)

    assert outcome.evidence[0].metadata.get(DISPLAY_CLAUSE_TITLE_KEY) is None
    assert outcome.applied == []


@pytest.mark.asyncio
async def test_review_clause_metadata_survives_invalid_output():
    outcome = await review_clause_metadata([_evidence()], model_call=_model_call("模型没有输出 JSON"))

    assert outcome.evidence[0].metadata.get(DISPLAY_CLAUSE_NO_KEY) is None
    assert outcome.applied == []


def test_parse_corrections_ignores_items_without_citation_key():
    corrections = parse_corrections({"items": [{"clause_title": "标题"}, "字符串", {"citation_key": "file-1#6.1"}]})

    assert list(corrections) == ["file-1#6.1"]
