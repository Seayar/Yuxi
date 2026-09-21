import pytest

from yuxi.knowledge.power_spec.intent import parse_power_spec_query


def test_parse_power_spec_query_extracts_intent_and_entities():
    parsed = parse_power_spec_query("110kV变压器额定容量与温升限值")

    assert parsed.intent == "设备参数查询"
    assert parsed.device_type == "变压器"
    assert parsed.voltage_level == "110kV"
    assert parsed.clarification_required is False
    assert "额定容量" in parsed.normalized_terms
    assert parsed.unknown_terms


def test_parse_power_spec_query_extracts_device_name():
    parsed = parse_power_spec_query("查询1号主变的巡检周期")

    assert parsed.intent == "检修标准查询"
    assert parsed.device_name == "1号主变"


def test_parse_power_spec_query_requires_clarification_for_ambiguous_input():
    parsed = parse_power_spec_query("帮我查一下相关规定")

    assert parsed.intent == "其他类型"
    assert parsed.clarification_required is True
    assert parsed.clarification_message == "未能识别完整的查询信息，请明确查询类型或设备信息"

def test_parse_power_spec_query_normalizes_colloquial_transformer_chromatography_question():
    query = parse_power_spec_query("那个 220 的变压器啊，色谱多久做一回？氢气总烃多少算超标？")

    assert query.intent == "检修标准查询"
    assert query.device_type == "变压器"
    assert query.voltage_level == "220kV"
    assert query.clarification_required is False
    assert query.answer_dimensions == ["色谱", "周期", "氢气", "总烃", "超标"]
    assert "220kV" in query.normalized_terms
    assert query.unknown_terms


def test_parse_power_spec_query_rejects_empty_input():
    with pytest.raises(ValueError, match="查询内容不能为空"):
        parse_power_spec_query(" ")
