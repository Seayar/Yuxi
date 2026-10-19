import pytest

from yuxi.knowledge.power_spec.ingestion import (
    POWER_SPEC_EXTRACTION_SCHEMA,
    build_clause_extraction,
    build_document_extraction,
    parse_power_spec_text,
)


@pytest.fixture
def parsed_document():
    return parse_power_spec_text(
        "DL/T 572—2021 电力变压器运行规程\n6.2.1 变压器巡检周期\n有人值班变电站每日巡视。",
        document_id="file-1",
        source_file="DL_T_572-2021.pdf",
    )


def test_build_document_extraction_is_versioned(parsed_document):
    metadata = build_document_extraction(parsed_document)

    assert metadata == {
        "schema": POWER_SPEC_EXTRACTION_SCHEMA,
        "document_id": "file-1",
        "standard_name": "电力变压器运行规程",
        "standard_no": "DL/T572—2021",
        "version": "2021",
        "status": "现行",
        "replaced_by": None,
        "source_file": "DL_T_572-2021.pdf",
        "scope": None,
        "clause_count": 1,
    }


def test_build_clause_extraction_is_stable(parsed_document):
    clause = parsed_document.clauses[0]

    metadata = build_clause_extraction(parsed_document, clause)

    assert metadata["schema"] == "power_spec.v1"
    assert metadata["clause_no"] == "6.2.1"
    assert metadata["source_page"] is None


def test_build_clause_extraction_rejects_clause_from_another_document(parsed_document):
    other_document = parse_power_spec_text(
        "DL/T 572—2021 电力变压器运行规程\n6.2.2 其他条款\n正文。",
        document_id="file-2",
    )

    with pytest.raises(ValueError, match="不属于文档"):
        build_clause_extraction(parsed_document, other_document.clauses[0])


def test_plain_text_clause_line_becomes_clause_with_body():
    """PDF 直出的纯文本：行首编号 + 句子即条款正文，不能被标题式识别漏掉。"""

    document = parse_power_spec_text(
        "DL/T 409—2023 电力线路安全工作规程\n"
        "6.3.2 直接验电时，应使用相应电压等级的验电器，并确证验电器良好。\n"
        "验电前应先在有电设备上进行试验。",
        document_id="file-3",
    )

    assert [clause.clause_no for clause in document.clauses] == ["6.3.2"]
    assert "确证验电器良好" in document.clauses[0].clause_body


def test_toc_line_with_dotted_leader_is_not_clause():
    """目录行（引导点 + 页码）不得当成条款。"""

    document = parse_power_spec_text(
        "DL/T 572—2021 电力变压器运行规程\n"
        "6.3.2 直接验电……37\n"
        "6.3.3 真实条款\n正文内容。",
        document_id="file-4",
    )

    assert [clause.clause_no for clause in document.clauses] == ["6.3.3"]


def test_nested_subclause_is_split_from_parent_clause():
    """行首编号是父条款严格延伸的子条款要切成独立条款，父引言单独保留。"""

    document = parse_power_spec_text(
        "DL/T 409—2023 电力线路安全工作规程\n"
        "6.3 带电作业一般要求\n"
        "本章规定带电作业的组织与技术要求，作业人员应经专门培训并考试合格后方可上岗。\n"
        "6.3.1 带电作业应在良好天气下进行。\n"
        "6.3.2 直接验电时，应使用相应电压等级的验电器。\n",
        document_id="file-5",
    )

    by_no = {clause.clause_no: clause for clause in document.clauses}
    # 父引言达到保留长度阈值，与两个子条款各自成条
    assert set(by_no) == {"6.3", "6.3.1", "6.3.2"}
    assert "专门培训" in by_no["6.3"].clause_body
    assert "良好天气" in by_no["6.3.1"].clause_body
    assert "确证验电器良好" in by_no["6.3.2"].clause_body or "验电器" in by_no["6.3.2"].clause_body


def test_nested_split_does_not_eat_cross_referenced_clause_numbers():
    """正文引用的其他条款号（非父编号延伸）不得被切成子条款。"""

    document = parse_power_spec_text(
        "DL/T 409—2023 电力线路安全工作规程\n"
        "6.4 接地线装设\n"
        "接地线装设要求见 6.4.1 与 7.2.3 的规定，作业班组应严格执行。\n"
        "6.4.2 工作完毕后应立即拆除接地线。\n",
        document_id="file-6",
    )

    nos = [clause.clause_no for clause in document.clauses]
    # 6.4 的正文引用了 6.4.1，但它不是行首子条款；只切出真正的 6.4.2
    assert "6.4.1" not in nos
    assert nos[-1] == "6.4.2"
