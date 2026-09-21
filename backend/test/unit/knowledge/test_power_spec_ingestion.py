import pytest

from yuxi.knowledge.power_spec.ingestion import (
    POWER_SPEC_EXTRACTION_SCHEMA,
    build_clause_extraction,
    build_clause_tags,
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


def test_build_clause_extraction_and_tags_are_stable(parsed_document):
    clause = parsed_document.clauses[0]

    metadata = build_clause_extraction(parsed_document, clause)
    tags = build_clause_tags(parsed_document, clause)

    assert metadata["schema"] == "power_spec.v1"
    assert metadata["clause_no"] == "6.2.1"
    assert metadata["source_page"] is None
    assert tags == [
        "power_spec",
        "power_spec:standard:DL/T572—2021",
        "power_spec:status:现行",
        "power_spec:clause:6.2.1",
    ]


def test_build_clause_extraction_rejects_clause_from_another_document(parsed_document):
    other_document = parse_power_spec_text(
        "DL/T 572—2021 电力变压器运行规程\n6.2.2 其他条款\n正文。",
        document_id="file-2",
    )

    with pytest.raises(ValueError, match="不属于文档"):
        build_clause_extraction(parsed_document, other_document.clauses[0])
