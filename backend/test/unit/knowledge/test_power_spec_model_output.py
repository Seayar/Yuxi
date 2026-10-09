"""模型输出的 JSON 提取。

整理、审查与条款校对共用同一套提取逻辑；某处退回严格整段解析时，模型「先写分析再给
JSON」的合法输出会被静默丢弃（整理丢答案、校对不修复）。
"""

import json

import pytest

from yuxi.knowledge.power_spec.model_output import parse_model_json
from yuxi.knowledge.power_spec.retrieval import parse_corrections


def _payload() -> str:
    return json.dumps({"items": [{"citation_key": "file-1#6.1", "clause_no": "6.1"}]}, ensure_ascii=False)


def test_extracts_json_from_fenced_block_after_prose():
    raw = "The user wants to check clause numbering.\n\n```json\n" + _payload() + "\n```\n"

    assert parse_model_json(raw)["items"][0]["clause_no"] == "6.1"


def test_extracts_bare_json_after_prose():
    assert parse_model_json("分析：原文按电压等级分档。\n" + _payload())["items"]


def test_keeps_braces_inside_string_values():
    raw = '前言。\n```json\n{"items":[{"citation_key":"a#1","clause_title":"判据 {x} 与 } 属于原文"}]}\n```\n'

    assert parse_model_json(raw)["items"][0]["clause_title"] == "判据 {x} 与 } 属于原文"


def test_extracts_object_wrapped_in_array():
    """负向：整段被解析成数组时仍要取出其中的对象。

    严格整段解析在这里得到 list，调用方会把它当成不可解析而丢弃合法内容。
    """

    assert parse_model_json("[" + _payload() + "]")["items"]


def test_dict_input_passes_through():
    assert parse_model_json({"items": []}) == {"items": []}


def test_rejects_output_without_json_object():
    """负向：整段既没有 JSON 也没有 JSON 块时必须报错，不能猜内容。"""

    with pytest.raises(ValueError):
        parse_model_json("我无法完成校对，因为原文缺失。")


def test_clause_correction_survives_array_wrapped_output():
    """负向：模型把对象包在数组里时，条款字段校对必须照常生效。

    严格整段解析会得到 list，`parse_corrections` 静默返回空映射，编号修复无声失效。
    """

    raw = "我逐条比对了原文与条款号。\n[" + _payload() + "]"

    corrections = parse_corrections(raw)

    assert list(corrections) == ["file-1#6.1"]
    assert corrections["file-1#6.1"].clause_no == "6.1"


def test_unparsable_correction_output_degrades_to_no_fix():
    """无法解析时保持既有降级语义：不做任何修复，而不是抛错打断链路。"""

    assert parse_corrections("无法校对。") == {}
