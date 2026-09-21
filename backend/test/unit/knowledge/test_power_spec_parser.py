import pytest

from yuxi.knowledge.power_spec.ingestion import parse_power_spec_text


POWER_SPEC_TEXT = """DL/T 572—2021 电力变压器运行规程
适用范围：适用于 10kV～110kV 电力变压器运行巡视。

6.1.2 上层油温限值
油浸式变压器上层油温一般不超过 85℃。

6.2.1 变压器巡检周期
有人值班变电站每日至少巡视一次；每周进行一次夜间巡视。
"""


def test_parse_power_spec_text_extracts_standard_and_clauses():
    document = parse_power_spec_text(
        POWER_SPEC_TEXT,
        document_id="file-1",
        source_file="DL_T_572-2021.pdf",
    )

    assert document.standard_no == "DL/T572—2021"
    assert document.standard_name == "电力变压器运行规程"
    assert document.version == "2021"
    assert document.status == "现行"
    assert document.scope == "适用于 10kV～110kV 电力变压器运行巡视。"
    assert [clause.clause_no for clause in document.clauses] == ["6.1.2", "6.2.1"]
    assert document.clauses[1].clause_title == "变压器巡检周期"
    assert "每周" in document.clauses[1].clause_body


def test_parse_power_spec_text_ignores_ocr_cell_marker_as_standard_name():
    document = parse_power_spec_text(
        "<fcel>DL/T 596—2021\n1.1 总则\n本条款正文内容。",
        document_id="file-3",
    )

    assert document.standard_name == "DL/T 596—2021"


def test_parse_power_spec_text_keeps_ocr_scope_prefix_line_in_body():
    document = parse_power_spec_text(
        "DL/T 596—2021 电力设备预防性试验规程\n1.1 总则\n范围开关、选择开关的动作顺序应符合产品技术文件的要求。",
        document_id="file-4",
    )

    assert document.scope is None
    assert "范围开关" in document.clauses[0].clause_body


def test_parse_power_spec_text_accepts_colon_scope_declaration():
    document = parse_power_spec_text(
        "DL/T 596—2021 电力设备预防性试验规程\n范围：本标准规定了电力设备的预防性试验要求。\n1.1 总则\n本条款正文内容。",
        document_id="file-5",
    )

    assert document.scope == "本标准规定了电力设备的预防性试验要求。"


def test_parse_power_spec_text_strips_ocr_table_markers_from_body():
    document = parse_power_spec_text(
        "DL/T 596—2021 电力设备预防性试验规程\n1.1 总则\n各部位无异常温升现象<fcel>检测方法参考DL/T 664<nl><lcel><lcel>",
        document_id="file-6",
    )

    body = document.clauses[0].clause_body
    assert "<fcel>" not in body
    assert "<lcel>" not in body
    assert "各部位无异常温升现象" in body
    assert "检测方法参考DL/T 664" in body


def test_parse_power_spec_text_rejects_overlong_standard_name_candidate():
    document = parse_power_spec_text(
        "DL/T 596—2021 12 | 操动机构在分闸、合闸下的操作压力下降值 | 应符合产品技术文件要求 | 76\n1.1 总则\n本条款正文内容。",
        document_id="file-7",
    )

    assert document.standard_name == "DL/T 596—2021"


def test_parse_power_spec_text_preserves_table_lines_in_clause_body():
    document = parse_power_spec_text(
        "DL/T 596—2021 电力设备预防性试验规程\n"
        "6.1 油浸式电力变压器\n"
        "油浸式电力变压器的试验项目见表5。\n"
        "序号 | 项目 | 周期\n"
        "---|---|---\n"
        "1 | 红外测温 | 1年\n",
        document_id="file-8",
    )

    body_lines = document.clauses[0].clause_body.splitlines()

    assert "序号 | 项目 | 周期" in body_lines
    assert "---|---|---" in body_lines
    assert "1 | 红外测温 | 1年" in body_lines


def test_parse_power_spec_text_marks_invalid_standard_as_abolished():
    document = parse_power_spec_text(
        "GB/T 1234-2020 旧规范\n本规范已废止。\n1.1 总则\n本条款正文。",
        document_id="file-2",
    )

    assert document.standard_no == "GB/T1234-2020"
    assert document.status == "废止"


def test_parse_power_spec_text_ignores_decimal_table_values_as_clauses():
    document = parse_power_spec_text(
        """GB/T 6451—2023 电力变压器技术参数和要求
5.1 额定参数
额定容量和温升应符合要求。
表格数据 0.20 不应成为章节。
""",
        document_id="file-5",
    )

    assert [clause.clause_no for clause in document.clauses] == ["5.1"]


def test_parse_power_spec_text_ignores_table_ratio_lines():
    document = parse_power_spec_text(
        """GB/T 6451—2023 电力变压器技术参数和要求
4.2.1通用要求
额定容量和温升应符合标准要求。
1.09/1.04
4.2.2安全要求
变压器应满足安全运行条件和保护要求。
""",
        document_id="file-6",
    )

    assert [clause.clause_no for clause in document.clauses] == ["4.2.1", "4.2.2"]


def test_parse_power_spec_text_rejects_missing_standard_number():
    with pytest.raises(ValueError, match="规范编号"):
        parse_power_spec_text("1.1 总则\n没有标准编号。", document_id="file-3")


def test_parse_power_spec_text_rejects_missing_clauses():
    with pytest.raises(ValueError, match="数字条款"):
        parse_power_spec_text("DL/T 572—2021 电力变压器运行规程", document_id="file-4")
