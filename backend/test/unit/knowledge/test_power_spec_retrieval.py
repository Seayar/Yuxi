import pytest

from yuxi.knowledge.power_spec.contracts import SpecClauseEvidence
from yuxi.knowledge.power_spec.intent import parse_power_spec_query
from yuxi.knowledge.power_spec.retrieval import PowerSpecSearchService


def _evidence(
    document_id: str,
    clause_no: str,
    *,
    score: float | None,
    status: str = "现行",
    body: str = "规范正文。额定容量 温升限值 色谱 周期 氢气 总烃 注意值。",
) -> SpecClauseEvidence:
    return SpecClauseEvidence(
        document_id=document_id,
        standard_name="电力变压器运行规程",
        standard_no="DL/T 572-2021",
        version="2021",
        status=status,
        clause_no=clause_no,
        clause_title=f"条款 {clause_no}",
        clause_body=body,
        rerank_score=score,
    )


@pytest.mark.asyncio
async def test_power_spec_search_requires_graph_and_rerank_match():
    query = parse_power_spec_query("110kV变压器巡检周期")
    graph_calls: list[str] = []
    vector_calls: list[str] = []

    async def graph_search(parsed_query):
        graph_calls.append(parsed_query.query)
        return [_evidence("doc-1", "6.2.1", score=None)]

    async def vector_search(query_text):
        vector_calls.append(query_text)
        return [
            _evidence("doc-1", "6.2.1", score=0.82),
            _evidence("doc-2", "1.1", score=0.99),
            _evidence("doc-1", "6.2.2", score=None),
        ]

    result = await PowerSpecSearchService(graph_search, vector_search).search(query)

    assert graph_calls == [query.query]
    assert vector_calls == [query.query]
    assert [item.citation_key for item in result.evidence] == ["doc-1#6.2.1"]
    assert result.evidence[0].metadata["graph_matched"] is True
    assert result.evidence_bundle is not None


@pytest.mark.asyncio
async def test_power_spec_search_excludes_abolished_and_reports_no_result():
    query = parse_power_spec_query("变压器巡检周期")

    async def graph_search(_parsed_query):
        return [_evidence("doc-1", "6.2.1", score=None)]

    async def vector_search(_query_text):
        return [_evidence("doc-1", "6.2.1", score=0.9, status="废止")]

    result = await PowerSpecSearchService(graph_search, vector_search).search(query)

    assert result.evidence == []
    assert result.no_result == "未在现行规范中找到可验证的相关条款"


@pytest.mark.asyncio
async def test_power_spec_search_returns_clarification_without_calling_retrievers():
    query = parse_power_spec_query("帮我查一下相关规定")
    called = False

    async def graph_search(_parsed_query):
        nonlocal called
        called = True
        return []

    async def vector_search(_query_text):
        nonlocal called
        called = True
        return []

    result = await PowerSpecSearchService(graph_search, vector_search).search(query)

    assert called is False
    assert result.clarification == "未能识别完整的查询信息，请明确查询类型或设备信息"


@pytest.mark.asyncio
async def test_power_spec_search_keeps_distinct_matching_clauses():
    query = parse_power_spec_query("变压器额定容量与温升限值")

    async def graph_search(_parsed_query):
        return [_evidence("doc-1", "8.1", score=None), _evidence("doc-1", "13.1.2", score=None)]

    async def vector_search(_query_text):
        return [
            _evidence("doc-1", "8.1", score=0.91),
            _evidence("doc-1", "8.1", score=0.89),
            _evidence("doc-1", "13.1.2", score=0.84),
            _evidence("doc-2", "4.3.1", score=0.81),
        ]

    result = await PowerSpecSearchService(graph_search, vector_search).search(query)

    assert [item.citation_key for item in result.evidence] == [
        "doc-1#8.1",
        "doc-1#13.1.2",
    ]


@pytest.mark.asyncio
async def test_power_spec_search_allows_vector_only_fallback_when_graph_is_unavailable():
    query = parse_power_spec_query("变压器巡检周期")

    async def graph_search(_parsed_query):
        return []

    async def vector_search(_query_text):
        return [_evidence("doc-1", "6.2.1", score=0.82), _evidence("doc-2", "1.1", score=0.8)]

    result = await PowerSpecSearchService(graph_search, vector_search).search(query)

    assert [item.citation_key for item in result.evidence] == ["doc-1#6.2.1", "doc-2#1.1"]
    assert result.evidence[0].metadata["graph_matched"] is False


@pytest.mark.asyncio
async def test_power_spec_search_rejects_candidates_without_question_dimensions():
    query = parse_power_spec_query("220kV变压器色谱多久做一回？氢气总烃多少算超标？")

    async def graph_search(_parsed_query):
        return []

    async def vector_search(_query_text):
        return [_evidence("doc-1", "2.5", score=0.99, body="泄漏电流和直流耐压试验要求。")]

    result = await PowerSpecSearchService(graph_search, vector_search).search(query)

    assert result.evidence == []
    assert result.no_result == "未在现行规范中找到可验证的相关条款"
