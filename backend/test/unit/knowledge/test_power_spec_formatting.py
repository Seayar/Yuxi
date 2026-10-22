from yuxi.knowledge.power_spec.answer import (
    SRS_CARD_FIELDS,
    apply_organized_clauses,
    format_power_spec_results,
)
from yuxi.knowledge.power_spec.contracts import SpecClauseEvidence

CARD_FIELD_KEYS = {"standard_name", "standard_no", "clause_no", "clause_body"}
REMOVED_CARD_LABELS = (
    "条款标题",
    "适用范围",
    "规范状态",
    "替代规范",
    "相关度",
    "源文件页码",
    "条款在源文件中的位置",
    "引用",
)


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


def test_format_power_spec_results_returns_only_four_card_fields():
    result = format_power_spec_results("变压器巡检周期", [_evidence()])
    item = result["items"][0]

    assert set(item) == CARD_FIELD_KEYS
    assert item["standard_name"] == "电力变压器运行规程"
    assert item["standard_no"] == "DL/T 572-2021"
    assert item["clause_no"] == "6.2.1"
    assert "有人值班变电站每日巡视" in item["clause_body"]


def test_render_srs_cards_contains_every_required_field_in_order():
    markdown = format_power_spec_results("变压器巡检周期", [_evidence()])["answer_markdown"]

    positions = []
    for _key, label in SRS_CARD_FIELDS:
        marker = f"**{label}**"
        assert marker in markdown, label
        positions.append(markdown.index(marker))

    assert positions == sorted(positions)


def test_render_srs_cards_omits_fields_outside_the_latest_contract():
    markdown = format_power_spec_results("巡检", [_evidence(status="废止", replaced_by="DL/T 572-2021")])[
        "answer_markdown"
    ]

    for label in REMOVED_CARD_LABELS:
        assert f"**{label}**" not in markdown, label
    assert "[source:" not in markdown


def test_render_srs_cards_keeps_clause_body_label_out_of_previous_item():
    markdown = format_power_spec_results("变压器巡检周期", [_evidence()])["answer_markdown"]

    # 「条款正文」标签必须是独立列表项、正文是列表项之外的独立段落：否则
    # Markdown 懒续行会把标签并进上一条「条款编号」，正文（含表格）无法独立渲染。
    assert "\n- **条款正文**\n\n" in markdown


def test_apply_organized_clauses_uses_model_values_but_keeps_code_format():
    evidence = _evidence()
    clause = type(
        "Clause",
        (),
        {
            "citation_key": "doc-1#6.2.1",
            "clause_no": None,
            "body": "有人值班变电站每日巡视一次。",
        },
    )()

    result = apply_organized_clauses("巡检周期", [evidence], [clause])
    item = result["items"][0]

    assert set(item) == CARD_FIELD_KEYS
    assert item["clause_body"] == "有人值班变电站每日巡视一次。"
    assert "**规范名称**：电力变压器运行规程" in result["answer_markdown"]
    assert "**条款正文**" in result["answer_markdown"]


def test_apply_organized_clauses_omits_evidence_without_organized_values():
    clause = type(
        "Clause",
        (),
        {"citation_key": "doc-9#9.9", "clause_no": None, "body": "内容"},
    )()

    result = apply_organized_clauses("巡检", [_evidence()], [clause])

    assert result["items"] == []


def test_apply_organized_clauses_publishes_only_organized_clauses():
    evidence = [_evidence(), _evidence(document_id="doc-2", clause_no="9.8", clause_title="负荷开关")]
    organized = type(
        "Clause",
        (),
        {"citation_key": "doc-1#6.2.1", "clause_no": None, "body": "整理后内容"},
    )()

    result = apply_organized_clauses("巡检", evidence, [organized])

    assert [item["standard_no"] for item in result["items"]] == ["DL/T 572-2021"]
    assert "### 规范卡片 2" not in result["answer_markdown"]
