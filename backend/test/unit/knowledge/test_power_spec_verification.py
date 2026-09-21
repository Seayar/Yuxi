from yuxi.knowledge.power_spec.contracts import SpecClauseEvidence
from yuxi.knowledge.power_spec.retrieval import verify_evidence_projection


def _evidence(body: str, raw_body: str | None = None) -> SpecClauseEvidence:
    return SpecClauseEvidence(
        document_id="doc-1",
        standard_name="规范",
        standard_no="DL/T 1-2021",
        version="2021",
        status="现行",
        clause_no="1.1",
        clause_title="总则",
        clause_body=body,
        metadata={"raw_clause_body": raw_body or body, "raw_clause_no": "1.1", "raw_clause_title": "总则"},
    )


def test_verify_evidence_projection_accepts_original_projection():
    assert verify_evidence_projection([_evidence("原文内容。")]).valid is True


def test_verify_evidence_projection_rejects_model_invented_body():
    result = verify_evidence_projection([_evidence("模型补充的内容。", "原文内容。")])

    assert result.valid is False
    assert result.issues[0].code == "body_not_supported"


def test_verify_evidence_projection_accepts_persisted_clause_projection():
    item = _evidence("绕组平均温升为65K。", "chunk without this OCR clause")
    item = item.model_copy(update={"metadata": {**item.metadata, "parsed_clause_body": "绕组平均温升为65K。"}})

    assert verify_evidence_projection([item]).valid is True


def test_verify_evidence_projection_accepts_selected_original_segments():
    raw_body = "第一段与温升有关的内容。中间一段无关内容。第三段与温升有关的内容。"
    display_body = "第一段与温升有关的内容。\n第三段与温升有关的内容。"

    assert verify_evidence_projection([_evidence(display_body, raw_body)]).valid is True


def test_verify_evidence_projection_rejects_invented_segment_in_projection():
    raw_body = "第一段与温升有关的内容。\n第三段与温升有关的内容。"
    display_body = "第一段与温升有关的内容。\n模型新增的温升结论。"

    result = verify_evidence_projection([_evidence(display_body, raw_body)])

    assert result.valid is False
    assert result.issues[0].code == "body_not_supported"


def test_verify_evidence_projection_rejects_reordered_segments():
    raw_body = "第一段与温升有关的内容。\n第三段与温升有关的内容。"
    display_body = "第三段与温升有关的内容。\n第一段与温升有关的内容。"

    result = verify_evidence_projection([_evidence(display_body, raw_body)])

    assert result.valid is False
    assert result.issues[0].code == "body_not_supported"


def test_verify_evidence_projection_reports_unsupported_clause_title():
    item = _evidence("原文内容。")
    item = item.model_copy(update={"clause_title": "模型编造标题"})

    result = verify_evidence_projection([item])

    assert result.valid is False
    assert [issue.code for issue in result.issues] == ["clause_title_not_supported"]