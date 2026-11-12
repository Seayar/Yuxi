"""电力规范测评图谱导入：两套解析器、质量门与建图语句。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from yuxi.knowledge.power_spec import qa_graph_seed as seed_module
from yuxi.knowledge.power_spec.qa_graph import normalize_question_text
from yuxi.knowledge.power_spec.qa_graph_seed import (
    SeedCitation,
    SeedQuestion,
    SeedVariant,
    SeedError,
    build_graph_seed,
    load_markdown_questions,
    load_xlsx_questions,
    planned_counts,
    seed_graph,
)

LEGACY_MD = """# Q01 · 主变巡视检查什么

> **最终审计状态**：`BENCHMARK_PASS`
> **E2E资格**：`READY_FOR_E2E`

**出处**：DL/T 393—2021 第 6 章 6.1.1 表 1
**标准页**：17
**PDF 页**：21

## 一、问法结构化映射表

| 编号 | 类型 | 用户问题（可直接作为检索 query） |
| --- | --- | --- |
| Q01-A | 标准书面 | 主变巡视检查什么？ |
| Q01-B | 语音口语句 | 主变平时巡检都看什么？ |

## 二、参考答案（9 个评分要点）

1. 检查本体。（依据：第6.1.1条；PDF第21页；标准第17页）
2. 检查套管。（依据：第6.1.1.2条；PDF第21页）

## 四、评分规则（二元评分）

1. 正确=1分。
"""

MODERN_MD = """# Q21｜验电前应确认什么？

> **最终审计状态**：`BENCHMARK_PASS`
> **E2E资格**：`READY_FOR_E2E`

**问题分类**：安全生产/安规
**标准依据**：DL/T 408—2023
**标准章节/标准页/PDF页**：第 6.3.2 条（验电）；标准第 7 页 / PDF 第 13 页。

### 1. 用户问题

**验电前应确认什么？**

### 2. 用户问法变体

| 问法类型 | 用户问题 |
| --- | --- |
| 标准书面问法 | 验电前应确认什么？ |
| 语音口语问法 | 验电前要看验电器啥状态？ |

### 3. 参考答案（评分要点）

1. 验电前应先确认验电器良好。（依据：第6.3.2条；PDF第13页；标准第7页）

### 4. 二元评分规则

- **1分**：回答验电器良好。
"""


def _seed_question(**overrides) -> SeedQuestion:
    payload = {
        "question_id": "md:Q01",
        "number": "Q01",
        "source": "md",
        "text": "主变巡视检查什么？",
        "category": "设备运行维护",
        "difficulty": None,
        "expected_behavior": "READY_FOR_E2E",
        "audit_status": "BENCHMARK_PASS",
        "e2e_status": "READY_FOR_E2E",
        "standard_basis": "DL/T 393—2021",
        "section": "第 6.1.1 条 表 1",
        "answer": "检查本体。",
        "atomic_claims": ("检查本体。",),
        "scoring": "正确=1分。",
        "variants": (SeedVariant("主变巡视检查什么？", "标准书面", 0),),
        "citations": (
            SeedCitation(
                "DL/T 393—2021",
                "6.1.1",
                section="第 6.1.1 条 表 1",
                excerpt="油浸式电力变压器和电抗器巡检项目及要求",
                rank=1,
            ),
        ),
        "refusal": False,
        "cross_source": False,
    }
    payload.update(overrides)
    return SeedQuestion(**payload)


def test_markdown_parser_reads_legacy_and_modern_formats(tmp_path):
    path = tmp_path / "testset.md"
    path.write_text(LEGACY_MD + "\n" + MODERN_MD, encoding="utf-8")

    questions = load_markdown_questions(path)

    assert [question.question_id for question in questions] == ["md:Q01", "md:Q21"]
    legacy, modern = questions
    assert legacy.text == "主变巡视检查什么"
    variant_keys = {normalize_question_text(variant.text) for variant in legacy.variants}
    assert variant_keys >= {
        normalize_question_text("主变巡视检查什么？"),
        normalize_question_text("主变平时巡检都看什么？"),
    }
    assert [citation.clause_no for citation in legacy.citations] == ["6.1.1", "6.1.1.2"]
    assert "正确=1分" in legacy.scoring
    assert modern.text == "验电前应确认什么？"
    assert [citation.clause_no for citation in modern.citations] == ["6.3.2"]
    assert modern.answer.startswith("验电前应先确认验电器良好")


def test_xlsx_parser_reads_sheets_and_variants(tmp_path):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "单跳用例"
    sheet.append(["标题"])
    sheet.append(
        [
            "用例编号",
            "子类型",
            "难度",
            "标准书面问题",
            "口语化提问变体",
            "行业俗语问法",
            "预期行为",
            "文档表示",
            "引用章节",
            "引用原文",
            "标准答案",
            "原子事实断言",
            "二元评分判定标准",
        ]
    )
    sheet.append(
        [
            "SINGLE_01",
            "核心参数提取",
            "简单",
            "10kV、35kV 及 110kV 高压设备不停电时的最小安全距离分别是多少？",
            "10千伏、35千伏还有110千伏设备的最小距离是多少米？",
            "10KV、35KV、110KV高压柜的最少避让距离？",
            "应基于文档准确回答",
            "《DL/T 408-2023》",
            "第 5.3.1.3 条 表 1",
            "表1 高压设备不停电时的安全距离：10kV及以下 0.70m；20、35kV 1.00m；66、110kV 1.50m。",
            "10kV 0.70 米；35kV 1.00 米；110kV 1.50 米。",
            "1. 10kV为0.70m。\n2. 35kV为1.00m。",
            "【Pass (1分)】数值正确。",
        ]
    )

    refusal = workbook.create_sheet("拒答抗幻觉用例")
    refusal.append(["标题"])
    refusal.append(["用例编号", "子类型", "难度", "标准书面问题"])
    refusal.append(
        [
            "NEG_01",
            "知识缺失",
            "简单",
            "锂电池消防喷淋安装距离是多少？",
            "",
            "",
            "应该如实拒答",
            "未包含",
            "无",
            "无",
            "拒答",
            "",
            "【Pass】拒答。",
        ]
    )

    path = tmp_path / "testset.xlsx"
    workbook.save(path)

    questions = load_xlsx_questions(path)

    assert [question.question_id for question in questions] == ["xlsx:SINGLE_01", "xlsx:NEG_01"]
    single, neg = questions
    assert single.citations[0].standard_no == "DL/T 408-2023"
    assert single.citations[0].clause_no == "5.3.1.3"
    assert single.citations[0].table_no == "1"
    assert {variant.kind for variant in single.variants} >= {"标准书面", "口语化", "行业俗语"}
    assert neg.refusal is True
    assert neg.citations == ()


def test_quality_gate_rejects_missing_citation():
    with pytest.raises(SeedError, match="缺少条款映射"):
        build_graph_seed([_seed_question(citations=())])


def test_quality_gate_rejects_refusal_with_citation():
    with pytest.raises(SeedError, match="拒答题不应有条款映射"):
        build_graph_seed([_seed_question(refusal=True, citations=(SeedCitation("DL/T 393—2021", "6.1.1"),))])


def test_build_seed_dedups_clauses_and_reports_counts():
    seed = build_graph_seed([_seed_question()])

    assert planned_counts(seed) == {
        "questions": 1,
        "answers": 1,
        "clauses": 1,
        "standards": 1,
        "has_answer_edges": 1,
        "clause_edges": 1,
        "standard_edges": 1,
    }
    assert seed.clauses["DL/T393-2021#6.1.1"].standard_no == "DL/T 393—2021"


class _Record:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data

    def get(self, key):
        return self._data.get(key)

    def __getitem__(self, key):
        return self._data[key]

    def single(self):
        return self

    def consume(self):
        return None


class _Session:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, cypher, **params):
        self.calls.append((cypher, params))
        if "db.vertexLabels()" in cypher or "db.edgeLabels()" in cypher:
            return [_Record({"label": "其他"})]
        if "count(" in cypher:
            return _Record({"total": 1})
        return _Record({})


class _Driver:
    def __init__(self):
        self.sessions: list[_Session] = []

    def session(self):
        session = _Session()
        self.sessions.append(session)
        return session

    def close(self):
        return None


def test_seed_graph_writes_schema_nodes_and_edges(monkeypatch):
    driver = _Driver()
    monkeypatch.setattr(seed_module, "ensure_graph", lambda **_kwargs: False)
    monkeypatch.setattr(seed_module.GraphDatabase, "driver", lambda *_args, **_kwargs: driver)
    seed = build_graph_seed([_seed_question()])

    report = seed_graph(
        seed,
        uri="bolt://tugraph:7688",
        username="admin",
        password="secret",
        graph="power_spec_qa",
    )

    assert report["status"] == "success"
    assert report["schema"] == {"vertex": 4, "edge": 3}
    assert report["counts"]["问题"] == 1
    statements = [cypher for session in driver.sessions for cypher, _params in session.calls]
    assert any("CALL db.createVertexLabel('问题'" in statement for statement in statements)
    assert any("CALL db.createEdgeLabel('依据条款'" in statement for statement in statements)
    edge_calls = [
        params
        for session in driver.sessions
        for cypher, params in session.calls
        if "MERGE (a)-[r:依据条款]->(b)" in cypher
    ]
    assert edge_calls and edge_calls[0]["rank"] == 1
    assert edge_calls[0]["excerpt"]


def test_question_props_contain_all_matching_keys():
    question = _seed_question(
        variants=(
            SeedVariant("主变巡视检查什么？", "标准书面", 0),
            SeedVariant("主变平时巡检都看什么？", "语音口语句", 0),
        )
    )

    props = seed_module._question_props(question)
    keys = json.loads(props["命中键"])

    assert [item["text"] for item in keys] == ["主变巡视检查什么？", "主变平时巡检都看什么？"]
    assert props["来源"] == "md"
    assert props["跨源要求"] == "false"


def test_refusal_seed_has_no_citation_but_valid_props():
    question = _seed_question(refusal=True, citations=())
    seed = build_graph_seed([replace(question, text="锂电池消防喷淋安装距离是多少？")])

    props = seed_module._answer_props(seed.questions[0])

    assert props["拒答"] == "true"
    assert planned_counts(seed)["clause_edges"] == 0


def _write_compact_workbook(path):
    from openpyxl import Workbook

    workbook = Workbook()
    single = workbook.active
    single.title = "单跳用例"
    single.append(["标题"])
    single.append(["序号", "问题", "引用文档", "引用章节", "引用原文", "标准答案", "评分判定标准"])
    single.append(
        [
            "SINGLE_01",
            "10kV 高压设备不停电时的最小安全距离是多少？",
            "DL/T 408-2023《电力安全工作规程 发电厂和变电站电气部分》",
            "DL/T 408-2023 5.3.1.3 表1",
            "表1：10kV及以下 0.70m；有绝缘遮蔽时 0.35m。",
            "10kV 为 0.70m。",
            "【Pass】答出 0.70m。",
        ]
    )

    supplement = workbook.create_sheet("补充用例")
    supplement.append(["标题"])
    supplement.append(["序号", "问题", "引用文档", "引用章节", "引用原文", "标准答案", "评分判定标准"])
    supplement.append(
        [
            "Q21",
            "110kV 主变压器停电后，验电前应确认验电器处于什么状态？",
            "DL/T 408-2023《电力安全工作规程 发电厂和变电站电气部分》",
            "DL/T 408-2023 6.3.2",
            "验电前，应先在相应电压等级的有电设备上确证验电器良好。",
            "验电器应良好。",
            "【Pass】答出验电器良好。",
        ]
    )

    hidden = workbook.create_sheet("内部数据")
    hidden.append(["序号", "原子事实"])
    hidden.append(["SINGLE_01", json.dumps(["10kV 为 0.70m。"], ensure_ascii=False)])
    hidden.sheet_state = "hidden"
    workbook.save(path)


def test_compact_xlsx_parser_reads_fields_citations_and_claims(tmp_path):
    path = tmp_path / "compact.xlsx"
    _write_compact_workbook(path)

    questions = load_xlsx_questions(path)

    assert [question.question_id for question in questions] == ["xlsx:SINGLE_01", "md:Q21"]
    single, supplement = questions
    assert single.source == "xlsx"
    assert single.text.startswith("10kV 高压设备")
    assert single.answer == "10kV 为 0.70m。"
    assert single.atomic_claims == ("10kV 为 0.70m。",)
    assert single.citations[0].standard_no == "DL/T 408-2023"
    assert single.citations[0].clause_no == "5.3.1.3"
    assert single.citations[0].table_no == "1"
    assert single.citations[0].excerpt.startswith("表1：")
    assert supplement.source == "md"
    assert supplement.e2e_status == "READY_FOR_E2E"
    assert supplement.citations[0].clause_no == "6.3.2"
    assert planned_counts(build_graph_seed(questions))["questions"] == 2


def test_compact_xlsx_parser_rejects_missing_required_header(tmp_path):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "单跳用例"
    sheet.append(["标题"])
    sheet.append(["序号", "问题", "标准答案"])
    sheet.append(["SINGLE_01", "问题", "答案"])
    path = tmp_path / "broken.xlsx"
    workbook.save(path)

    with pytest.raises(SeedError, match="缺少必需表头"):
        load_xlsx_questions(path)
