"""人工整理的标准文档解析：Markdown 条款标题、目录与源 PDF 页码。"""

import pytest

from yuxi.knowledge.power_spec.ingestion import parse_power_spec_text


# 国标/行标人工整理后的典型结构：封面、目次、前言、正文条款标题。
# 封面只写被代替的编号，正文条款标题是 Markdown 二级标题。
MARKDOWN_STANDARD = """<!-- source_pdf_page: 1 -->

# 中华人民共和国电力行业标准

代替 DL/T 393—2010

<!-- source_pdf_page: 1 -->
# 输变电设备状态检修试验规程

2021-12-22发布 2022-03-22实施国家能源局 发布

<!-- source_pdf_page: 2 -->
## 目 次

### 前言

### 1 范围1

### 6 交流设备17

### 6.1 油浸式电力变压器和电抗器17

### 6.22 调相机79

<!-- source_pdf_page: 4 -->
## 前 言

本文件代替DL/T393—2010《输变电设备状态检修试验规程》。

<!-- source_pdf_page: 5 -->
# 输变电设备状态检修试验规程

## 1 范围

本文件适用于超高压及以下交、直流输变电设备。

<!-- source_pdf_page: 5 -->
## 2 规范性引用文件

下列文件中的内容通过文中的规范性引用而构成本文件必不可少的条款。

<!-- source_pdf_page: 21 -->
## 6.1 油浸式电力变压器和电抗器

<!-- source_pdf_page: 21 -->
## 6.1.1.1 巡检项目及要求

遵循4.3的要求，项目及要求见表1。

<!-- source_pdf_page: 21 -->
<table class="resolved-table">
<tr><td>本体检查</td><td>110 kV：3个月</td></tr>
</table>

<!-- source_pdf_page: 83 -->
## 6.22.3.1 停电试验项目及要求

a）基础无位移、沉降等异常。
b）设备标识、接地标识等齐全、清晰。
"""


def _parse(text: str = MARKDOWN_STANDARD):
    return parse_power_spec_text(text, document_id="file-md", source_file="标准.md")


def test_markdown_headings_split_clauses_instead_of_toc_lines():
    """负向：目录区块内的条款标题不能被当成条款。"""

    document = _parse()
    numbers = [clause.clause_no for clause in document.clauses]

    assert numbers == ["1", "2", "6.1.1.1", "6.22.3.1"]
    assert "6.22" not in numbers  # 目录条目 `### 6.22 调相机79` 不得成为条款
    assert document.clauses[0].clause_title == "范围"


def test_clause_body_stops_at_next_heading():
    """正文只在同一标题区间内累积，不跨章节形成巨块。"""

    document = _parse()
    by_no = {clause.clause_no: clause for clause in document.clauses}

    assert "超高压及以下" in by_no["1"].clause_body
    assert "规范性引用" not in by_no["1"].clause_body
    assert "本体检查" in by_no["6.1.1.1"].clause_body
    assert "停电试验" not in by_no["6.1.1.1"].clause_body


def test_source_pdf_page_comes_from_page_marker():
    """条款页码取标题之前最近的源 PDF 页码标记。"""

    document = _parse()
    by_no = {clause.clause_no: clause for clause in document.clauses}

    assert by_no["1"].source_page == 5
    assert by_no["6.1.1.1"].source_page == 21
    assert by_no["6.22.3.1"].source_page == 83


def test_page_marker_never_leaks_into_body():
    document = _parse()

    for clause in document.clauses:
        assert "source_pdf_page" not in clause.clause_body


def test_section_without_own_body_is_not_evidence():
    """只有标题、没有自身正文的分层章节（如 6.1）不产出条款。"""

    document = _parse()

    assert "6.1" not in [clause.clause_no for clause in document.clauses]


def test_standard_number_uses_publish_year_when_cover_only_shows_replaced_one():
    """负向：封面只写被代替的编号时，不能把标准编号写成上一版。"""

    document = _parse()

    assert document.standard_no == "DL/T 393-2021"
    assert document.version == "2021"
    assert document.status == "现行"


# 分段整理出来的单条款片段：条款标题只有编号，标题写在下一级标题里。
NUMBER_ONLY_HEADING_WITH_SUBHEADING = """# DL/T 393-2021
## 6.4.1.1
### 表17 S F6 断路器设备巡检项目及要求
#### 智能控制柜/汇控柜检查
| 字段 | 内容 |
| --- | --- |
| 项目 | 智能控制柜/汇控柜检查 |
| 说明条款 | 6.4.1.4 |
"""

# 无标题条款：编号写在标题里，正文行重复编号后再写条款内容。
NUMBER_ONLY_HEADING_WITH_TEXT = """# DL/T 408-2023

## 7.3.5.2

7.3.5.2 停电操作应按照“断路器一负荷侧隔离开关一电源侧隔离开关”的顺序依次进行。
"""


def test_number_only_heading_takes_title_from_subheading():
    """纯编号标题的条款标题取紧邻的下一级标题，正文从该标题之后开始。"""

    document = _parse(NUMBER_ONLY_HEADING_WITH_SUBHEADING)

    assert [clause.clause_no for clause in document.clauses] == ["6.4.1.1"]
    clause = document.clauses[0]
    assert clause.clause_title == "表17 S F6 断路器设备巡检项目及要求"
    assert "智能控制柜/汇控柜检查" in clause.clause_body
    assert "表17" not in clause.clause_body


def test_number_only_heading_without_subheading_keeps_number_as_title():
    """无标题条款用编号作标题，正文保留原文（含重复的编号）。"""

    document = _parse(NUMBER_ONLY_HEADING_WITH_TEXT)

    assert [clause.clause_no for clause in document.clauses] == ["7.3.5.2"]
    clause = document.clauses[0]
    assert clause.clause_title == "7.3.5.2"
    assert clause.clause_body.startswith("7.3.5.2 停电操作应按照")


def test_number_only_single_level_heading_is_not_a_clause():
    """负向：`## 6` 这类单级标题不带标题时按章节处理，不能切成一条条款。"""

    with pytest.raises(ValueError, match="未识别到数字条款"):
        _parse("# DL/T 393-2021\n## 6\n\n这一章下面还有分层内容。\n")


def test_standard_name_prefers_replaced_clause_title_then_heading():
    document = _parse()

    assert document.standard_name == "输变电设备状态检修试验规程"


def test_standard_name_falls_back_to_document_heading_without_replaced_clause():
    text = MARKDOWN_STANDARD.replace("本文件代替DL/T393—2010《输变电设备状态检修试验规程》。", "")

    assert _parse(text).standard_name == "输变电设备状态检修试验规程"


def test_standard_scope_uses_current_wording():
    """`本文件适用于` 是现行标准写法，必须识别为标准适用范围。"""

    assert _parse().scope == "本文件适用于超高压及以下交、直流输变电设备。"


def test_plain_text_document_still_uses_line_prefix_rule():
    """负向：没有 Markdown 标题的旧式 PDF 文本仍按行首编号解析。"""

    document = parse_power_spec_text(
        "DL/T 572—2021 电力变压器运行规程\n6.1.2 上层油温限值\n油浸式变压器上层油温一般不超过 85℃。\n",
        document_id="file-plain",
    )

    assert [clause.clause_no for clause in document.clauses] == ["6.1.2"]
    assert document.standard_name == "电力变压器运行规程"
    assert document.clauses[0].source_page is None


def test_markdown_document_without_clause_headings_is_rejected():
    with pytest.raises(ValueError):
        _parse("# 只有标题\n\n没有编号条款。\n")
