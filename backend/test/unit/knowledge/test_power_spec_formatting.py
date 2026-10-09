from yuxi.knowledge.power_spec.answer import (
    CONDITIONAL_SRS_FIELDS,
    SRS_CARD_FIELDS,
    apply_organized_clauses,
    format_power_spec_results,
)
from yuxi.knowledge.power_spec.contracts import SpecClauseEvidence


def _evidence(**overrides) -> SpecClauseEvidence:
    payload = {
        "document_id": "doc-1",
        "standard_name": "电力变压器运行规程",
        "standard_no": "DL/T 572-2021",
        "version": "2021",
        "status": "现行",
        "clause_no": "6.2.1",
        "clause_title": "变压器巡检周期",
        "clause_body": "变压器巡检周期：有人值班变电站每日巡视。",
        "scope": "适用于变压器运行巡视。",
        "rerank_score": 0.82,
        "source_file": "standard.pdf",
        "source_page": 12,
        "source_page_ref": "第12页第3段",
    }
    payload.update(overrides)
    return SpecClauseEvidence(**payload)


def test_format_power_spec_results_matches_spec_card_contract():
    result = format_power_spec_results("变压器巡检周期", [_evidence()])
    item = result["items"][0]

    assert item["relevance_score"] == 82.0
    assert item["relevance_label"] == "高度相关"
    assert item["source_page"] == 12
    assert item["matched_terms"] == ["变压器巡检周期"]
    assert "[source:doc-1#6.2.1]" in result["answer_markdown"]


def test_render_srs_cards_contains_every_required_field_in_order():
    markdown = format_power_spec_results("变压器巡检周期", [_evidence()])["answer_markdown"]

    positions = []
    for key, label in SRS_CARD_FIELDS:
        if key in CONDITIONAL_SRS_FIELDS:
            continue
        marker = f"**{label}**"
        assert marker in markdown, label
        positions.append(markdown.index(marker))

    assert positions == sorted(positions)


def test_render_srs_cards_omits_replaced_by_for_current_standard():
    markdown = format_power_spec_results("巡检", [_evidence()])["answer_markdown"]

    assert "**替代规范**" not in markdown


def test_render_srs_cards_includes_replaced_by_for_abolished_standard():
    evidence = _evidence(status="废止", replaced_by="DL/T 572-2021")

    markdown = format_power_spec_results("巡检", [evidence])["answer_markdown"]

    assert "**替代规范**：DL/T 572-2021" in markdown


def test_render_srs_cards_reports_missing_values_instead_of_omitting_fields():
    evidence = _evidence(scope=None, source_page=None, source_page_ref=None)

    markdown = format_power_spec_results("巡检", [evidence])["answer_markdown"]

    assert "**适用范围**：未提供" in markdown
    assert "**源文件页码**：未提供" in markdown
    assert "**条款在源文件中的位置**：未提供" in markdown


def test_apply_organized_clauses_uses_model_values_but_keeps_code_format():
    evidence = _evidence()
    clause = type(
        "Clause",
        (),
        {
            "citation_key": "doc-1#6.2.1",
            "clause_title": "变压器巡检周期",
            "clause_no": None,
            "body": "有人值班变电站每日巡视一次。",
            "scope": None,
        },
    )()

    result = apply_organized_clauses("巡检周期", [evidence], [clause])
    item = result["items"][0]

    assert item["clause_body"] == "有人值班变电站每日巡视一次。"
    assert "**规范名称**：电力变压器运行规程" in result["answer_markdown"]
    assert "**条款正文**" in result["answer_markdown"]


def test_apply_organized_clauses_omits_evidence_without_organized_values():
    clause = type(
        "Clause",
        (),
        {"citation_key": "doc-9#9.9", "clause_title": None, "clause_no": None, "body": "内容", "scope": None},
    )()

    result = apply_organized_clauses("巡检", [_evidence()], [clause])

    assert result["items"] == []


def test_apply_organized_clauses_publishes_only_organized_clauses():
    evidence = [_evidence(), _evidence(document_id="doc-2", clause_no="9.8", clause_title="负荷开关")]
    organized = type(
        "Clause",
        (),
        {"citation_key": "doc-1#6.2.1", "clause_title": None, "clause_no": None, "body": "整理后内容", "scope": None},
    )()

    result = apply_organized_clauses("巡检", evidence, [organized])

    assert [item["citation_key"] for item in result["items"]] == ["doc-1#6.2.1"]
    assert "**规范卡片 2**" not in result["answer_markdown"]
