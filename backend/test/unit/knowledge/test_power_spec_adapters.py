from yuxi.knowledge.power_spec.intent import parse_power_spec_query
from yuxi.knowledge.power_spec.retrieval import adapt_graph_results, adapt_vector_results


DOCUMENT_METADATA = {
    "standard_name": "电力变压器运行规程",
    "standard_no": "DL/T572—2021",
    "version": "2021",
    "status": "现行",
    "source_file": "DL_T_572-2021.pdf",
    "clauses": [
        {
            "clause_no": "6.2.1",
            "clause_title": "变压器巡检周期",
            "scope": "适用于变压器运行巡视。",
            "source_page": None,
            "source_page_ref": None,
        }
    ],
}


def test_adapt_vector_results_preserves_rerank_and_source_metadata():
    evidence = adapt_vector_results(
        [
            {
                "content": "有人值班变电站每日巡视。",
                "rerank_score": 0.91,
                "metadata": {"file_id": "file-1", "chunk_id": "chunk-1"},
            }
        ],
        {"file-1": DOCUMENT_METADATA},
    )

    assert len(evidence) == 1
    assert evidence[0].citation_key == "file-1#6.2.1"
    assert evidence[0].rerank_score == 0.91
    assert evidence[0].metadata["chunk_id"] == "chunk-1"


def test_adapt_vector_results_ignores_unmapped_files_and_clauses():
    evidence = adapt_vector_results(
        [
            {"content": "正文", "rerank_score": 0.9, "metadata": {"file_id": "unknown"}},
            {
                "content": "正文",
                "rerank_score": 0.8,
                "metadata": {"file_id": "file-1", "clause_no": "9.9.9"},
            },
        ],
        {"file-1": DOCUMENT_METADATA},
    )

    assert evidence == []


def test_adapt_vector_results_maps_multi_clause_chunk_by_title():
    evidence = adapt_vector_results(
        [
            {
                "content": "6.2.1 变压器巡检周期\n有人值班变电站每日巡视。",
                "rerank_score": 0.8,
                "metadata": {"file_id": "file-1", "chunk_id": "chunk-1"},
            }
        ],
        {
            "file-1": {
                **DOCUMENT_METADATA,
                "clauses": [
                    DOCUMENT_METADATA["clauses"][0],
                    {"clause_no": "6.2.2", "clause_title": "变压器试验周期"},
                ],
            }
        },
    )

    assert [item.citation_key for item in evidence] == ["file-1#6.2.1"]


def test_adapt_vector_results_maps_ocr_chunk_by_clause_body():
    evidence = adapt_vector_results(
        [
            {
                "content": "有人值班变电站每日巡视，发现异常及时处理。",
                "rerank_score": 0.8,
                "metadata": {"file_id": "file-1", "chunk_id": "chunk-1"},
            }
        ],
        {
            "file-1": {
                **DOCUMENT_METADATA,
                "clauses": [
                    {"clause_no": "6.2.1", "clause_title": "6.2.1", "clause_body": "有人值班变电站每日巡视，发现异常及时处理。"},
                    {"clause_no": "6.2.2", "clause_title": "6.2.2", "clause_body": "无人值班变电站每周巡视一次。"},
                ],
            }
        },
    )

    assert [item.citation_key for item in evidence] == ["file-1#6.2.1"]


def test_adapt_vector_results_uses_parsed_clause_body_instead_of_whole_chunk():
    evidence = adapt_vector_results(
        [
            {
                "content": "6.2.1 变压器巡检周期\n有人值班变电站每日巡视。\n13.1.2 温升限值\n绕组平均温升为65K。",
                "rerank_score": 0.8,
                "metadata": {"file_id": "file-1", "chunk_id": "chunk-1"},
            }
        ],
        {
            "file-1": {
                **DOCUMENT_METADATA,
                "clauses": [
                    {
                        "clause_no": "13.1.2",
                        "clause_title": "13.1.2",
                        "clause_body": "绕组平均温升为65K。",
                    }
                ],
            }
        },
    )

    assert evidence[0].clause_title == "温升限值"
    assert evidence[0].clause_body == "绕组平均温升为65K。"


def test_adapt_vector_results_does_not_use_table_decimal_as_clause_title():
    evidence = adapt_vector_results(
        [
            {
                "content": "22.2 变压器温升限值\n绕组平均温升为65K。",
                "rerank_score": 0.8,
                "metadata": {"file_id": "file-1"},
            }
        ],
        {
            "file-1": {
                **DOCUMENT_METADATA,
                "clauses": [
                    {"clause_no": "22.2", "clause_title": "0.40", "clause_body": "绕组平均温升为65K。"}
                ],
            }
        },
    )

    assert evidence[0].clause_title == "变压器温升限值"


def test_adapt_vector_results_keeps_only_query_relevant_original_segments():
    evidence = adapt_vector_results(
        [
            {
                "content": "额定容量按表14确定。\n温升限值：绕组平均温升为65K。\n本条还规定外观检查。",
                "rerank_score": 0.8,
                "metadata": {"file_id": "file-1"},
            }
        ],
        {
            "file-1": {
                **DOCUMENT_METADATA,
                "clauses": [
                    {
                        "clause_no": "5.1",
                        "clause_title": "性能参数",
                        "clause_body": "额定容量按表14确定。温升限值：绕组平均温升为65K。本条还规定外观检查。",
                    }
                ],
            }
        },
        "温升限值是多少",
    )

    assert evidence[0].clause_body == "温升限值：绕组平均温升为65K。"
    assert "raw_clause_body" in evidence[0].metadata


def test_adapt_graph_results_converts_clause_nodes_without_inventing_score():
    query = parse_power_spec_query("变压器巡检周期")
    evidence = adapt_graph_results(
        {
            "nodes": [
                {
                    "properties": {
                        "type": "clause",
                        "file_id": "file-1",
                        "clause_no": "6.2.1",
                        "clause_body": "有人值班变电站每日巡视。",
                    }
                }
            ]
        },
        {"file-1": DOCUMENT_METADATA},
        query,
    )

    assert len(evidence) == 1
    assert evidence[0].citation_key == "file-1#6.2.1"
    assert evidence[0].rerank_score is None
    assert evidence[0].metadata["graph_node_id"] is None


def test_adapt_vector_results_keeps_whole_table_when_row_matches():
    metadata = {
        **DOCUMENT_METADATA,
        "clauses": [
            {
                "clause_no": "6.2.1",
                "clause_title": "试验项目",
                "clause_body": (
                    "序号 | 项目 | 周期\n"
                    "---|---|---\n"
                    "1 | 红外测温 | 1年\n"
                    "本节内容与查询无关。"
                ),
            }
        ],
    }

    evidence = adapt_vector_results(
        [
            {
                "content": "unused",
                "rerank_score": 0.9,
                "metadata": {"file_id": "file-1", "chunk_id": "chunk-1"},
            }
        ],
        {"file-1": metadata},
        "红外测温",
    )

    assert evidence[0].clause_body.splitlines() == [
        "序号 | 项目 | 周期",
        "---|---|---",
        "1 | 红外测温 | 1年",
    ]
