from yuxi.knowledge.power_spec.answer import (
    SRS_CARD_FIELDS,
    apply_organized_clauses,
    format_power_spec_results,
)
from yuxi.knowledge.power_spec.contracts import SpecClauseEvidence

CARD_FIELD_KEYS = {"standard_name", "standard_no", "clause_no", "clause_body"}


def _plain(value: str) -> str:
    return value.replace("<mark>", "").replace("</mark>", "")


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


def test_render_srs_cards_appends_pdf_marker_to_each_card():
    """每张卡片末尾只带自己的原始文档地址，前端据此请求并打开 PDF。"""

    first_url = "http://minio:9000/knowledgebases/kb-1/upload/DL_T_572.pdf"
    second_url = "http://minio:9000/knowledgebases/kb-1/upload/DL_T_596.pdf"
    markdown = format_power_spec_results(
        "巡检",
        [
            _evidence(source_url=first_url),
            _evidence(document_id="doc-2", clause_no="9.8", source_url=second_url),
        ],
    )["answer_markdown"]

    first_card, second_card = markdown.split("### 规范卡片 2")
    assert first_card.rstrip().endswith(f"[[PDF:{first_url}]]")
    assert second_card.rstrip().endswith(f"[[PDF:{second_url}]]")
    assert first_url not in second_card


def test_render_srs_cards_omits_pdf_marker_without_source_url():
    markdown = format_power_spec_results("巡检", [_evidence()])["answer_markdown"]

    assert "[[PDF:" not in markdown


def test_render_srs_cards_omits_pdf_marker_that_breaks_marker_boundary():
    for source_url in ("http://minio/kb/doc]]pdf", "http://minio/kb/doc\npdf"):
        markdown = format_power_spec_results("巡检", [_evidence(source_url=source_url)])["answer_markdown"]

        assert "[[PDF:" not in markdown


def test_render_srs_cards_omits_pdf_marker_for_non_requestable_url():
    """前端只能请求 http(s) 或同源相对地址；minio:// 等内部协议不能进标记。"""

    markdown = format_power_spec_results("巡检", [_evidence(source_url="minio://kb-1/DL_T_572.pdf")])[
        "answer_markdown"
    ]

    assert "[[PDF:" not in markdown


def test_render_srs_cards_allows_relative_pdf_url():
    source_url = "/minio/knowledgebases/kb-1/upload/DL_T_572.pdf"
    markdown = format_power_spec_results("巡检", [_evidence(source_url=source_url)])["answer_markdown"]

    assert f"[[PDF:{source_url}]]" in markdown


def test_apply_organized_clauses_publishes_cards_without_summary_prefix():
    """答案只由规范卡片组成：卡片前不再有逐题结论摘要。"""

    clause = type(
        "Clause",
        (),
        {"citation_key": "doc-1#6.2.1", "clause_no": None, "body": "有人值班变电站每日巡视一次。"},
    )()

    result = apply_organized_clauses("巡检周期", [_evidence()], [clause])

    assert result["answer_markdown"].startswith("### 规范卡片 1")


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


def test_apply_organized_clauses_appends_each_card_own_pdf_marker():
    first_url = "http://minio:9000/knowledgebases/kb-1/upload/DL_T_572.pdf"
    second_url = "http://minio:9000/knowledgebases/kb-1/upload/DL_T_596.pdf"
    evidence = [
        _evidence(source_url=first_url),
        _evidence(document_id="doc-2", clause_no="9.8", clause_title="负荷开关", source_url=second_url),
    ]

    result = apply_organized_clauses(
        "巡检",
        evidence,
        [_organized("doc-1#6.2.1", "整理内容"), _organized("doc-2#9.8", "整理内容")],
    )

    first_card, second_card = result["answer_markdown"].split("### 规范卡片 2")
    assert first_card.rstrip().endswith(f"[[PDF:{first_url}]]")
    assert second_card.rstrip().endswith(f"[[PDF:{second_url}]]")


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


def _graph_evidence(
    source_url: str | None = None,
    *,
    rank: int = 1,
    clause_no: str = "6.2.1",
) -> SpecClauseEvidence:
    return _evidence(
        source_url=source_url,
        clause_no=clause_no,
        metadata={
            "qa_graph_hit": True,
            "graph_rank": rank,
            "raw_clause_body": "变压器巡检周期：有人值班变电站每日巡视。",
        },
    )


def _organized(citation_key: str, body: str):
    return type("Clause", (), {"citation_key": citation_key, "clause_no": None, "body": body})()


def test_apply_organized_clauses_pins_graph_card_first_and_uses_kb_body():
    graph = _graph_evidence()
    other = _evidence(document_id="doc-2", clause_no="9.8", clause_title="负荷开关")
    clauses = [
        _organized("doc-2#9.8", "模型整理的其它条款"),
        _organized("doc-1#6.2.1", "模型改写的图谱条款"),
    ]

    result = apply_organized_clauses("巡检", [other, graph], clauses)

    assert [item["clause_no"] for item in result["items"]] == ["6.2.1", "9.8"]
    assert "有人值班变电站每日巡视" in result["items"][0]["clause_body"]
    assert "模型改写的图谱条款" not in result["items"][0]["clause_body"]
    assert result["items"][1]["clause_body"] == "模型整理的其它条款"
    assert result["answer_markdown"].index("### 规范卡片 1") < result["answer_markdown"].index("### 规范卡片 2")


def test_apply_organized_clauses_orders_two_graph_cards_by_rank():
    """多张图谱卡按“排序”置前，不依赖输入顺序。"""

    second = _graph_evidence(rank=2, clause_no="9.9")
    first = _graph_evidence(rank=1, clause_no="6.2.1")

    result = apply_organized_clauses("巡检", [second, first], [])

    assert [item["clause_no"] for item in result["items"]] == ["6.2.1", "9.9"]


def test_apply_organized_clauses_forces_missing_graph_card_first():
    graph = _graph_evidence()
    other = _evidence(document_id="doc-2", clause_no="9.8", clause_title="负荷开关")

    result = apply_organized_clauses("巡检", [graph, other], [_organized("doc-2#9.8", "模型整理的其它条款")])

    assert [item["clause_no"] for item in result["items"]] == ["6.2.1", "9.8"]
    assert "有人值班变电站每日巡视" in _plain(result["items"][0]["clause_body"])


def test_apply_organized_clauses_appends_pdf_marker_to_pinned_graph_card():
    """图谱卡由代码补卡，PDF 标记也必须跟随它而不是模型选择的卡片。"""

    graph_url = "http://minio:9000/knowledgebases/kb-1/upload/DL/T_393.pdf"
    other_url = "http://minio:9000/knowledgebases/kb-1/upload/DL_T_596.pdf"
    graph = _graph_evidence(source_url=graph_url)
    other = _evidence(document_id="doc-2", clause_no="9.8", clause_title="负荷开关", source_url=other_url)

    result = apply_organized_clauses("巡检", [other, graph], [_organized("doc-2#9.8", "模型整理的其它条款")])

    first_card, second_card = result["answer_markdown"].split("### 规范卡片 2")
    assert first_card.rstrip().endswith(f"[[PDF:{graph_url}]]")
    assert second_card.rstrip().endswith(f"[[PDF:{other_url}]]")
